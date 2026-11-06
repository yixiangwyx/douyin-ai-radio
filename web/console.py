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
from airadio.library import Library  # noqa: E402
from airadio import generate as gen  # noqa: E402

STATE: dict = {}
LIB: Library = None  # type: ignore


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
        if p == "/api/library":
            return self._send(200, json.dumps(LIB.snapshot(), ensure_ascii=False).encode())
        if p == "/api/starter_topics":
            return self._send(200, json.dumps({"topics": [{"title": t, "note": n} for t, n in gen.STARTER_TOPICS]},
                                              ensure_ascii=False).encode())
        if p == "/api/voices":
            # 列出该账号已复刻的音色（cosyvoice-v2）
            if not self.orch.has_api_key():
                return self._send(200, json.dumps({"voices": []}))
            import requests
            api_key = self.orch.api_key or os.environ.get("DASHSCOPE_API_KEY")
            resp = requests.post(
                "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization",
                timeout=30,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"model": "voice-enrollment",
                      "input": {"action": "list_voice",
                                "target_model": self.orch.model}})
            voices = []
            if resp.status_code == 200:
                for v in resp.json().get("output", {}).get("voice_list", []):
                    voices.append({"voice_id": v.get("voice_id", ""),
                                   "status": v.get("status", "")})
            return self._send(200, json.dumps({"voices": voices}))
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
            if p == "/api/library/topic":
                t = LIB.add_topic(body.get("title", ""), body.get("note", ""))
                return self._send(200, json.dumps({"ok": True, "topic": t}, ensure_ascii=False))
            if p == "/api/library/topic_delete":
                return self._send(200, json.dumps({"ok": LIB.remove_topic(body.get("id", ""))}))
            if p == "/api/library/script_delete":
                return self._send(200, json.dumps({"ok": LIB.remove_script(body.get("id", ""))}))
            if p == "/api/library/save":
                item = LIB.add_script(body.get("title", ""), body.get("text", ""),
                                      body.get("source", "手动保存"))
                return self._send(200, json.dumps({"ok": True, "id": item["id"],
                                                   "sentences": item["sentences"]}, ensure_ascii=False))
            if p == "/api/library/use":
                item = LIB.get_script(body.get("id", ""))
                if not item:
                    return self._send(404, json.dumps({"error": "稿子不存在"}))
                n = self.orch.update_script(item["text"], apply_now=bool(body.get("apply_now", True)))
                idx = next((i for i, s in enumerate(LIB.data["scripts"]) if s["id"] == item["id"]), None)
                if idx is not None:
                    self.orch.set_rotate(self.orch.rotate_enabled, LIB.rotate_source, idx=idx)
                self.orch.script_title = item["title"]
                return self._send(200, json.dumps({"ok": True, "title": item["title"],
                                                   "sentences": n, "applied": bool(body.get("apply_now", True))},
                                                  ensure_ascii=False))
            if p == "/api/rotate":
                self.orch.set_rotate(bool(body.get("enabled")), LIB.rotate_source,
                                     after_s=body.get("after_s"),
                                     after_lines=body.get("after_lines"))
                return self._send(200, json.dumps({"ok": True, "enabled": self.orch.rotate_enabled,
                                                   "after_s": self.orch.rotate_after_s,
                                                   "after_lines": self.orch.rotate_after_lines}))
            if p == "/api/generate":
                topic = (body.get("topic") or "").strip()
                if not topic:
                    return self._send(400, json.dumps({"error": "先选或填一个话题"}))
                note = body.get("note", "") or ""
                n_sent = int(body.get("sentences") or 10)
                mode = body.get("mode", "auto")   # auto=先AI后本地 / local=只用本地模板
                used, model = "", ""
                try:
                    if mode != "local":
                        text, model = gen.generate_script(topic, note, n_sent,
                                                          api_key=self.orch.api_key or os.environ.get("DASHSCOPE_API_KEY"))
                        used = "ai"
                    else:
                        raise RuntimeError("按要求只用本地生成")
                except Exception as e:
                    text = gen.draft_from_topic(topic, note, n_sent)
                    used = "local"
                    err = str(e)[:160]
                else:
                    err = ""
                for t in LIB.data["topics"]:
                    if t["title"] == topic:
                        t["used"] = int(t.get("used", 0)) + 1
                if not any(t["title"] == topic for t in LIB.data["topics"]):
                    LIB.add_topic(topic, note)
                item = LIB.add_script(item_title := (body.get("title") or topic), text,
                                      source=("百炼 " + model) if used == "ai" else "本地模板")
                return self._send(200, json.dumps({"ok": True, "id": item["id"], "text": text,
                                                   "used": used, "model": model, "fallback_error": err,
                                                   "title": item_title, "sentences": item["sentences"]},
                                                  ensure_ascii=False))
            if p == "/api/params":
                window = body.get("window")
                pkw = body.get("per_kind_windows") or {}
                clean = {}
                for k, v in pkw.items():
                    if isinstance(v, list) and len(v) == 2:
                        clean[k] = [v[0], v[1]]
                    elif v is None:
                        clean[k] = None
                self.orch.update_params(
                    voice=body.get("voice"), model=body.get("model"),
                    rate=body.get("rate"), min_gap=body.get("min_gap"),
                    window=(window[0], window[1]) if isinstance(window, list) and len(window) == 2 else None,
                    per_kind_windows=clean or None,
                    prefetch_ahead=body.get("prefetch_ahead"),
                    sentence_gap_ms=body.get("sentence_gap_ms"),
                    merge_sentences=body.get("merge_sentences"),
                    trim_silence=body.get("trim_silence"))
                return self._send(200, json.dumps({"ok": True}))
            if p == "/api/prefetch":
                n = body.get("limit")
                limit = None if (n is None or n == "" or n == "all") else max(1, int(n))
                queued = self.orch.prefetch_script(limit)
                return self._send(200, json.dumps({"ok": True, "queued": queued}))
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
                elif action == "next_script":
                    if not self.orch._maybe_rotate():
                        return self._send(422, json.dumps(
                            {"error": "轮换没开或稿子库里不足 2 套稿"}, ensure_ascii=False))
                else:
                    return self._send(400, b'{"error":"unknown action"}')
                return self._send(200, json.dumps({"ok": True}))
            if p == "/api/api_key":
                key = str(body.get("key", "")).strip()
                if not key.startswith("sk-") or len(key) < 20:
                    return self._send(422, json.dumps(
                        {"error": "Key 应以 sk- 开头（百炼控制台- API-Key 创建）"}))
                self.orch.set_api_key(key)
                return self._send(200, json.dumps({"ok": True}))
            if p == "/api/clone_voice":
                # 百炼声音复刻：URL 或 base64 参考音频 → 生成专属 voice_id
                url = str(body.get("url", "")).strip()
                b64 = body.get("audio_base64")
                prefix = str(body.get("prefix", "airadio")).strip()
                if not prefix.isalnum() or len(prefix) >= 10 or not prefix.islower():
                    return self._send(422, json.dumps(
                        {"error": "音色名(prefix)只能是 <10 位小写字母数字"}))
                if not self.orch.has_api_key():
                    return self._send(422, json.dumps({"error": "先填写百炼 API Key"}))
                import base64 as _b64
                import shutil
                import subprocess
                import tempfile
                api_key = self.orch.api_key or os.environ.get("DASHSCOPE_API_KEY")
                audio_data_uri = ""
                if b64:
                    raw = _b64.b64decode(b64.split(",", 1)[-1])
                    if len(raw) > 10 * 1024 * 1024:
                        return self._send(422, json.dumps({"error": "音频超过 10MB"}))
                    suffix = str(body.get("format") or "webm")
                    with tempfile.TemporaryDirectory() as td:
                        src = os.path.join(td, f"in.{suffix}")
                        dst = os.path.join(td, "out.wav")
                        with open(src, "wb") as f:
                            f.write(raw)
                        ffmpeg = shutil.which("ffmpeg")
                        if ffmpeg:
                            # 统一转成 24kHz 单声道 16bit WAV（百炼要求）
                            r = subprocess.run(
                                [ffmpeg, "-y", "-i", src, "-ar", "24000", "-ac", "1",
                                 "-sample_fmt", "s16", dst],
                                capture_output=True, text=True, timeout=60)
                            if r.returncode == 0:
                                audio_data_uri = "data:audio/wav;base64," + _b64.b64encode(
                                    open(dst, "rb").read()).decode("ascii")
                            else:
                                audio_data_uri = "data:audio/webm;base64," + _b64.b64encode(raw).decode("ascii")
                        else:
                            audio_data_uri = f"data:audio/{suffix};base64," + _b64.b64encode(raw).decode("ascii")
                elif url.startswith("http"):
                    audio_data_uri = ""  # 走下方 URL 分支
                else:
                    return self._send(422, json.dumps({"error": "需要录音(audio_base64)或参考音频 URL"}))
                import requests
                if audio_data_uri:
                    resp = requests.post(
                        "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization",
                        timeout=90,
                        headers={"Authorization": f"Bearer {api_key}",
                               "Content-Type": "application/json"},
                        json={"model": "voice-enrollment",
                              "input": {"action": "create_voice",
                                        "target_model": self.orch.model,
                                        "prefix": prefix, "audio": audio_data_uri}})
                    if resp.status_code != 200:
                        return self._send(502, json.dumps(
                            {"error": f"百炼: {resp.text[:200]}"}, ensure_ascii=False))
                    voice_id = resp.json().get("output", {}).get("voice_id", "")
                else:
                    from dashscope.audio.tts_v2 import VoiceEnrollmentService
                    svc = VoiceEnrollmentService(api_key=api_key, model=self.orch.model)
                    voice_id = svc.create_voice(target_model=self.orch.model,
                                                prefix=prefix, url=url)
                self.orch.log_append(f"voice: 复刻生成 {voice_id}")
                return self._send(200, json.dumps({"ok": True, "voice_id": voice_id}))
            if p == "/api/preview_voice":
                # 用指定音色（含新复刻的）合成一段试听
                text = (body.get("text") or "试听：这是新的音色。").strip()[:200]
                voice = str(body.get("voice", "")).strip()
                if not voice or not self.orch.has_api_key():
                    return self._send(422, json.dumps({"error": "需要音色与 API Key"}))
                from airadio.tts import TTSStreamer
                api_key = self.orch.api_key or os.environ.get("DASHSCOPE_API_KEY")
                t = TTSStreamer(api_key=api_key, model=self.orch.model, voice=voice)
                data = t.synth([text])
                if not data:
                    return self._send(502, json.dumps({"error": t.last_error or "合成失败"},
                                                      ensure_ascii=False))
                return self._send(200, data, "audio/mpeg")
            if p == "/api/tts_test":
                text = (body.get("text") or "").strip()[:200]
                if not text:
                    return self._send(400, b'{"error":"text required"}')
                if not self.orch.has_api_key():
                    return self._send(422, json.dumps({"error": "先填写百炼 API Key"}))
                audio = self.orch._synth([text])
                if not audio:
                    err = (self.orch.tts.last_error if self.orch.tts
                           else (self.orch.tts_init_error or "未配置 TTS"))
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
    global LIB
    LIB = LIB or Library(ROOT / "data" / "library.json")
    have = {t["title"] for t in LIB.data["topics"]}     # 只补新增的起点话题，不重复
    for t, n in gen.STARTER_TOPICS:
        if t not in have:
            LIB.add_topic(t, n)
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
