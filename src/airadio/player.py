"""输出侧：播放器（本机外放）与分块文件（浏览器播放）双通道。"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from .mp3len import duration_ms


class ChunkWriter:
    """把每次要播的话术落成 out/chunks/NNN.mp3 + manifest.json（浏览器/外部消费用）。

    manifest 记录每块的真实时长与句间停顿：浏览器据此无缝接播（不在块之间留空档）。
    """

    def __init__(self, out_dir: str | Path = "out/chunks"):
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest: list[dict] = []
        self._lock = threading.Lock()
        self._pending_gap_ms = 0   # 下一块之前的停顿（由 [[pause:ms]] 或句间停顿参数设置）

    def write(self, kind: str, text: str, audio: bytes, gap_before_ms: int = 0) -> Path:
        with self._lock:
            idx = len(self.manifest)
            p = self.dir / f"{idx:04d}.mp3"
            p.write_bytes(audio)
            self.manifest.append({
                "i": idx, "kind": kind, "text": text,
                "file": p.name, "t": time.time(),
                "duration_ms": int(duration_ms(audio)),
                "gap_before_ms": int(gap_before_ms + self._pending_gap_ms),
            })
            self._pending_gap_ms = 0
            (self.dir / "manifest.json").write_text(
                json.dumps(self.manifest, ensure_ascii=False), encoding="utf-8")
            return p

    def add_gap(self, ms: float) -> None:
        """要求下一块之前先停顿 ms 毫秒（[[pause:…]] 用）。"""
        with self._lock:
            self._pending_gap_ms = max(self._pending_gap_ms, int(max(0, ms)))


class FFplaySink:
    """把 mp3 字节流写进 ffplay 子进程 stdin —— 本机扬声器外放。"""

    def __init__(self):
        self._p = subprocess.Popen(
            ["ffplay", "-autoexit", "-nodisp", "-loglevel", "error", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._lock = threading.Lock()

    @property
    def alive(self) -> bool:
        return self._p.poll() is None

    def write(self, audio: bytes):
        with self._lock:
            if not self.alive:
                return
            try:
                self._p.stdin.write(audio)
                self._p.stdin.flush()
            except (BrokenPipeError, ValueError):
                pass

    def close(self):
        try:
            with self._lock:
                if self._p.stdin:
                    self._p.stdin.close()
                self._p.wait(timeout=3)
        except Exception:
            self._p.kill()
