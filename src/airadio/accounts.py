"""accounts.py — 账号、审批状态、登录 token。

规则（翔哥要求）：
- 用户可自己注册；注册后状态是 pending，**后台管理员批准（active）后才能用**。
- 管理员可以停用（disabled）：停用的账号不能登录、不能请求任何电台功能。
- 管理员账号默认 admin（密码在首次启动时写入，之后可用 /admin 改）。
- 密码只存 pbkdf2 哈希 + 随机盐；token 用 HMAC 签名，不存 session 表。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from pathlib import Path

STATUSES = ("pending", "active", "disabled")
_PBKDF2_ROUNDS = 120_000


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """返回 (salt, hash)。"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), _PBKDF2_ROUNDS)
    return salt, digest.hex()


def verify_password(password: str, salt: str, stored_hash: str) -> bool:
    _, digest = hash_password(password or "", salt)
    return hmac.compare_digest(digest, stored_hash)


def valid_username(name: str) -> bool:
    # 必须是 ASCII 字母/数字/下划线/减号：str.isalnum() 对「中文」也返回 True，
    # 用它会让注册接受中文用户名、而接口错误提示却说不能——两边不一致。
    return bool(name) and len(name) <= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", name) is not None


class Accounts:
    """data/users.json 的读写 + 鉴权。线程安全。"""

    def __init__(self, path: str | Path, admin_password: str = ""):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.data = {"users": [], "secret": secrets.token_hex(32), "admin_password_changed": False}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except (OSError, ValueError):
                pass
        if not self.data.get("secret"):
            self.data["secret"] = secrets.token_hex(32)
        # 首次启动播种管理员（密码不落明文，只存哈希）
        if not any(u.get("role") == "admin" for u in self.data["users"]):
            salt, digest = hash_password(admin_password or "changeme")
            self.data["users"].append({
                "username": "admin", "role": "admin", "status": "active",
                "salt": salt, "hash": digest, "created": time.time(),
                "approved": True, "approved_by": "seed",
            })
            self._save()

    # ---------- 持久化 ----------
    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def _find(self, username: str) -> dict | None:
        for u in self.data["users"]:
            if u.get("username") == username:
                return u
        return None

    # ---------- 注册 / 登录 ----------
    def register(self, username: str, password: str) -> dict | tuple[int, str]:
        username = (username or "").strip()
        if not valid_username(username):
            return (422, "用户名只能用字母、数字、下划线或减号（不超过 32 位）")
        if len(password or "") < 6:
            return (422, "密码至少 6 位")
        with self._lock:
            if self._find(username):
                return (409, "这个用户名已经存在")
            salt, digest = hash_password(password)
            user = {"username": username, "role": "user", "status": "pending",
                    "salt": salt, "hash": digest, "created": time.time()}
            self.data["users"].append(user)
            self._save()
        return {"ok": True, "username": username, "status": "pending"}

    def login(self, username: str, password: str) -> dict | tuple[int, str]:
        user = self._find((username or "").strip())
        if not user or not verify_password(password or "", user.get("salt", ""), user.get("hash", "")):
            return (401, "用户名或密码不对")
        if user.get("status") == "pending":
            return (403, "这个账号还在等管理员批准，批准后就能用了")
        if user.get("status") == "disabled":
            return (403, "这个账号已被停用，需要管理员重新启用才能使用")
        return {"ok": True, "username": user["username"], "role": user.get("role", "user"),
                "token": self.issue_token(user["username"])}

    # ---------- 审批 ----------
    def approve(self, username: str) -> bool:
        return self._set_status(username, "active", approved_by="admin")

    def disable(self, username: str) -> bool:
        return self._set_status(username, "disabled")

    def enable(self, username: str) -> bool:
        return self._set_status(username, "active")

    def _set_status(self, username: str, status: str, approved_by: str = "") -> bool:
        if status not in STATUSES:
            return False
        with self._lock:
            user = self._find(username)
            if not user or user.get("role") == "admin":
                return False      # 管理员不可被停用/删除
            user["status"] = status
            if status == "active":
                user["approved"] = True
                user["approved_by"] = approved_by or "admin"
                user["approved_at"] = time.time()
            self._save()
        return True

    def set_password(self, username: str, password: str) -> bool:
        if len(password or "") < 6:
            return False
        with self._lock:
            user = self._find(username)
            if not user:
                return False
            user["salt"], user["hash"] = hash_password(password)
            self._save()
        return True

    def delete_user(self, username: str) -> bool:
        with self._lock:
            user = self._find(username)
            if not user or user.get("role") == "admin":
                return False
            self.data["users"] = [u for u in self.data["users"] if u.get("username") != username]
            self._save()
        return True

    def list_users(self) -> list[dict]:
        out = []
        for u in self.data["users"]:
            out.append({"username": u["username"], "role": u.get("role", "user"),
                        "status": u.get("status", "pending"),
                        "created": u.get("created", 0), "approved_at": u.get("approved_at", 0),
                        "approved_by": u.get("approved_by", "")})
        out.sort(key=lambda x: (x["role"] != "admin", x["created"]))
        return out

    def status_of(self, username: str) -> str:
        u = self._find(username)
        return u.get("status", "") if u else ""

    def role_of(self, username: str) -> str:
        u = self._find(username)
        return u.get("role", "") if u else ""

    # ---------- token ----------
    def issue_token(self, username: str) -> str:
        payload = f"{username}|{int(time.time())}"
        sig = hmac.new(self.data["secret"].encode(), payload.encode(), "sha256").hexdigest()[:32]
        return f"{payload}|{sig}"

    def verify_token(self, token: str) -> str:
        """返回 username（校验通过）或 ''（不通过）。不校验过期：token 里带签发时间，超 30 天不认。"""
        parts = (token or "").split("|")
        if len(parts) != 3:
            return ""
        username, issued, sig = parts
        payload = f"{username}|{issued}"
        want = hmac.new(self.data["secret"].encode(), payload.encode(), "sha256").hexdigest()[:32]
        if not hmac.compare_digest(want, sig):
            return ""
        try:
            if time.time() - int(issued) > 30 * 86400:
                return ""
        except ValueError:
            return ""
        return username
