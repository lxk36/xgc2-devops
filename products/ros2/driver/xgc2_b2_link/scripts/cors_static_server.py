#!/usr/bin/env python3
"""Static file server with CORS so Lichtblick ( :8080 ) can fetch layoutUrl."""

from __future__ import annotations

import argparse
import mimetypes
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class CORSRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # quieter
        pass


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8091)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--directory", required=True)
    args = p.parse_args()
    os.chdir(args.directory)
    mimetypes.add_type("application/json", ".json")
    httpd = ThreadingHTTPServer((args.bind, args.port), CORSRequestHandler)
    print(f"cors_static_server dir={args.directory} http://{args.bind}:{args.port}/", flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
