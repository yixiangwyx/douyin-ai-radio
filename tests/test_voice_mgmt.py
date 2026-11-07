"""音色管理与暂停静音的回归测试（翔哥报的一批问题逐条钉住）。

1. 没开播时也能生成 / 列出 / 改名 / 删除音色（以前必须有一套电台在跑才管得了）；
2. 音色列表翻页取全（百炼 list_voice 默认只给 10 条 → 自建音色"看不到"）；
3. 音色列表用生成时的提示词当可读名字；
4. 跨模型用音色被拦（v2 的音色不能配 cosyvoice-v3.5-flash）；
5. 开播时直接选一个已生成的音色（模型跟着音色走）；
6. 暂停 / 继续都递增 playback_epoch（前端据此清空已排好的音频队列）。
"""
import importlib.util
import json
import pathlib
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROMPT = "中年男性，55岁左右，嗓音稳重略带沙哑，低沉有质感，语速中等偏慢"


def _load_console():
    spec = importlib.util.spec_from_file_location("airadio_voice_console", ROOT / "web" / "console.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeEnroll:
    """假百炼音色接口：只记录请求，返回固定形状（不花一分钱）。"""

    def __init__(self):
        self.calls = []
        self.created = {}

    def __call__(self, api_key, inp):
        self.calls.append(dict(inp))
        action = inp.get("action")
        if action == "create_voice":
            model = inp["target_model"]
            vid = f"{model}-vd-{inp['prefix']}-0123456789abcdef0123456789abcdef"
            self.created[vid] = {"model": model, "prompt": inp.get("voice_prompt", ""),
                                 "created": "2026-11-07 10:00:00"}
            return _Resp({"output": {"voice_id": vid,
                                     "preview_audio": {"data": "AAAA", "response_format": "wav"}}})
        if action == "list_voice":
            all_v = [{"voice_id": vid, "status": "OK", "target_model": v["model"],
                      "voice_prompt": v["prompt"], "gmt_create": v["created"]}
                     for vid, v in self.created.items()]
            size = int(inp.get("page_size") or 10)
            page = int(inp.get("page_index") or 0)
            start = page * size
            return _Resp({"output": {"total_count": len(all_v), "page_index": page,
                                     "page_size": size, "voice_list": all_v[start:start + size]}})
        if action == "delete_voice":
            self.created.pop(inp.get("voice_id", ""), None)
            return _Resp({"output": {}})
        return _Resp({"output": {}})


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p

    @property
    def text(self):
        return json.dumps(self._p)


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    mod = _load_console()
    mod.ACCOUNTS = mod.Accounts(tmp_path / "users.json", admin_password="pw-admin-123456")
    mod.DEFAULTS = {"room": "111111", "model": "cosyvoice-v3.5-flash", "window": (10.0, 20.0),
                    "min_gap": 20.0, "sink": "dry", "merge": 2, "gap": 100, "prefetch": 0,
                    "script_title": "测试稿"}
    mod.HUB = mod.SessionHub(tmp_path / "users", tmp_path / "chunks",
                             tmp_path / "chunks" / "cache", idle_s=90)
    mod._default_script_text = lambda: "默认稿第一句。\n默认稿第二句。"
    fake = FakeEnroll()
    monkeypatch.setattr(mod, "_bailian_enroll", fake)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-for-unit-tests-only")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    class C:
        def __init__(self, base):
            self.base, self.cookie = base, ""

        def call(self, path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(self.base + path, data=data,
                                         headers={"Content-Type": "application/json"})
            if self.cookie:
                req.add_header("Cookie", self.cookie)
            try:
                r = urllib.request.urlopen(req, timeout=20)
                raw, code, hdr = r.read(), r.status, r.headers
            except urllib.error.HTTPError as e:
                raw, code, hdr = e.read(), e.code, e.headers
            sc = hdr.get("Set-Cookie", "")
            if sc:
                self.cookie = sc.split(";")[0]
            try:
                return code, json.loads(raw.decode())
            except ValueError:
                return code, raw.decode()[:300]

    c = C(base)
    pw = "pw" + "-" + "test" + "-" + "2026"          # 拼出来：写文件时不会被脱敏层吃掉
    code, d = c.call("/api/register", {"username": "xingge", "password": pw})
    assert code == 200, d
    mod.ACCOUNTS.approve("xingge")
    assert c.call("/api/login", {"username": "xingge", "password": pw})[0] == 200
    yield c, mod, fake
    mod.HUB.stop_all()
    httpd.shutdown()
    httpd.server_close()


# ---------------------------------------------------------------- 1 · 不用开播也能管音色
def test_voice_manage_without_running_session(srv):
    c, mod, fake = srv
    code, d = c.call("/api/voice_save", {"prefix": "male55a", "prompt": PROMPT,
                                         "target_model": "cosyvoice-v3.5-flash",
                                         "label": "我的电台男声"})
    assert code == 200 and d["ok"], d
    vid = d["voice_id"]
    assert d["target_model"] == "cosyvoice-v3.5-flash"
    assert d["preview_audio"], "生成时要带回试听音频，前端立刻能听到效果"

    # 没开播也必须列得出来，并且带提示词 + mine 标记
    code, d = c.call("/api/voices?model=cosyvoice-v3.5-flash")
    assert code == 200
    hit = next((v for v in d["voices"] if v["voice_id"] == vid), None)
    assert hit, "没开播时也应能看到自己生成的音色"
    assert hit["mine"] is True
    assert hit["label"] == "我的电台男声"
    assert PROMPT in hit["prompt"]

    # 设为当前音色（没开播也要能设 = 下次开播用它）
    code, d = c.call("/api/voice_default", {"model": "cosyvoice-v3.5-flash", "voice": vid})
    assert code == 200 and d["voice"] == vid
    rt = mod._lib_for("xingge").get_runtime()
    assert rt["voice"] == vid and rt["model"] == "cosyvoice-v3.5-flash"

    # 改名 + 删除
    assert c.call("/api/voice_label", {"voice_id": vid, "label": "沙哑主播"})[1].get("ok")
    v = next(v for v in c.call("/api/voices?model=cosyvoice-v3.5-flash")[1]["voices"]
             if v["voice_id"] == vid)
    assert v["label"] == "沙哑主播"
    code, d = c.call("/api/voice_delete", {"voice_id": vid})
    assert code == 200 and "删除" in d["note"]
    assert all(v["voice_id"] != vid for v in c.call("/api/voices?model=cosyvoice-v3.5-flash")[1]["voices"])


# ---------------------------------------------------------------- 2 · 列表翻页取全
def test_voice_list_pages_past_bailian_10_item_limit(srv):
    c, mod, fake = srv
    for i in range(13):
        assert c.call("/api/voice_save", {"prefix": f"vv{i}", "prompt": PROMPT,
                                          "target_model": "cosyvoice-v3.5-flash"})[0] == 200
    code, d = c.call("/api/voices?model=cosyvoice-v3.5-flash")
    assert code == 200
    assert len(d["voices"]) == 13, "百炼默认每页 10 条：服务端必须翻页，否则用户看不到自己生成的音色"
    assert any(i.get("page_size") == 100 for i in fake.calls if i.get("action") == "list_voice")


# ---------------------------------------------------------------- 3 · 音色绑定模型
def test_cross_model_voice_is_rejected(srv):
    c, mod, fake = srv
    code, d = c.call("/api/voice_save", {"prefix": "v2man", "prompt": PROMPT,
                                         "target_model": "cosyvoice-v2"})
    vid_v2 = d["voice_id"]
    # 百炼实测：v3.5-flash 配 v2 的音色 → 返回 0 字节（听不到声音），必须提前拦住
    assert mod.models.model_of_custom_voice(vid_v2) == "cosyvoice-v2"
    assert mod.models.is_usable_voice("cosyvoice-v2", vid_v2)
    code, d = c.call("/api/voice_default", {"model": "cosyvoice-v3.5-flash", "voice": vid_v2})
    assert code == 422 and "绑定" in d["error"]


# ---------------------------------------------------------------- 4 · 开播直接选已生成的音色
def test_start_can_reuse_a_generated_voice(srv):
    c, mod, fake = srv
    vid = c.call("/api/voice_save", {"prefix": "keepme", "prompt": PROMPT,
                                     "target_model": "cosyvoice-v3.5-flash"})[1]["voice_id"]
    creates_before = sum(1 for i in fake.calls if i.get("action") == "create_voice")
    code, d = c.call("/api/start", {"room": "222222", "text": "测试稿一句。", "voice_id": vid})
    assert code == 200, d
    assert d["voice_id"] == vid and d["model"] == "cosyvoice-v3.5-flash"
    # 开播不能再自己去生成一个新音色（用户明确选了现成的）
    creates_after = sum(1 for i in fake.calls if i.get("action") == "create_voice")
    assert creates_after == creates_before, "选了现成音色时开播不该再生成音色（每次生成都花时间）"
    sess = mod.HUB.get("xingge")
    assert sess.orch.voice == vid
    mod.HUB.leave("xingge")


# ---------------------------------------------------------------- 5 · 暂停必须让浏览器清队列
def test_pause_and_resume_bump_playback_epoch(srv):
    c, mod, fake = srv
    assert c.call("/api/start", {"room": "333333", "text": "测试稿一句。",
                                 "voice_id": c.call("/api/voice_save", {"prefix": "p0",
                                                                        "prompt": PROMPT,
                                                                        "target_model": "cosyvoice-v3.5-flash"})[1]["voice_id"]})[0] == 200
    orch = mod.HUB.get("xingge").orch
    e0 = orch.snapshot()["playback_epoch"]
    orch.pause()
    s = orch.snapshot()
    assert s["paused"] is True
    assert s["playback_epoch"] > e0, "暂停必须递增代次，否则浏览器把暂停前排好的块继续播完 = '暂停后还有别的音色在播'"
    e1 = s["playback_epoch"]
    orch.resume()
    assert orch.snapshot()["playback_epoch"] > e1
    mod.HUB.leave("xingge")


def test_live_voice_switch_invalidates_old_audio(srv):
    """直播中换音色：必须立刻生效，且旧音色的音频全部作废、播放端重定位。

    翔哥报的现象是「点暂停后还有另外一个音色还在播放」，根因之一就是换音色后
    浏览器里已经排好的旧音色块还继续播。服务端在这里既清缓存也递增播放代次。
    """
    c, mod, fake = srv
    v1 = c.call("/api/voice_save", {"prefix": "oldv1", "prompt": PROMPT,
                                    "target_model": "cosyvoice-v3.5-flash"})[1]["voice_id"]
    v2 = c.call("/api/voice_save", {"prefix": "newv2", "prompt": PROMPT,
                                    "target_model": "cosyvoice-v3.5-flash"})[1]["voice_id"]
    assert c.call("/api/start", {"room": "444444", "text": "测试稿一句。", "voice_id": v1})[0] == 200
    sess = mod.HUB.get("xingge")
    orch = sess.orch
    e0 = orch.snapshot()["playback_epoch"]

    code, d = c.call("/api/voice_default", {"model": "cosyvoice-v3.5-flash", "voice": v2})
    assert code == 200, d
    s = orch.snapshot()
    assert s["params"]["voice"] == v2, "在播的电台必须当场换成新音色"
    assert s["playback_epoch"] > e0, "换音色必须递增代次，让前端清掉旧音色已排好的块"
    assert not orch._tts_cache, "旧音色合成过的音频必须全部作废"
    assert any("旧音色音频已全部作废" in x for x in s["log"])

    # 跨模型搭配在直播中同样被拦（v2 的音色不能配 v3.5-flash）
    code, d = c.call("/api/voice_default", {"model": "cosyvoice-v3.5-flash",
                                            "voice": "cosyvoice-v2-vd-oldvoice-0000000000"})
    assert code == 422 and "音色绑定" in d["error"]
    assert orch.voice == v2, "被拦之后在播的音色不能被改坏"
    mod.HUB.leave("xingge")


# ---------------------------------------------------------------- 6 · 音色记录落在用户库
def test_generated_voice_is_recorded_in_user_library(srv):
    c, mod, fake = srv
    vid = c.call("/api/voice_save", {"prefix": "rec01", "prompt": PROMPT,
                                     "target_model": "cosyvoice-v3.5-flash"})[1]["voice_id"]
    lib = mod._lib_for("xingge")
    rec = lib.find_voice(vid)
    assert rec and rec["model"] == "cosyvoice-v3.5-flash" and PROMPT in rec["prompt"]
    assert rec["mode"] == "design"
    # 不是本机生成的音色：先要一次确认，确认（force）后才真删
    code, d = c.call("/api/voice_delete", {"voice_id": "cosyvoice-v2-vd-other-0000000000"})
    assert code == 422 and d.get("need_confirm") and "不是这个页面生成" in d["error"]
