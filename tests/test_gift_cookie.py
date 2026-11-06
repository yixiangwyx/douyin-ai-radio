"""抖音礼物消息的登录 Cookie 规则（实测结论的回归测试，不联网）。

实测：游客态（只有 ttwid）能收到 弹幕/进场/关注/点赞/灯牌/房间统计，
但一条 WebcastGiftMessage 都不会有；带含 sessionid_ss 的登录 Cookie 后礼物才出现。
所以：登录 Cookie 必须出现在 WSS 握手的 HTTP header 里（不是 URL 参数）。
"""
import sys

from airadio.events import InteractionQueue
from airadio.live import LOGIN_COOKIE_KEYS, LiveCollector, cookie_has_login


def test_login_cookie_keys_detected():
    assert cookie_has_login("ttwid=abc; sessionid_ss=xyz; sid_tt=xyz") is True
    assert cookie_has_login("ttwid=abc; sessionid=xyz") is True
    assert cookie_has_login("ttwid=abc; __ac_nonce=1; __ac_signature=2") is False
    assert cookie_has_login("") is False
    assert cookie_has_login(None) is False
    # 设备指纹/反爬 Cookie 不能当登录态用
    assert cookie_has_login("ttwid=1%7Cabc; passport_cs0=abc; UIFID_TEMP=abc") is False
    assert "ttwid" not in LOGIN_COOKIE_KEYS


def test_wss_cookie_header_uses_login_cookie():
    """握手 header 的 cookie 串：有登录 Cookie 就整串带上，否则退回 ttwid（游客态）。"""
    q = InteractionQueue()
    c = LiveCollector("123", q, login_cookie="ttwid=mine; sessionid_ss=s1")
    assert c._cookie_header() == "ttwid=mine; sessionid_ss=s1"

    c2 = LiveCollector("123", InteractionQueue())
    c2.room = type("R", (), {"ttwid": "guest"})()
    assert c2._cookie_header() == "ttwid=guest"


def test_set_login_cookie_applies_on_reconnect():
    c = LiveCollector("123", InteractionQueue())
    c.room = type("R", (), {"ttwid": "guest", "logged_in": False})()
    c.set_login_cookie("ttwid=mine; sessionid_ss=s1")
    assert c.login_cookie == "ttwid=mine; sessionid_ss=s1"
    assert c.room.logged_in is True
    assert "sessionid_ss" in c._cookie_header()


def test_gift_messages_are_merged_per_group():
    """同一次送礼上游连发多条（repeat_count 递增）：合并成一条、数量取最大。"""
    from airadio.events import InteractionEvent

    q = InteractionQueue()
    c = LiveCollector("123", q, login_cookie="sessionid_ss=s1")
    for n in (1, 3, 9):
        c._push(InteractionEvent(kind="gift", nick="A", gift="玫瑰", count=n, value=1))
    gifts = [e for e in q.recent(10) if e["kind"] == "gift"]
    assert len(gifts) == 1
    assert gifts[0]["count"] == 9

    # 不同人 / 不同礼物 = 不同事件，不合并
    c._push(InteractionEvent(kind="gift", nick="B", gift="玫瑰", count=1, value=1))
    c._push(InteractionEvent(kind="gift", nick="A", gift="棒棒糖", count=2, value=1))
    gifts = [e for e in q.recent(10) if e["kind"] == "gift"]
    assert len(gifts) == 3


def test_orchestrator_has_cookie_before_collector():
    """构造顺序坑：LiveCollector 需要 self.douyin_cookie，必须在其之前初始化。"""
    from airadio.orchestrator import Orchestrator

    o = Orchestrator("0", "第一句。\n第二句。", sink="dry", out_dir="/tmp/airadio_cookie_test")
    assert hasattr(o, "douyin_cookie")
    assert o.collector.login_cookie == o.douyin_cookie
    o.set_douyin_cookie("ttwid=x; sessionid_ss=y")
    assert o.collector.login_cookie == "ttwid=x; sessionid_ss=y"
    assert o.set_douyin_cookie("ttwid=x") is False      # 没有登录字段
    snap = o.snapshot()
    assert snap["douyin_login"]["cookie_set"] is True
    assert snap["douyin_login"]["has_login"] is False
    o.stop()
