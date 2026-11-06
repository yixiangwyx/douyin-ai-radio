"""生成一个带新鲜签名的 webcast WSS 地址，写进 out/chunks/probe.json（供浏览器直连试验用）。

用途：验证"纯前端（浏览器直接连抖音 webcast WebSocket）"是否可行 —— 浏览器不需要服务端
就能拿到 room_id + ttwid + signature 的话，整个采集环节可以搬到浏览器里。
"""
import json
import sys
import time

sys.path.insert(0, "src")

from airadio.live import build_wss_url, resolve_room  # noqa: E402

web_rid = sys.argv[1] if len(sys.argv) > 1 else "550620363313"
room = resolve_room(web_rid)
wss = build_wss_url(room.room_id, room.ttwid)
out = {
    "room_id": room.room_id,
    "web_rid": web_rid,
    "ttwid_prefix": room.ttwid[:24],
    "wss": wss,
    "made_at": time.time(),
}
with open("out/chunks/probe.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False)
print("room_id:", room.room_id)
print("wss len:", len(wss), "signature:", wss[-30:])
