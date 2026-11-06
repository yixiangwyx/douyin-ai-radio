"""sign.js 签名生成：MD5(模板参数) → get_sign(md5hex)。与 DouyinLiveWebFetcher 同路线。

引擎优先 mini-racer；不可用时自动回退到本机 node 子进程（Termux/Docker 等没有
mini-racer 预编译包的环境可用，只要 `pkg install nodejs` 即可）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.parse

_SIGN_JS = os.path.join(os.path.dirname(__file__), "..", "..", "vendor", "sign.js")

PARAMS = ("live_id,aid,version_code,webcast_sdk_version,"
          "room_id,sub_room_id,sub_channel_id,did_rule,"
          "user_unique_id,device_platform,device_type,ac,"
          "identity").split(",")

_ctx = None
_engine: str | None = None  # "mini_racer" | "node"


def _md5_of(wss_url: str) -> str:
    query = urllib.parse.urlparse(wss_url).query
    wss_maps = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
    tpl = ",".join(f"{k}={wss_maps.get(k, '')}" for k in PARAMS)
    return hashlib.md5(tpl.encode()).hexdigest()


def _get_ctx():
    global _ctx
    if _ctx is None:
        try:
            from py_mini_racer import MiniRacer  # type: ignore
        except ImportError:
            from py_mini_racer.py_mini_racer import MiniRacer  # type: ignore
        with open(_SIGN_JS, encoding="utf-8") as f:
            script = f.read()
        _ctx = MiniRacer()
        _ctx.eval(script)
    return _ctx


def _sign_node(md5_hex: str) -> str:
    """node 子进程跑 sign.js：签名与 mini-racer 完全一致（同一份 JS）。"""
    runner = """
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const sandbox = {document: {}, window: {}, navigator: {userAgent: 'Mozilla/5.0'}};
const vm = require('vm');
const ctx = vm.createContext(sandbox);
vm.runInContext(src, ctx);
process.stdout.write(ctx.get_sign(process.argv[3]));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(runner)
        runner_path = f.name
    try:
        out = subprocess.run(
            ["node", runner_path, _SIGN_JS, md5_hex],
            capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            raise RuntimeError(f"node sign failed: {out.stderr[:200]}")
        return out.stdout.strip()
    finally:
        os.unlink(runner_path)


def signature_for(wss_url: str) -> str:
    global _engine
    md5_hex = _md5_of(wss_url)
    if _engine in (None, "mini_racer"):
        try:
            sig = _get_ctx().call("get_sign", md5_hex)
            _engine = "mini_racer"
            return sig
        except Exception:
            _engine = "node" if shutil.which("node") else None
            if _engine is None:
                raise RuntimeError("需要 mini-racer 或 node 来生成抖音签名")
    return _sign_node(md5_hex)

