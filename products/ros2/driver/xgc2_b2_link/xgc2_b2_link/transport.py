"""Zenoh transport with TCP framed fallback for dev without eclipse-zenoh."""

from __future__ import annotations

import socket
import struct
import threading
from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Optional

OnMessage = Callable[[str, bytes], None]


class Transport(ABC):
    @abstractmethod
    def put(self, key: str, payload: bytes) -> None: ...

    @abstractmethod
    def subscribe(self, key_expr: str, handler: OnMessage) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


def open_transport(
    *,
    kind: str = "auto",
    zenoh_mode: str = "peer",
    zenoh_listen: Optional[List[str]] = None,
    zenoh_connect: Optional[List[str]] = None,
    tcp_host: str = "127.0.0.1",
    tcp_port: int = 7448,
    tcp_role: str = "auto",
) -> Transport:
    """
    kind: auto | zenoh | tcp
    tcp_role: auto | server | client  (server=listen, client=connect)
    """
    kind = (kind or "auto").lower()
    if kind == "auto":
        try:
            import zenoh  # noqa: F401

            kind = "zenoh"
        except ImportError:
            kind = "tcp"
    if kind == "zenoh":
        return ZenohTransport(
            mode=zenoh_mode,
            listen=zenoh_listen or ["tcp/127.0.0.1:7447"],
            connect=zenoh_connect or [],
        )
    if kind == "tcp":
        role = tcp_role
        if role == "auto":
            role = "server"
        return TcpFramedTransport(host=tcp_host, port=tcp_port, role=role)
    raise ValueError(f"unknown transport kind {kind}")


class ZenohTransport(Transport):
    def __init__(
        self,
        *,
        mode: str = "peer",
        listen: Optional[List[str]] = None,
        connect: Optional[List[str]] = None,
    ) -> None:
        import zenoh

        conf = zenoh.Config()
        # Best-effort config; zenoh API varies by version — keep endpoints via insert_json5 when available.
        endpoints = {
            "mode": mode,
            "listen": {"endpoints": listen or []},
            "connect": {"endpoints": connect or []},
        }
        try:
            conf.insert_json5("mode", f'"{mode}"')
            if listen:
                conf.insert_json5("listen/endpoints", str(listen).replace("'", '"'))
            if connect:
                conf.insert_json5("connect/endpoints", str(connect).replace("'", '"'))
        except Exception:
            pass
        self._zenoh = zenoh
        self._session = zenoh.open(conf)
        self._subs = []
        self._endpoints = endpoints

    def put(self, key: str, payload: bytes) -> None:
        self._session.put(key, payload)

    def subscribe(self, key_expr: str, handler: OnMessage) -> None:
        def _cb(sample) -> None:
            try:
                k = str(sample.key_expr)
                payload = bytes(sample.payload)
            except Exception:
                # older API
                k = str(getattr(sample, "key_expr", key_expr))
                raw = getattr(sample, "payload", b"")
                payload = bytes(raw) if not isinstance(raw, bytes) else raw
            handler(k, payload)

        sub = self._session.declare_subscriber(key_expr, _cb)
        self._subs.append(sub)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass


class TcpFramedTransport(Transport):
    """
    Simple multi-client hub:
    - server: listen, fan-out puts to all clients; deliver client frames to local handlers
    - client: connect to server
    Frame: u32be key_len | key_utf8 | u32be payload_len | payload
    """

    def __init__(self, *, host: str, port: int, role: str) -> None:
        self._host = host
        self._port = port
        self._role = role
        self._handlers: List[tuple] = []
        self._lock = threading.Lock()
        self._peers: List[socket.socket] = []
        self._stop = threading.Event()
        self._server_sock: Optional[socket.socket] = None
        self._client_sock: Optional[socket.socket] = None
        if role == "server":
            self._start_server()
        elif role == "client":
            self._start_client()
        else:
            raise ValueError("tcp role must be server or client")

    def _start_server(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self._host, self._port))
        s.listen(8)
        s.settimeout(0.5)
        self._server_sock = s
        threading.Thread(target=self._accept_loop, name="b2-tcp-accept", daemon=True).start()

    def _accept_loop(self) -> None:
        assert self._server_sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(0.5)
            with self._lock:
                self._peers.append(conn)
            threading.Thread(
                target=self._read_loop, args=(conn,), name="b2-tcp-read", daemon=True
            ).start()

    def _start_client(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((self._host, self._port))
        s.settimeout(0.5)
        self._client_sock = s
        with self._lock:
            self._peers.append(s)
        threading.Thread(target=self._read_loop, args=(s,), name="b2-tcp-read", daemon=True).start()

    def _read_loop(self, conn: socket.socket) -> None:
        try:
            while not self._stop.is_set():
                try:
                    header = self._recvexact(conn, 4)
                    if not header:
                        break
                    (klen,) = struct.unpack(">I", header)
                    key = self._recvexact(conn, klen).decode("utf-8")
                    (plen,) = struct.unpack(">I", self._recvexact(conn, 4))
                    payload = self._recvexact(conn, plen)
                except socket.timeout:
                    continue
                except OSError:
                    break
                self._dispatch(key, payload)
        finally:
            with self._lock:
                if conn in self._peers:
                    self._peers.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _recvexact(self, conn: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return b""
            buf += chunk
        return buf

    def _dispatch(self, key: str, payload: bytes) -> None:
        with self._lock:
            handlers = list(self._handlers)
        for expr, handler in handlers:
            if _key_match(expr, key):
                try:
                    handler(key, payload)
                except Exception:
                    pass

    def put(self, key: str, payload: bytes) -> None:
        frame = struct.pack(">I", len(key.encode("utf-8"))) + key.encode("utf-8")
        frame += struct.pack(">I", len(payload)) + payload
        dead = []
        with self._lock:
            peers = list(self._peers)
        for p in peers:
            try:
                p.sendall(frame)
            except OSError:
                dead.append(p)
        if dead:
            with self._lock:
                for p in dead:
                    if p in self._peers:
                        self._peers.remove(p)
                    try:
                        p.close()
                    except OSError:
                        pass

    def subscribe(self, key_expr: str, handler: OnMessage) -> None:
        with self._lock:
            self._handlers.append((key_expr, handler))

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            peers = list(self._peers)
            self._peers.clear()
        for p in peers:
            try:
                p.close()
            except OSError:
                pass
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass


def _key_match(expr: str, key: str) -> bool:
    # minimal: exact, or prefix*  (zenoh-like trailing /**)
    if expr == key:
        return True
    if expr.endswith("/**"):
        return key.startswith(expr[:-3])
    if expr.endswith("/*"):
        prefix = expr[:-1]
        if not key.startswith(prefix):
            return False
        rest = key[len(prefix) :]
        return "/" not in rest.rstrip("/") or rest.count("/") == 0
    if "**" in expr or "*" in expr:
        # crude: prefix before first *
        head = expr.split("*", 1)[0]
        return key.startswith(head)
    return False
