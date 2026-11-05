"""sign.js 签名生成：MD5(模板参数) → get_sign(md5hex)。与 DouyinLiveWebFetcher 同路线。"""
from __future__ import annotations

import hashlib
import os
import urllib.parse

_SIGN_JS = os.path.join(os.path.dirname(__file__), "..", "..", "vendor", "sign.js")

PARAMS = ("live_id,aid,version_code,webcast_sdk_version,"
          "room_id,sub_room_id,sub_channel_id,did_rule,"
          "user_unique_id,device_platform,device_type,ac,"
          "identity").split(",")

_ctx = None


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


def signature_for(wss_url: str) -> str:
    query = urllib.parse.urlparse(wss_url).query
    wss_maps = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
    tpl = ",".join(f"{k}={wss_maps.get(k, '')}" for k in PARAMS)
    md5_hex = hashlib.md5(tpl.encode()).hexdigest()
    ctx = _get_ctx()
    return ctx.call("get_sign", md5_hex)
