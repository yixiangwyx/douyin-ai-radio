"""占位符 / 昵称播报规则 / 互动话术多条随机 / 随机报时 / 模型-音色可用性。"""
import time

from airadio.events import InteractionEvent, InteractionQueue
from airadio.models import catalog, default_voice_for, is_usable_voice, model_info, model_names
from airadio.placeholders import fill, needs_data, speakable_nick
from airadio.timeline import DEFAULT_TIME_TEMPLATES, INTERACTION_KINDS, Scheduler, ScheduleConfig


def _sched(cfg=None):
    q = InteractionQueue()
    return Scheduler(cfg or ScheduleConfig(), q), q


# ---------- 占位符 ----------

def test_time_placeholders_are_global():
    out = fill("现在是[时点][时分]，[月]月[日]日[星期][时段]好。")
    assert "[" not in out and "]" not in out
    assert "点" in out and "分" in out and "月" in out and "周" in out


def test_minute_pads_zero_but_single_tokens_do_not():
    from datetime import datetime
    t = datetime(2026, 11, 6, 9, 5, 3)
    assert fill("[时点][时分]", {}, t) == "9点05分"
    assert fill("[时]点[分]分", {}, t) == "9点5分"      # 单字占位符给裸数字
    assert fill("[时段]", {}, t) == "上午"


def test_data_placeholders_and_legacy_aliases():
    out = fill("感谢[昵称]送的[礼物][数量]", {"nick": "兴旺的大刘", "gift": "玫瑰",
                                              "count": "3个"})
    assert out == "感谢兴旺的大刘送的玫瑰3个"
    assert fill("谢谢{nick}", {"nick": "甲"}) == "谢谢甲"
    assert needs_data("[昵称]") == "nick"
    assert needs_data("{nick}") == "nick"
    assert needs_data("现在是[时点][时分]") is None


# ---------- 昵称规则 ----------

def test_nick_han_only_and_max_chars():
    raw = "兴旺🌹的大刘啊abcdef123"
    assert speakable_nick(raw, True, 3) == "兴旺的"
    assert speakable_nick(raw, True, 20) == "兴旺的大刘啊"
    assert speakable_nick(raw, False, 4) == "兴旺🌹的"        # 关掉只读汉字
    assert speakable_nick("🌹🌹", True, 3) == "直播间的朋友"   # 全 emoji：不硬念
    assert speakable_nick("abc123", True, 3) == "abc"          # 全字母：没有汉字可用，仍读原文
    assert speakable_nick("兴旺🌹的大刘啊", True, 2) == "兴旺"
    assert speakable_nick("", True, 3) == "直播间的朋友"


def test_scheduler_uses_nick_rules_in_phrases():
    cfg = ScheduleConfig(nick_han_only=True, nick_max_chars=3)
    sch, q = _sched(cfg)
    ev = InteractionEvent(kind="member", nick="兴旺🌹的大刘啊")
    out = sch._render(ev)
    assert "兴旺的" in out and "🌹" not in out and "abcdef" not in out
    assert out.count("兴旺的") == 1


# ---------- 每类话术多条随机、随时改 ----------

def test_phrases_multi_random_and_editable():
    cfg = ScheduleConfig(templates={
        "follow": ["[昵称] 关注了，谢谢。", "感谢 [昵称] 的关注！", "[昵称] 点了关注，欢迎常来。"],
    })
    sch, q = _sched(cfg)
    seen = set()
    for i in range(40):
        ev = InteractionEvent(kind="follow", nick=f"朋友{i}", t=time.time())
        seen.add(sch._render(ev))
    assert len(seen) >= 3                       # 三条都出现过
    assert all(s for s in seen)                 # 没有空话术
    # 随时改：换 cfg.templates 立刻生效
    cfg.templates["follow"] = ["新话术一条"]
    assert sch._render(InteractionEvent(kind="follow", nick="甲")) == "新话术一条"


def test_badge_kind_has_templates_and_is_a_known_kind():
    assert "badge" in INTERACTION_KINDS
    sch, q = _sched()
    out = sch._render(InteractionEvent(kind="badge", nick="小明"))
    assert out and "小明" in out and "[" not in out


def test_phrase_without_needed_data_falls_back_to_safe_one():
    cfg = ScheduleConfig(templates={"gift": ["感谢[昵称]送的[礼物]", "谢谢主播的礼物支持"]})
    sch, q = _sched(cfg)
    out = sch._render(InteractionEvent(kind="gift", nick="甲", gift="", count=1))
    assert "[礼物]" not in out
    assert out in ("谢谢主播的礼物支持", "谢谢甲的礼物支持") or "礼物" in out


# ---------- 随机报时 ----------

