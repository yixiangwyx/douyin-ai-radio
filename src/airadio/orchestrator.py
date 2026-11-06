"""编排器：读稿 → 逐句 TTS → 句边界插播互动 → 外放/落块。支持运行中控制（暂停/换稿/调参/换房）。"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from .events import InteractionQueue
from .live import LiveCollector
from .player import ChunkWriter, FFplaySink
from .mp3len import duration_ms
from .silence import trim_edges
from .timeline import INTERACTION_KINDS, Scheduler, ScheduleConfig, parse_script
from .models import is_usable_voice, model_info, model_names
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
        self.out_dir = out_dir        # 生成过的音频落 out_dir/cache，重启进程后仍可复用
        self.voice, self.model, self.speech_rate = voice, model, speech_rate
        self.api_key = api_key
        self.tts = None
        self.tts_init_error = ""
        if sink != "dry":
            try:
                self.tts = TTSStreamer(api_key=api_key, model=model, voice=voice,
                                       speech_rate=speech_rate, on_audio=self._on_audio)
            except RuntimeError as e:
                # 没填 Key 也能先启动（采集/调度/页面照常可用），Key 在页面填好后即时启用
                self.tts_init_error = str(e)

        self.ffplay: FFplaySink | None = None
        self.status = "init"
        self.log: list[str] = []
        self._log_total = 0      # 已产生过的日志条数（单调游标，供增量拉取）
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._restart = False
        self._seg_i = 0            # 稿内播放进度（暂停后从这里继续）
        self._pending_script: str | None = None
        self._lock = threading.Lock()
        # TTS 缓存：稿子未变的句子/固定话术只合成一次，循环复用；新互动才重新合成。
        # key 含 model+voice+rate：换音色/语速后旧缓存自动失效、按需重新合成。
        self._tts_cache: dict[str, bytes] = {}
        self.tts_calls = 0      # 真实调用百炼次数
        self.cache_hits = 0     # 缓存命中（复用音频）次数
        self._tts_lock = threading.Lock()
        self.prefetch_ahead = int(os.environ.get("AIRADIO_PREFETCH", "3"))  # 提前预生成的句数
        # 句间停顿（毫秒）：0 = 一句接一句不换气。节拍一律用真实音频时长，不按字数估算。
        self.sentence_gap_ms = int(os.environ.get("AIRADIO_SENTENCE_GAP", "150"))
        # 连续正文句合并成一次 TTS 任务（组内不再有静音/停顿，语气连贯）；1=不合并
        self.merge_sentences = max(1, min(6, int(os.environ.get("AIRADIO_MERGE", "2"))))
        # 裁掉每块首尾静音（需要 ffmpeg）
        self.trim_silence = os.environ.get("AIRADIO_TRIM", "1") != "0"
        self.trim_removed_ms = 0
        self._prefetch_thread: threading.Thread | None = None
        self.prefetch_pending = 0
        self.prefetch_done = 0
        # 多稿轮换：一轮播完自动切到稿子库的下一套稿（script_source 由控制台注入）
        self.script_source = None            # callable -> [{"title","text"}, ...]
        self.rotate_enabled = False
        self._rotate_idx = 0
        self.script_title = ""
        # 每套稿最多播这么多秒后，在下一段边界切下一套（0 = 必须整稿播完才切）
        self.rotate_after_s = 0.0
        # 按稿子行切换：这套稿播完 N 行就切下一套（0 = 不按行切）
        self.rotate_after_lines = 0
        self._script_started_at = 0.0
        self._lines_played = 0
        self._last_line_no = None
        self._rotate_due = False
        self.cache_size_peak = 0
        self.disk_cache_hits = 0      # 命中上一次进程（重启前）生成的音频
        self.disk_cache_writes = 0

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
        self._script_started_at = time.time()
        self._lines_played = 0
        self._last_line_no = None
        self._rotate_due = False
        self.log_append("script: 新稿已生效")
        return True

    def _maybe_rotate(self) -> bool:
        """轮换开着头就切到稿子库的下一套稿；返回是否切了。"""
        if not self.rotate_enabled or not self.script_source:
            return False
        try:
            items = [s for s in (self.script_source() or []) if (s.get("text") or "").strip()]
        except Exception:
            return False
        if len(items) < 2:
            return False
        # 从"当前正在播的那一套"往后切，避免切回同一套（手动切稿后索引可能不同步）
        cur = next((i for i, s in enumerate(items) if s.get("title") == self.script_title), None)
        base = cur if cur is not None else self._rotate_idx
        self._rotate_idx = (base + 1) % len(items)
        item = items[self._rotate_idx]
        if item.get("title") == self.script_title:
            return False
        try:
            segs = parse_script(item["text"])
        except Exception:
            return False
        if not any(s.kind == "speech" for s in segs):
            return False
        with self._lock:
            self.script_text = item["text"]
            self.segments = segs
            self.script_title = item.get("title", "")
        self._seg_i = 0
        self._script_started_at = time.time()
        self._lines_played = 0
        self._last_line_no = None
        self._rotate_due = False
        self.log_append(f"rotate: 已切到下一套稿「{self.script_title}」")
        self._prefetch_ahead(0)
        return True

    def set_rotate(self, enabled: bool, source=None, idx: int | None = None,
                   after_s: float | None = None, after_lines: int | None = None) -> None:
        changed = self.rotate_enabled != bool(enabled)
        self.rotate_enabled = bool(enabled)
        if source is not None:
            self.script_source = source
        if idx is not None:
            self._rotate_idx = int(idx)
        if after_s is not None:
            self.rotate_after_s = max(0.0, min(3600.0, float(after_s)))
        if after_lines is not None:
            self.rotate_after_lines = max(0, min(500, int(after_lines)))
        if changed:
            if self.rotate_after_lines:
                how = f"（每 {self.rotate_after_lines} 行切一次）"
            elif self.rotate_after_s:
                how = f"（每套最多 {self.rotate_after_s/60:.0f} 分钟）"
            else:
                how = "（整稿播完才切）"
            self.log_append(f"rotate: 多稿轮换{'开' if self.rotate_enabled else '关'}{how}")

    def update_params(self, *, voice: str | None = None, model: str | None = None,
                      rate: float | None = None, min_gap: float | None = None,
                      window: tuple | list | None = None,
                      per_kind_windows: dict | None = None,
                      prefetch_ahead: int | None = None,
                      sentence_gap_ms: int | None = None,
                      merge_sentences: int | None = None,
                      trim_silence: bool | None = None,
                      templates: dict | None = None,
                      nick_han_only: bool | None = None,
                      nick_max_chars: int | None = None,
                      announce_time: bool | None = None,
                      announce_interval_s: tuple | list | None = None,
                      time_templates: list | None = None,
                      fallback_templates: list | None = None) -> None:
        """改模型/音色前先校验：v3.5 系列没有系统音色（填 longanyang 会被百炼拒 418），
        而且音色绑定创建时的 target_model，跨模型用不了。校验不过就抛 ValueError。"""
        cfg = self.scheduler.cfg
        new_model = (model or self.model).strip()
        new_voice = (voice or self.voice).strip()
        if model or voice:
            info = model_info(new_model)
            if info is None:
                raise ValueError(f"未知模型 {new_model}（可选：{', '.join(model_names())}）")
            if not is_usable_voice(new_model, new_voice):
                raise ValueError(
                    f"音色 {new_voice} 不能用在 {new_model} 上：音色是绑定生成时的模型的；"
                    f"{'该模型没有系统音色，请先用提示词设计或录音复刻生成音色再切过去' if info['needs_custom_voice'] else '请选该模型的系统音色'}")
        if templates:
            saved = {}
            for kind, lines in templates.items():
                if kind not in cfg.templates:
                    continue
                clean = [str(x).strip() for x in (lines or []) if str(x).strip()]
                if clean:
                    saved[kind] = clean
            if saved:
                cfg.templates.update(saved)
                self.log_append("话术: 已更新 " + "、".join(
                    f"{k}({len(v)}条)" for k, v in saved.items()))
        if nick_han_only is not None:
            cfg.nick_han_only = bool(nick_han_only)
            self.log_append(f"params: 昵称只读汉字={cfg.nick_han_only}")
        if nick_max_chars is not None:
            cfg.nick_max_chars = max(1, min(20, int(nick_max_chars)))
            self.log_append(f"params: 昵称最多读 {cfg.nick_max_chars} 个字")
        if announce_time is not None:
            cfg.announce_time = bool(announce_time)
            self.log_append(f"params: 随机报时={'开' if cfg.announce_time else '关'}")
        if announce_interval_s is not None:
            lo, hi = float(announce_interval_s[0]), float(announce_interval_s[1])
            cfg.announce_interval_s = (max(30.0, lo), max(lo + 30.0, hi))
        if time_templates:
            clean = [str(x).strip() for x in time_templates if str(x).strip()]
            if clean:
                cfg.time_templates = clean
                self.log_append(f"报时: 已更新话术 {len(clean)} 条")
        if fallback_templates is not None:
            clean = [str(x).strip() for x in fallback_templates if str(x).strip()]
            cfg.fallback_templates = clean or cfg.fallback_templates
            self.log_append(f"params: 无互动兜底话术 {len(cfg.fallback_templates)} 条")
        if sentence_gap_ms is not None:
            self.sentence_gap_ms = max(0, min(5000, int(sentence_gap_ms)))
            self.log_append(f"params: 句间停顿={self.sentence_gap_ms}ms")
        if merge_sentences is not None:
            self.merge_sentences = max(1, min(6, int(merge_sentences)))
            self.log_append(f"params: 每次合并句数={self.merge_sentences}")
        if trim_silence is not None:
            self.trim_silence = bool(trim_silence)
            self.log_append(f"params: 裁首尾静音={self.trim_silence}")
        if prefetch_ahead is not None:
            self.prefetch_ahead = max(0, min(20, int(prefetch_ahead)))
            self.log_append(f"params: 自动预生成句数={self.prefetch_ahead}")
        if min_gap is not None:
            cfg.min_gap_s = max(0.0, float(min_gap))
        if window is not None:
            w0, w1 = (float(window[0]), float(window[1]))
            cfg.window_s = (max(0.0, w0), max(max(0.0, w0) + 1.0, w1))
        if per_kind_windows:
            for kind, w in per_kind_windows.items():
                if kind not in INTERACTION_KINDS:
                    continue
                if w is None:
                    cfg.per_kind_windows.pop(kind, None)
                    continue
                lo, hi = float(w[0]), float(w[1])
                lo = max(0.0, lo)
                cfg.per_kind_windows[kind] = (lo, max(lo + 1.0, hi))
            self.log_append("params: 各互动插播区间 " + json.dumps(
                {k: [round(a), round(b)] for k, (a, b) in cfg.per_kind_windows.items()}))
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

    def has_api_key(self) -> bool:
        return bool(self.api_key) or bool(os.environ.get("DASHSCOPE_API_KEY"))

    def set_api_key(self, key: str) -> bool:
        """运行中填入百炼 API Key：立即启用/重建 TTS（不用重启进程）。"""
        key = (key or "").strip()
        if not key:
            return False
        self.api_key = key
        os.environ["DASHSCOPE_API_KEY"] = key
        if self.sink_name != "dry":
            self.tts = TTSStreamer(api_key=key, model=self.model, voice=self.voice,
                                   speech_rate=self.speech_rate, on_audio=self._on_audio)
            self.tts_init_error = ""
            self.log_append("params: 百炼 Key 已设置，TTS 已启用")
        return True

    def set_room(self, web_rid: str) -> None:
        """切换直播间（线程安全重启采集器）。"""
        self.collector.stop()
        time.sleep(0.2)
        self.queue = InteractionQueue()
        self.scheduler.q = self.queue
        self.collector = LiveCollector(web_rid, self.queue, on_status=self._on_status)
        self.collector.start()
        self.log_append(f"room: 切换到 {web_rid}")

    def snapshot(self, events_n: int = 40, log_n: int = 40, log_after: int | None = None,
                 lite: bool = False) -> dict:
        room = self.collector.room
        cfg = self.scheduler.cfg
        speech_total = sum(1 for s in self.segments if s.kind == "speech")
        prefix = f"{self.model}|{self.voice}|{self.speech_rate}|"
        ready_sents = 0
        ready_groups = 0
        j = 0
        while j < len(self.segments):
            s = self.segments[j]
            if s.kind != "speech":
                j += 1
                continue
            idxs = self._group_at(j)
            j = idxs[-1] + 1
            # 就绪 = 内存或磁盘已生成过（重启后页面仍显示"音频就绪"，不用重新生成）
            if self._cache_hit(prefix + self._group_cache_key(idxs)):
                ready_sents += len(idxs)
                ready_groups += 1
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
            "audio": self.chunk_writer.reuse_stats(),
            "events": self.queue.recent(events_n),
            # 日志：给了 log_after 就只回增量（前端轮询不必每次重拉整段历史）
            "log": (self.log[max(0, log_after - (self._log_total - len(self.log))):]
                    if log_after is not None else self.log[-log_n:]),
            "log_total": self._log_total,
            "params": {
                "voice": self.voice, "model": self.model, "rate": self.speech_rate,
                "min_gap": cfg.min_gap_s, "window": list(cfg.window_s),
                "per_kind_windows": {k: list(v) for k, v in cfg.per_kind_windows.items()},
            },
            # 话术/昵称/报时配置（前端可直接编辑并保存）
            "phrases": {
                "templates": {k: list(v) for k, v in cfg.templates.items()},
                "kinds": list(INTERACTION_KINDS),
                "nick_han_only": cfg.nick_han_only,
                "nick_max_chars": cfg.nick_max_chars,
                "announce_time": cfg.announce_time,
                "announce_interval_s": list(cfg.announce_interval_s),
                "time_templates": list(cfg.time_templates),
                "fallback_templates": list(cfg.fallback_templates),
            },
            "announced": self.scheduler.announced_count,
            "tts_calls": self.tts_calls,
            "cache_hits": self.cache_hits,
            "disk_cache_hits": self.disk_cache_hits,
            "disk_cache_writes": self.disk_cache_writes,
            "cache_size": len(self._tts_cache),
            "prefetch_ahead": self.prefetch_ahead,
            "prefetch_pending": self.prefetch_pending,
            "prefetch_done": self.prefetch_done,
            "prefetch_running": bool(self._prefetch_thread and self._prefetch_thread.is_alive()),
            "cache_ready": ready_sents,       # 本稿已预生成/已缓存的正文句数
            "cache_total": speech_total,      # 本稿正文句总数
            "groups_ready": ready_groups,
            "merge_sentences": self.merge_sentences,
            "sentence_gap_ms": self.sentence_gap_ms,
            "trim_silence": self.trim_silence,
            "trim_removed_ms": self.trim_removed_ms,
            "seg_index": self._seg_i,
            "seg_count": len(self.segments),
            "script_title": self.script_title,
            "rotate_enabled": self.rotate_enabled,
            "rotate_index": self._rotate_idx,
            "rotate_after_s": self.rotate_after_s,
            "rotate_after_lines": self.rotate_after_lines,
            "lines_played": self._lines_played,
            "script_elapsed_s": (time.time() - self._script_started_at) if self._script_started_at else 0,
            "tts_error": (self.tts.last_error if self.tts else
                          (self.tts_init_error or ("(dry 模式，未合成)" if self.sink_name == "dry" else ""))),
            "api_key_set": self.has_api_key(),
        }

    def log_append(self, line: str):
        self.log.append(f"[{time.strftime('%H:%M:%S')}] {line}")
        self._log_total += 1   # 单调游标：前端据此只要增量，不用每次拉整段历史
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
                elif self._seg_i >= len(self.segments):
                    # 整轮自然播完：轮换开着头就切下一套稿，否则从头循环（音频走缓存）
                    if not self._maybe_rotate():
                        self._seg_i = 0  # 整轮自然播完 → 从头循环（音频走缓存，不再请求TTS）
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
        with self._tts_lock:
            self.tts_calls += 1
            audio = self.tts.synth(texts)
        if audio and self.trim_silence:
            # 百炼每块自带首尾静音（实测尾部 0.25–0.65s），逐句连播就成了"每句都停顿"
            audio, removed = trim_edges(audio)
            if removed:
                self.trim_removed_ms += removed
        return audio

    def _synth_cached(self, key: str, texts: list[str]) -> bytes:
        """文本没变就用缓存音频（循环重播不再请求百炼）；只有新内容才真实合成。

        两层缓存：内存（本轮循环）+ 磁盘 out/chunks/cache（**重启服务也不用重新花钱**）。
        """
        cache_key = f"{self.model}|{self.voice}|{self.speech_rate}|{key}"
        disk_path = self._audio_cache_path(cache_key)
        with self._tts_lock:
            if cache_key in self._tts_cache:
                self.cache_hits += 1
                return self._tts_cache[cache_key]
        if disk_path is not None:
            try:
                audio = disk_path.read_bytes()
            except OSError:
                audio = b""
            if audio:
                # 之前生成过（可能是上一次进程生成的）：直接复用，不请求百炼
                self.disk_cache_hits += 1
                self.cache_hits += 1
                with self._tts_lock:
                    self._tts_cache[cache_key] = audio
                    self.cache_size_peak = max(self.cache_size_peak, len(self._tts_cache))
                return audio
        audio = self._synth(texts)
        if audio:
            with self._tts_lock:
                self._tts_cache[cache_key] = audio
                self.cache_size_peak = max(self.cache_size_peak, len(self._tts_cache))
                if len(self._tts_cache) > 400:
                    # 只保留最近合成的，防止极长场次内存增长
                    for k in list(self._tts_cache)[:100]:
                        self._tts_cache.pop(k, None)
            if disk_path is not None:
                try:
                    disk_path.parent.mkdir(parents=True, exist_ok=True)
                    disk_path.write_bytes(audio)
                    self.disk_cache_writes += 1
                except OSError:
                    pass
        return audio

    def _audio_cache_path(self, cache_key: str) -> Path | None:
        """生成过的音频按 (模型+音色+语速+文本) 落一份，重启进程后仍可复用。"""
        if self.sink_name == "dry":
            return None
        h = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()
        return Path(self.out_dir) / "cache" / f"{h}.mp3"

    # ---------- 预生成（提前把后面几句合成好，播放时不被 TTS 拖节拍） ----------

    def _seg_cache_key(self, idx: int, seg) -> str:
        return f"seg:{idx}:{seg.text}"

    def _group_at(self, i: int) -> list[int]:
        """把 i 起连续最多 merge_sentences 个正文句合成一组（同一次 TTS 任务 → 语气连贯、无中间静音）。

        只在纯正文句之间合并；遇到停顿标记 / 固定话术(mark) 就断组。
        代价：组内不再有插播点（组尾仍可插播）。merge_sentences<2 时不合并。
        """
        n = self.merge_sentences
        if n < 2 or i >= len(self.segments):
            return [i] if i < len(self.segments) else []
        idxs = [i]
        for j in range(i + 1, min(i + n, len(self.segments))):
            if self.segments[j].kind != "speech":
                break
            idxs.append(j)
        return idxs

    def _group_texts(self, idxs: list[int]) -> list[str]:
        return [self.segments[j].text for j in idxs]

    def _group_cache_key(self, idxs: list[int]) -> str:
        if len(idxs) == 1:
            return self._seg_cache_key(idxs[0], self.segments[idxs[0]])
        return "grp:" + ",".join(str(j) for j in idxs) + ":" + "|".join(self._group_texts(idxs))

    def _full_cache_key(self, key: str) -> str:
        """缓存键必须含模型/音色/语速：换任何一个都要重新生成。"""
        return f"{self.model}|{self.voice}|{self.speech_rate}|{key}"

    def _cache_hit(self, cache_key: str) -> bool:
        """这段音频是否已经生成过（内存或磁盘，含上次进程留下的）。用于就绪统计与预生成排队。"""
        with self._tts_lock:
            if cache_key in self._tts_cache:
                return True
        p = self._audio_cache_path(cache_key)
        return bool(p and p.exists())

    def _prefetch_todo(self, start: int, limit: int | None):
        """按'将要播放的组'排队（与 _play_pass 的分组一致，预生成的就是播放要用的那块音频）。"""
        todo = []
        end = len(self.segments) if limit is None else min(start + limit, len(self.segments))
        j = start
        while j < end:
            s = self.segments[j]
            if s.kind != "speech":
                j += 1
                continue
            idxs = self._group_at(j)
            j = idxs[-1] + 1
            key = self._group_cache_key(idxs)
            if not self._cache_hit(self._full_cache_key(key)):
                todo.append((idxs[0], key, self._group_texts(idxs)))
        return todo

    def prefetch_script(self, limit: int | None = 0) -> int:
        """显式预生成：limit=None 整稿（从第 1 句起），limit>0 从当前位置起 N 句。返回排队句数。"""
        if self.tts is None:
            raise ValueError("未配置百炼 API Key，无法预生成")
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            raise ValueError("已有预生成任务在跑，稍后再试")
        start = 0 if limit is None else self._seg_i
        todo = self._prefetch_todo(start, limit)
        if not todo:
            return 0
        with self._lock:
            self.prefetch_pending = len(todo)
            self.prefetch_done = 0
        self.log_append(f"prefetch: 预生成 {len(todo)} 句"
                        + ("（整稿）" if limit is None else f"（自第{self._seg_i + 1}句起）"))
        self._prefetch_thread = threading.Thread(
            target=self._prefetch_worker, args=(todo,), daemon=True)
        self._prefetch_thread.start()
        return len(todo)

    def _prefetch_ahead(self, i: int) -> None:
        """播放间隙自动把接下来 prefetch_ahead 个'正文句'先合成进缓存。"""
        n = self.prefetch_ahead
        if n <= 0 or self.tts is None:
            return
        todo = self._prefetch_todo(i, n)
        if not todo:
            return
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            return  # 一次只跑一个预生成任务
        with self._lock:
            self.prefetch_pending = len(todo)
        self.log_append(f"prefetch: 自动预生成 {len(todo)} 句（{','.join(str(j) for j, _, _ in todo)}）")
        self._prefetch_thread = threading.Thread(
            target=self._prefetch_worker, args=(todo,), daemon=True)
        self._prefetch_thread.start()

    def _prefetch_worker(self, todo) -> None:
        try:
            for idx, key, texts in todo:
                if self._stop.is_set() or self._restart:
                    return
                # 走 _synth_cached：已生成过的（含上次进程留下的磁盘缓存）不再重复请求百炼，
                # 新生成的也会落盘 —— 预生成的成果同样能跨重启复用。
                audio = self._synth_cached(key, texts)
                if not audio:
                    self.log_append(f"prefetch: 第{idx}组合成失败 "
                                    f"{self.tts.last_error if self.tts else ''}")
                    return
                with self._lock:
                    self.prefetch_done += 1
                    self.prefetch_pending = max(0, self.prefetch_pending - 1)
        except Exception as e:
            self.log_append(f"prefetch error: {type(e).__name__}: {e}")

    def _play_pass(self):
        i = self._seg_i
        while i < len(self.segments):
            if self._stop.is_set():
                return
            if self._restart or self._pause.is_set():
                self._seg_i = i
                return  # 回到 run() 处理换稿/暂停；resume 时从 _seg_i 继续
            seg = self.segments[i]
            if seg.kind == "pause":
                # 定点停顿：告诉播放端下一块前先停这么多，然后本地也等
                self.chunk_writer.add_gap(seg.duration_ms)
                i += 1
                self._seg_i = i
                time.sleep(seg.duration_ms / 1000.0)
                continue
            if seg.kind == "speech":
                # 连续正文句按组一次合成（语气连贯、组内无静音）；未变的组只合成一次
                idxs = self._group_at(i)
                texts = self._group_texts(idxs)
                cache_key = self._group_cache_key(idxs)
                kind = "speech"
            else:  # mark：固定话术模板，占位符没被互动数据填时也能缓存
                idxs = [i]
                latest = self.queue.items[-1] if self.queue.items else None
                text = self.scheduler.render_mark(seg.text, latest)
                texts = [text]
                cache_key = f"mark:{text}"
                kind = seg.kind
            i = idxs[-1] + 1
            self._seg_i = i
            # 统计这套稿已经播过多少行（同一行只算一次）
            for k in idxs:
                ln = self.segments[k].line_no
                if ln is not None and ln >= 0 and ln != self._last_line_no:
                    self._last_line_no = ln
                    self._lines_played += 1
            audio = self._synth_cached(cache_key, texts)
            # 第一块不留空；后续每块前留句间停顿（与本地 sleep 一致，浏览器据此接播）
            gap_ms = max(0, int(self.sentence_gap_ms)) if self.chunk_writer.manifest else 0
            self.chunk_writer.write(kind, " ".join(texts), audio or b"", gap_before_ms=gap_ms)
            self.log_append(f"play[{kind}]{'' if len(idxs) < 2 else f'×{len(idxs)}'}: {' '.join(texts)}")
            # 节拍 = 真实音频时长（解析 mp3 帧，与 ffprobe 校准到 20ms），不再按字数估算。
            # 音频还没就绪（预生成没跟上）时才退回估算，避免播得比生成快。
            dur_ms = duration_ms(audio) if audio else 0.0
            dur_s = dur_ms / 1000.0 if dur_ms > 0 else self._estimate_elapsed_s(texts)
            self._prefetch_ahead(i)  # 当前组播出的这段时间，后台先合成后面几组
            time.sleep(max(0.0, dur_s) + gap_ms / 1000.0)
            # 句边界：能不能插播？（看下一个边界是否受保护）
            nxt_protected = self._next_boundary_protected(i)
            insert_text = self.scheduler.on_boundary(time.time(), nxt_protected)
            if insert_text:
                # 互动插播内容每次都不同 → 总是真实合成（不缓存）
                ins_audio = self._synth([insert_text])
                self.chunk_writer.write("interrupt", insert_text, ins_audio or b"")
                self.log_append(f"INSERT: {insert_text}")
            # 轮换触发：按行（播够 N 行）或按时间（播够 N 秒），都在段边界切下一套
            due_lines = (self.rotate_after_lines > 0
                         and self._lines_played >= self.rotate_after_lines)
            due_time = (self.rotate_after_s > 0 and self._script_started_at > 0
                        and time.time() - self._script_started_at >= self.rotate_after_s)
            if self.rotate_enabled and self.script_source and (due_lines or due_time):
                if self._maybe_rotate():
                    return

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
