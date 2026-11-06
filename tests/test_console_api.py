"""控制台 HTTP 层：注册→审批→登录→开播→状态；停用的账号任何 API 都不能用。

这里起的是真的 HTTP 服务（console.py 的 Handler），因为"薄服务端 + 多用户 + 自动停止"
这套东西只有在真实请求路径上才能验证：cookie、401/403/410、按用户隔离的 manifest。
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
    spec = importlib.util.spec_from_file_location("airadio_console", ROOT / "web" / "console.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def srv(tmp_path):
    mod = _load_console()
    mod.ACCOUNTS = mod.Accounts(tmp_path / "users.json", admin_password="e5232382")
    mod.DEFAULTS = {"room": "", "model": "cosyvoice-v2", "window": (10.0, 20.0), "min_gap": 20.0,
                    "sink": "dry", "merge": 2, "gap": 100, "prefetch": 0, "script_title": "测试稿"}
    mod.HUB = mod.SessionHub(tmp_path / "users", tmp_path / "chunks",
                             tmp_path / "chunks" / "cache", idle_s=5)
    mod._default_script_text = lambda: "默认稿第一句。\n默认稿第二句。"   # 不依赖仓库里的真实稿
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


def _register_and_approve(c, name, pw="abc12345"):
    """注册（普通用户自己注册）→ 管理员批准 → 登录。返回登录后的用户客户端。"""
    code, d = c.call("/api/register", {"username": name, "password": pw})
    assert code == 200 and d["status"] == "pending"
    admin = _admin_client(c.base)
    code, d = admin.call("/api/admin/approve", {"username": name})      # 管理员批准
    assert code == 200 and d["ok"]
    code, d = c.call("/api/login", {"username": name, "password": pw})
    assert code == 200 and d["token"]
    return c


def test_register_requires_admin_approval_before_use(srv):
    c, mod = srv
    code, d = c.call("/api/register", {"username": "newbie", "password": "abc12345"})
    assert code == 200
    code, d = c.call("/api/login", {"username": "newbie", "password": "abc12345"})
    assert code == 403 and "批准" in d["error"]
    code, d = c.call("/api/state")                    # 没批准也拿不到电台
    assert code == 401
    # 没批准前：未登录的浏览器不能自己批准自己（先被 401 拦下）
    code, d = c.call("/api/admin/approve", {"username": "newbie"})
    assert code == 401
    admin = _admin_client(c.base)
    assert admin.call("/api/admin/approve", {"username": "newbie"})[1]["ok"] is True
    code, d = c.call("/api/login", {"username": "newbie", "password": "abc12345"})
    assert code == 200
    code, d = c.call("/me")
    assert code == 200 and d["username"] == "newbie"


def test_disabled_account_is_blocked_on_every_api(srv):
    c, mod = srv
    _register_and_approve(c, "u1")
    assert c.call("/api/state")[0] == 410 or c.call("/api/state")[0] == 200   # 没开播→410 提示
    code, d = c.call("/api/start", {"room": "111111", "skip_voice": True})
    assert code == 200, d
    assert c.call("/api/state")[0] == 200
    # 管理员停用
    code, d = _admin_client(c.base).call("/api/admin/disable", {"username": "u1"})
    assert code == 200 and d["ok"]
    for path in ("/api/state", "/api/script", "/api/phrases"):
        code, d = c.call(path)
        assert code == 403, (path, code, d)
        assert "停用" in d["error"]
    code, d = c.call("/api/control", {"action": "pause"})
    assert code == 403


def _admin_client(base):
    class A:
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
            return code, json.loads(raw.decode())
    a = A(base)
    assert a.call("/api/login", {"username": "admin", "password": "e5232382"})[0] == 200
    return a


def test_admin_endpoints_require_admin_role(srv):
    c, mod = srv
    _register_and_approve(c, "plain")
    code, d = c.call("/api/admin/users", {})
    assert code == 403 and "管理员" in d["error"]
    admin = _admin_client(c.base)
    code, d = admin.call("/api/admin/users", {})
    assert code == 200
    names = [u["username"] for u in d["users"]]
    assert "admin" in names and "plain" in names
    assert names[0] == "admin"


def test_start_creates_session_and_second_user_is_isolated(srv):
    c, mod = srv
    _register_and_approve(c, "aaa")
    code, d = c.call("/api/start", {"room": "222222", "skip_voice": True,
                                    "text": "A的稿第一句。", "title": "A稿"})
    assert code == 200 and d["sentences"] >= 1
    code, s1 = c.call("/api/state")
    assert code == 200 and s1["room"]["web_rid"] == "222222"
    assert s1["script_title"] == "A稿"

    b = _admin_client(c.base)          # 另一个人（用管理员账号做第二个会话，只为看隔离）
    code, d = b.call("/api/start", {"room": "333333", "skip_voice": True, "text": "B的稿。"})
    assert code == 200
    code, s2 = b.call("/api/state")
    assert s2["room"]["web_rid"] == "333333"
    # 两个人同时在跑：再拉一次状态才看得到 online_users（s1 是第二个人开播前拉的）
    code, s1b = c.call("/api/state")
    assert s1b["session"]["online_users"] == 2
    # 各自 manifest 目录
    code, st = c.call("/api/state")
    assert st["session"]["username"] == "aaa"
    c.call("/api/leave", {})
    code, st = c.call("/api/state")
    assert code == 410 and st["need_start"]
    assert st["error"]


def test_leave_stops_session_immediately(srv):
    c, mod = srv
    _register_and_approve(c, "gone")
    assert c.call("/api/start", {"room": "444444", "skip_voice": True})[0] == 200
    sess = mod.HUB.get("gone")
    assert sess is not None
    assert c.call("/api/leave", {})[1]["ok"] is True
    assert sess.orch._stop.is_set() and sess.orch.collector._stop.is_set()
    assert mod.HUB.get("gone") is None
    assert c.call("/api/state")[0] == 410


def test_phrases_and_script_are_per_user(srv):
    c, mod = srv
    _register_and_approve(c, "ph1")
    assert c.call("/api/start", {"room": "555555", "skip_voice": True,
                                 "text": "第一句。\n第二句。"})[0] == 200
    code, d = c.call("/api/phrases", {"templates": {"gift": ["感谢 [昵称] 的 [礼物]"]},
                                      "nick_max_chars": 4})
    assert code == 200
    code, ph = c.call("/api/phrases")
    assert "感谢 [昵称] 的 [礼物]" in ph["templates"]["gift"]
    assert ph["nick_max_chars"] == 4
    # 另一个用户看不到这份话术
    b = _admin_client(c.base)
    assert b.call("/api/start", {"room": "666666", "skip_voice": True, "text": "另一套稿。"})[0] == 200
    code, ph2 = b.call("/api/phrases")
    assert "感谢 [昵称] 的 [礼物]" not in (ph2["templates"].get("gift") or [])


def test_unauthenticated_gets_login_page_not_console(srv):
    c, mod = srv
    data = urllib.request.urlopen(c.base + "/", timeout=20).read().decode()
    assert "登录" in data and "id=\"btnLogin\"" in data
    code, d = c.call("/api/models")
    assert code == 401
