"""Static Floor 0 annex. Files only. No forms, no shell, no database."""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "www"
# Inside the compose network only. The compose file publishes no host port.
# Do not bind this to 0.0.0.0:80 on the Grayline host.
BIND = "0.0.0.0"
PORT = 8080


def flag_open() -> bool:
    return os.environ.get("FLAG_OPEN", "1").strip().lower() not in {"0", "false", "no"}


def resolve(url_path: str, root: Path | None = None, open_flag: bool | None = None) -> tuple[int, str, bytes]:
    base = (root or ROOT).resolve()
    opened = flag_open() if open_flag is None else open_flag
    path = url_path.split("?", 1)[0]
    if path == "/healthz":
        return 200, "text/plain; charset=utf-8", b"ok\n"
    if not opened:
        closed = base / "closed.html"
        if closed.is_file():
            return 200, "text/html; charset=utf-8", closed.read_bytes()
        return 200, "text/plain; charset=utf-8", b"closed for remodeling\n"
    if path in {"", "/"}:
        path = "/index.html"
    rel = path.lstrip("/")
    if not rel or "\\" in rel or rel.startswith("/"):
        return 404, "text/plain; charset=utf-8", b"not found\n"
    candidate = (base / rel).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return 404, "text/plain; charset=utf-8", b"not found\n"
    if not candidate.is_file():
        return 404, "text/plain; charset=utf-8", b"not found\n"
    kind = "text/html; charset=utf-8" if candidate.suffix == ".html" else "text/plain; charset=utf-8"
    return 200, kind, candidate.read_bytes()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        status, kind, body = resolve(self.path)
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        status, kind, body = resolve(self.path)
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> None:
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
