from airadio.timeline import Scheduler, ScheduleConfig
from airadio.events import InteractionQueue, InteractionEvent


def test_per_kind_windows_independent():
    """进场用短区间(0,5)，关注跟随全局(60,150)：同一时刻只有到期的进场被插。"""
    import random
    q = InteractionQueue(ttl=3600)
    q.push(InteractionEvent(kind="member", nick="进场者", t=1000.0))
    q.push(InteractionEvent(kind="follow", nick="关注者", t=1000.0))
    cfg = ScheduleConfig(min_gap_s=0, window_s=(60, 150),
                        per_kind_windows={"member": (0, 5)})
    sched = Scheduler(cfg, q, rng=random.Random(7))
    out = sched.on_boundary(1006.0, protected_after=False)
    assert out and "进场者" in out and "关注者" not in out
    out2 = None
    for now in [1060.0 + i for i in range(0, 100, 5)]:
        out2 = sched.on_boundary(now, protected_after=False)
        if out2:
            break
    assert out2 and "关注者" in out2


def test_priority_and_latest_only():
    """到期批次：礼物优先于点赞；同类型内只念最新的。"""
    import random
    q = InteractionQueue(ttl=3600)
    q.push(InteractionEvent(kind="like", nick="", t=1000.0))
    q.push(InteractionEvent(kind="gift", nick="老大哥", gift="嘉年华", value=66666, t=1002.0))
    q.push(InteractionEvent(kind="member", nick="早进的", t=1000.0))
    q.push(InteractionEvent(kind="member", nick="刚进的", t=1004.0))
    cfg = ScheduleConfig(min_gap_s=0, window_s=(0, 5), max_events=2)
    # gift 走 urgent 计划(=t+12.5) → 手动设 urgent_min_gap 大一点让计划落在 1015
    cfg.urgent_min_gap_s = 25
    sched = Scheduler(cfg, q, rng=random.Random(11))
    out = None
    for now in [1015.0 + i for i in range(0, 30, 2)]:
        out = sched.on_boundary(now, protected_after=False)
        if out:
            break
    assert out
    assert "老大哥" in out and "嘉年华" in out          # 礼物排最前
    assert out.index("老大哥") < out.index("刚进的")     # 优先级排序
    assert "早进的" not in out                            # max_events=2，只取最新的进场


def test_cache_reuse_for_unchanged_script(tmp_path):
    """稿子未变的句子只真实合成一次，循环复用；新互动内容才再合成。"""
    from airadio.orchestrator import Orchestrator

    class FakeTTS:
        def __init__(self):
            self.calls = 0
            self.last_error = ""
        def synth(self, texts, timeout_ms=30000):
            self.calls += 1
            return b"audio:" + " ".join(texts).encode()

    orch = Orchestrator("0", "第一句。\n第二句。", sink="dry", out_dir=str(tmp_path))
    orch.tts = FakeTTS()
    audio1 = orch._synth_cached("seg:0:第一句。", ["第一句。"])
    audio2 = orch._synth_cached("seg:0:第一句。", ["第一句。"])
    expect = "audio:第一句。".encode()
    assert audio1 == audio2 == expect
    assert orch.tts.calls == 1 and orch.cache_hits == 1
    # 换音色后 key 变化 → 重新合成
    orch.voice = "longcheng_v2"
    orch._synth_cached("seg:0:第一句。", ["第一句。"])
    assert orch.tts.calls == 2
