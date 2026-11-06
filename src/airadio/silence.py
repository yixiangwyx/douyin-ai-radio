"""裁掉 TTS 输出块的首尾静音（可选，需 ffmpeg）。

CosyVoice 每次请求都会在首尾留下静音（实测尾部 0.25–0.65s）。逐句播放时这些静音
会连起来变成"每句都有停顿"。这里在落块前把首尾静音裁掉（-c copy，按帧切，不重编码）。
没有 ffmpeg 时原样返回，不影响可用性。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .mp3len import duration_ms

_NOISE_DB = "-35dB"
_MIN_SILENCE_S = "0.10"
_MAX_TRIM_S = 1.2          # 安全上限：只裁边缘，绝不误伤正文


def _silence_spans(audio: bytes) -> tuple[float, float, float]:
    """返回 (总时长, 首部静音结束点, 尾部静音开始点)；失败返回 (0,0,0)。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return 0.0, 0.0, 0.0
    total = duration_ms(audio) / 1000.0
    if total <= 0:
        return 0.0, 0.0, 0.0
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.mp3"
        src.write_bytes(audio)
        r = subprocess.run(
            [ffmpeg, "-v", "info", "-i", str(src),
             "-af", f"silencedetect=noise={_NOISE_DB}:d={_MIN_SILENCE_S}",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=30)
        log = r.stderr or ""
    starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    lead = 0.0
    for s, e in zip(starts, ends):
        if s <= 0.05:
            lead = min(e, _MAX_TRIM_S)
            break
    tail = total
    if starts and ends:
        last_start = max(starts)
        last_end = max(ends)
        if last_end >= total - 0.12:                 # 尾部静音一直到结尾
            tail = max(lead, min(last_start, total - 0.15))
            if total - tail > _MAX_TRIM_S:
                tail = total - _MAX_TRIM_S
    return total, lead, tail


def trim_edges(audio: bytes) -> tuple[bytes, int]:
    """裁首尾静音。返回 (音频, 裁掉的毫秒)。无 ffmpeg / 无需裁 → 原样返回。"""
    if not audio or not shutil.which("ffmpeg"):
        return audio, 0
    total, lead, tail = _silence_spans(audio)
    if total <= 0 or (lead <= 0.02 and tail >= total - 0.02):
        return audio, 0
    if tail - lead < 0.3:        # 裁完不足 0.3s，说明检测不可靠，放弃
        return audio, 0
    ffmpeg = shutil.which("ffmpeg")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.mp3"
        dst = Path(td) / "out.mp3"
        src.write_bytes(audio)
        r = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", str(src),
             "-ss", f"{lead:.3f}", "-to", f"{tail:.3f}", "-c", "copy", str(dst)],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or not dst.is_file():
            return audio, 0
        out = dst.read_bytes()
    if duration_ms(out) < 200:
        return audio, 0
    removed = int((total - duration_ms(out) / 1000.0) * 1000)
    return out, max(0, removed)
