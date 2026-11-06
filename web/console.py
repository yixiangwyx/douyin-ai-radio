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
import subprocess
import sys
import threading
import urllib.parse
from urllib.parse import urlparse
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
from airadio import models  # noqa: E402
from airadio import placeholders  # noqa: E402
from airadio.library import Library  # noqa: E402
from airadio import generate as gen  # noqa: E402

STATE: dict = {}
LIB: Library = None  # type: ignore


def _enroll_error_hint(text: str) -> str:
    """把百炼的复刻报错翻译成人能照着改的话（都是实测过的错误码）。"""
    hints = {
        "prefix should be english letter and number": "音色名只能用英文小写字母或数字（中文名会被拒）",
        "prefix should not be longer than 10 characters": "音色名要少于 10 位",
        "AudioShortError": "参考音频里的有效语音太短：请录 10–20 秒连贯说话（不要只留环境声）",
        "request asr failed": "这段录音百炼听不出说了什么：安静环境、离麦克风近一点、连续说话再试",
        "InputDownloadFailed": "参考音频链接取不到（链接过期/需要登录），改用页面上的录音或选文件",
        "Throttling": "请求太频繁，稍等几秒再点一次",
        "QuotaExceeded": "该账号的复刻音色数量或额度到上限了",
    }
    for key, hint in hints.items():
        if key in text:
            return f"{hint}（原始：{text[:160]}）"
    return f"百炼: {text[:200]}"


def _prep_reference_audio(b64: str, fmt) -> str | tuple:
    """浏览器录音/上传文件 → 百炼要的 24kHz 单声道 WAV data URI，并在本地先体检一遍。

    本地体检的原因：百炼只回"audio too short / asr failed"，看不出是录太短还是没说话。
    """
    import base64 as _b64
    import shutil
    import subprocess
    import tempfile

    raw = _b64.b64decode(str(b64).split(",", 1)[-1])
    if len(raw) > 10 * 1024 * 1024:
        return (422, json.dumps({"error": "音频超过 10MB，录 10–20 秒就够"}))
    suffix = str(fmt or "webm").lower().replace("mpeg", "mp3")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, f"in.{suffix}")
        dst = os.path.join(td, "out.wav")
        with open(src, "wb") as f:
            f.write(raw)
        conv = subprocess.run([ffmpeg, "-y", "-i", src, "-ar", "24000", "-ac", "1",
                              "-sample_fmt", "s16", dst], capture_output=True, text=True,
                              timeout=60) if ffmpeg else None
        usable = dst if (conv and conv.returncode == 0) else src
        if conv is None:
            usable = src  # 没装 ffmpeg：原样送上去（让百炼报错）
        else:
            # 体检：总时长 + 有效语音时长（去掉静音）
            dur = _probe_duration(ffprobe, usable)
            speech = _speech_seconds(ffmpeg, usable)
            if dur is not None and dur < 4.0:
                return (422, json.dumps({
                    "error": f"录音只有 {dur:.1f} 秒，太短：请录 10–20 秒连贯朗读再提交"}))
            if speech is not None and speech < 1.5:
                return (422, json.dumps({
                    "error": f"录了 {dur:.1f} 秒但几乎听不到说话（有效语音 {speech:.1f} 秒）："
                             "请在安静环境里离麦克风近一点、连续说 10 秒以上"}))
        return "data:audio/wav;base64," + _b64.b64encode(open(usable, "rb").read()).decode("ascii")


def _probe_duration(ffprobe, path: str) -> float | None:
    if not ffprobe:
        return None
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                           "-of", "default=nk=1:nw=1", path], capture_output=True, text=True,
                           timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return None


