"""AI 生成新稿接播：讲到一半后台生成、播完自动接、每稿只用一次不保留；开播默认音色 male55。"""
import importlib.util
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


class FakeTTS:
    def __init__(self):
        self.calls = 0
        self.last_error = ""

    def synth(self, texts, timeout_ms=30000):
        self.calls += 1
        return b"audio:" + " ".join(texts).encode()


def _mk_orch(tmp_path, script):
    from airadio.orchestrator import Orchestrator
    o = Orchestrator("0", script, sink="dry", out_dir=str(tmp_path))
    o.tts = FakeTTS()
    o.trim_silence = False
    o.merge_sentences = 1
    o.prefetch_ahead = 0
    return o


def test_half_script_triggers_autogen_hook(tmp_path):
    o = _mk_orch(tmp_path, "第一句。\n第二句。\n第三句。\n第四句。")
    fired = []
    o.set_ai_feed(gen_hook=lambda: fired.append(1), at=0.5)
    o._play_pass()                      # 4 段全部过边界
    assert len(fired) == 1             # 到 50% 只触发一次，不会每段都触发


def test_finished_script_switches_to_generated_one(tmp_path):
    o = _mk_orch(tmp_path, "旧稿第一句。\n旧稿第二句。")
    queue = [{"title": "AI稿A", "text": "新稿第一句。\n新稿第二句。"}]
    o.set_ai_feed(feeder=lambda: queue.pop(0) if queue else None, at=0.5)
    o._script_is_one_shot = True
    assert o._next_generated_script() is True
    assert "新稿第一句。" in o.script_text
    assert o.script_title == "AI稿A"
    assert o._seg_i == 0
    assert "AI稿A" in o.played_titles
    # 队列空了：不会凭空造稿
    assert o._next_generated_script() is False


def test_one_shot_script_is_not_looped_when_gen_pending(tmp_path):
    """AI稿播完而下一稿还在生成：边界等待，不重播已用完的稿。"""
    o = _mk_orch(tmp_path, "AI稿唯一句。")
    # 不真连抖音：把采集器换成本地桩，只测接播逻辑
    class StubCollector:
        web_rid = "0"
        room = None
        gift_available = False

        def __init__(self):
            self._thread = None

        def start(self):
            pass

        def stop(self):
            pass

        def set_login_cookie(self, c):
            pass
    o.collector = StubCollector()
    o.status = "connected"
    o.set_ai_feed(feeder=lambda: None, gen_hook=lambda: None, at=0.99)
    o.gen_busy = lambda: True
    o._script_is_one_shot = True
    o._seg_i = len(o.segments)                     # 已经整稿播完
    assert o._script_progress() >= 1.0

    # 把节拍等待改成空转，避免真等 150 秒
    o._sleep = lambda s: True
    queue = [{"title": "AI稿B", "text": "接上新稿句。"}]
    calls = {"n": 0}

    def feeder():
        calls["n"] += 1
        return queue.pop(0) if (calls["n"] > 1 and queue) else None

    o.script_feeder = feeder
    busy_calls = {"n": 0}

    def busy():
        busy_calls["n"] += 1
        return busy_calls["n"] <= 1                # 忙一次就完成（模拟生成好）

    o.gen_busy = busy
    o.run(max_loops=1, loop_pause_s=0.05)
    assert "接上新稿句。" in o.script_text          # 等到新稿接上，没有重播已用完稿
    assert o.script_title == "AI稿B"
    assert o._script_is_one_shot is True


def test_generated_script_avoids_repeating_played_titles(tmp_path):
    from airadio import generate as gen
    captured = {}

    class Msg:
        content = "换一个主题的新稿子第一行。\n换一个主题的新稿子第二行。"

    class Choice:
        message = Msg()

    class Out:
        choices = [Choice()]

    class R:
        status_code = 200
        code = ""
        message = ""
        output = Out()

    def fake_sys_gen(**kw):
        captured["user"] = kw["messages"][1]["content"]
        return R()

    import dashscope
    real = dashscope.Generation.call
    dashscope.Generation.call = staticmethod(fake_sys_gen)
    try:
        text, model = gen.generate_script_llm({"backend": "dashscope", "model": "m",
                                               "api_key": "sk-x", "lines": 10},
                                              avoid=["相亲女 第2版"])
    finally:
        dashscope.Generation.call = real
    assert "相亲女 第2版" in captured["user"]          # 生成指令里带了"本场播过什么"
    assert model == "m"
    assert "换一个主题的新稿子第一行。" in text


