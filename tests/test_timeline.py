import json
import pathlib
import pytest

from airadio.timeline import parse_script, Scheduler, ScheduleConfig
from airadio.events import InteractionQueue, InteractionEvent

SCRIPT = (pathlib.Path(__file__).resolve().parents[1] / "scripts" / "demo_script.txt").read_text(encoding="utf-8")


def test_parse_sentences_and_protection():
    segs = parse_script(SCRIPT)
    assert segs and all(s.kind in ("speech", "pause", "mark") for s in segs)
    # 保护块内的句子不可插播打断
    protected = [s for s in segs if s.protected and s.kind == "speech"]
    assert protected, "demo script should contain a protected block"
    assert all(not s.can_interrupt_after for s in protected)
    # 普通句子边界可插播
    normal = [s for s in segs if s.kind == "speech" and not s.protected]
    assert normal and all(s.can_interrupt_after for s in normal)
    # 待插标记存在
    assert any(s.kind == "mark" for s in segs)
    # 保护块内容完整（不被拆开导致语义丢失）
    joined = "".join(s.text for s in protected)
    assert "你讲给谁听" in joined and "后面全白搭" in joined


def test_long_line_is_one_uninterruptible_sentence():
    segs = parse_script("这一段整段都不许插播因为中间没有句号它是一整句直到这里才结束。")
    assert len(segs) == 1
    assert segs[0].can_interrupt_after is True  # 句子结束后可以插
    segs2 = parse_script("[[no interrupt]]整段保护没有内部边界[[/no interrupt]]")
    assert all(s.protected for s in segs2)
    assert all(not s.can_interrupt_after for s in segs2)


def test_pause_marker():
    segs = parse_script("第一句。[[pause:300]]第二句。")
    pauses = [s for s in segs if s.kind == "pause"]
    assert pauses and pauses[0].duration_ms == 300


def test_scheduler_respects_gap_and_window():
    import random
    q = InteractionQueue(ttl=3600)
    q.push(InteractionEvent(kind="member", nick="测试用户A", t=1000.0))
    cfg = ScheduleConfig(min_gap_s=45, window_s=(60, 150))
    sched = Scheduler(cfg, q, rng=random.Random(42))
    planned = None
    # 事件未到计划时刻前不插；到点后插入
    for now in [1010.0, 1040.0, 1059.0]:
        assert sched.on_boundary(now, protected_after=False) is None
    out = None
    for now in [x / 10.0 for x in range(10600, 11600, 50)]:
        out = sched.on_boundary(now, protected_after=False)
        if out:
            planned = now
            break
    # 昵称默认只读汉字：字母 A 被去掉
    assert out and "测试用户" in out and "A" not in out and planned >= 1060.0  # 计划时刻落在窗口 [60,150] 内
    # 紧接着再来边界：最小间隔不足 → 不插
    assert sched.on_boundary(planned + 5, protected_after=False) is None
    # 45s 后可再插
    q.push(InteractionEvent(kind="follow", nick="粉丝B", t=planned))
    out2 = None
    for now in [planned + 45 + i for i in range(0, 160, 5)]:
        out2 = sched.on_boundary(now, protected_after=False)
        if out2:
            break
    assert out2 and "粉丝" in out2 and "B" not in out2   # 只读汉字：字母被去掉


def test_scheduler_skips_protected_boundary():
    import random
    q = InteractionQueue(ttl=3600)
    q.push(InteractionEvent(kind="member", nick="路人", t=0.0))
    cfg = ScheduleConfig(min_gap_s=45, window_s=(80, 120))
    sched = Scheduler(cfg, q, rng=random.Random(1))
    assert sched.on_boundary(70.0, protected_after=True) is None   # 保护边界不插
    assert sched.on_boundary(70.0, protected_after=False) is None  # 未到计划时刻
    out = None
    for now in [120.0, 125.0, 130.0]:
        out = sched.on_boundary(now, protected_after=False)
        if out:
            break
    assert out and "路人" in out  # 计划到期后仍可插


def test_dedup_and_chat_blocklist():
    q = InteractionQueue()
    e1 = InteractionEvent(kind="chat", nick="a", content="在吗", t=1.0)
    assert q.push(e1) is False  # 无意义弹幕不入库
    e2 = InteractionEvent(kind="member", nick="dup", t=1.0)
    assert q.push(e2) is True
    e3 = InteractionEvent(kind="member", nick="dup", t=5.0)
    assert q.push(e3) is False  # 同签名去重


def test_gift_priority_templates():
    import random
    q = InteractionQueue()
    q.push(InteractionEvent(kind="gift", nick="大哥", gift="火箭", count=3, value=5000, t=0.0))
    cfg = ScheduleConfig(min_gap_s=10, window_s=(0, 10), urgent_min_gap_s=2)
    sched = Scheduler(cfg, q, rng=random.Random(3))
    out = sched.on_boundary(5.0, protected_after=False)  # 高价值礼物计划时刻≈1s，可插队
    assert out and "大哥" in out and "火箭" in out and "3个" in out


def test_render_mark_placeholders():
    q = InteractionQueue()
    ev = InteractionEvent(kind="gift", nick="金主", gift="玫瑰", t=1.0)
    sched = Scheduler(ScheduleConfig(), q)
    text = sched.render_mark("感谢{nick}送的{gift}", ev)
    assert "金主" in text and "玫瑰" in text


def test_captured_samples_parse():
    """真实抓包样本 → 事件模型解析验证（协议回归测试）。"""
    import base64
    from airadio.proto import ChatMessage, SocialMessage, LikeMessage
    data = json.loads((pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sample_frames.json").read_text())
    samples = data["samples"]
    n = 0
    for s in samples.get("chat", []):
        m = ChatMessage(); m.ParseFromString(base64.b64decode(s))
        assert m.user.nickname or m.content
        n += 1
    for s in samples.get("social", []):
        m = SocialMessage(); m.ParseFromString(base64.b64decode(s))
        assert m.user.nickname
        n += 1
    for s in samples.get("like", []):
        m = LikeMessage(); m.ParseFromString(base64.b64decode(s))
        assert m.user.nickname
        n += 1
    assert n > 0