def _speech_seconds(ffmpeg, path: str) -> float | None:
    """非静音时长（ffmpeg silencedetect 反推）。没有 ffmpeg 时返回 None（不拦）。"""
    if not ffmpeg:
        return None
    try:
        total = None
        r = subprocess.run([ffmpeg, "-i", path, "-af", "silencedetect=n=-35dB:d=0.35",
                           "-f", "null", "-"], capture_output=True, text=True, timeout=60)
        text = r.stderr
        dur = None
        for line in text.splitlines():
            if "Duration:" in line:
                m = line.split("Duration:")[1].split(",")[0].strip()
                h, mi, s = (m.split(":") + ["0"])[:3]
                dur = int(h) * 3600 + int(mi) * 60 + float(s)
                break
        total = dur or 0.0
        silent = 0.0
        for line in text.splitlines():
            if "silence_duration:" in line:
                silent += float(line.split("silence_duration:")[1].split()[0])
        return max(0.0, total - silent)
    except Exception:
        return None


def load_dotenv_script(path: str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    orch: Orchestrator = None  # type: ignore

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body, ctype="application/json; charset=utf-8", cache=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache or "no-store")
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
            q = urllib.parse.parse_qs(urlparse(self.path).query)
            log_after = None
            if q.get("log_after"):
                try:
                    log_after = max(0, int(q["log_after"][0]))
                except ValueError:
                    log_after = None
            lite = bool(q.get("lite"))
            # lite：轮询用，不带话术配置（前端另有 /api/phrases）；log_after 只要新增日志
            snap = self.orch.snapshot(events_n=12 if lite else 40, log_after=log_after,
                                      lite=lite)
            if lite:
                snap.pop("phrases", None)
            return self._send(200, json.dumps(snap, ensure_ascii=False).encode())
        if p == "/api/script":
            return self._send(200, json.dumps({"text": self.orch.get_script()}, ensure_ascii=False).encode())
        if p == "/api/library":
            return self._send(200, json.dumps(LIB.snapshot(), ensure_ascii=False).encode())
        if p == "/api/starter_topics":
            return self._send(200, json.dumps({"topics": [{"title": t, "note": n} for t, n in gen.STARTER_TOPICS]},
                                              ensure_ascii=False).encode())
        if p == "/api/models":
            # 可切换的语音模型 + 每个模型实测可用的系统音色
            return self._send(200, json.dumps({"models": models.catalog(),
                                               "current": {"model": self.orch.model,
                                                          "voice": self.orch.voice}},
                                              ensure_ascii=False).encode())
        if p == "/api/placeholders":
            return self._send(200, json.dumps(placeholders.doc(), ensure_ascii=False).encode())
        if p == "/api/phrases":
            return self._send(200, json.dumps(self.orch.snapshot()["phrases"],
                                              ensure_ascii=False).encode())
        if p == "/api/voices":
            # 列出该账号已生成（复刻/提示词设计）的音色；?model= 决定列哪个模型的
            if not self.orch.has_api_key():
                return self._send(200, json.dumps({"voices": []}))
            import requests
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            want = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
            target = (want.get("model") or self.orch.model).strip()
            api_key = self.orch.api_key or os.environ.get("DASHSCOPE_API_KEY")
            resp = requests.post(
                "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization",
                timeout=30,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"model": "voice-enrollment",
                      "input": {"action": "list_voice", "target_model": target}})
            voices = []
            if resp.status_code == 200:
                for v in resp.json().get("output", {}).get("voice_list", []):
                    voices.append({"voice_id": v.get("voice_id", ""),
                                   "status": v.get("status", "")})
            return self._send(200, json.dumps({"voices": voices, "target_model": target},
                                              ensure_ascii=False).encode())
        if p.startswith("/chunks/"):
            fp = (ROOT / "out" / p.lstrip("/")).resolve()
            if not str(fp).startswith(str((ROOT / "out").resolve())) or not fp.is_file():
                return self._send(404, b'{"error":"not found"}')
            data = fp.read_bytes()
            if fp.suffix == ".mp3":
                # 文件名带音频内容哈希 ⇒ 这个地址的内容永远不会变，浏览器可以永久缓存：
                # 一轮循环把整稿下载一次，之后循环全部命中本地缓存（网络下行≈0）。
                return self._send(200, data, "audio/mpeg",
                                  cache="public, max-age=31536000, immutable")
            return self._send(200, data, "application/json", cache="no-store")
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
                if body.get("model") or body.get("voice"):
                    LIB.save_runtime({"model": self.orch.model, "voice": self.orch.voice})
                return self._send(200, json.dumps({"ok": True}))
            if p == "/api/phrases":
                # 保存 + 即时生效：互动话术（多套随机）、昵称规则、随机报时、兜底话术
                patch_body = {}
                tpl = body.get("templates")
                if isinstance(tpl, dict):
                    patch_body["templates"] = {k: [str(x).strip() for x in (v or []) if str(x).strip()]
                                               for k, v in tpl.items()}
                if "nick_han_only" in body:
                    patch_body["nick_han_only"] = bool(body["nick_han_only"])
                if body.get("nick_max_chars") not in (None, ""):
                    patch_body["nick_max_chars"] = int(body["nick_max_chars"])
                if "announce_time" in body:
                    patch_body["announce_time"] = bool(body["announce_time"])
                iv = body.get("announce_interval_s")
                if isinstance(iv, list) and len(iv) == 2:
                    patch_body["announce_interval_s"] = [float(iv[0]), float(iv[1])]
                for key in ("time_templates", "fallback_templates"):
                    if isinstance(body.get(key), list):
                        patch_body[key] = [str(x).strip() for x in body[key] if str(x).strip()]
                self.orch.update_params(
                    templates=patch_body.get("templates"),
                    nick_han_only=patch_body.get("nick_han_only"),
                    nick_max_chars=patch_body.get("nick_max_chars"),
                    announce_time=patch_body.get("announce_time"),
                    announce_interval_s=patch_body.get("announce_interval_s"),
                    time_templates=patch_body.get("time_templates"),
                    fallback_templates=patch_body.get("fallback_templates"))
                saved = LIB.save_phrases(patch_body)
                return self._send(200, json.dumps({"ok": True, "saved": saved}, ensure_ascii=False).encode())
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
                # 两种生成方式：① 声音复刻（参考音频 URL 或录音/文件 base64）
                #                 ② 声音设计（只填提示词描述想要的音色）
                url = str(body.get("url", "")).strip()
                b64 = body.get("audio_base64")
                prompt = str(body.get("prompt", "")).strip()
                preview = str(body.get("preview_text", "")).strip()
                prefix = str(body.get("prefix", "airadio")).strip()
                if not re.fullmatch(r"[a-z0-9]{1,9}", prefix):
                    return self._send(422, json.dumps(
                        {"error": "音色名要用英文小写字母或数字，且少于 10 位（例：xiangge01）"}))
                if not self.orch.has_api_key():
                    return self._send(422, json.dumps({"error": "先填写百炼 API Key"}))
                target_model = str(body.get("target_model") or self.orch.model).strip()
                import requests
                api_key = self.orch.api_key or os.environ.get("DASHSCOPE_API_KEY")
                inp: dict = {"action": "create_voice", "target_model": target_model, "prefix": prefix}
                if prompt and not b64 and not url:
                    # 声音设计：voice_prompt + preview_text 必须成对给（实测）
                    if len(prompt) < 5:
                        return self._send(422, json.dumps({"error": "音色描述太短，写清性别/年龄/语气/用途"}))
                    inp["voice_prompt"] = prompt[:500]
                    inp["preview_text"] = (preview or "欢迎来到直播间，我们开始吧。")[:60]
                    if len(inp["preview_text"]) < 5:
                        inp["preview_text"] = "欢迎来到直播间，我们开始吧。"
                    inp["preferred_name"] = prefix
                else:
                    if b64:
                        audio_data_uri = _prep_reference_audio(b64, body.get("format"))
                        if isinstance(audio_data_uri, tuple):   # (code, message) 错误
                            return self._send(*audio_data_uri)
                        inp["audio"] = audio_data_uri
                    elif url.startswith("http"):
                        inp["url"] = url
                    else:
                        return self._send(422, json.dumps(
                            {"error": "要么录音/选文件（复刻），要么填音色描述（提示词生成）"}))
                resp = requests.post(
                    "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization",
                    timeout=120,
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"model": "voice-enrollment", "input": inp})
                if resp.status_code != 200:
                    return self._send(502, json.dumps(
                        {"error": _enroll_error_hint(resp.text[:300])}, ensure_ascii=False))
                voice_id = resp.json().get("output", {}).get("voice_id", "")
                how = "提示词生成" if "voice_prompt" in inp else "录音复刻"
                self.orch.log_append(f"voice: {how}生成 {voice_id}（{target_model}）")
                return self._send(200, json.dumps({
                    "ok": True, "voice_id": voice_id, "target_model": target_model,
                    "mode": "design" if "voice_prompt" in inp else "clone"}, ensure_ascii=False))
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
    ap.add_argument("--model", default="cosyvoice-v2",
                    help="cosyvoice-v3.5-flash / v3.5-plus（这两个必须配自建音色）/ v3-flash / v2")
    ap.add_argument("--voice", default="")
    ap.add_argument("--window", default="60,150")
    ap.add_argument("--min-gap", type=float, default=45.0)
    ap.add_argument("--merge", type=int, default=None)
    ap.add_argument("--gap", type=int, default=None)
    ap.add_argument("--no-trim-silence", action="store_true")
    ap.add_argument("--prefetch", type=int, default=None)
    ap.add_argument("--prefetch-all", action="store_true")
    args = ap.parse_args()

    w0, w1 = (float(x) for x in args.window.split(","))
    global LIB
    LIB = LIB or Library(ROOT / "data" / "library.json")
    have = {t["title"] for t in LIB.data["topics"]}     # 只补新增的起点话题，不重复
    for t, n in gen.STARTER_TOPICS:
        if t not in have:
            LIB.add_topic(t, n)

    runtime = LIB.get_runtime()        # 上次在页面上选的模型/音色：重启后自动接上
    model = runtime.get("model") or args.model
    voice = runtime.get("voice") or args.voice or models.default_voice_for(model)
    if not voice:
        raise SystemExit(f"{model} 没有系统音色：--voice 要给一个已生成的音色 id "
                         f"（页面上用提示词或录音生成，或 /api/clone_voice）")
    orch = Orchestrator(
        args.room, load_dotenv_script(ROOT / args.script),
        schedule=ScheduleConfig(min_gap_s=args.min_gap, window_s=(w0, w1)),
        model=model, voice=voice, out_dir=str(ROOT / "out" / "chunks"), sink=args.sink)
    saved = LIB.get_phrases()      # 页面上保存过的话术/昵称规则/报时设置：重启后继续用
    if saved:
        orch.update_params(
            templates=saved.get("templates"),
            nick_han_only=saved.get("nick_han_only"),
            nick_max_chars=saved.get("nick_max_chars"),
            announce_time=saved.get("announce_time"),
            announce_interval_s=saved.get("announce_interval_s"),
            time_templates=saved.get("time_templates"),
            fallback_templates=saved.get("fallback_templates"))
    orch.update_params(merge_sentences=args.merge, sentence_gap_ms=args.gap,
                       prefetch_ahead=args.prefetch,
                       trim_silence=(False if args.no_trim_silence else None))
    Handler.orch = orch
    threading.Thread(target=orch.run, daemon=True).start()
    if args.prefetch_all:
        try:
            print(f"预生成整稿：排队 {orch.prefetch_script(None)} 句")
        except ValueError as e:
            print("预生成整稿跳过：", e)

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"控制台: http://0.0.0.0:{args.port}  (手机同一WiFi直接打开)")
    print("Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        orch.stop()


if __name__ == "__main__":
    main()
