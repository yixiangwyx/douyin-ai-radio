"""连贯性：合并生成（组）、句间停顿参数、真实时长节拍、静音裁剪降级、mp3 时长解析。"""
import struct
import time

from airadio.mp3len import _BITRATES_L3, duration_ms, parse_frames
from airadio.orchestrator import Orchestrator
from airadio.silence import trim_edges


class FakeTTS:
    def __init__(self, delay=0.0):
        self.calls = 0
        self.last_error = ""
        self.delay = delay
        self.groups: list[list[str]] = []

    def synth(self, texts, timeout_ms=30000):
        if self.delay:
            time.sleep(self.delay)
        self.calls += 1
        self.groups.append(list(texts))
        return b"audio:" + " ".join(texts).encode()


def _mk(tmp_path, script, merge=2, gap=0, prefetch=0):
    o = Orchestrator("0", script, sink="dry", out_dir=str(tmp_path))
    o.tts = FakeTTS()
    o.merge_sentences = merge
    o.sentence_gap_ms = gap
    o.prefetch_ahead = prefetch
    o.trim_silence = False
    return o


SCRIPT = "第一句。\n第二句。\n第三句。\n第四句。"


def test_merge_groups_sentences(tmp_path):
    """默认合并 2 句：4 句稿 → 2 次 TTS 调用，每次带 2 段文本。"""
    o = _mk(tmp_path, SCRIPT, merge=2)
    o._play_pass()
    assert o.tts.calls == 2
    assert [len(g) for g in o.tts.groups] == [2, 2]
    assert o.tts.groups[0] == ["第一句。", "第二句。"]


def test_merge_respects_marks_and_boundaries(tmp_path):
    """互动标记 [[待插:…]] 必须单独成块：它是句边界插播点，不能被并进组里。"""
    o = _mk(tmp_path, "第一句。\n第二句。\n[[待插:感谢关注]]\n第三句。\n第四句。", merge=3)
    o._play_pass()
    kinds = [c["kind"] for c in o.chunk_writer.manifest]
    # 组只能由连续正文句构成，mark 自己一块
    assert kinds == ["speech", "mark", "speech"]
    assert [len(g) for g in o.tts.groups] == [2, 1, 2]          # 1+2 合并 → mark 单独 → 3+4 合并


def test_gap_zero_writes_no_gap(tmp_path):
    o = _mk(tmp_path, SCRIPT, merge=1, gap=0)
    o._play_pass()
    assert all(c["gap_before_ms"] == 0 for c in o.chunk_writer.manifest)


def test_gap_param_applies(tmp_path):
    o = _mk(tmp_path, SCRIPT, merge=1, gap=0)
    o.update_params(sentence_gap_ms=300)
    assert o.snapshot()["sentence_gap_ms"] == 300
    o._play_pass()
    # 第一块不留空（开头），后续每块前 300ms
    gaps = [c["gap_before_ms"] for c in o.chunk_writer.manifest]
    assert gaps[0] == 0 and all(g == 300 for g in gaps[1:])


def test_beat_uses_real_audio_not_estimate(tmp_path):
    """节拍必须等于真实音频时长（+gap），不再按字数估算。"""
    o = _mk(tmp_path, SCRIPT, merge=1, gap=0)
    fixed = _mp3(seconds=1.0)

    def fake_synth(texts, timeout_ms=30000):
        o.tts.calls += 1
        o.tts.groups.append(list(texts))
        return fixed

    o.tts.synth = fake_synth
    t0 = time.time()
    o._play_pass()
    elapsed = time.time() - t0
    # 4 句 × 1.0s；按字数估算会明显更长（每句约 1.3s）
    assert 3.7 < elapsed < 4.6, f"elapsed={elapsed:.2f}"


def _mp3(seconds: float, sr: int = 24000, kbps_idx: int = 14) -> bytes:
    """造 MPEG2 LayerIII 帧（与百炼输出同参数：24kHz / 160kbps / 480 字节每帧）。"""
    kbps = _BITRATES_L3[2][kbps_idx]
    spf = 576
    n_frames = max(1, int(seconds * sr / spf))
    frame_len = int(spf / 8.0 * kbps * 1000 / sr)      # 含 4 字节帧头
    b1 = 0xF3                                          # MPEG2 + LayerIII + 带CRC（与百炼输出一致）
    b2 = (kbps_idx << 4) | (1 << 2)                    # 160kbps(idx14) + sri1=24kHz
    frame = bytes([0xFF, b1, b2, 0x00]) + b"\x00" * (frame_len - 4)
    return frame * n_frames


def test_mp3_duration_parser_matches_frames():
    for secs in (1.0, 2.5, 5.0):
        data = _mp3(secs)
        frames = parse_frames(data)
        assert len(frames) > 10
        d = duration_ms(data)
        assert abs(d - secs * 1000) < 60, f"{secs}s → {d}ms"


def test_merge_prefetch_uses_same_groups(tmp_path):
    """预生成的排队单位必须与播放一致：合并后的组，否则缓存永远不命中。"""
    o = _mk(tmp_path, SCRIPT, merge=2, prefetch=0)
    assert o.prefetch_script(None) == 2            # 2 组，不是 4 句
    o._prefetch_thread.join(timeout=10)
    assert o.tts.calls == 2
    assert o.snapshot()["cache_ready"] == 4 and o.snapshot()["cache_total"] == 4
    assert o.snapshot()["groups_ready"] == 2
    o._play_pass()
    assert o.tts.calls == 2                        # 播放全程零新请求
    assert o.snapshot()["cache_hits"] == 2


def test_trim_edges_without_ffmpeg_is_safe():
    """没有 ffmpeg 或音频太短时原样返回，不能报错。"""
    audio, removed = trim_edges(b"not really mp3")
    assert audio == b"not really mp3" and removed == 0
