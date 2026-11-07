"""一次性探针：确认百炼「按提示词设计音色」的真实返回形状。

用法: set -a && source .env && set +a && uv run python scripts/probe_design_voice.py
"""
import json
import os

import requests

API = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
PREVIEW = "大家好，欢迎来到我的直播间，今天我们来聊聊直播间的留人方法。"
VOICE_PROMPT = "中年男性，55岁左右，嗓音稳重略带沙哑，低沉有质感，语速中等偏慢"


def call(inp):
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not key:
        print("no DASHSCOPE_API_KEY in env")
        raise SystemExit(2)
    r = requests.post(API, headers={"Authorization": "Bearer " + key,
                                    "Content-Type": "application/json"},
                      json={"model": "voice-enrollment", "input": inp}, timeout=120)
    print("target_model=", inp.get("target_model"), "action=", inp.get("action"), "->", r.status_code)
    try:
        d = r.json()
    except Exception:
        print("raw:", r.text[:300])
        return None
    out = d.get("output", {}) or {}
    print("voice_id:", out.get("voice_id"))
    pa = out.get("preview_audio") or {}
    print("preview_audio keys:", list(pa.keys()), "bytes:", len(str(pa.get("data") or "")))
    if r.status_code != 200:
        print("body:", json.dumps(d, ensure_ascii=False)[:400])
    return d


if __name__ == "__main__":
    for model in ["cosyvoice-v3.5-flash", "cosyvoice-v3-flash"]:
        d = call({"action": "create_voice", "target_model": model,
                  "prefix": "probe01", "voice_prompt": VOICE_PROMPT,
                  "preview_text": PREVIEW, "preferred_name": "probe01"})
        if d and d.get("output", {}).get("voice_id"):
            vid = d["output"]["voice_id"]
            print("  list_voice ->")
            call({"action": "list_voice", "target_model": model})
            print("  delete_voice ->")
            call({"action": "delete_voice", "voice_id": vid})
        print("----")
