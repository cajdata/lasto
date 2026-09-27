"""Local preview that behaves like GitHub Pages: same content types, 404.html with a 404
status, directory URLs with a trailing slash. Bound to 127.0.0.1 only."""

from __future__ import annotations

import functools
import http.server
import threading
import time
from pathlib import Path
from typing import Callable

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    "": "text/plain; charset=utf-8",
}


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = TYPES

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        page = Path(self.directory) / "404.html"
        if code == 404 and page.exists():
            body = page.read_bytes()
            self.send_response(404)
            self.send_header("Content-Type", TYPES[".html"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        super().send_error(code, message, explain)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def _mtimes(roots: list[Path]) -> dict[Path, int]:
    out: dict[Path, int] = {}
    for root in roots:
        for p in root.rglob("*"):
            if p.is_file() and ".cache" not in p.parts and "dist" not in p.parts:
                out[p] = p.stat().st_mtime_ns
    return out


def serve(directory: Path, port: int, watch_roots: list[Path] | None = None, rebuild: Callable[[], None] | None = None) -> None:
    handler = functools.partial(Handler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"Serving {directory} at http://127.0.0.1:{port}/ (Ctrl+C to stop)")
    if watch_roots and rebuild:
        def loop() -> None:
            seen = _mtimes(watch_roots)
            while True:
                time.sleep(0.75)
                now = _mtimes(watch_roots)
                if now != seen:
                    seen = now
                    try:
                        rebuild()
                        print("Rebuilt.")
                    except Exception as exc:  # keep serving the last good build
                        print(f"Build failed: {exc}")
        threading.Thread(target=loop, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
