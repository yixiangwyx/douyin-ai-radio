#!/usr/bin/env python3
"""端到端真实验证：注册 → 管理员批准 → 登录 → 开播 → 状态 → 停用 → 被拦 → 离开自动停止。
用法: AIRADIO_ADMIN_PASSWORD=*** python3 scripts/e2e_users.py [base_url]   (默认 http://127.0.0.1:8010)
"""
import json
import os
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"


class Client:
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
            r = urllib.request.urlopen(req, timeout=40)
            raw, code, hdr = r.read(), r.status, r.headers
        except urllib.error.HTTPError as e:
            raw, code, hdr = e.read(), e.code, e.headers
        sc = hdr.get("Set-Cookie", "")
        if sc:
            self.cookie = sc.split(";")[0]
        try:
            return code, json.loads(raw.decode())
        except ValueError:
            return code, raw[:200].decode(errors="replace")


def main():
    name = "e2e" + str(int(time.time()))[-5:]
    user = Client(BASE)
    admin = Client(BASE)

    print(f"[1] 注册 {name}（未审批）")
    print("   ", user.call("/api/register", {"username": name, "password": "abc12345"}))
    print("    未批准就登录 →", user.call("/api/login", {"username": name, "password": "abc12345"}))

    print("[2] 管理员登录后台并批准")
    pw = os.environ.get("AIRADIO_ADMIN_PASSWORD", "")
    print("   ", admin.call("/api/login", {"username": "admin", "password": pw})[0])
    code, d = admin.call("/api/admin/users", {})
    pend = [u["username"] for u in d["users"] if u["status"] == "pending"]
    print("    待审批列表:", pend)
    print("    批准:", admin.call("/api/admin/approve", {"username": name}))

    print("[3] 用户登录 → 开播（跳过生成新音色，用现有音色）")
    print("   ", user.call("/api/login", {"username": name, "password": "abc12345"})[0])
    code, d = user.call("/api/start", {"room": "550620363313", "skip_voice": True,
                                       "text": "端到端验证第一句。\n端到端验证第二句。",
                                       "title": "E2E稿"})
    print("    start:", code, {k: d.get(k) for k in ("ok", "sentences", "voice_id", "skipped_voice")})

    print("[4] 状态（自己的房间/稿/音色）")
    code, s = user.call("/api/state?lite=1&log_after=999999")
    print("    state:", code, s.get("room"), s.get("script_title"), s.get("params", {}).get("voice"))
    print("    session:", s.get("session"))

    print("[5] 管理员后台看到几个在跑的电台")
    code, d = admin.call("/api/admin/users", {})
    print("    online:", json.dumps(d.get("online"), ensure_ascii=False)[:300])

    print("[6] 管理员停用该账号 → 该用户任何 API 都被拦")
    print("    disable:", admin.call("/api/admin/disable", {"username": name}))
    for path in ("/api/state", "/api/phrases", "/api/script"):
        print(f"    {path} →", user.call(path)[0])

    print("[7] 重新启用 + 用户主动离开 → 电台立即停止")
    print("    enable:", admin.call("/api/admin/enable", {"username": name})[1])
    print("    用户再登录:", user.call("/api/login", {"username": name, "password": "abc12345"})[0])
    print("    leave:", user.call("/api/leave", {}))
    code, s = user.call("/api/state")
    print("    leave 后 state →", code, s.get("error"))

    print("[8] 删除测试账号")
    print("   ", admin.call("/api/admin/delete", {"username": name}))
    print("完成。")


if __name__ == "__main__":
    main()
