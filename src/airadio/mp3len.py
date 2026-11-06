"""MP3 时长（纯 Python，不依赖 ffprobe）。

解析 MPEG audio layer III 帧头，累加每帧采样数 → 精确到一帧（≈26ms）。
用于播放节拍：用真实音频时长而不是按字数估算，避免每句之间多等 1~2 秒。
"""
from __future__ import annotations

# version_index: 3=MPEG1, 2=MPEG2, 0=MPEG2.5
_SAMPLE_RATES = {
    3: {0: 44100, 1: 48000, 2: 32000},
    2: {0: 22050, 1: 24000, 2: 16000},
    0: {0: 11025, 1: 12000, 2: 8000},
}
# layer III bitrates in kbps, indexed by the 4-bit bitrate index
_BITRATES_L3 = {
    3: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    0: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_SAMPLES_PER_FRAME = {3: 1152, 2: 576, 0: 576}

# 编码器/容器首尾填充（帧之外）。实测对 CosyVoice 输出校准所得（秒）。
_ENCODER_PAD_S = 0.02


def _skip_id3(data: bytes, pos: int) -> int:
    if data[pos:pos + 3] == b"ID3":
        size = ((data[pos + 6] & 0x7F) << 21 | (data[pos + 7] & 0x7F) << 14
                | (data[pos + 8] & 0x7F) << 7 | (data[pos + 9] & 0x7F))
        return pos + 10 + size
    return pos


def parse_frames(data: bytes) -> list[tuple[int, int, int]]:
    """返回 [(sample_rate, samples_per_frame, frame_bytes), ...]。"""
    out: list[tuple[int, int, int]] = []
    i = _skip_id3(data, 0)
    n = len(data)
    while i < n - 4:
        if data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
            i += 1
            continue
        b1, b2 = data[i + 1], data[i + 2]
        version = (b1 >> 3) & 3          # 3=MPEG1 2=MPEG2 0=MPEG2.5 1=reserved
        layer = (b1 >> 1) & 3            # 1=Layer III
        if version == 1 or layer != 1:
            i += 1
            continue
        bri = (b2 >> 4) & 0x0F
        sri = (b2 >> 2) & 3
        padding = (b2 >> 1) & 1
        if bri in (0, 0x0F) or sri == 3:
            i += 1
            continue
        sr = _SAMPLE_RATES[version][sri]
        bitrate = _BITRATES_L3[version][bri] * 1000
        spf = _SAMPLES_PER_FRAME[version]
        # 帧长公式已含 4 字节帧头（MPEG1 L3=144*bpsr, MPEG2/2.5 L3=72*bpsr）
        frame_len = int(spf / 8.0 * bitrate / sr) + padding
        if frame_len <= 4:
            i += 1
            continue
        out.append((sr, spf, frame_len))
        i += frame_len
    return out


def duration_ms(audio: bytes) -> float:
    """mp3 字节 → 毫秒时长；解析失败返回 0（调用方自行退回估算）。"""
    frames = parse_frames(audio or b"")
    if not frames:
        return 0.0
    total_samples = sum(spf for _, spf, _ in frames)
    sr = frames[0][0]
    return total_samples / sr * 1000.0 + _ENCODER_PAD_S * 1000.0