# ---------------- HTTP 层 ----------------
def _load_console():
    spec = importlib.util.spec_from_file_location("airadio_console_gen", ROOT / "web" / "console.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def srv(tmp_path):
    yield from _start_srv(tmp_path, local=False)


@pytest.fixture()
def srv_local(tmp_path):
    yield from _start_srv(tmp_path, local=True)


def _start_srv(tmp_path, local):
    mod = _load_console()
    mod.LOCAL_MODE = local
    mod.ACCOUNTS = mod.Accounts(tmp_path / "users.json", admin_password="admin-test-pw")
    mod.DEFAULTS = {"room": "", "model": "cosyvoice-v2", "window": (10.0, 20.0), "min_gap": 20.0,
                    "sink": "dry", "merge": 2, "gap": 100, "prefetch": 0, "script_title": "测试稿"}
    mod.HUB = mod.SessionHub(tmp_path / "users", tmp_path / "chunks",
                             tmp_path / "chunks" / "cache", idle_s=5)
    mod.HUB.wire_hook = mod._wire_generator
    mod._default_script_text = lambda: "默认稿第一句。\n默认稿第二句。"
    # 假"大模型"：固定产出一段稿，别真联网
    def fake_llm(cfg, *, avoid=None, system_override=None, api_key_fallback=None):
        return ("生成的新稿第一行。\n生成的新稿第二行。", "fake-model")
    real_llm = mod.gen.generate_script_llm
    mod.gen.generate_script_llm = fake_llm
    # 假音色注册：不许真调百炼（.env 里有真 Key，真调会花钱）；顺便记下请求内容供断言
    mod.ENROLL_CALLS = []

    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"output": {"voice_id": "cosyvoice-v2-fake123"}}
    mod._bailian_enroll = lambda api_key, inp: (mod.ENROLL_CALLS.append(inp), FakeResp())[1]
    mod.HUB.start_reaper()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"

    class C:
        def __init__(self, base):
            self.base = base
            self.cookie = ""

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
                parsed = json.loads(raw.decode())
            except ValueError:
                parsed = raw.decode()
            return code, parsed

    c = C(base)
    yield c, mod
    mod.HUB.stop_all()
    httpd.shutdown()
    httpd.server_close()
    mod.gen.generate_script_llm = real_llm


def _approve(mod, name):
    mod.ACCOUNTS.approve(name)


def test_local_mode_needs_no_login(srv_local):
    c, mod = srv_local
    code, d = c.call("/me")
    assert code == 200 and d["ok"] and d["username"] == "local"
    # 没开播时任何 API 也不弹登录：返回 410 need_start（让前端引导点「开始连续播放」）
    code, d = c.call("/api/state")
    assert code == 410 and d.get("need_start")
    code, d = c.call("/api/start", {"room": "54235570430", "skip_voice": True})
    assert code == 200, d
    code, d = c.call("/api/state")
    assert code == 200


def test_local_session_is_not_reaped_on_idle(srv_local):
    c, mod = srv_local
    assert c.call("/api/start", {"room": "111111", "skip_voice": True})[0] == 200
    gone = mod.HUB.reap(force_idle_s=0)     # 就算超时也不该被回收
    assert gone == []
    assert c.call("/api/state")[0] == 200


