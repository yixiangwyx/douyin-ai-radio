"""WSS 探针（直接给 19 位 room_id）：统计游客态连接在 N 秒内收到的消息类型分布。

用途：语音/电台房即使拿到了真实 room_id，webcast WSS 到底给不给弹幕/进场/点赞。
"""
import asyncio
import gzip
import sys
import time
from collections import Counter

sys.path.insert(0, "src")
from airadio.live import UA, _session, build_wss_url  # noqa: E402
from airadio.proto import PushFrame, Response  # noqa: E402

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
ROOM_IDS = sys.argv[2:]


async def probe(room_id, ttwid):
    import websockets
    counts = Counter()
    url = build_wss_url(room_id, ttwid)
    try:
        async with websockets.connect(url, max_size=None, ping_interval=25, ping_timeout=20,
                                     additional_headers={"cookie": f"ttwid={ttwid}",
                                                        "user-agent": UA}) as ws:
            print(f"{room_id} connected", flush=True)
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
        print(f"{room_id} WSS ERROR {type(e).__name__}: {e}", flush=True)
        return
    print(f"{room_id} total={sum(counts.values())} " + " ".join(
        f"{k.replace('Webcast', '')}={v}" for k, v in counts.most_common()), flush=True)


async def main():
    s = _session()
    s.get("https://live.douyin.com/", timeout=15)
    ttwid = s.cookies.get("ttwid", "")
    for rid in ROOM_IDS:
        await probe(rid, ttwid)


if __name__ == "__main__":
    asyncio.run(main())
