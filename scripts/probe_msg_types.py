"""临时探针：统计游客态（仅 ttwid）wss 连接收到的消息 method 分布，验证礼物是否下发。"""
import asyncio
import gzip
import re
import sys
import time
import uuid
from collections import Counter

import requests

sys.path.insert(0, "src")
from airadio.live import UA, build_wss_url, resolve_room  # noqa: E402
from airadio.proto import PushFrame, Response  # noqa: E402

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 60


async def probe(web_rid: str):
    room = resolve_room(web_rid)
    if room.status != 2:
        print(f"{web_rid}: status={room.status} (不在直播中)")
        return
    url = build_wss_url(room.room_id, room.ttwid)
    import websockets
    counts = Counter()
    async with websockets.connect(url, max_size=None, ping_interval=25,
                                  additional_headers={"cookie": f"ttwid={room.ttwid}",
                                                     "user-agent": UA}) as ws:
        end = time.time() + SECONDS
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
    print(f"{web_rid} room={room.room_id} ttwid={'yes' if room.ttwid else 'no'}")
    for k, v in counts.most_common():
        print(f"   {k:<40} {v}")


if __name__ == "__main__":
    for rid in sys.argv[2:] or []:
        asyncio.run(probe(rid))