def test_random_time_announcement_respects_interval_and_gap():
    now = time.time()
    cfg = ScheduleConfig(announce_time=True, announce_interval_s=(60, 61), min_gap_s=0)
    sch, q = _sched(cfg)
    out, fired_at = None, None
    for i in range(200):                     # 第一次边界只是排期，真报时要等到 interval 之后
        out = sch.on_boundary(now + i, False)
        if out:
            fired_at = i
            break
    assert out and "点" in out and "[" not in out
    assert 60 <= fired_at <= 61              # 落在设定的随机区间里
    assert sch.announced_count == 1
    assert sch._next_announce_at > now + fired_at
    assert sch.on_boundary(now + fired_at + 1, False) is None


def test_time_announcement_off_by_default():
    cfg = ScheduleConfig(announce_time=False)
    sch, q = _sched(cfg)
    now = time.time()
    for i in range(30):
        assert sch.on_boundary(now + i, False) is None


def test_time_announcement_attaches_to_interaction_insert():
    """互动一直排队时，报时到点就跟着这次插播一起说（不会永远抢不到边界）。"""
    now = time.time()
    cfg = ScheduleConfig(announce_time=True, announce_interval_s=(10, 11), min_gap_s=0,
                         window_s=(0, 1))
    sch, q = _sched(cfg)
    combined = None
    for i in range(40):
        q.push(InteractionEvent(kind="member", nick=f"朋友{i}", t=now + i))   # 每句边界都有新互动
        out = sch.on_boundary(now + i, False)
        if out and "点" in out:
            combined = out
            break
    assert combined and "点" in combined and "朋友" in combined   # 互动 + 报时同一次插播
    assert sch.announced_count == 1


def test_time_templates_are_custom():
    cfg = ScheduleConfig(announce_time=True, announce_interval_s=(1, 2), min_gap_s=0,
                         time_templates=["自定义报时：[月]月[日]日[时]时。"])
    sch, q = _sched(cfg)
    now = time.time()
    got = None
    for i in range(30):
        got = sch.on_boundary(now + i, False)
        if got:
            break
    assert got and got.startswith("自定义报时") and "月" in got
    assert DEFAULT_TIME_TEMPLATES


def test_mark_render_uses_global_placeholders():
    sch, q = _sched()
    q.push(InteractionEvent(kind="chat", nick="观众甲", content="怎么接流量"))
    out = sch.render_mark("刚看到 [昵称] 问：[内容]，现在是[时点][时分]，我们展开讲。",
                          q.items[-1])
    assert "观众甲" in out and "怎么接流量" in out and "[" not in out


# ---------- 模型 / 音色目录 ----------

def test_model_catalog_lists_switchable_models():
    names = model_names()
    for want in ("cosyvoice-v3.5-flash", "cosyvoice-v3.5-plus", "cosyvoice-v3-flash", "cosyvoice-v2"):
        assert want in names
    cat = catalog()
    v35 = model_info("cosyvoice-v3.5-flash")
    assert v35["needs_custom_voice"] is True
    # v3.5 没有任何系统音色（实测），v3-flash 有
    by = {m["model"]: m for m in cat}
    assert by["cosyvoice-v3.5-flash"]["system_voices"] == []
    assert len(by["cosyvoice-v3-flash"]["system_voices"]) > 5
    assert default_voice_for("cosyvoice-v3.5-flash") == ""
    assert default_voice_for("cosyvoice-v2")


def test_voice_is_bound_to_model():
    assert is_usable_voice("cosyvoice-v2", "longxiaochun_v2")
    assert not is_usable_voice("cosyvoice-v2", "longanyang")          # 实测 418
    assert is_usable_voice("cosyvoice-v3-flash", "longanyang")
    # 自建音色（复刻/设计）以模型名开头 → 放行
    assert is_usable_voice("cosyvoice-v3.5-flash",
                           "cosyvoice-v3.5-flash-vd-design2-e03e0791b4694b389ccb409683c73660")
    assert is_usable_voice("cosyvoice-v2", "cosyvoice-v2-xiangge-09dbe79898194df79c28caddc072eb25")
    assert not is_usable_voice("cosyvoice-v2", "")


def test_orchestrator_rejects_bad_model_voice_pair(tmp_path):
    from airadio.orchestrator import Orchestrator
    orch = Orchestrator("550620363313", "测试稿。\n第二句。", sink="dry")
    try:
        orch.update_params(model="cosyvoice-v3.5-flash", voice="longxiaochun_v2")
        raise AssertionError("应该拒绝：v3.5 没有系统音色")
    except ValueError as e:
        assert "没有系统音色" in str(e)
    orch.update_params(model="cosyvoice-v3.5-flash",
                       voice="cosyvoice-v3.5-flash-vd-x-1234")   # 自建音色可以
