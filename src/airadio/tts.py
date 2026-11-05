"""阿里百炼 CosyVoice 实时流式合成（dashscope tts_v2.SpeechSynthesizer）。

一次任务 = 一次 run-task…finish-task（streaming_call 多段 + streaming_complete）。
每个任务结束后新建下一个任务（CosyVoice 协议一轮一个 task_id）。
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from dashscope.audio.tts_v2 import AudioFormat, ResultCallback, SpeechSynthesizer

# 常用音色（cosyvoice-v2）：longxiaochun_v2 女、longxiaoxia_v2、longcheng_v2 男、longwan_v2 等
DEFAULT_MODEL = "cosyvoice-v2"
DEFAULT_VOICE = "longxiaochun_v2"


class _Callback(ResultCallback):
    def __init__(self, on_audio: Optional[Callable[[bytes], None]], chunks: list[bytes]):
        self.on_audio = on_audio
        self.chunks = chunks
        self.error: Optional[str] = None

    def on_open(self):
        pass

    def on_data(self, message: bytes):
        if message:
            self.chunks.append(bytes(message))
            if self.on_audio:
                self.on_audio(bytes(message))

    def on_complete(self):
        pass

    def on_error(self, request_id, error):
        self.error = f"{request_id}: {error}"

    def on_close(self):
        pass


class TTSStreamer:
    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 voice: str = DEFAULT_VOICE, sample_rate: int = 24000,
                 speech_rate: float = 1.0, volume: int = 60,
                 on_audio: Optional[Callable[[bytes], None]] = None):
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY 未设置（export DASHSCOPE_API_KEY=sk-...）")
        self.model = model
        self.voice = voice
        fmt = {22050: AudioFormat.MP3_22050HZ_MONO_256KBPS,
               24000: AudioFormat.MP3_24000HZ_MONO_256KBPS,
               48000: AudioFormat.MP3_48000HZ_MONO_128KBPS}.get(
                   sample_rate, AudioFormat.MP3_24000HZ_MONO_256KBPS)
        self.fmt = fmt
        self.speech_rate = speech_rate
        self.volume = volume
        self.on_audio = on_audio
        self.last_error = ""

    def synth(self, texts: list[str], timeout_ms: int = 30000) -> bytes:
        """把多段文本作为一次流式任务合成，返回 mp3 字节。"""
        chunks: list[bytes] = []
        cb = _Callback(self.on_audio, chunks)
        ss = SpeechSynthesizer(model=self.model, voice=self.voice, format=self.fmt,
                               volume=self.volume, speech_rate=self.speech_rate,
                               callback=cb)
        try:
            for t in texts:
                ss.streaming_call(t)
            ss.streaming_complete(timeout=timeout_ms / 1000.0)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return b"".join(chunks)
        if cb.error:
            self.last_error = cb.error
        return b"".join(chunks)
