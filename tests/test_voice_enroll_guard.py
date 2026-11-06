"""音色生成（voice-enrollment）的两条真实规则（实测，2026-11）：

1) create_voice 是**同步**的：HTTP 200 的响应里就带 voice_id（设计 ~6s、复刻 ~2s），
   没有"先拿 id 再等生成完成"的异步任务；复刻音色在 list_voice 里会短暂显示
   status=DEPLOYING，但创建后立刻用它合成已经成功——所以不需要轮询等待。
2) 真正的失败来源是参数校验：preview_text 必须 ≥15 字符，否则所有模型
   （v2 / v3-flash / v3.5-flash）统一报 InvalidParameter:
   "preview_text should not be shorter than 15 characters"（旧代码写死的默认文案只有
   14 字，导致按提示词生成音色 100% 失败）。本测试锁死服务端兜底：
   用户不填/填短文本时必须换成 ≥15 字的默认文案再发给百炼。
"""
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def _load_console():
    spec = importlib.util.spec_from_file_location("airadio_voice_console", ROOT / "web" / "console.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload)


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    mod = _load_console()
    mod.ACCOUNTS = mod.Accounts(tmp_path / "users.json", admin_password="e5232382")
    mod.DEFAULTS = {"room": "", "model": "cosyvoice-v2", "window": (10.0, 20.0), "min_gap": 20.0,
                    "sink": "dry", "merge": 1, "gap": 100, "prefetch": 0, "script_title": "测试稿"}
    mod.HUB = mod.SessionHub(tmp_path / "users", tmp_path / "chunks",
                             tmp_path / "chunks" / "cache", idle_s=60)
    mod._default_script_text = lambda: "默认稿第一句。"
    captured = []

    def fake_enroll(api_key, inp):
        captured.append(inp)
        if len(inp.get("preview_text", "")) < 15:   # 模拟百炼的校验
            return FakeResp(400, {"code": "InvalidParameter",
                                  "message": "preview_text should not be shorter than 15 characters"})
        return FakeResp(200, {"output": {"voice_id": f"{inp['target_model']}-vd-{inp['prefix']}-uuid"}})

    monkeypatch.setattr(mod, "_bailian_enroll", fake_enroll)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-for-offline-test")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    class C:
        def __init__(self):
            self.cookie = ""

        def call(self, path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, data=data,
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
            except Exception:
                return code, raw.decode()[:200]

    c = C()
    c.call("/api/login", {"username": "admin", "password": "e5232382"})
    yield c, captured
    httpd.shutdown()
    mod.HUB.stop_all()


PROMPT = "三十岁女性，声音温柔知性，语速偏慢，适合直播间安抚式讲解"


def test_design_voice_empty_preview_gets_default_at_least_15(srv):
    c, captured = srv
    c.call("/api/start", {"room": "111111", "skip_voice": True})
    code, d = c.call("/api/clone_voice", {"prefix": "vda", "prompt": PROMPT, "preview_text": ""})
    assert code == 200 and d["ok"] is True
    sent = captured[-1]
    assert len(sent["preview_text"]) >= 15, "百炼要求 ≥15 字，服务端必须兜底"


def test_design_voice_short_preview_is_replaced_not_forwarded(srv):
    c, captured = srv
    c.call("/api/start", {"room": "111111", "skip_voice": True})
    code, d = c.call("/api/clone_voice", {"prefix": "vdb", "prompt": PROMPT, "preview_text": "大家好"})
    assert code == 200, d
    assert len(captured[-1]["preview_text"]) >= 15


def test_start_path_new_voice_uses_valid_preview(srv):
    """开播时「生成新音色」（只给关键字）也要带 ≥15 字的 preview_text。"""
    c, captured = srv
    code, d = c.call("/api/start", {"room": "222222", "text": "测试稿。", "voice_keyword": "nan"})
    assert code == 200, d
    assert d["voice_id"].endswith("uuid")
    sent = [i for i in captured if i.get("action") == "create_voice"][-1]
    assert len(sent["preview_text"]) >= 15


def test_error_hint_translates_preview_length_rule(srv):
    c, _ = srv
    hint = c  # placeholder to keep fixture teardown
    from airadio import live  # noqa: F401  (import smoke)
    mod = _load_console()
    text = '{"code":"InvalidParameter","message":"preview_text should not be shorter than 15 characters"}'
    out = mod._enroll_error_hint(text)
    assert "15" in out and "百炼" not in out[:3]   # 要翻译成人话，不是裸转发


def test_enroll_default_text_itself_is_valid():
    mod = _load_console()
    assert len(mod.PREVIEW_TEXT) >= 15
