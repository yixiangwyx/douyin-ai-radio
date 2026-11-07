"""探针：轮询某个 web_rid，一旦开播就用游客态 WSS 统计消息类型分布。

用途：判断「手机电台/语音房开播」时，web 端 webcast 连接到底能拿到哪些互动消息
（chat/member/like/gift 是否下发）。
"""
import asyncio
import gzip
import sys
import time
from collections import Counter

sys.path.insert(0, "src")
from airadio.live import UA, build_wss_url, resolve_room  # noqa: E402
from airadio.proto import PushFrame, Response  # noqa: E402

TOTAL = int(sys.argv[1]) if len(sys.argv) > 1 else 300
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 60
RIDS = sys.argv[3:] or []


async def probe_once(web_rid: str) -> bool:
    room = resolve_room(web_rid)
    ts = time.strftime("%H:%M:%S")
    if room.status != 2:
        print(f"{ts} {web_rid} status={room.status} room_id={room.room_id} (不在直播中)")
        return False
    print(f"{ts} {web_rid} LIVE room_id={room.room_id}")
    url = build_wss_url(room.room_id, room.ttwid)
    import websockets
    counts = Counter()
    try:
        async with websockets.connect(url, max_size=None, ping_interval=25, ping_timeout=20,
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
    except Exception as e:
        print(f"   WSS ERROR {type(e).__name__}: {e}")
        return False
    total = sum(counts.values())
    print(f"   {time.strftime('%H:%M:%S')} total={total} " + " ".join(
        f"{k.replace('Webcast', '')}={v}" for k, v in counts.most_common()))
    return True


async def main():
    deadline = time.time() + TOTAL
    while time.time() < deadline:
        for rid in RIDS:
            await probe_once(rid)
        await asyncio.sleep(15)


if __name__ == "__main__":
    asyncio.run(main())
