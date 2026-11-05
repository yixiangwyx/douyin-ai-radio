#!/usr/bin/env python3
"""serve_player.py — 提供播放页与 out/chunks 音频块（默认 8000 端口）。

用法: uv run python web/serve_player.py [--port 8000]
手机/电脑浏览器打开 http://<本机IP>:8000 点"开始连续播放"即可外放。
"""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    out_dir = ROOT / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "chunks").mkdir(exist_ok=True)
    handler = partial(Handler, directory=str(out_dir))
    # 把 player.html 映射到 /
    html = (ROOT / "web" / "player.html").read_text(encoding="utf-8")

    class H(handler):
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                super().do_GET()

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), H)
    print(f"player on http://0.0.0.0:{args.port}  (Ctrl+C 停止)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
