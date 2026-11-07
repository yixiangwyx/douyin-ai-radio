"""sessions.py — 多用户会话：一个用户一套电台状态，页面关了就回收。

为什么需要它：原来整个进程只有一个 Orchestrator + 一个全局 STATE，两台设备打开同一个地址
就是同一个电台（互相能改对方的稿、互相打断对方的播放）。这里把"一份电台"变成"每用户一份"：

- 每个登录用户 → 自己的 Orchestrator（自己的房间、稿子、话术、音色、播放进度、日志、chunk manifest）
- 每个登录用户 → 自己的稿子库/话术配置（data/users/<user>/library.json）与音频目录（out/chunks/<user>/）
- 音频缓存仍共享（out/chunks/cache）：同一稿同音色，A 生成过 B 就不用再花钱
- **空闲自动停止**：每次 API 请求算一次心跳；超过 idle_s 没有任何请求（关页面、锁屏、忘记关页面）
  就把这个电台彻底停掉（停播放线程、停抖音采集），不留在后台继续跑、不占内存、不再请求百炼。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from .library import Library
from .orchestrator import Orchestrator
from .timeline import ScheduleConfig

DEFAULT_IDLE_S = 90          # 90 秒没有任何请求 = 认定页面已关/用户已走


class Session:
    def __init__(self, username: str, orch: Orchestrator, lib: Library, out_dir: Path):
        self.username = username
        self.orch = orch
        self.lib = lib
        self.out_dir = out_dir
        self.created = time.time()
        self.last_seen = time.time()
        self.thread: threading.Thread | None = None
        self.busy = False
        self.keep_alive = False      # PC 单机模式：不因空闲被回收（关了页面电台也不停）

    def touch(self):
        self.last_seen = time.time()

    def idle_s(self) -> float:
        return time.time() - self.last_seen

    def stop(self):
        try:
            self.orch.stop()
        except Exception:
            pass


class SessionHub:
    """按用户名管理会话；后台线程负责回收空闲会话（"前端关闭就停止"）。"""

    def __init__(self, users_dir: str | Path, chunks_dir: str | Path,
                 audio_cache_dir: str | Path, idle_s: int = DEFAULT_IDLE_S):
        self.users_dir = Path(users_dir)
        self.chunks_dir = Path(chunks_dir)
        self.audio_cache_dir = Path(audio_cache_dir)
        self.idle_s = max(5, int(idle_s))
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.events: list[str] = []
        self._reaper: threading.Thread | None = None

    # ---------- 生命周期 ----------
    def start_reaper(self):
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
        self._reaper.start()

    def _reap_loop(self):
        while not self._stop.is_set():
            self._stop.wait(min(5.0, max(1.0, self.idle_s / 2)))
            self.reap()

    def reap(self, force_idle_s: int | None = None) -> list[str]:
        """把超过 idle_s 没有心跳的会话彻底停掉并删除（"前端关了就不再挂后台"）。

        返回被回收的用户名。用户主动暂停的会话不按空闲回收（那是明确要保留，且它不请求百炼）。
        """
        limit = force_idle_s or self.idle_s
        gone: list[Session] = []
        with self._lock:
            for name, s in list(self._sessions.items()):
                if s.orch.paused or s.keep_alive:
                    continue
                if s.idle_s() > limit:
                    gone.append(s)
                    self._sessions.pop(name, None)
        for s in gone:      # stop 放在锁外：播放线程持有 orch 自己的锁，别互相等
            s.stop()
            self._events(f"session: {s.username} 无心跳 {int(s.idle_s())}s，电台已停止（不再占后台）")
        return [s.username for s in gone]

    def _events(self, line: str):
        stamp = time.strftime("%H:%M:%S")
        self.events.append(f"[{stamp}] {line}")
        self.events = self.events[-200:]
        print(line, flush=True)

    def get(self, username: str) -> Session | None:
        with self._lock:
            s = self._sessions.get(username)
        if s:
            s.touch()
        return s

    def create(self, username: str, *, room: str, script_text: str, schedule: ScheduleConfig,
               model: str, voice: str, api_key: str | None, sink: str,
               default_params: dict | None = None) -> Session:
        """为该用户建一套电台（已有则复用，不换房）。"""
        with self._lock:
            existing = self._sessions.get(username)
            if existing:
                existing.touch()
                return existing
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in username)
        user_dir = self.users_dir / safe
        out_dir = self.chunks_dir / safe
        user_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        lib = Library(user_dir / "library.json")
        orch = Orchestrator(room, script_text, schedule=schedule, model=model, voice=voice,
                           api_key=api_key, out_dir=str(out_dir), sink=sink)
        # 音频生成缓存共享：同稿同音色跨用户不再重复请求百炼
        orch.shared_cache_dir = str(self.audio_cache_dir)
        sess = Session(username, orch, lib, out_dir)
        saved = lib.get_phrases()
        if saved or default_params:
            orch.update_params(**({k: v for k, v in (saved or {}).items() if v is not None}))
            if default_params:
                orch.update_params(**{k: v for k, v in default_params.items() if v is not None})
        # AI 生成新稿接播（讲到一半自动生成、播完自动接；控制台注入回调）
        if getattr(self, "wire_hook", None):
            self.wire_hook(sess)
        sess.thread = threading.Thread(target=orch.run, daemon=True, name=f"radio-{username}")
        sess.thread.start()
        with self._lock:
            self._sessions[username] = sess
        self._events(f"session: {username} 开播（房间 {room}）")
        return sess

    def leave(self, username: str) -> bool:
        """用户明确离开（页面关闭/点退出）：立即停掉这套电台。"""
        with self._lock:
            s = self._sessions.pop(username, None)
        if not s:
            return False
        s.stop()
        self._events(f"session: {username} 已停止（页面关闭/手动退出）")
        return True

    def stop_all(self):
        with self._lock:
            items = list(self._sessions.values())
            self._sessions.clear()
        for s in items:
            s.stop()

    def online(self) -> list[dict]:
        with self._lock:
            out = []
            for name, s in sorted(self._sessions.items()):
                out.append({"username": name, "idle_s": round(s.idle_s()),
                            "room": s.orch.collector.web_rid,
                            "script_title": s.orch.script_title,
                            "model": s.orch.model, "voice": s.orch.voice,
                            "tts_calls": s.orch.tts_calls, "cache_hits": s.orch.cache_hits,
                            "chunks": len(s.orch.chunk_writer.manifest),
                            "status": s.orch.status, "paused": s.orch.paused})
            return out

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)