def test_gen_cfg_save_and_templates_survive_without_session(srv):
    c, mod = srv
    c.call("/api/register", {"username": "g1", "password": "abc12345"})
    _approve(mod, "g1")
    assert c.call("/api/login", {"username": "g1", "password": "abc12345"})[0] == 200
    # 电台没在跑也能保存配置（先配置后开播）
    code, d = c.call("/api/gen_cfg_save", {"backend": "dashscope", "model": "qwen3.5-plus",
                                           "prompt": "我的直播框架：先讲A再讲B", "lines": 30,
                                           "auto_generate": True, "auto_generate_at": 0.4})
    assert code == 200 and d["ok"] and d.get("note")
    assert d["saved"]["model"] == "qwen3.5-plus"
    assert d["saved"].get("api_key", "") == ""       # key 不回传
    code, d = c.call("/api/gen_cfg")
    assert code == 200
    assert d["gen_cfg"]["prompt"] == "我的直播框架：先讲A再讲B"
    assert abs(d["gen_cfg"]["auto_generate_at"] - 0.4) < 1e-6
    assert d["default_voice_keyword"] == "male55"
    assert "55" in d["default_voice_desc"] and "沙哑" in d["default_voice_desc"]
    # 提示词模板可保存多套
    code, d = c.call("/api/gen_template_save", {"title": "留人型", "text": "框架一"})
    assert code == 200
    code, d2 = c.call("/api/gen_template_save", {"title": "夜话型", "text": "框架二"})
    assert code == 200 and d2["id"] != d["id"]
    code, d3 = c.call("/api/gen_cfg")
    assert len(d3["gen_templates"]) == 2
    assert c.call("/api/gen_template_delete", {"id": d["id"]})[1]["ok"]
    assert len(c.call("/api/gen_cfg")[1]["gen_templates"]) == 1


def test_start_with_generate_script_is_one_shot(srv):
    c, mod = srv
    c.call("/api/register", {"username": "g2", "password": "abc12345"})
    _approve(mod, "g2")
    assert c.call("/api/login", {"username": "g2", "password": "abc12345"})[0] == 200
    code, d = c.call("/api/start", {"room": "222222", "skip_voice": True,
                                    "generate_script": True, "gen_template": "我的框架"})
    assert code == 200, d
    assert d["generated_script"] is True
    assert d["title"].startswith("AI稿")
    code, s = c.call("/api/state")
    assert code == 200
    assert s["ai_gen"]["one"] is True                 # 开播生成的 AI 稿只用一次
    code, gcfg = c.call("/api/gen_cfg")
    assert gcfg["gen_cfg"].get("auto_generate", True) is True   # 自动接播开着
    assert any("只用一次" in x for x in s["log"])
    assert any("开播生成新稿" in x for x in s["log"])
    # AI 开播稿不进稿子库（用完即弃）
    code, lib = c.call("/api/library")
    assert not any(str(t).startswith("AI稿") for t in [x["title"] for x in lib["scripts"]])


def test_default_voice_keyword_male55(srv):
    c, mod = srv
    c.call("/api/register", {"username": "g3", "password": "abc12345"})
    _approve(mod, "g3")
    assert c.call("/api/login", {"username": "g3", "password": "abc12345"})[0] == 200
    # 不填音色关键字也不 skip_voice：默认 male55 + 55岁稳重沙哑描述，音色名自动生成（关键字+时间码）
    code, d = c.call("/api/start", {"room": "333333"})
    assert code == 200, d
    assert d["voice_prefix"] == "male55"
    req = mod.ENROLL_CALLS[-1]
    assert req["prefix"].startswith("male55") and len(req["prefix"]) <= 9
    assert "55" in req["voice_prompt"] and "沙哑" in req["voice_prompt"]
    assert len(req["preview_text"]) >= 15


def test_mid_script_autogeneration_wires_to_generator(srv):
    """开播后把稿跑到一半：Generator 真的被触发（假模型），队列里能拿到新稿。"""
    c, mod = srv
    c.call("/api/register", {"username": "g4", "password": "abc12345"})
    _approve(mod, "g4")
    assert c.call("/api/login", {"username": "g4", "password": "abc12345"})[0] == 200
    assert c.call("/api/start", {"room": "444444", "skip_voice": True})[0] == 200
    sess = mod.HUB.get("g4")
    g = getattr(sess, "generator", None)
    assert g is not None and sess.orch.script_feeder is not None and sess.orch.auto_gen_hook is not None
    total = sum(1 for s in sess.orch.segments if s.kind in ("speech", "mark"))
    sess.orch._seg_i = total // 2          # 模拟讲到一半
    sess.orch._maybe_autogen()
    deadline = time.time() + 10
    while time.time() < deadline and not g.pending:
        time.sleep(0.1)
    assert g.pending, "半稿触发后 Generator 应该产出新稿"
    item = g.take()
    assert "生成的新稿第一行。" in item["text"]
    assert not g.pending                    # take 一次就没第二份（用完即弃）
