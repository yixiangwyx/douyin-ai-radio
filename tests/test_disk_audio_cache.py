"""生成过的音频落磁盘缓存：重启服务后同稿同音色不再请求百炼（省钱、秒开）。"""
from airadio.orchestrator import Orchestrator


class FakeTTS:
    def __init__(self):
        self.calls = 0
        self.last_error = ""

    def synth(self, texts, timeout_ms=30000):
        self.calls += 1
        # 真实一点的 MP3 帧（MPEG2 LayerIII + CRC），让时长解析也能过
        return b"\xff\xf3\xc0\x00" + bytes(416 - 4)


def _play(o: Orchestrator, loops: int = 1):
    """像 run() 那样反复播整稿，但不启动网络采集（测试里不连抖音）。
    merge_sentences=1：让"每句一次合成"的计数可预测（默认会把连续句合并成一组）。
    """
    o.trim_silence = False
    o.prefetch_ahead = 0
    o.merge_sentences = 1
    for _ in range(loops):
        o._play_pass()
        o._seg_i = 0
    return o


def _mk(tmp_path, script, voice="longxiaochun_v2", sink="file"):
    o = Orchestrator("0", script, sink=sink, voice=voice, out_dir=str(tmp_path / "c"))
    o.tts = FakeTTS()
    return o


def test_audio_is_written_to_disk_cache(tmp_path):
    o = _play(_mk(tmp_path, "第一句。\n第二句。"))
    assert o.tts.calls == 2
    assert o.disk_cache_writes == 2
    assert len(list((tmp_path / "c" / "cache").glob("*.mp3"))) == 2


def test_restart_reuses_disk_cache_without_tts_calls(tmp_path):
    script = "第一句。\n第二句。\n第三句。"
    assert _play(_mk(tmp_path, script)).tts.calls == 3

    # 模拟服务重启：新 orchestrator，内存缓存是空的
    second = _mk(tmp_path, script)
    assert len(second._tts_cache) == 0
    _play(second)
    assert second.tts.calls == 0                       # 一次都没问百炼
    assert second.disk_cache_hits == 3


def test_voice_change_does_not_reuse_disk_cache(tmp_path):
    script = "第一句。"
    _play(_mk(tmp_path, script))                       # 默认音色生成过一次

    other = _play(_mk(tmp_path, script, voice="longxiaoxia_v2"))
    assert other.tts.calls == 1                        # 换音色必须重新生成
    assert other.disk_cache_hits == 0


def test_same_text_different_rate_is_not_reused(tmp_path):
    script = "第一句。"
    _play(_mk(tmp_path, script))
    faster = _mk(tmp_path, script)
    faster.speech_rate = 1.5
    _play(faster)
    assert faster.tts.calls == 1
    assert faster.disk_cache_hits == 0


def test_dry_mode_does_not_touch_disk(tmp_path):
    o = _play(_mk(tmp_path, "第一句。", sink="dry"))
    assert o.tts.calls == 1
    assert o.disk_cache_writes == 0
    assert not (tmp_path / "c" / "cache").exists()
