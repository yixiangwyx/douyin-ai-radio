"""编排器：读稿 → 逐句 TTS → 句边界插播互动 → 外放/落块。支持运行中控制（暂停/换稿/调参/换房）。"""
from __future__ import annotations

import threading
import time

from .events import InteractionQueue
from .live import LiveCollector
from .player import ChunkWriter, FFplaySink
from .timeline import Scheduler, ScheduleConfig, parse_script
from .tts import TTSStreamer


class Orchestrator:
    def __init__(self, web_rid: str, script_text: str, *,
                 schedule: ScheduleConfig | None = None,
                 voice: str = "longxiaochun_v2", model: str = "cosyvoice-v2",
                 api_key: str | None = None, speech_rate: float = 1.0,
                 out_dir: str = "out/chunks", sink: str = "file"):
        self.sink_name = sink
        self.queue = InteractionQueue()
        self.collector = LiveCollector(web_rid, self.queue, on_status=self._on_status)
        self.script_text = script_text
        self.segments = parse_script(script_text)
        self.scheduler = Scheduler(schedule or ScheduleConfig(), self.queue)
        self.chunk_writer = ChunkWriter(out_dir)
        self.voice, self.model, self.speech_rate = voice, model, speech_rate
        self.api_key = api_key
        self.tts = None
        if sink != "dry":
            self.tts = TTSStreamer(api_key=api_key, model=model, voice=voice,
                                   speech_rate=speech_rate, on_audio=self._on_audio)
        self.ffplay: FFplaySink | None = None
        self.status = "init"
        self.log: list[str] = []
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._restart = False
        self._seg_i = 0            # 稿内播放进度（暂停后从这里继续）
        self._pending_script: str | None = None
        self._lock = threading.Lock()

    # ---------- 控制 API（供 web 控制台 / CLI 调用） ----------

    def pause(self):
        self._pause.set()

    def resume(self):
        self._pause.clear()

    def skip_sentence(self):
        """跳过当前正在处理/即将播放的一句。"""
        self._seg_i = min(self._seg_i + 1, len(self.segments))
        self._restart = False
        self.log_append("control: 跳过当前句")

    def rewind(self):
        """整稿从头重播。"""
        self._seg_i = 0
        self.log_append("control: 从头重播")

    def stop(self):
        self._stop.set()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def get_script(self) -> str:
        return self.script_text

    def update_script(self, text: str, apply_now: bool = False) -> int:
        """校验并暂存新稿；默认下一轮循环开始时生效，apply_now=True 则立即从头应用。返回句数。"""
        segs = parse_script(text)
        if not any(s.kind == "speech" for s in segs):
            raise ValueError("稿子没有可播内容")
        with self._lock:
            self._pending_script = text
        if apply_now:
            self._restart = True
        return sum(1 for s in segs if s.kind in ("speech", "mark"))

    def apply_pending_script(self) -> bool:
        with self._lock:
            if self._pending_script is None:
                return False
            self.script_text = self._pending_script
            self.segments = parse_script(self.script_text)
            self._pending_script = None
        self._seg_i = 0
        self.log_append("script: 新稿已生效")
        return True

    def update_params(self, *, voice: str | None = None, model: str | None = None,
                      rate: float | None = None, min_gap: float | None = None,
                      window: tuple | list | None = None) -> None:
        cfg = self.scheduler.cfg
        if min_gap is not None:
            cfg.min_gap_s = max(0.0, float(min_gap))
        if window is not None:
            w0, w1 = (float(window[0]), float(window[1]))
            cfg.window_s = (max(0.0, w0), max(max(0.0, w0) + 1.0, w1))
        rebuild = False
        if voice:
            self.voice = voice.strip()
            rebuild = True
        if model:
            self.model = model.strip()
            rebuild = True
        if rate is not None:
            self.speech_rate = min(2.0, max(0.5, float(rate)))
            rebuild = True
        if rebuild and self.sink_name != "dry":
            self.tts = TTSStreamer(api_key=self.api_key, model=self.model,
                                   voice=self.voice, speech_rate=self.speech_rate,
                                   on_audio=self._on_audio)
            self.log_append(f"params: model={self.model} voice={self.voice} rate={self.speech_rate}")
        self.log_append(f"params: min_gap={cfg.min_gap_s}s window={cfg.window_s}")

    def set_room(self, web_rid: str) -> None:
        """切换直播间（线程安全重启采集器）。"""
        self.collector.stop()
        time.sleep(0.2)
        self.queue = InteractionQueue()
        self.scheduler.q = self.queue
        self.collector = LiveCollector(web_rid, self.queue, on_status=self._on_status)
        self.collector.start()
        self.log_append(f"room: 切换到 {web_rid}")

    def snapshot(self, events_n: int = 40, log_n: int = 40) -> dict:
        room = self.collector.room
        cfg = self.scheduler.cfg
        return {
            "status": self.status,
            "room": {
                "web_rid": self.collector.web_rid,
                "room_id": room.room_id if room else "",
                "title": room.title if room else "",
                "live": bool(room and room.status == 2),
            },
            "paused": self.paused,
            "stats": dict(self.queue.stats),
            "inserts": self.scheduler.inserted_count,
            "segments": sum(1 for s in self.segments if s.kind in ("speech", "mark")),
            "chunks": len(self.chunk_writer.manifest),
            "events": self.queue.recent(events_n),
            "log": self.log[-log_n:],
            "params": {
                "voice": self.voice, "model": self.model, "rate": self.speech_rate,
                "min_gap": cfg.min_gap_s, "window": list(cfg.window_s),
            },
            "tts_error": self.tts.last_error if self.tts else ("(dry 模式，未配置 API Key)" if self.sink_name == "dry" else ""),
        }

    def log_append(self, line: str):
        self.log.append(f"[{time.strftime('%H:%M:%S')}] {line}")
        if len(self.log) > 600:
            self.log = self.log[-600:]

    # ---------- 内部 ----------

    def _on_status(self, s: str):
        self.status = s
        self.log_append(f"status: {s}")

    def _on_audio(self, chunk: bytes):
        if self.ffplay:
            self.ffplay.write(chunk)

    def run(self, max_loops: int = 0, loop_pause_s: float = 2.0):
        if self.sink_name == "ffplay":
            self.ffplay = FFplaySink()
        self.collector.start()
        loop = 0
        try:
            while not self._stop.is_set():
                if self._pause.is_set():
                    time.sleep(0.5)
                    continue
                if "connected" not in self.status and not self.queue.stats:
                    # 未连接成功：等待采集器（房间可能未开播，采集线程会重试）
                    time.sleep(min(5.0, loop_pause_s))
                    if max_loops and loop > max_loops * 4:
                        break
                    continue
                self._play_pass()
                if self._restart:
                    self._restart = False
                    self.apply_pending_script()
                    continue
                if self._pending_script is not None:  # 下一轮开头生效
                    self.apply_pending_script()
                if max_loops:
                    loop += 1
                    if loop >= max_loops:
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
        i = self._seg_i
        while i < len(self.segments):
            if self._stop.is_set():
                return
            if self._restart or self._pause.is_set():
                self._seg_i = i
                return  # 回到 run() 处理换稿/暂停；resume 时从 _seg_i 继续
            seg = self.segments[i]
            i += 1
            self._seg_i = i
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
            self.log_append(f"play[{kind}]: {' '.join(texts)}")
            est = self._estimate_elapsed_s(texts)
            time.sleep(min(8.0, est))  # 按播报时长节拍推进
            # 句边界：能不能插播？（看下一个边界是否受保护）
            nxt_protected = self._next_boundary_protected(i)
            insert_text = self.scheduler.on_boundary(time.time(), nxt_protected)
            if insert_text:
                ins_audio = self._synth([insert_text])
                self.chunk_writer.write("interrupt", insert_text, ins_audio or b"")
                self.log_append(f"INSERT: {insert_text}")

    def _next_boundary_protected(self, i: int) -> bool:
        for j in range(i, len(self.segments)):
            s = self.segments[j]
            if s.kind == "pause":
                continue
            return s.protected
        return True  # 稿尾后没有边界

    @staticmethod
    def _estimate_elapsed_s(texts: list[str]) -> float:
        # 中文播报约 4.5 字/秒 + 0.6s 缓冲
        return sum(len(t) for t in texts) / 4.5 + 0.6
