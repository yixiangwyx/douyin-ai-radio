#!/usr/bin/env python3
"""console.py — 手机端控制台：播放 / 实时互动流 / 编辑稿子 / 百炼参数 / 试听。

用法:
  uv run python web/console.py --room <直播间号> [--script scripts/demo_script.txt] [--port 8000] [--sink file|ffplay]
手机浏览器打开 http://<本机IP>:8000
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 载入 .env（若存在）
_envp = ROOT / ".env"
if _envp.exists():
    for line in _envp.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z_]+)=(.+)$", line.strip())
        if m and m.group(1) not in os.environ:
            os.environ[m.group(1)] = m.group(2).strip()

from airadio.orchestrator import Orchestrator  # noqa: E402
from airadio.timeline import ScheduleConfig  # noqa: E402

STATE: dict = {}


def load_dotenv_script(path: str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    orch: Orchestrator = None  # type: ignore

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

    # ---- GET ----
    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            html = (ROOT / "web" / "console.html").read_text(encoding="utf-8")
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if p == "/api/state":
            return self._send(200, json.dumps(self.orch.snapshot(), ensure_ascii=False).encode())
        if p == "/api/script":
            return self._send(200, json.dumps({"text": self.orch.get_script()}, ensure_ascii=False).encode())
        if p.startswith("/chunks/"):
            fp = (ROOT / "out" / p.lstrip("/")).resolve()
            if not str(fp).startswith(str((ROOT / "out").resolve())) or not fp.is_file():
                return self._send(404, b'{"error":"not found"}')
            data = fp.read_bytes()
            ctype = "audio/mpeg" if fp.suffix == ".mp3" else "application/json"
            return self._send(200, data, ctype)
        return self._send(404, b'{"error":"no route"}')

    # ---- POST ----
    def do_POST(self):
        p = self.path.split("?")[0]
        try:
            body = self._json_body()
        except Exception:
            return self._send(400, b'{"error":"bad json"}')
        try:
            if p == "/api/script":
                n = self.orch.update_script(body.get("text", ""), apply_now=bool(body.get("apply_now")))
                return self._send(200, json.dumps({"ok": True, "sentences": n,
                                                   "applied": bool(body.get("apply_now"))}))
            if p == "/api/params":
                window = body.get("window")
                self.orch.update_params(
                    voice=body.get("voice"), model=body.get("model"),
                    rate=body.get("rate"), min_gap=body.get("min_gap"),
                    window=(window[0], window[1]) if isinstance(window, list) and len(window) == 2 else None)
                return self._send(200, json.dumps({"ok": True}))
            if p == "/api/room":
                self.orch.set_room(str(body.get("room", "")).strip())
                return self._send(200, json.dumps({"ok": True}))
            if p == "/api/control":
                action = body.get("action")
                if action == "pause":
                    self.orch.pause()
                elif action == "resume":
                    self.orch.resume()
                elif action == "skip":
                    self.orch.skip_sentence()
                elif action == "rewind":
                    self.orch.rewind()
                else:
                    return self._send(400, b'{"error":"unknown action"}')
                return self._send(200, json.dumps({"ok": True}))
            if p == "/api/tts_test":
                text = (body.get("text") or "").strip()[:200]
                if not text:
                    return self._send(400, b'{"error":"text required"}')
                audio = self.orch._synth([text])
                if not audio:
                    err = self.orch.tts.last_error if self.orch.tts else "dry 模式无音频"
                    return self._send(502, json.dumps({"error": err}, ensure_ascii=False).encode())
                return self._send(200, audio, "audio/mpeg")
        except ValueError as e:
            return self._send(422, json.dumps({"error": str(e)}, ensure_ascii=False).encode())
        except Exception as e:
            return self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"},
                                              ensure_ascii=False).encode())
        return self._send(404, b'{"error":"no route"}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", required=True)
    ap.add_argument("--script", default="scripts/demo_script.txt")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--sink", default="file", choices=["file", "ffplay", "dry"])
    ap.add_argument("--voice", default="longxiaochun_v2")
    ap.add_argument("--window", default="60,150")
    ap.add_argument("--min-gap", type=float, default=45.0)
    args = ap.parse_args()

    w0, w1 = (float(x) for x in args.window.split(","))
    orch = Orchestrator(
        args.room, load_dotenv_script(ROOT / args.script),
        schedule=ScheduleConfig(min_gap_s=args.min_gap, window_s=(w0, w1)),
        voice=args.voice, out_dir=str(ROOT / "out" / "chunks"), sink=args.sink)
    Handler.orch = orch
    threading.Thread(target=orch.run, daemon=True).start()

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"控制台: http://0.0.0.0:{args.port}  (手机同一WiFi直接打开)")
    print("Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        orch.stop()


if __name__ == "__main__":
    main()
