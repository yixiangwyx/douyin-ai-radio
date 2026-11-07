"""账号：注册要审批、停用的不能用、密码只存哈希、管理员不能被停用。"""
import json

from airadio.accounts import Accounts, hash_password, valid_username, verify_password


def _acct(tmp_path, pw="admin-test-pw"):
    return Accounts(tmp_path / "users.json", admin_password=pw)


def test_password_hash_not_plaintext(tmp_path):
    salt, digest = hash_password("admin-test-pw")
    assert digest != "admin-test-pw" and len(digest) == 64
    assert verify_password("admin-test-pw", salt, digest)
    assert not verify_password("e5232383", salt, digest)
    # 同一个密码两次哈希不同（有随机盐）
    assert hash_password("admin-test-pw")[1] != digest


def test_admin_seeded_and_cannot_be_disabled(tmp_path):
    a = _acct(tmp_path)
    assert a.status_of("admin") == "active"
    assert a.role_of("admin") == "admin"
    assert a.login("admin", "admin-test-pw")["role"] == "admin"
    assert a.disable("admin") is False           # 管理员不能被停用
    assert a.delete_user("admin") is False
    # 密码不能明文落盘
    raw = json.loads((tmp_path / "users.json").read_text(encoding="utf-8"))
    assert "admin-test-pw" not in json.dumps(raw)


def test_register_is_pending_and_blocked_until_approved(tmp_path):
    a = _acct(tmp_path)
    out = a.register("xiangge01", "abc12345")
    assert out["status"] == "pending"
    rejected = a.login("xiangge01", "abc12345")       # 没批准不能登录
    assert isinstance(rejected, tuple) and rejected[0] == 403
    assert "批准" in rejected[1]
    assert a.approve("xiangge01") is True
    ok = a.login("xiangge01", "abc12345")
    assert ok["token"] and a.verify_token(ok["token"]) == "xiangge01"


def test_disabled_account_cannot_use_any_feature(tmp_path):
    a = _acct(tmp_path)
    a.register("user2", "abc12345")
    a.approve("user2")
    token = a.login("user2", "abc12345")["token"]
    assert a.verify_token(token) == "user2"           # token 仍然有效（谁在请求）
    assert a.disable("user2") is True
    assert a.status_of("user2") == "disabled"
    # 控制台靠 status 拦下：停用的账号任何 API 都不能用
    assert a.login("user2", "abc12345")[0] == 403
    assert "停用" in a.login("user2", "abc12345")[1]
    assert a.enable("user2") is True                  # 重新启用后能登录
    assert a.login("user2", "abc12345")["ok"] is True


def test_token_tamper_and_expiry(tmp_path):
    a = _acct(tmp_path)
    a.register("u3", "abc12345"); a.approve("u3")
    tok = a.login("u3", "abc12345")["token"]
    assert a.verify_token(tok) == "u3"
    name, issued, sig = tok.split("|")
    assert a.verify_token(f"admin|{issued}|{sig}") == ""      # 换用户名 → 签名不对
    assert a.verify_token(f"{name}|{issued}|{'0'*32}") == ""  # 换签名
    assert a.verify_token("") == ""
    old = a.issue_token("u3")                                  # 造一个 31 天前签发的
    a.data["users"]  # noqa
    import time as _t
    stale = f"u3|{int(_t.time()) - 31*86400}|" + old.split("|")[2]
    # 签名按新时间戳算过才算数：先签一个旧时间戳的
    import hmac
    sig = hmac.new(a.data["secret"].encode(), f"u3|{int(_t.time()) - 31*86400}".encode(), "sha256").hexdigest()[:32]
    assert a.verify_token(f"u3|{int(_t.time()) - 31*86400}|{sig}") == ""


def test_bad_inputs_rejected(tmp_path):
    a = _acct(tmp_path)
    assert a.register("中文用户名", "abc12345")[0] == 422
    assert a.register("u", "123")[0] == 422                   # 密码太短
    a.register("dup", "abc12345")
    assert a.register("dup", "abc12345")[0] == 409
    assert valid_username("ok_name-1") is True
    assert valid_username("bad name") is False


def test_password_change_and_list(tmp_path):
    a = _acct(tmp_path)
    a.register("u4", "abc12345"); a.approve("u4")
    assert a.set_password("u4", "newpass9") is True
    assert a.login("u4", "abc12345")[0] == 401
    assert a.login("u4", "newpass9")["ok"] is True
    names = [u["username"] for u in a.list_users()]
    assert names[0] == "admin"                    # 管理员排在最前
    assert "u4" in names
    assert a.set_password("u4", "1") is False
