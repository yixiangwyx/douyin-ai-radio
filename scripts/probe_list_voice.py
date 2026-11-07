"""探针2：看 list_voice 的真实返回结构（前端音色列表靠它）。"""
import json
import os

import requests

API = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
PREVIEW = "大家好，欢迎来到我的直播间，今天我们来聊聊直播间的留人方法。"
PROMPT = "中年男性，55岁左右，嗓音稳重略带沙哑，低沉有质感，语速中等偏慢"


def call(inp):
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    r = requests.post(API, headers={"Authorization": "Bearer " + key,
                                    "Content-Type": "application/json"},
                      json={"model": "voice-enrollment", "input": inp}, timeout=120)
    print("input:", json.dumps(inp, ensure_ascii=False), "->", r.status_code)
    try:
        print(json.dumps(r.json(), ensure_ascii=False)[:1500])
    except Exception:
        print("raw:", r.text[:400])
    print()


if __name__ == "__main__":
    call({"action": "list_voice", "target_model": "cosyvoice-v3.5-flash"})
    call({"action": "list_voice", "target_model": "cosyvoice-v3.5-flash", "page_size": 20, "page_index": 1})
    call({"action": "list_voice"})
    d = call({"action": "create_voice", "target_model": "cosyvoice-v3.5-flash",
              "prefix": "probe02", "voice_prompt": PROMPT, "preview_text": PREVIEW})
    vid = None
    try:
        vid = d["output"]["voice_id"]
    except Exception:
        pass
    if vid:
        call({"action": "query_voice", "voice_id": vid})
        call({"action": "list_voice", "target_model": "cosyvoice-v3.5-flash", "page_size": 20, "page_index": 1})
        call({"action": "delete_voice", "voice_id": vid})
