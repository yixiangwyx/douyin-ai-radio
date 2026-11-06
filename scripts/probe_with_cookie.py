"""临时探针：用浏览器登录态 Cookie（从 live.douyin.com 抓包复制）连接 wss，统计消息类型。

用法: DOUYIN_COOKIE="ttwid=...; sessionid_ss=..." .venv/bin/python scripts/probe_with_cookie.py <秒> <web_rid>
"""
import asyncio
import gzip
import os
import sys
import time
from collections import Counter

sys.path.insert(0, "src")
from airadio.live import UA, build_wss_url, resolve_room  # noqa: E402
from airadio.proto import PushFrame, Response  # noqa: E402

COOKIE = os.environ.get("DOUYIN_COOKIE", "")


async def probe(web_rid: str, seconds: int):
    room = resolve_room(web_rid)
    if room.status != 2:
        print(f"{web_rid}: not live")
        return
    url = build_wss_url(room.room_id, room.ttwid)
    import websockets
    counts = Counter()
    async with websockets.connect(url, max_size=None, ping_interval=25,
                                  additional_headers={"cookie": COOKIE or f"ttwid={room.ttwid}",
                                                     "user-agent": UA}) as ws:
        end = time.time() + seconds
        while time.time() < end:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=20)
            except asyncio.TimeoutError:
                await ws.send(PushFrame(payloadType="hb").SerializeToString())
                continue
            if isinstance(msg, str):
                continue
            pf = PushFrame()
            pf.ParseFromString(msg)
            if pf.payloadType in ("internal", "ack"):
                continue
            try:
                data = gzip.decompress(pf.payload)
            except OSError:
                data = pf.payload
            resp = Response()
            resp.ParseFromString(data)
            if resp.needAck:
                await ws.send(PushFrame(payloadType="ack", payload=resp.internalExt.encode(),
                                       logId=pf.logId).SerializeToString())
            for m in resp.messagesList:
                counts[m.method] += 1
    print(f"{web_rid} room={room.room_id} cookie_keys="
          + ",".join(p.split("=")[0].strip() for p in COOKIE.split(";") if "=" in p))
    for k, v in counts.most_common():
        print(f"   {k:<40} {v}")


if __name__ == "__main__":
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    for rid in sys.argv[2:]:
        asyncio.run(probe(rid, secs))
