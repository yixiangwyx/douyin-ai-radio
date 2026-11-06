"""会话：每个用户一套电台（互不干扰）；页面关掉/无心跳 → 自动停止，不挂后台。"""
import time

from airadio.accounts import Accounts
from airadio.sessions import SessionHub
from airadio.timeline import ScheduleConfig

SCRIPT = "第一句。\n第二句。"


def _hub(tmp_path, idle=5):
    hub = SessionHub(tmp_path / "users", tmp_path / "chunks", tmp_path / "chunks" / "cache", idle_s=idle)
    hub.start_reaper()
    return hub


def _create(hub, name, room):
    return hub.create(name, room=room, script_text=SCRIPT,
                      schedule=ScheduleConfig(min_gap_s=30, window_s=(10, 20)),
                      model="cosyvoice-v2", voice="longxiaochun_v2", api_key=None, sink="dry")


def test_two_users_get_independent_state(tmp_path):
    hub = _hub(tmp_path)
    a = _create(hub, "userA", "111111")
    b = _create(hub, "userB", "222222")
    assert a.orch is not b.orch
    assert a.lib is not b.lib
    # 各自的房间
    assert a.orch.collector.web_rid == "111111"
    assert b.orch.collector.web_rid == "222222"
    # A 改稿不影响 B
    a.orch.update_script("A的新稿一句。\n再来一句。", apply_now=True)
    a.orch.apply_pending_script()
    assert "A的新稿一句" not in b.orch.get_script()
    assert "第一句。" in b.orch.get_script()
    # 各自的输出目录（manifest 不互相覆盖）
    assert a.out_dir != b.out_dir
    assert hub.count() == 2
    hub.stop_all()


def test_same_user_reuses_one_session(tmp_path):
    hub = _hub(tmp_path)
    a1 = _create(hub, "userA", "111111")
    a2 = hub.get("userA")
    assert a2 is a1
    assert hub.count() == 1
    hub.stop_all()


def test_idle_session_is_stopped_not_left_running(tmp_path):
    """"前端关闭就停止"：超过 idle 秒没有请求（= 没有心跳）就停掉线程与采集。"""
    hub = _hub(tmp_path, idle=5)
    s = _create(hub, "ghost", "333333")
    assert s.thread.is_alive()
    time.sleep(6.5)
    gone = hub.reap()
    assert gone == ["ghost"]
    assert hub.count() == 0
    assert s.orch._stop.is_set()             # 播放线程被叫停
    assert s.orch.collector._stop.is_set()   # 抖音采集也停了
    assert hub.get("ghost") is None
    hub.stop_all()


def test_leave_stops_immediately(tmp_path):
    hub = _hub(tmp_path, idle=3600)
    s = _create(hub, "userX", "444444")
    assert hub.leave("userX") is True
    assert s.orch._stop.is_set()
    assert hub.count() == 0
    assert hub.leave("userX") is False       # 第二次离开没什么可停
    hub.stop_all()


def test_paused_session_is_not_reaped(tmp_path):
    """用户主动暂停 = 明确要保留（也不请求百炼），不按空闲回收。"""
    hub = _hub(tmp_path, idle=5)
    s = _create(hub, "paused", "555555")
    s.orch.pause()
    time.sleep(6.5)
    assert hub.reap() == []
    assert hub.count() == 1
    hub.leave("paused")
    hub.stop_all()


def test_per_user_library_and_douyin_cookie(tmp_path):
    hub = _hub(tmp_path)
    a = _create(hub, "keepA", "666666")
    b = _create(hub, "keepB", "777777")
    a.lib.add_script("A的稿", "A只有一句。", source="测试")
    assert b.lib.data["scripts"] == []            # 稿子库按用户隔离
    assert [s["title"] for s in a.lib.data["scripts"]] == ["A的稿"]
    a.lib.save_douyin_cookie("ttwid=abc; sessionid_ss=xyz")
    assert a.lib.get_douyin_cookie().startswith("ttwid=abc")
    assert b.lib.get_douyin_cookie() == ""
    hub.stop_all()


def test_shared_audio_cache_dir_is_configured(tmp_path):
    """生成缓存跨用户共享：同稿同音色 B 不用再花一次钱。"""
    hub = _hub(tmp_path)
    a = _create(hub, "c1", "888888")
    b = _create(hub, "c2", "999999")
    assert a.orch.shared_cache_dir == b.orch.shared_cache_dir
    assert a.orch.out_dir != b.orch.out_dir      # 但播放块/manifest 各自一份
    hub.stop_all()
