"""编排器：读稿 → 逐句 TTS → 句边界插播互动 → 外放/落块。"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .events import InteractionQueue
from .live import LiveCollector
from .player import ChunkWriter, FFplaySink
from .timeline import Scheduler, ScheduleConfig, parse_script
from .tts import TTSStreamer


@dataclass
class PlayItem:
    kind: str  # speech | interrupt | mark | pause
    text: str


class Orchestrator:
    def __init__(self, web_rid: str, script_text: str, *,
                 schedule: ScheduleConfig | None = None,
                 voice: str = "longxiaochun_v2", model: str = "cosyvoice-v2",
                 api_key: str | None = None, speech_rate: float = 1.0,
                 out_dir: str = "out/chunks", sink: str = "file"):
        self.queue = InteractionQueue()
        self.collector = LiveCollector(web_rid, self.queue, on_status=self._on_status)
        self.segments = parse_script(script_text)
        self.scheduler = Scheduler(schedule or ScheduleConfig(), self.queue)
        self.chunk_writer = ChunkWriter(out_dir)
        self.sink_name = sink
        self.tts = None
        if sink != "dry":
            self.tts = TTSStreamer(api_key=api_key, model=model, voice=voice,
                                   speech_rate=speech_rate, on_audio=self._on_audio)
        self.ffplay: FFplaySink | None = None
        self.status = "init"
        self.log: list[str] = []
        self._pending_audio_parts: list[bytes] = []

    def _on_status(self, s: str):
        self.status = s
        self.log.append(f"[{time.strftime('%H:%M:%S')}] status: {s}")

    def _on_audio(self, chunk: bytes):
        if self.ffplay:
            self.ffplay.write(chunk)
        else:
            self._pending_audio_parts.append(chunk)

    def run(self, max_loops: int = 0, loop_pause_s: float = 2.0):
        if self.sink_name == "ffplay":
            self.ffplay = FFplaySink()
        self.collector.start()
        loop = 0
        try:
            while True:
                loop += 1
                if "connected" not in self.status and not self.queue.stats:
                    # 未连接成功：等待采集器（房间可能未开播，采集线程会重试）
                    time.sleep(min(5.0, loop_pause_s))
                    if max_loops and loop > max_loops * 4:
                        break
                    continue
                self._play_pass()
                if max_loops and loop >= max_loops:
                    break
                time.sleep(loop_pause_s)
        finally:
            self.collector.stop()
            if self.ffplay:
                self.ffplay.close()

    def _synth(self, texts: list[str]) -> bytes:
        if self.tts is None:  # dry 模式：不合成，只走调度
            return b""
        return self.tts.synth(texts)

    def _play_pass(self):
        for i, seg in enumerate(self.segments):
            if seg.kind == "pause":
                time.sleep(seg.duration_ms / 1000.0)
                continue
            texts = [seg.text]
            kind = seg.kind
            if seg.kind == "mark":
                latest = self.queue.items[-1] if self.queue.items else None
                text = self.scheduler.render_mark(seg.text, latest)
                texts = [text]
                kind = "mark"
            audio = self._synth(texts)
            self.chunk_writer.write(kind, " ".join(texts), audio or b"")
            self.log.append(f"[{time.strftime('%H:%M:%S')}] play[{kind}]: {' '.join(texts)}")
            est = self._estimate_elapsed_s(texts)
            time.sleep(min(8.0, est))  # 按播报时长节拍推进
            # 句边界：能不能插播？（看下一个边界是否受保护）
            nxt_protected = self._next_boundary_protected(i)
            insert_text = self.scheduler.on_boundary(time.time(), nxt_protected)
            if insert_text:
                ins_audio = self._synth([insert_text])
                self.chunk_writer.write("interrupt", insert_text, ins_audio or b"")
                self.log.append(f"[{time.strftime('%H:%M:%S')}] INSERT: {insert_text}")

    def _next_boundary_protected(self, i: int) -> bool:
        for j in range(i + 1, len(self.segments)):
            s = self.segments[j]
            if s.kind == "pause":
                continue
            return s.protected
        return True  # 稿尾后没有边界

    @staticmethod
    def _estimate_elapsed_s(texts: list[str]) -> float:
        # 中文播报约 4.5 字/秒 + 0.6s 缓冲
        return sum(len(t) for t in texts) / 4.5 + 0.6

    @staticmethod
    def _estimate_gap_s(i: int) -> float:
        return 0.5
