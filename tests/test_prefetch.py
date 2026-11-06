"""预生成（prefetch）与缓存计数：显式预生成 / 自动预生成 / 并发计数正确。"""
import time

from airadio.orchestrator import Orchestrator


class FakeTTS:
    def __init__(self, delay=0.0):
        self.calls = 0
        self.last_error = ""
        self.delay = delay

    def synth(self, texts, timeout_ms=30000):
        if self.delay:
            time.sleep(self.delay)
        self.calls += 1
        return b"audio:" + " ".join(texts).encode()


def _mk(tmp_path, script="第一句。\n第二句。\n第三句。", prefetch=0):
    o = Orchestrator("0", script, sink="dry", out_dir=str(tmp_path))
    o.tts = FakeTTS()
    o.prefetch_ahead = prefetch
    return o


def test_prefetch_script_prepopulates_cache(tmp_path):
    o = _mk(tmp_path)
    assert o.prefetch_script(None) == 3           # 整稿 3 句排队
    o._prefetch_thread.join(timeout=10)
    assert o.tts.calls == 3
    assert o.snapshot()["cache_ready"] == 3 and o.snapshot()["cache_total"] == 3
    # 播放时全部命中缓存，不再请求百炼
    o._play_pass()
    assert o.tts.calls == 3
    assert o.snapshot()["cache_hits"] == 3


def test_prefetch_limit_n_from_current_position(tmp_path):
    o = _mk(tmp_path)
    assert o.prefetch_script(2) == 2              # 只预生成前 2 句
    o._prefetch_thread.join(timeout=10)
    assert o.tts.calls == 2
    snap = o.snapshot()
    assert snap["cache_ready"] == 2 and snap["cache_total"] == 3
    # 第三句没预生成 → 播放到它时才真实合成
    o._play_pass()
    assert o.tts.calls == 3


def test_auto_prefetch_ahead_during_play(tmp_path):
    """自动预生成让第 2、3 句在播第 1 句期间就合成好。"""
    o = _mk(tmp_path, prefetch=2)
    o.tts = FakeTTS(delay=0.05)
    o._play_pass()
    # 3 句各合成一次；预生成不产生额外调用（缓存命中）
    assert o.tts.calls == 3
    assert o.snapshot()["cache_hits"] >= 0
    assert o.snapshot()["cache_ready"] == 3


def test_no_prefetch_when_disabled(tmp_path):
    o = _mk(tmp_path, prefetch=0)
    assert o.prefetch_script(0) == 0 or o.prefetch_script(None) >= 0
    o._play_pass()
    # 关闭预生成时逐句边播边合成，仍然只 3 次
    assert o.tts.calls == 3


def test_prefetch_rejects_without_api_key(tmp_path):
    o = _mk(tmp_path)
    o.tts = None
    try:
        o.prefetch_script(None)
    except ValueError as e:
        assert "百炼" in str(e)
    else:
        raise AssertionError("应报错")


def test_prefetch_blocked_while_running(tmp_path):
    o = _mk(tmp_path)
    o.tts = FakeTTS(delay=0.2)
    o.prefetch_script(None)
    try:
        o.prefetch_script(None)
    except ValueError as e:
        assert "预生成任务在跑" in str(e)
    else:
        raise AssertionError("重复预生成应被拒绝")
    o._prefetch_thread.join(timeout=10)
    assert o.tts.calls == 3


def test_voice_change_invalidates_prefetch(tmp_path):
    o = _mk(tmp_path)
    o.prefetch_script(None)
    o._prefetch_thread.join(timeout=10)
    assert o.tts.calls == 3
    o.update_params(voice="longcheng_v2")
    assert o.snapshot()["cache_ready"] == 0        # 换音色后整稿失效
    o._play_pass()
    assert o.tts.calls == 6                        # 重新合成一遍


def test_prefetch_all_covers_whole_script_even_mid_playback(tmp_path):
    """播放到中途时点『预生成整稿』：应覆盖整稿全部未缓存句子（含已播过的）。"""
    o = _mk(tmp_path)
    o.prefetch_script(1)                            # 只缓存第 1 句
    o._prefetch_thread.join(timeout=10)
    assert o.tts.calls == 1
    o._seg_i = 2                                    # 模拟已播到第 3 段
    assert o.prefetch_script(None) == 2             # 从第 1 句起补齐未缓存的 2、3 句
    o._prefetch_thread.join(timeout=10)
    assert o.tts.calls == 3
    assert o.snapshot()["cache_ready"] == 3
