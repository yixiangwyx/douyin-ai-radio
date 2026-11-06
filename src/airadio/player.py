"""输出侧：播放器（本机外放）与分块文件（浏览器播放）双通道。

关键设计（服务端变薄、客户端只下载一次）：
- 音频按**内容哈希**落文件（out/chunks/audio/<hash>.mp3）。同一段话循环到第 N 遍、
  或进程重启后再播到，都复用同一个文件 —— 不再把相同字节重复写 N 次。
- manifest 里每块带 `h`（哈希）与 `url`（可永久缓存的地址）。文件名带哈希 ⇒ 内容永不变化，
  服务端据此发 `Cache-Control: immutable`，浏览器第二轮起直接命中本地缓存，网络下行≈0。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path

from .mp3len import duration_ms


def audio_hash(audio: bytes) -> str:
    return hashlib.sha1(audio).hexdigest()[:20]


class ChunkWriter:
    """把每次要播的话术落成可永久缓存的音频文件 + manifest.json（浏览器/外部消费用）。

    manifest 记录每块的真实时长与句间停顿：浏览器据此无缝接播（不在块之间留空档）。
    """

    def __init__(self, out_dir: str | Path = "out/chunks"):
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir = self.dir / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.manifest: list[dict] = []
        self._lock = threading.Lock()
        self._pending_gap_ms = 0   # 下一块之前的停顿（由 [[pause:ms]] 或句间停顿参数设置）
        # 复用统计：证明"循环不重复写盘、客户端不必重复下载"
        self.bytes_written = 0
        self.bytes_reused = 0
        self.blocks_written = 0
        self.blocks_reused = 0

    def write(self, kind: str, text: str, audio: bytes, gap_before_ms: int = 0) -> Path | None:
        with self._lock:
            idx = len(self.manifest)
            h = audio_hash(audio) if audio else ""
            p = None
            if audio:
                p = self.audio_dir / f"{h}.mp3"
                if p.exists():
                    # 内容完全相同（循环重播 / 重启后再播）：不重复写盘，也不该重复下载
                    self.bytes_reused += len(audio)
                    self.blocks_reused += 1
                else:
                    p.write_bytes(audio)
                    self.bytes_written += len(audio)
                    self.blocks_written += 1
            self.manifest.append({
                "i": idx, "kind": kind, "text": text,
                "h": h, "url": f"/chunks/audio/{h}.mp3" if h else "",
                "file": f"audio/{h}.mp3" if h else "",   # 兼容旧字段名
                "t": time.time(),
                "duration_ms": int(duration_ms(audio)),
                "gap_before_ms": int(gap_before_ms + self._pending_gap_ms),
            })
            self._pending_gap_ms = 0
            (self.dir / "manifest.json").write_text(
                json.dumps(self.manifest, ensure_ascii=False), encoding="utf-8")
            return p

    def reuse_stats(self) -> dict:
        with self._lock:
            return {"bytes_written": self.bytes_written, "bytes_reused": self.bytes_reused,
                    "blocks_written": self.blocks_written, "blocks_reused": self.blocks_reused,
                    "unique_blocks": self.blocks_written,
                    "disk_bytes": sum(f.stat().st_size for f in self.audio_dir.glob("*.mp3"))}

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
