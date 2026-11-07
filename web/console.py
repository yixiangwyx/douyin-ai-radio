#!/usr/bin/env python3
"""console.py — 手机/浏览器控制台（多用户版）：登录 → 开播 → 播放/互动/改稿/音色/话术。

架构（薄服务端 + 每用户一套电台）：
- 服务端只做四件事：抖音采集（需要 cookie）、百炼 Key 代理、生成结果与音频的持久缓存、账号与稿件持久化。
- 每个登录用户一套 Orchestrator（自己的房间/稿子/话术/音色/播放进度/manifest）。
- 生成缓存 out/chunks/cache 跨用户共享：同稿同音色只花一次钱。
- 页面关闭（fetch keepalive /api/leave）或超过 --idle 秒没有请求 → 这套电台自动停止，不挂后台。

用法:
  云端多人版: uv run python web/console.py [--port 8010] [--idle 90] [--admin-password ***]
  PC 单机版:  uv run python web/app.py            （免登录，自动打开本机浏览器）
未登录打开首页 = 登录/注册页；管理后台 /admin。
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
import time
import urllib.parse
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_envp = ROOT / ".env"
if _envp.exists():
    for line in _envp.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z_]+)=(.+)$", line.strip())
        if m and m.group(1) not in os.environ:
            os.environ[m.group(1)] = m.group(2).strip()

from airadio.accounts import Accounts  # noqa: E402
from airadio.library import Library  # noqa: E402
from airadio.orchestrator import Orchestrator  # noqa: E402
from airadio.sessions import SessionHub, Session  # noqa: E402
from airadio.timeline import ScheduleConfig  # noqa: E402
from airadio import models  # noqa: E402
from airadio import placeholders  # noqa: E402
from airadio import generate as gen  # noqa: E402

ACCOUNTS: Accounts = None  # type: ignore
HUB: SessionHub = None  # type: ignore
DEFAULTS: dict = {}
COOKIE = "airadio_token"
LOCAL_MODE = False          # PC 单机模式：免登录，所有请求都算本机主人
LOCAL_USER = "local"

# 开播默认音色（翔哥指定）：中年男性 55 岁，稳重沙哑
DEFAULT_VOICE_KEYWORD = "male55"
# 新建音色默认走这个模型（翔哥指定）：v3.5-flash 只能用自建音色，正好配合提示词生成
DEFAULT_VOICE_MODEL = "cosyvoice-v3.5-flash"
VOICE_PRESETS = {
    "male55": "中年男性，55岁左右，嗓音稳重略带沙哑，低沉有质感，语速中等偏慢，语气沉稳可信，适合电台口播长时间听不累",
}


class Generator:
    """每用户一个：按生成配置在后台写新稿，写完排队等着接播（只用一次，不进稿子库）。"""

    def __init__(self, lib: "Library"):
        self.lib = lib
        self.pending: list[dict] = []      # 已生成好、还没播的稿
        self.worker: threading.Thread | None = None
        self.last_error = ""
        self.on_log = None

    def busy(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def start(self, template_text: str = "") -> bool:
        """后台生成下一稿；已有任务在跑或队列已够就跳过。返回是否起了任务。"""
        cfg = self.lib.get_gen_cfg()
        if cfg.get("auto_generate") is False:
            return False
        if self.busy() or len(self.pending) >= 2:
            return False
        avoid = list(getattr(self, "avoid", []) or [])
        self.worker = threading.Thread(target=self._run, args=(template_text, avoid), daemon=True)
        self.worker.start()
        return True

    def _run(self, template_text: str, avoid: list[str]) -> None:
        cfg = self.lib.get_gen_cfg()
        try:
            text, model = gen.generate_script_llm(
                cfg, avoid=avoid,
                system_override=(template_text or None),
                api_key_fallback=os.environ.get("DASHSCOPE_API_KEY"))
        except Exception as e:
            self.last_error = str(e)
            if self.on_log:
                self.on_log(f"ai_gen: 生成失败 {self.last_error}")
            return
        title = "AI稿 " + time.strftime("%m-%d %H:%M")
        item = {"title": title, "text": text, "model": model, "created": time.time()}
        self.pending.append(item)
        self.pending = self.pending[-4:]
        if self.on_log:
            self.on_log(f"ai_gen: 新稿已生成「{title}」（{model}，{len(text.splitlines())} 行），排队等接播")

    def take(self) -> dict | None:
        if self.pending:
            return self.pending.pop(0)
        return self.lib.pop_gen_script()     # 上次进程没播完的 AI 稿也用掉，不留存


def _wire_generator(sess: "Session") -> Generator:
    """把 Generator 接到这套电台的播放循环上（讲到一半自动生成、播完自动接）。"""
    g = Generator(sess.lib)
    g.on_log = sess.orch.log_append
    g.avoid = sess.orch.played_titles
    orch, lib = sess.orch, sess.lib
    orch.set_ai_feed(
        feeder=lambda: g.take(),
        gen_hook=lambda: g.start(),
        queue_depth=lambda: len(g.pending) + len(lib.data.get("gen_queue") or []))
    orch.gen_busy = g.busy
    orch.last_gen_error = getattr(g, "last_error", "") or ""
    cfg = lib.get_gen_cfg()
    orch.set_ai_feed(enabled=bool(cfg.get("auto_generate", True)),
                     at=float(cfg.get("auto_generate_at") or 0.5))
    if LOCAL_MODE:
        sess.keep_alive = True    # 单机版：关掉页面电台也不停，只有点「停止」才停
    sess.generator = g
    return g


# ---------------------------------------------------------------- 鉴权
def _cookie_token(handler) -> str:
    raw = handler.headers.get("Cookie", "")
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return ""


def _whoami(handler) -> str:
    if LOCAL_MODE:
        return LOCAL_USER
    return ACCOUNTS.verify_token(_cookie_token(handler))


def _auth_required(handler):
    """返回 username；不通过时已发出 401/403 并返回 None。"""
    if LOCAL_MODE:
        return LOCAL_USER
    token = _cookie_token(handler)
    username = ACCOUNTS.verify_token(token)
    if not username:
        _send(handler, 401, {"error": "请先登录", "need_login": True})
        return None
    status = ACCOUNTS.status_of(username)
    if status == "disabled":
        _send(handler, 403, {"error": "这个账号已被管理员停用，不能继续使用", "need_login": True})
        return None
    if status == "pending":
        _send(handler, 403, {"error": "账号还在等管理员批准", "need_login": True})
        return None
    return username


def _session_for(handler):
    """取当前用户的电台会话。返回 (username, session, replied)：
    replied=True 表示已经发过响应（401/403/410），调用方直接 return。"""
    username = _auth_required(handler)
    if username is None:
        return None, None, True
    sess = HUB.get(username)
    if sess is None:
        _send(handler, 410, {"error": "这一场已经停止了（页面关掉后空闲一段时间会自动停止），点「开始新直播」重新开始",
                             "need_start": True})
        return username, None, True
    return username, sess, False


# ---------------------------------------------------------------- 响应helper
def _send(handler, code: int, payload, ctype="application/json; charset=utf-8", cache=None,
          extra_headers=None):
    body = payload if isinstance(payload, bytes) else (
        payload.encode("utf-8") if isinstance(payload, str)
        else json.dumps(payload, ensure_ascii=False).encode())
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", cache or "no-store")
    # 额外头（如 Set-Cookie）必须在 send_response 之后写：写在前面会让状态行错位，
    # 严格客户端直接报 BadStatusLine（curl 宽容所以本地测不出来）。
    for k, v in (extra_headers or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


def _page(handler, filename: str):
    html = (ROOT / "web" / filename).read_text(encoding="utf-8")
    _send(handler, 200, html.encode("utf-8"), "text/html; charset=utf-8")


# ---------------------------------------------------------------- 百炼音色
def _enroll_error_hint(text: str) -> str:
    hints = {
        "prefix should be english letter and number": "音色名只能用英文小写字母或数字（中文名会被拒）",
        "prefix should not be longer than 10 characters": "音色名要少于 10 位",
        "AudioShortError": "参考音频里的有效语音太短：请录 10–20 秒连贯说话（不要只留环境声）",
        "request asr failed": "这段录音百炼听不出说了什么：安静环境、离麦克风近一点、连续说话再试",
        "InputDownloadFailed": "参考音频链接取不到（链接过期/需要登录），改用页面上的录音或选文件",
        "Throttling": "请求太频繁，稍等几秒再点一次",
        "preview_text should not be shorter than 15 characters": "试听文本太短（百炼要求至少 15 个字），用默认那句就行",
        "QuotaExceeded": "该账号的复刻音色数量或额度到上限了",
    }
    for key, hint in hints.items():
        if key in text:
            return f"{hint}（原始：{text[:160]}）"
    return f"百炼: {text[:200]}"


def _prep_reference_audio(b64: str, fmt) -> str | tuple:
    """浏览器录音/上传文件 → 百炼要的 24kHz 单声道 WAV data URI，并在本地先体检一遍。"""
    import base64 as _b64
    import shutil
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
                              "-sample_fmt", s16fmt(), dst], capture_output=True, text=True,
                              timeout=60) if ffmpeg else None
        usable = dst if (conv and conv.returncode == 0) else src
        if conv is None:
            usable = src
        else:
            dur = _probe_duration(ffprobe, usable)
            speech = _speech_seconds(ffmpeg, usable)
            if dur is not None and dur < 4.0:
                return (422, json.dumps({"error": f"录音只有 {dur:.1f} 秒，太短：请录 10–20 秒连贯朗读再提交"}))
            if speech is not None and speech < 1.5:
                return (422, json.dumps({"error": f"录了 {dur:.1f} 秒但几乎听不到说话（有效语音 {speech:.1f} 秒）："
                                            "请在安静环境里离麦克风近一点、连续说 10 秒以上"}))
        return "data:audio/wav;base64," + _b64.b64encode(open(usable, "rb").read()).decode("ascii")


def s16fmt():
    return "s16"


def _probe_duration(ffprobe, path: str) -> float | None:
    if not ffprobe:
        return None
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                           "-of", "default=nk=1:nw=1", path], capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return None


def _speech_seconds(ffmpeg, path: str) -> float | None:
    if not ffmpeg:
        return None
    try:
        r = subprocess.run([ffmpeg, "-i", path, "-af", "silencedetect=n=-35dB:d=0.35",
                           "-f", "null", "-"], capture_output=True, text=True, timeout=60)
        text = r.stderr
        total = 0.0
        for line in text.splitlines():
            if "Duration:" in line:
                m = line.split("Duration:")[1].split(",")[0].strip()
                h, mi, s = (m.split(":") + ["0"])[:3]
                total = int(h) * 3600 + int(mi) * 60 + float(s)
                break
        silent = 0.0
        for line in text.splitlines():
            if "silence_duration:" in line:
                silent += float(line.split("silence_duration:")[1].split()[0])
        return max(0.0, total - silent)
    except Exception:
        return None


# 百炼要求 preview_text ≥15 个字符（短的会 InvalidParameter: preview_text should not be shorter than 15 characters）
PREVIEW_TEXT = "欢迎来到直播间，我们现在开始今天的直播。"


def _bailian_enroll(api_key: str, inp: dict):
    import requests
    resp = requests.post("https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization",
                         timeout=120, headers={"Authorization": f"Bearer {api_key}",
                                              "Content-Type": "application/json"},
                         json={"model": "voice-enrollment", "input": inp})
    return resp


def _lib_for(user: str) -> Library:
    """该用户的库（和会话用的是同一个文件）：没开播时也要能读配置/Key/音色记录。"""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in user)
    return Library(HUB.users_dir / safe / "library.json")


def _bailian_key(lib: Library | None = None, orch=None) -> str:
    """按优先级找百炼 Key：这套电台的 → 该用户存过的 → 服务器 .env。"""
    if orch is not None and getattr(orch, "api_key", ""):
        return orch.api_key
    if lib is not None and lib.get_bailian_key():
        return lib.get_bailian_key()
    return os.environ.get("DASHSCOPE_API_KEY") or ""


def _voice_owner(voice_id: str, lib: Library) -> bool:
    """这个音色是不是本机/本账号在这个控制台建的：库里存过才算（没记录的 = 别的渠道建的，只显示不删）。"""
    return bool(lib.find_voice(voice_id))


def _voice_label(v: dict, recorded: dict | None) -> str:
    """给前端的音色可读名字：优先我们自己记的名字，其次百炼返回的 voice_prompt。"""
    if recorded and recorded.get("label"):
        return recorded["label"]
    return (v.get("voice_prompt") or "").strip()


def _auto_voice_prefix(keyword: str) -> str:
    """自动生成音色名：关键字 + 时间码（每场不同，不会撞同一个名字），≤9 位。"""
    kw = re.sub(r"[^a-z0-9]", "", (keyword or DEFAULT_VOICE_KEYWORD).lower()) or DEFAULT_VOICE_KEYWORD
    return (kw[:6] + time.strftime("%d%H%M"))[:9]


def _default_script_text() -> str:
    """默认稿：优先稿子库里管理员准备的那套，否则 scripts/default_script.txt。"""
    fallback = ROOT / "scripts" / "default_script.txt"
    return fallback.read_text(encoding="utf-8") if fallback.exists() else ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

    # ------------------------------- GET -------------------------------
    def do_GET(self):
        p = self.path.split("?")[0]
        q = urllib.parse.parse_qs(urlparse(self.path).query)
        try:
            if p in ("/", "/index.html", "/console"):
                if LOCAL_MODE:
                    return _page(self, "console.html")
                return _page(self, "login.html") if not _whoami(self) else _page(self, "console.html")
            if p == "/admin":
                # 未登录/不是管理员 → 直接给登录页（不能先发 401 再发页面：那会是两次响应）
                user = _whoami(self)
                if not user or ACCOUNTS.role_of(user) != "admin":
                    return _page(self, "login.html")
                return _page(self, "admin.html")
            if p.endswith((".js", ".css")):
                fp = (ROOT / "web" / p.lstrip("/")).resolve()
                if not str(fp).startswith(str((ROOT / "web").resolve())) or not fp.is_file():
                    return _send(self, 404, {"error": "no route"})
                kind = "application/javascript" if p.endswith(".js") else "text/css"
                return _send(self, 200, fp.read_bytes(), f"{kind}; charset=utf-8")
            if p == "/me":
                user = _whoami(self)
                if not user:
                    return _send(self, 401, {"ok": False, "need_login": True})
                return _send(self, 200, {"ok": True, "username": user,
                                         "role": ACCOUNTS.role_of(user),
                                         "session": bool(HUB.get(user)),
                                         "local": LOCAL_MODE,
                                         "idle_stop_s": 0 if LOCAL_MODE else HUB.idle_s})
        except Exception as e:
            return _send(self, 500, {"error": f"{type(e).__name__}: {e}"})

        if p == "/api/state":
            user, sess, replied = _session_for(self)
            if replied:
                return
            log_after = None
            if q.get("log_after"):
                try:
                    log_after = max(0, int(q["log_after"][0]))
                except ValueError:
                    log_after = None
            lite = bool(q.get("lite"))
            snap = sess.orch.snapshot(events_n=12 if lite else 40, log_after=log_after, lite=lite)
            if lite:
                snap.pop("phrases", None)
            snap["session"] = {"username": user,
                               "idle_stop_s": 0 if LOCAL_MODE else HUB.idle_s,
                               "idle_s": round(sess.idle_s()), "online_users": HUB.count()}
            return _send(self, 200, snap)
        if p == "/api/status":
            user = _auth_required(self)
            if user is None:
                return
            return _send(self, 200, {"online": HUB.online(), "count": HUB.count(),
                                     "idle_stop_s": HUB.idle_s, "log": HUB.events[-30:]})
        if p == "/api/script":
            _, sess, replied = _session_for(self)
            if replied:
                return
            return _send(self, 200, {"text": sess.orch.get_script(), "title": sess.orch.script_title})
        if p == "/api/library":
            user, sess, replied = _session_for(self)
            if replied:
                return
            snap = sess.lib.snapshot()
            snap["default_title"] = DEFAULTS.get("script_title", "")
            return _send(self, 200, snap)
        if p == "/api/models":
            user = _auth_required(self)
            if user is None:
                return
            lib = _lib_for(user)
            sess = HUB.get(user)
            if sess:
                cur = {"model": sess.orch.model, "voice": sess.orch.voice}
            else:
                # 没开播时也要看得到"下次开播会用哪个模型+音色"
                rt = lib.get_runtime()
                cur = {"model": rt.get("model") or DEFAULT_VOICE_MODEL,
                       "voice": rt.get("voice") or "", "not_running": True}
            return _send(self, 200, {"models": models.catalog(), "current": cur,
                                     "default_model": DEFAULT_VOICE_MODEL,
                                     "key_set": bool(_bailian_key(lib, sess.orch if sess else None))})
        if p == "/api/douyin_cookie":
            _, sess, replied = _session_for(self)
            if replied:
                return
            d = sess.orch.douyin_cookie
            from airadio.live import cookie_has_login
            return _send(self, 200, {"cookie_set": bool(d), "has_login": cookie_has_login(d),
                                     "gift_available": sess.orch.collector.gift_available})
        if p == "/api/placeholders":
            if _auth_required(self) is None:
                return
            return _send(self, 200, placeholders.doc())
        if p == "/api/phrases":
            _, sess, replied = _session_for(self)
            if replied:
                return
            return _send(self, 200, sess.orch.snapshot()["phrases"])
        if p == "/api/voices":
            user = _auth_required(self)
            if user is None:
                return
            lib = _lib_for(user)
            sess = HUB.get(user)
            api_key = _bailian_key(lib, sess.orch if sess else None)
            if not api_key:
                return _send(self, 200, {"voices": [], "target_model": "", "key_set": False})
            target = (q.get("model") or [(sess.orch.model if sess
                                          else lib.get_runtime().get("model")
                                          or DEFAULTS.get("model", DEFAULT_VOICE_MODEL))])[0]
            # 百炼 list_voice 默认只给 10 条（实测 total_count 21 时页面就"看不到自己生成的"）：翻页取全
            voices = []
            total, page = None, 0
            while True:
                resp = _bailian_enroll(api_key, {"action": "list_voice", "target_model": target,
                                                 "page_index": page, "page_size": 100})
                if resp.status_code != 200:
                    break
                out = resp.json().get("output", {}) or {}
                voices.extend(out.get("voice_list", []) or [])
                total = out.get("total_count")
                page += 1
                if total is None or len(voices) >= int(total) or page > 20:
                    break
            raw_voices = list(voices)
            voices = []
            for v in raw_voices:
                vid = v.get("voice_id", "")
                if len(vid) < 4:
                    continue
                rec = lib.find_voice(vid)
                voices.append({
                    "voice_id": vid,
                    "status": v.get("status", ""),
                    "created": v.get("gmt_create", ""),
                    # 用生成时的提示词当可读名字（百炼自己返回 voice_prompt）
                    "prompt": (v.get("voice_prompt") or (rec or {}).get("prompt", "") or "").strip(),
                    "label": (rec or {}).get("label", "") or "",
                    "mode": (rec or {}).get("mode", ""),
                    "preview_text": (v.get("preview_text") or "").strip(),
                    "mine": bool(rec),          # 只有本机建过的才允许删
                })
            voices.sort(key=lambda x: x.get("created") or "", reverse=True)
            return _send(self, 200, {"voices": voices, "target_model": target, "key_set": True})
        if p == "/api/local_llm":
            # 找这台电脑/局域网里已经在跑的 OpenAI 兼容模型（翔哥那台推理机）：
            # 用它写直播稿不花百炼文本额度
            tried = []
            # 默认只探本机；局域网那台模型用环境变量 AIRADIO_LOCAL_LLM 指定
            cands = [os.environ.get("AIRADIO_LOCAL_LLM", ""), "http://127.0.0.1:8080/v1"]
            cands += [c.strip() for c in os.environ.get("AIRADIO_LOCAL_LLM_EXTRA", "").split(",") if c.strip()]
            for base in cands:
                base = (base or "").strip().rstrip("/")
                if not base or base in tried:
                    continue
                tried.append(base)
                try:
                    with urllib.request.urlopen(base + "/models", timeout=4) as r:
                        data = json.loads(r.read().decode())
                    ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
                    if ids:
                        return _send(self, 200, {"ok": True, "base_url": base, "models": ids})
                except Exception:
                    continue
            return _send(self, 200, {"ok": False, "base_url": "", "models": []})
        if p == "/api/gen_cfg":
            user = _auth_required(self)
            if user is None:
                return
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in user)
            lib = Library(HUB.users_dir / safe / "library.json")
            sess = HUB.get(user)
            d = lib.snapshot()
            d["default_prompt"] = gen.DEFAULT_GEN_PROMPT
            d["default_model"] = gen.DEFAULT_TEXT_MODEL
            d["default_voice_keyword"] = DEFAULT_VOICE_KEYWORD
            d["default_voice_desc"] = VOICE_PRESETS[DEFAULT_VOICE_KEYWORD]
            d["generating"] = bool(sess and getattr(sess, "generator", None) and sess.generator.busy())
            # Key 不回传内容，只告诉前端"已保存"
            if d.get("gen_cfg", {}).get("api_key"):
                d["gen_cfg"] = dict(d["gen_cfg"], api_key="")
                d["gen_cfg"]["api_key_set"] = True
            return _send(self, 200, d)
        if p.startswith("/chunks/"):
            # 音频按用户目录找（每用户一份 manifest），内容哈希文件名可被浏览器永久缓存
            user = _auth_required(self)
            if user is None:
                return
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in user)
            fp = (ROOT / "out" / "chunks" / safe / p.lstrip("/chunks/")).resolve()
            if not str(fp).startswith(str((ROOT / "out").resolve())) or not fp.is_file():
                return _send(self, 404, {"error": "not found"})
            data = fp.read_bytes()
            if fp.suffix == ".mp3":
                return _send(self, 200, data, "audio/mpeg",
                            cache="public, max-age=31536000, immutable")
            return _send(self, 200, data, "application/json")
        return _send(self, 404, {"error": "no route"})

    # ------------------------------- POST ------------------------------
    def do_POST(self):
        p = self.path.split("?")[0]
        try:
            body = self._json_body()
        except Exception:
            return _send(self, 400, {"error": "bad json"})
        try:
            # ---------- 账号 ----------
            if p == "/api/register":
                out = ACCOUNTS.register(str(body.get("username", "")), str(body.get("password", "")))
                if isinstance(out, tuple):
                    return _send(self, out[0], {"error": out[1]})
                return _send(self, 200, out)
            if p == "/api/login":
                out = ACCOUNTS.login(str(body.get("username", "")), str(body.get("password", "")))
                if isinstance(out, tuple):
                    return _send(self, out[0], {"error": out[1]})
                return _send(self, 200, out,
                             extra_headers={"Set-Cookie":
                                            f"{COOKIE}={out['token']}; Path=/; HttpOnly; SameSite=Lax"})
            if p == "/api/logout":
                user = _whoami(self)
                if user:
                    HUB.leave(user)
                return _send(self, 200, {"ok": True},
                             extra_headers={"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0"})
            if p == "/api/password":
                user = _auth_required(self)
                if user is None:
                    return
                old = str(body.get("old", ""))
                new = str(body.get("new", ""))
                if not ACCOUNTS.login(user, old).get("ok"):
                    return _send(self, 401, {"error": "原密码不对"})
                if not ACCOUNTS.set_password(user, new):
                    return _send(self, 422, {"error": "新密码至少 6 位"})
                return _send(self, 200, {"ok": True})

            # ---------- 管理后台 ----------
            if p.startswith("/api/admin/"):
                user = _auth_required(self)
                if user is None:
                    return
                if ACCOUNTS.role_of(user) != "admin":
                    return _send(self, 403, {"error": "只有管理员能进后台"})
                target = str(body.get("username", "")).strip()
                if p == "/api/admin/users":
                    return _send(self, 200, {"users": ACCOUNTS.list_users(), "online": HUB.online()})
                if p == "/api/admin/approve":
                    return _send(self, 200, {"ok": ACCOUNTS.approve(target)})
                if p == "/api/admin/disable":
                    ok = ACCOUNTS.disable(target)
                    if ok:
                        HUB.leave(target)      # 停用即踢下线
                    return _send(self, 200, {"ok": ok})
                if p == "/api/admin/enable":
                    return _send(self, 200, {"ok": ACCOUNTS.enable(target)})
                if p == "/api/admin/delete":
                    ok = ACCOUNTS.delete_user(target)
                    if ok:
                        HUB.leave(target)
                    return _send(self, 200, {"ok": ok})
                if p == "/api/admin/stop_user":
                    return _send(self, 200, {"ok": HUB.leave(target)})
                if p == "/api/admin/password":
                    return _send(self, 200, {"ok": ACCOUNTS.set_password(target, str(body.get("new", "")))})
                return _send(self, 404, {"error": "no route"})

            # ---------- AI 生成配置（不需要电台在跑：先配好再生成/开播） ----------
            if p in ("/api/gen_cfg", "/api/gen_cfg_save", "/api/gen_template_save",
                     "/api/gen_template_delete", "/api/generate_now", "/api/gen_use"):
                user = _auth_required(self)
                if user is None:
                    return
                safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in user)
                lib = Library(HUB.users_dir / safe / "library.json")   # 和会话用的是同一份库
                sess = HUB.get(user)

                if p == "/api/gen_cfg":
                    d = lib.snapshot()
                    d["default_prompt"] = gen.DEFAULT_GEN_PROMPT
                    d["default_model"] = gen.DEFAULT_TEXT_MODEL
                    d["default_voice_keyword"] = DEFAULT_VOICE_KEYWORD
                    d["default_voice_desc"] = VOICE_PRESETS[DEFAULT_VOICE_KEYWORD]
                    d["generating"] = bool(sess and getattr(sess, "generator", None) and sess.generator.busy())
                    return _send(self, 200, d)
                if p == "/api/gen_cfg_save":
                    patch_body = {k: body[k] for k in
                                  ("backend", "model", "base_url", "api_key", "prompt", "lines",
                                   "auto_generate", "auto_generate_at") if k in body}
                    saved = lib.save_gen_cfg(patch_body)
                    if sess and getattr(sess, "generator", None):
                        sess.orch.set_ai_feed(enabled=bool(saved.get("auto_generate", True)),
                                              at=float(saved.get("auto_generate_at") or 0.5))
                        sess.orch.log_append(
                            "ai_gen: 配置已更新（自动生成"
                            f"{'开' if saved.get('auto_generate', True) else '关'}）")
                    out = {k: v for k, v in saved.items() if k != "api_key"}
                    out["api_key_set"] = bool(saved.get("api_key"))
                    note = None if sess else "配置已保存；这套电台现在没在跑，开播时生效"
                    return _send(self, 200, {"ok": True, "saved": out, "note": note})
                if p == "/api/gen_template_save":
                    item = lib.add_gen_template(body.get("title", ""), body.get("text", ""))
                    return _send(self, 200, {"ok": True, "id": item["id"], "title": item["title"]})
                if p == "/api/gen_template_delete":
                    return _send(self, 200, {"ok": lib.remove_gen_template(body.get("id", ""))})
                if p == "/api/generate_now":
                    template = ""
                    if body.get("template_id"):
                        t = lib.get_gen_template(str(body.get("template_id")))
                        if not t:
                            return _send(self, 404, {"error": "提示词不存在"})
                        template = t["text"]
                    elif str(body.get("prompt", "")).strip():
                        template = str(body.get("prompt")).strip()
                    cfg = lib.get_gen_cfg()
                    try:
                        text, gmodel = gen.generate_script_llm(
                            cfg, avoid=(sess.orch.played_titles if sess else []),
                            system_override=(template or None),
                            api_key_fallback=os.environ.get("DASHSCOPE_API_KEY"))
                    except Exception as e:
                        return _send(self, 502, {"error": f"生成失败：{e}"})
                    title = "AI稿 " + time.strftime("%m-%d %H:%M")
                    if sess:
                        sess.orch.update_script(text, apply_now=bool(body.get("apply_now", True)))
                        sess.orch.script_title = title
                        sess.orch.played_titles.append(title)
                        sess.orch.log_append(f"ai_gen: 手动生成新稿「{title}」（{gmodel}，{len(text.splitlines())} 行）")
                    return _send(self, 200, {"ok": True, "title": title, "model": gmodel,
                                             "text": text, "lines": len(text.splitlines()),
                                             "applied": bool(sess and body.get("apply_now", True))})
                if p == "/api/gen_use":
                    if not sess:
                        return _send(self, 410, {"error": "这套电台没在跑", "need_start": True})
                    g = getattr(sess, "generator", None)
                    item = (g.take() if g else None) or lib.pop_gen_script()
                    if not item:
                        return _send(self, 422, {"error": "没有已生成的新稿（先在上面点「生成新稿」或等自动生成）"})
                    sess.orch.update_script(item["text"], apply_now=True)
                    sess.orch.script_title = item.get("title", "AI新稿")
                    sess.orch.played_titles.append(sess.orch.script_title)
                    sess.orch._script_is_one_shot = True
                    return _send(self, 200, {"ok": True, "title": sess.orch.script_title,
                                             "lines": len(item["text"].splitlines())})

            # ---------- 音色管理（不需要电台在跑：没开播也能生成/列出/改名/删除/试听） ----------
            if p in ("/api/voice_save", "/api/voice_delete", "/api/voice_label",
                     "/api/voice_preview", "/api/voice_default", "/api/api_key"):
                user = _auth_required(self)
                if user is None:
                    return
                lib = _lib_for(user)
                sess = HUB.get(user)
                orch = sess.orch if sess else None

                if p == "/api/api_key":
                    key = str(body.get("key", "")).strip()
                    if not key.startswith("sk-") or len(key) < 20:
                        return _send(self, 422, {"error": "Key 应以 sk- 开头（百炼控制台 - API-Key 创建）"})
                    lib.save_bailian_key(key)          # 存下来：没开播时也能生成/管理音色
                    os.environ.setdefault("DASHSCOPE_API_KEY", key)
                    if orch:
                        orch.set_api_key(key)
                    return _send(self, 200, {"ok": True})

                if p == "/api/voice_save":
                    api_key = _bailian_key(lib, orch)
                    if not api_key:
                        return _send(self, 422, {"error": "先填百炼 API Key（本页上面那格，或服务器 .env）"})
                    url = str(body.get("url", "")).strip()
                    b64 = body.get("audio_base64")
                    prompt = str(body.get("prompt", "")).strip()
                    preview = str(body.get("preview_text", "")).strip()
                    label = str(body.get("label", "")).strip()
                    prefix = str(body.get("prefix") or DEFAULT_VOICE_KEYWORD).strip().lower()
                    prefix = _auto_voice_prefix(prefix)
                    if not re.fullmatch(r"[a-z0-9]{1,9}", prefix):
                        return _send(self, 422, {"error": "音色名要用英文小写字母或数字，且少于 10 位（例：xiangge01）"})
                    target_model = str(body.get("target_model") or "").strip() \
                        or (orch.model if orch else "") \
                        or lib.get_runtime().get("model") or DEFAULT_VOICE_MODEL
                    inp = {"action": "create_voice", "target_model": target_model, "prefix": prefix}
                    if prompt and not b64 and not url:
                        if len(prompt) < 5:
                            return _send(self, 422, {"error": "音色描述太短，写清性别/年龄/语气/用途"})
                        inp["voice_prompt"] = prompt[:500]
                        inp["preview_text"] = (preview or PREVIEW_TEXT)[:60]
                        if len(inp["preview_text"]) < 15:
                            inp["preview_text"] = PREVIEW_TEXT
                        inp["preferred_name"] = prefix
                    else:
                        if b64:
                            audio_data_uri = _prep_reference_audio(b64, body.get("format"))
                            if isinstance(audio_data_uri, tuple):
                                return _send(self, *audio_data_uri)
                            inp["audio"] = audio_data_uri
                        elif url.startswith("http"):
                            inp["url"] = url
                        else:
                            return _send(self, 422, {"error": "要么录音/选文件（复刻），要么填音色描述（提示词生成）"})
                    resp = _bailian_enroll(api_key, inp)
                    if resp.status_code != 200:
                        return _send(self, 502, {"error": _enroll_error_hint(resp.text[:300])})
                    voice_id = resp.json().get("output", {}).get("voice_id", "")
                    if not voice_id:
                        # 200 但没有 voice_id：不能当成功返回，否则前端会选中一个空音色
                        return _send(self, 502, {"error": "百炼返回了成功但没有音色 ID（生成未完成，请重试）"})
                    mode = "design" if "voice_prompt" in inp else "clone"
                    rec = lib.add_voice_record(voice_id, model=target_model,
                                               label=label or prefix, prompt=inp.get("voice_prompt", ""),
                                               mode=mode)
                    if orch:
                        orch.log_append(f"voice: {'提示词生成' if mode == 'design' else '录音复刻'} {voice_id}（{target_model}）")
                    return _send(self, 200, {"ok": True, "voice_id": voice_id, "target_model": target_model,
                                             "mode": mode, "label": rec.get("label", ""),
                                             "preview_audio": (resp.json().get("output", {}) or {}).get("preview_audio", {}).get("data", "")})

                if p == "/api/voice_delete":
                    vid = str(body.get("voice_id", "")).strip()
                    if not vid:
                        return _send(self, 422, {"error": "没给音色 ID"})
                    if not lib.find_voice(vid) and not body.get("force"):
                        # 本机没记录（在百炼后台/别的工具做的）：先问一句，用户确认了才真删
                        return _send(self, 422, {"error": "这个音色不是这个页面生成的。确认要删就再点一次「删除」"
                                                        "（百炼那边会一起删掉，删了不能恢复）",
                                                 "need_confirm": True})
                    api_key = _bailian_key(lib, orch)
                    resp = _bailian_enroll(api_key, {"action": "delete_voice", "voice_id": vid}) if api_key else None
                    lib.remove_voice_record(vid)
                    runtime = lib.get_runtime()
                    note = "已从百炼删除并清掉本机记录"
                    if resp is not None and resp.status_code != 200:
                        note = "本机记录已清掉；但百炼那边没删成：" + _enroll_error_hint(resp.text[:200])
                    if runtime.get("voice") == vid:
                        lib.save_runtime({"voice": ""})
                        note += "（它原来是当前音色，已清空，下次开播前请另选一个）"
                        if orch:
                            orch.log_append("voice: 当前音色被删除，需另选")
                    return _send(self, 200, {"ok": True, "note": note})

                if p == "/api/voice_label":
                    vid = str(body.get("voice_id", "")).strip()
                    if not lib.set_voice_label(vid, str(body.get("label", ""))):
                        return _send(self, 404, {"error": "本机没有这个音色的记录（只能改本机生成的）"})
                    return _send(self, 200, {"ok": True})

                if p == "/api/voice_default":
                    # 记住"下次开播用哪个模型+音色"；电台在跑就同时即时生效
                    model = str(body.get("model", "")).strip()
                    voice = str(body.get("voice", "")).strip()
                    if model or voice:
                        info = models.model_info(model) if model else None
                        if model and info is None:
                            return _send(self, 422, {"error": f"未知模型 {model}"})
                        if voice and model and not models.is_usable_voice(model, voice):
                            return _send(self, 422, {"error": f"音色 {voice} 不能用在 {model} 上：音色绑定生成它的模型"})
                        lib.save_runtime({k: v for k, v in (("model", model), ("voice", voice)) if v})
                    if orch and (model or voice):
                        try:
                            orch.update_params(model=model or None, voice=voice or None)
                        except ValueError as e:
                            return _send(self, 422, {"error": str(e)})
                    return _send(self, 200, {"ok": True, "model": model or (orch.model if orch else lib.get_runtime().get("model", "")),
                                             "voice": voice or (orch.voice if orch else lib.get_runtime().get("voice", ""))})

                if p == "/api/voice_preview":
                    text = (body.get("text") or "试听：这是新的音色，欢迎来到直播间。").strip()[:200]
                    voice = str(body.get("voice", "")).strip()
                    model = str(body.get("model", "")).strip() \
                        or (orch.model if orch else "") or lib.get_runtime().get("model") or DEFAULT_VOICE_MODEL
                    api_key = _bailian_key(lib, orch)
                    if not voice or not api_key:
                        return _send(self, 422, {"error": "需要音色与 API Key"})
                    from airadio.tts import TTSStreamer
                    try:
                        t = TTSStreamer(api_key=api_key, model=model, voice=voice)
                    except RuntimeError as e:
                        return _send(self, 422, {"error": str(e)})
                    data = t.synth([text])
                    if not data:
                        return _send(self, 502, {"error": t.last_error or "合成失败"})
                    return _send(self, 200, data, "audio/mpeg")

            # ---------- 开播 / 会话 ----------
            if p == "/api/start":
                user = _auth_required(self)
                if user is None:
                    return
                room = str(body.get("room", "")).strip() or DEFAULTS.get("room", "")
                if not room:
                    return _send(self, 422, {"error": "要填直播间号（live.douyin.com/ 后面的数字）"})
                title = str(body.get("title", "")).strip() or DEFAULTS.get("script_title", "本场稿")
                model = str(body.get("model", "")).strip() or DEFAULTS.get("model", DEFAULT_VOICE_MODEL)
                sess_lib = _lib_for(user)
                api_key = _bailian_key(sess_lib)
                skip_voice = bool(body.get("skip_voice"))
                # 默认音色方向（翔哥）：中年男性 55 岁、稳重沙哑；音色名自动生成
                keyword = str(body.get("voice_keyword", "")).strip() or DEFAULT_VOICE_KEYWORD
                prompt = str(body.get("voice_prompt", "")).strip() or VOICE_PRESETS.get(keyword, "")
                # 开播前就按提示词生成一套新稿（可选）
                gen_template = str(body.get("gen_template", "")).strip()
                gen_now = bool(body.get("generate_script")) or bool(gen_template)
                script_text = ""
                gen_note = ""
                if gen_now:
                    try:
                        gcfg = sess_lib.get_gen_cfg()
                        text, gmodel = gen.generate_script_llm(
                            gcfg, system_override=(gen_template or None),
                            api_key_fallback=api_key)
                        title = "AI稿 " + time.strftime("%m-%d %H:%M")
                        script_text = text
                        gen_note = f"开播生成新稿（{gmodel}，{len(text.splitlines())} 行）"
                    except Exception as e:
                        return _send(self, 502, {"error": f"生成新稿失败：{e}"})
                else:
                    script_text = str(body.get("text", "")).strip() or _default_script_text()
                voice = ""
                voice_note = ""
                runtime = sess_lib.get_runtime()
                picked_voice = str(body.get("voice_id", "")).strip()
                if picked_voice:
                    # 开播时直接选一个已生成的音色：模型必须跟着音色走（音色绑定生成它的模型）
                    owner = models.model_of_custom_voice(picked_voice)
                    model = owner or model
                    if not models.is_usable_voice(model, picked_voice):
                        return _send(self, 422, {"error": f"音色 {picked_voice} 不能用在 {model} 上：音色绑定生成它的模型"})
                    voice = picked_voice
                    model = models.model_of_custom_voice(voice) or model
                    sess_lib.save_runtime({"model": model, "voice": voice})
                    voice_note = f"选用已生成的音色：{voice}（{model}）"
                    skip_voice = True
                elif not skip_voice:
                    if not re.fullmatch(r"[a-z0-9]{1,9}", keyword or ""):
                        return _send(self, 422, {"error": "音色关键字只能用英文小写字母或数字，且少于 10 位"
                                                        "（例：xiangge01）。不想换音色就点「跳过，用现有音色」"})
                    if not api_key:
                        return _send(self, 422, {"error": "生成新音色需要百炼 API Key（.env 或本页填 Key），否则请点「跳过」"})
                    prompt = prompt or _voice_prompt_from_keyword(keyword)
                    # 自动生成音色名：关键字 + 时间戳短码，避免同一关键字反复撞同一个名字
                    prefix = keyword if body.get("voice_prefix_fixed") else _auto_voice_prefix(keyword)
                    inp = {"action": "create_voice", "target_model": model, "prefix": prefix,
                           "voice_prompt": prompt[:500],
                           "preview_text": PREVIEW_TEXT, "preferred_name": prefix}
                    resp = _bailian_enroll(api_key, inp)
                    if resp.status_code != 200:
                        return _send(self, 502, {"error": _enroll_error_hint(resp.text[:300])})
                    voice = resp.json().get("output", {}).get("voice_id", "")
                    if not voice:
                        return _send(self, 502, {"error": "百炼返回了成功但没有音色 ID（生成未完成，请重试或点「跳过」）"})
                    # 记进本机音色库：这样「音色」页能看到它、认得出它（用哪句提示词生成的）
                    sess_lib.add_voice_record(voice, model=model, label=prefix,
                                              prompt=prompt[:300], mode="design")
                    voice_note = f"本场新音色：{prefix} → {voice}（{model}）"
                else:
                    # 「跳过」= 沿用上次那套模型+音色。音色绑定生成它的模型，所以模型跟着音色走，
                    # 否则会出现"v3.5 的音色配 v2 的模型"这种合成必失败的组合（播出来还是旧音色）。
                    voice = runtime.get("voice") or ""
                    owner = models.model_of_custom_voice(voice) if voice else ""
                    if owner:
                        model = owner
                    elif runtime.get("model"):
                        model = runtime["model"]
                    if not voice:
                        voice = models.default_voice_for(model)
                    if not voice or not models.is_usable_voice(model, voice):
                        return _send(self, 422, {"error": f"没有可沿用的音色（{model} 没有合适的现成音色）："
                                                          "请到「音色」页生成一个，或勾选「生成新音色并开始」"})
                    voice_note = f"沿用已有音色：{voice}（{model}）"
                HUB.leave(user)      # 上一场彻底停掉，不留后台
                sess = HUB.create(user, room=room, script_text=script_text,
                                 schedule=ScheduleConfig(min_gap_s=DEFAULTS.get("min_gap", 45.0),
                                                        window_s=DEFAULTS.get("window", (25.0, 40.0))),
                                 model=model, voice=voice, api_key=api_key,
                                 sink=DEFAULTS.get("sink", "file"),
                                 default_params={"merge_sentences": DEFAULTS.get("merge"),
                                                 "sentence_gap_ms": DEFAULTS.get("gap"),
                                                 "prefetch_ahead": DEFAULTS.get("prefetch")})
                saved_cookie = sess.lib.get_douyin_cookie()
                if saved_cookie:
                    sess.orch.set_douyin_cookie(saved_cookie)
                sess.orch.script_title = title
                sess.orch.played_titles.append(title)
                sess.lib.save_runtime({"model": model, "voice": voice})
                if script_text.strip() and not gen_now:
                    sess.lib.add_script(title, script_text, source="开播")
                if gen_now:
                    # 开播生成的 AI 稿也只用一次：接播链建好之后再置位
                    sess.orch._script_is_one_shot = True
                    sess.orch.log_append("ai_gen: 开播稿是 AI 现生成的，只用一次，播完换新稿")
                if body.get("auto_generate") is not None:
                    sess.lib.save_gen_cfg({"auto_generate": bool(body.get("auto_generate"))})
                    sess.orch.set_ai_feed(enabled=bool(body.get("auto_generate")))
                sess.orch.log_append(voice_note or "voice: 沿用已有音色")
                if gen_note:
                    sess.orch.log_append(gen_note)
                return _send(self, 200, {"ok": True, "voice_id": voice, "voice_prefix": keyword,
                                         "model": model,
                                         "room": room, "skipped_voice": skip_voice,
                                         "generated_script": gen_now,
                                         "title": title,
                                         "sentences": sum(1 for s in sess.orch.segments
                                                          if s.kind in ("speech", "mark"))})
            if p == "/api/leave":
                user = _whoami(self)
                if not user:
                    return _send(self, 200, {"ok": False})
                return _send(self, 200, {"ok": HUB.leave(user)})

            # ---------- 需要一套正在运行的电台 ----------
            if p in ("/api/script", "/api/library/save", "/api/library/use", "/api/library/script_delete",
                    "/api/params", "/api/phrases", "/api/prefetch", "/api/room", "/api/control",
                    "/api/douyin_cookie",
                    "/api/api_key", "/api/clone_voice", "/api/preview_voice", "/api/tts_test"):
                user, sess, replied = _session_for(self)
                if replied:
                    return
                orch, lib = sess.orch, sess.lib

                if p == "/api/script":
                    n = orch.update_script(body.get("text", ""), apply_now=bool(body.get("apply_now")))
                    if body.get("apply_now"):
                        orch.script_title = str(body.get("title", "")).strip() or orch.script_title
                    return _send(self, 200, {"ok": True, "sentences": n,
                                             "applied": bool(body.get("apply_now"))})
                if p == "/api/library/save":
                    item = lib.add_script(body.get("title", ""), body.get("text", ""),
                                          body.get("source", "手动保存"))
                    return _send(self, 200, {"ok": True, "id": item["id"], "sentences": item["sentences"]})
                if p == "/api/library/script_delete":
                    return _send(self, 200, {"ok": lib.remove_script(body.get("id", ""))})
                if p == "/api/library/use":
                    item = lib.get_script(body.get("id", ""))
                    if not item:
                        return _send(self, 404, {"error": "稿子不存在"})
                    n = orch.update_script(item["text"], apply_now=bool(body.get("apply_now", True)))
                    idx = next((i for i, s in enumerate(lib.data["scripts"]) if s["id"] == item["id"]), None)
                    if idx is not None:
                        orch.set_rotate(orch.rotate_enabled, lib.rotate_source, idx=idx)
                    orch.script_title = item["title"]
                    return _send(self, 200, {"ok": True, "title": item["title"], "sentences": n,
                                             "applied": bool(body.get("apply_now", True))})
                if p == "/api/params":
                    pkw = body.get("per_kind_windows") or {}
                    clean = {k: ([v[0], v[1]] if isinstance(v, list) and len(v) == 2 else None)
                             for k, v in pkw.items()}
                    orch.update_params(
                        voice=body.get("voice"), model=body.get("model"), rate=body.get("rate"),
                        min_gap=body.get("min_gap"),
                        window=(body["window"][0], body["window"][1])
                        if isinstance(body.get("window"), list) and len(body["window"]) == 2 else None,
                        per_kind_windows=clean or None,
                        prefetch_ahead=body.get("prefetch_ahead"),
                        sentence_gap_ms=body.get("sentence_gap_ms"),
                        merge_sentences=body.get("merge_sentences"),
                        trim_silence=body.get("trim_silence"))
                    if body.get("model") or body.get("voice"):
                        lib.save_runtime({"model": orch.model, "voice": orch.voice})
                    return _send(self, 200, {"ok": True})
                if p == "/api/phrases":
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
                    orch.update_params(**patch_body)
                    saved = lib.save_phrases(patch_body)
                    return _send(self, 200, {"ok": True, "saved": saved})
                if p == "/api/prefetch":
                    n = body.get("limit")
                    limit = None if (n is None or n == "" or n == "all") else max(1, int(n))
                    return _send(self, 200, {"ok": True, "queued": orch.prefetch_script(limit)})
                if p == "/api/room":
                    orch.set_room(str(body.get("room", "")).strip())
                    return _send(self, 200, {"ok": True})
                if p == "/api/douyin_cookie":
                    cookie = str(body.get("cookie", "")).strip()
                    if cookie and len(cookie) < 40:
                        return _send(self, 422, {"error": "这段不像 Cookie：要粘贴整串（含 sessionid_ss=… ttwid=…）"})
                    ok = orch.set_douyin_cookie(cookie)
                    if cookie and not ok:
                        return _send(self, 422, {"error": "Cookie 里没有抖音登录字段（sessionid_ss / sessionid / sid_tt）："
                                                 "请先在浏览器登录抖音再复制 Cookie"})
                    lib.save_douyin_cookie(cookie)
                    from airadio.live import cookie_has_login
                    return _send(self, 200, {"ok": True, "cookie_set": bool(cookie),
                                             "has_login": cookie_has_login(cookie),
                                             "cleared": not cookie})
                if p == "/api/control":
                    action = body.get("action")
                    if action == "pause":
                        orch.pause()
                    elif action == "resume":
                        orch.resume()
                    elif action == "skip":
                        orch.skip_sentence()
                    elif action == "rewind":
                        orch.rewind()
                    elif action == "next_script":
                        if not orch._maybe_rotate():
                            return _send(self, 422, {"error": "轮换没开或稿子库里不足 2 套稿"})
                    else:
                        return _send(self, 400, {"error": "unknown action"})
                    return _send(self, 200, {"ok": True})
                if p == "/api/api_key":
                    key = str(body.get("key", "")).strip()
                    if not key.startswith("sk-") or len(key) < 20:
                        return _send(self, 422, {"error": "Key 应以 sk- 开头（百炼控制台 - API-Key 创建）"})
                    ok = orch.set_api_key(key)
                    if ok:
                        os.environ.setdefault("DASHSCOPE_API_KEY", key)
                    return _send(self, 200, {"ok": ok})
                if p == "/api/clone_voice":
                    url = str(body.get("url", "")).strip()
                    b64 = body.get("audio_base64")
                    prompt = str(body.get("prompt", "")).strip()
                    preview = str(body.get("preview_text", "")).strip()
                    prefix = str(body.get("prefix", "airadio")).strip()
                    if not re.fullmatch(r"[a-z0-9]{1,9}", prefix):
                        return _send(self, 422, {"error": "音色名要用英文小写字母或数字，且少于 10 位（例：xiangge01）"})
                    if not orch.has_api_key():
                        return _send(self, 422, {"error": "先填写百炼 API Key"})
                    target_model = str(body.get("target_model") or orch.model).strip()
                    api_key = orch.api_key or os.environ.get("DASHSCOPE_API_KEY")
                    inp = {"action": "create_voice", "target_model": target_model, "prefix": prefix}
                    if prompt and not b64 and not url:
                        if len(prompt) < 5:
                            return _send(self, 422, {"error": "音色描述太短，写清性别/年龄/语气/用途"})
                        inp["voice_prompt"] = prompt[:500]
                        inp["preview_text"] = (preview or PREVIEW_TEXT)[:60]
                        if len(inp["preview_text"]) < 15:
                            inp["preview_text"] = PREVIEW_TEXT
                        inp["preferred_name"] = prefix
                    else:
                        if b64:
                            audio_data_uri = _prep_reference_audio(b64, body.get("format"))
                            if isinstance(audio_data_uri, tuple):
                                return _send(self, *audio_data_uri)
                            inp["audio"] = audio_data_uri
                        elif url.startswith("http"):
                            inp["url"] = url
                        else:
                            return _send(self, 422, {"error": "要么录音/选文件（复刻），要么填音色描述（提示词生成）"})
                    resp = _bailian_enroll(api_key, inp)
                    if resp.status_code != 200:
                        return _send(self, 502, {"error": _enroll_error_hint(resp.text[:300])})
                    voice_id = resp.json().get("output", {}).get("voice_id", "")
                    if not voice_id:
                        # 200 但没有 voice_id：不能当成功返回，否则前端会选中一个空音色
                        return _send(self, 502, {"error": "百炼返回了成功但没有音色 ID（生成未完成，请重试）"})
                    how = "提示词生成" if "voice_prompt" in inp else "录音复刻"
                    orch.log_append(f"voice: {how}生成 {voice_id}（{target_model}）")
                    return _send(self, 200, {"ok": True, "voice_id": voice_id,
                                             "target_model": target_model,
                                             "mode": "design" if "voice_prompt" in inp else "clone"})
                if p == "/api/preview_voice":
                    text = (body.get("text") or "试听：这是新的音色。").strip()[:200]
                    voice = str(body.get("voice", "")).strip()
                    if not voice or not orch.has_api_key():
                        return _send(self, 422, {"error": "需要音色与 API Key"})
                    from airadio.tts import TTSStreamer
                    t = TTSStreamer(api_key=orch.api_key or os.environ.get("DASHSCOPE_API_KEY"),
                                    model=orch.model, voice=voice)
                    data = t.synth([text])
                    if not data:
                        return _send(self, 502, {"error": t.last_error or "合成失败"})
                    return _send(self, 200, data, "audio/mpeg")
                if p == "/api/tts_test":
                    text = (body.get("text") or "").strip()[:200]
                    if not text:
                        return _send(self, 400, {"error": "text required"})
                    if not orch.has_api_key():
                        return _send(self, 422, {"error": "先填写百炼 API Key"})
                    audio = orch._synth([text])
                    if not audio:
                        err = (orch.tts.last_error if orch.tts else (orch.tts_init_error or "未配置 TTS"))
                        return _send(self, 502, {"error": err})
                    return _send(self, 200, audio, "audio/mpeg")
        except ValueError as e:
            return _send(self, 422, {"error": str(e)})
        except BrokenPipeError:
            return None      # 客户端已断开（常见于 keepalive 的 /api/leave），不必再写响应
        except Exception as e:
            return _send(self, 500, {"error": f"{type(e).__name__}: {e}"})
        return _send(self, 404, {"error": "no route"})


def _voice_prompt_from_keyword(keyword: str) -> str:
    """用户只给一个关键字时，把它展开成百炼能用的音色描述（描述里不写示例台词）。"""
    hints = {
        "nv": "成年女性，25-35 岁，声音温柔亲切，语速适中，像邻家姐姐聊天",
        "woman": "成年女性，25-35 岁，声音温柔亲切，语速适中，像邻家姐姐聊天",
        "male": "成年男性，30-40 岁，声音沉稳清晰，语气亲和可信，语速中等",
        "nan": "成年男性，30-40 岁，声音沉稳清晰，语气亲和可信，语速中等",
        "warm": "成年人，声音温暖有亲和力，语气真诚不夸张，适合长时间陪伴式口播",
        "lively": "成年人，声音明亮有活力，语气热情有感染力，适合直播间留人互动",
        "calm": "成年人，声音平稳沉着，吐字清晰，语速偏慢，适合讲解与教学",
        "lengjing": "成年人，声音冷静干练，语速偏快但不含糊，适合快节奏促单",
    }
    key = (keyword or "").lower()
    base = next((v for k, v in hints.items() if k in key), "成年人，声音清晰自然，语气亲和，语速适中，适合直播间口播")
    return f"{base}。用途：抖音直播间 AI 电台整段口播，长时间听不累。关键字：{keyword}"


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true",
                    help="PC 单机模式：免登录、电台不因空闲停止（web/app.py 用的就是这条）")
    ap.add_argument("--open", action="store_true", help="启动后自动打开本机浏览器（配合 --local）")
    ap.add_argument("--room", default="", help="默认直播间号（页面上「开始新直播」时可改）")
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--idle", type=int, default=90, help="多少秒没有请求就判定页面已关、自动停止该用户的电台")
    ap.add_argument("--admin-password", default=os.environ.get("AIRADIO_ADMIN_PASSWORD", "e5232382"))
    ap.add_argument("--sink", default="file", choices=["file", "ffplay", "dry"])
    ap.add_argument("--model", default="cosyvoice-v2")
    ap.add_argument("--window", default="25,40")
    ap.add_argument("--min-gap", type=float, default=45.0)
    ap.add_argument("--merge", type=int, default=2)
    ap.add_argument("--gap", type=int, default=150)
    ap.add_argument("--prefetch", type=int, default=3)
    ap.add_argument("--douyin-cookie", default=os.environ.get("DOUYIN_COOKIE", ""),
                    help="浏览器复制的抖音登录 Cookie（含 sessionid_ss）；没有它抖音不下发礼物消息")
    args = ap.parse_args(argv)

    global ACCOUNTS, HUB, DEFAULTS, LOCAL_MODE
    LOCAL_MODE = bool(args.local)
    ACCOUNTS = Accounts(ROOT / "data" / "users.json", admin_password=args.admin_password)
    w0, w1 = (float(x) for x in args.window.split(","))
    os.environ["DOUYIN_COOKIE"] = args.douyin_cookie or os.environ.get("DOUYIN_COOKIE", "")
    DEFAULTS = {"room": args.room, "model": args.model, "window": (w0, w1), "min_gap": args.min_gap,
                "sink": args.sink, "merge": args.merge, "gap": args.gap, "prefetch": args.prefetch,
                "script_title": ""}
    text = _default_script_text()
    # 默认稿名字：能匹配 data/library.json 里管理员准备的稿就用它的标题
    legacy = ROOT / "data" / "library.json"
    if legacy.is_file():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            hit = next((s for s in (data.get("scripts") or [])
                        if (s.get("text") or "").strip() == text.strip()), None)
            if hit:
                DEFAULTS["script_title"] = hit.get("title", "")
        except Exception:
            pass
    DEFAULTS["script_title"] = DEFAULTS["script_title"] or "本场稿"

    HUB = SessionHub(ROOT / "data" / "users", ROOT / "out" / "chunks",
                     ROOT / "out" / "chunks" / "cache", idle_s=args.idle)
    HUB.wire_hook = _wire_generator      # 每套电台一建好就接上 AI 生成链
    if LOCAL_MODE:
        HUB.keep_alive_default = True    # 见 _wire_generator 里 sess.keep_alive
    HUB.start_reaper()

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    if LOCAL_MODE:
        print(f"AI电台（PC 单机版）已启动： {url}   ← 浏览器会自动打开")
        print("免登录；电台不会因页面空闲自动停止，只有点「停止连续播放/立即停止我这个电台」才停。")
        print("Ctrl+C 退出程序（同时停止电台）")
    else:
        print(f"控制台: http://0.0.0.0:{args.port}  （未登录会先进登录/注册页；管理后台 /admin）")
        print(f"空闲 {HUB.idle_s} 秒无请求 ⇒ 该用户电台自动停止（不挂后台）")
        print("Ctrl+C 停止")
    if args.open:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        HUB.stop_all()


if __name__ == "__main__":
    main()
