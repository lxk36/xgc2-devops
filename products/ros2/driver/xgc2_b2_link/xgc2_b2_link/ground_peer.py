"""G4 ground peer (Zenoh/TCP → local ROS1 recovery + semantic snapshot)."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from xgc2_b2_link.codec import high_level_cmd, now_ms, pack_json, unpack_cdr_ros2, unpack_json
from xgc2_b2_link.contract import full_key, load_contract
from xgc2_b2_link.transport import open_transport


@dataclass
class LatestStore:
    lock: threading.Lock = field(default_factory=threading.Lock)
    by_channel: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def set(self, channel: str, meta: Dict[str, Any]) -> None:
        with self.lock:
            self.by_channel[channel] = meta

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "t_ms": now_ms(),
                "channels": {k: dict(v) for k, v in self.by_channel.items()},
            }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="XGC2 B2 ground peer (G4 Zenoh/ROS half)")
    p.add_argument("--robot-id", required=True)
    p.add_argument("--transport", default="auto", choices=["auto", "zenoh", "tcp"])
    p.add_argument("--tcp-host", default="127.0.0.1")
    p.add_argument("--tcp-port", type=int, default=7448)
    p.add_argument("--tcp-role", default="server", choices=["client", "server"])
    p.add_argument("--zenoh-listen", action="append", default=[])
    p.add_argument("--zenoh-connect", action="append", default=[])
    p.add_argument(
        "--publish-ros",
        action="store_true",
        help="recover ROS1 topics (odom, joint_states, path, tf, power, arm)",
    )
    p.add_argument("--print-hz", type=float, default=0.5, help="print semantic snapshot rate (0=off)")
    p.add_argument("--send-echo", action="store_true", help="periodically send echo_value cmd")
    return p


class GroundPeer:
    def __init__(self, robot_id: str, transport, *, publish_ros: bool = False) -> None:
        self.robot_id = robot_id
        self.transport = transport
        self.contract = load_contract()
        self.prefix_tpl = self.contract.get("key_prefix_template", "xgc2/{robot_id}")
        self.store = LatestStore()
        self._ros1 = None
        if publish_ros:
            from xgc2_b2_link.ground_ros1 import Ros1RecoveredPubs

            self._ros1 = Ros1RecoveredPubs()
            print(
                "ROS1 recovery: /remote/b2/odom joint_states path power; "
                "/joint_states; /remote/arm/slave_joint_states; tf odom→b2_description",
                flush=True,
            )
        expr = full_key(robot_id, "up/**", self.prefix_tpl)
        self.transport.subscribe(expr, self._on_up)
        print(f"G4 ground peer listening robot_id={robot_id} expr={expr}", flush=True)

    def _on_up(self, key: str, payload: bytes) -> None:
        parts = key.split("/")
        try:
            up_idx = parts.index("up")
            name = "/".join(parts[up_idx + 1 :])
        except ValueError:
            name = parts[-1]
        meta: Dict[str, Any] = {"key": key, "t_ms": now_ms(), "bytes": len(payload)}
        # Prefer JSON (sim + power/arm); fall back to CDR envelope
        try:
            meta["json"] = unpack_json(payload)
        except Exception:
            try:
                type_name, cdr = unpack_cdr_ros2(payload)
                meta["type"] = type_name
                meta["cdr_len"] = len(cdr)
            except Exception as exc:
                meta["error"] = str(exc)
        channel = name.replace("/", "_")
        self.store.set(channel, meta)
        if self._ros1 is not None and "json" in meta:
            try:
                self._ros1.handle(name, meta)
            except Exception as exc:
                print(f"ROS1 publish error on {name}: {exc}", flush=True)

    def send_command(self, cmd_type: str, value: str = "") -> None:
        rel = self.contract["channels"]["down"]["cmd"]["key"]
        key = full_key(self.robot_id, rel, self.prefix_tpl)
        payload = pack_json(high_level_cmd(cmd_type, value, source="xgc2-g4-ground-peer"))
        self.transport.put(key, payload)

    def spin(self, print_hz: float = 0.5, send_echo: bool = False) -> None:
        period = 1.0 / print_hz if print_hz and print_hz > 0 else 1.0
        n = 0
        try:
            while True:
                time.sleep(period)
                if print_hz and print_hz > 0:
                    snap = self.store.snapshot()
                    ch = snap.get("channels") or {}
                    summary = {
                        "t_ms": snap["t_ms"],
                        "n_channels": len(ch),
                        "keys": sorted(ch.keys()),
                    }
                    # include power soc if present
                    for k, v in ch.items():
                        if "power" in k and isinstance(v.get("json"), dict):
                            summary["soc"] = v["json"].get("soc")
                    print(json.dumps(summary, ensure_ascii=False), flush=True)
                if send_echo:
                    self.send_command("echo_value", f"ping-{n}")
                    n += 1
        except KeyboardInterrupt:
            pass


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    transport = open_transport(
        kind=args.transport,
        zenoh_listen=args.zenoh_listen or None,
        zenoh_connect=args.zenoh_connect or None,
        tcp_host=args.tcp_host,
        tcp_port=args.tcp_port,
        tcp_role=args.tcp_role,
    )
    peer = GroundPeer(args.robot_id, transport, publish_ros=args.publish_ros)
    try:
        peer.spin(print_hz=args.print_hz, send_echo=args.send_echo)
    finally:
        transport.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
