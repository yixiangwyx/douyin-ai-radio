"""抓取抖音直播间真实 protobuf 样本帧，用于字段号核对与单测。
用法: uv run python scripts/capture_frames.py <web_rid> [real_room_id] [--seconds 60]
"""
import argparse, asyncio, base64, gzip, json, re, sys, time
sys.path.insert(0, "src")
import requests

from airadio.live import build_wss_url, UA
from airadio.proto import PushFrame, Response

INTEREST = {
    "WebcastChatMessage": "chat",
    "WebcastMemberMessage": "member",
    "WebcastSocialMessage": "social",
    "WebcastLikeMessage": "like",
    "WebcastGiftMessage": "gift",
    "WebcastRoomUserSeqMessage": "stats",
}

ROOM_PATTERNS = [
    r'\\"roomId\\":\\"(\d{15,20})',      # RSC flight 双重转义 JSON（当前 SSR 格式）
    r'"roomId":"(\d{15,20})"',
    r'"room_id":"(\d{15,20})"',
    r'reflow/(\d{15,20})',
]


def find_room_id(sess, web_rid):
    r = sess.get(f"https://live.douyin.com/{web_rid}", timeout=15)
    for pat in ROOM_PATTERNS:
        m = re.search(pat, r.text)
        if m:
            status = 0
            ms = re.search(r'\\?"status\\?":\s*\\?"?(\d)', r.text)
            if ms:
                status = int(ms.group(1))
            return m.group(1), status
    return None, 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("web_rid")
    ap.add_argument("real_room_id", nargs="?", default=None)
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--out", default="scripts/sample_frames.json")
    args = ap.parse_args()

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Referer": "https://live.douyin.com/"})
    sess.cookies.set("__ac_nonce", "06712f0a00abc123456789ab")
    sess.get("https://live.douyin.com/", timeout=15)  # warm: 拿 ttwid（WSS 必需）
    room_id, status = (args.real_room_id, 2) if args.real_room_id else find_room_id(sess, args.web_rid)
    if not room_id:
        print("room_id not found (room offline?)"); sys.exit(2)
    ttwid = sess.cookies.get("ttwid", "")
    print(f"room_id={room_id}")

    import asyncio
    asyncio.run(run(room_id, ttwid, args))


async def run(room_id, ttwid, args):
    import websockets
    url = build_wss_url(room_id, ttwid)
    samples = {k: [] for k in INTEREST.values()}
    counts = {}
    async with websockets.connect(url, max_size=None, ping_interval=25,
                                  additional_headers={"cookie": f"ttwid={ttwid}", "user-agent": UA}) as ws:
        end = time.time() + args.seconds
        while time.time() < end:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=20)
            except asyncio.TimeoutError:
                hb = PushFrame(payloadType="hb")
                await ws.send(hb.SerializeToString())
                continue
            if isinstance(msg, str):
                continue
            pf = PushFrame(); pf.ParseFromString(msg)
            if pf.payloadType in ("internal", "ack"):
                continue
            try:
                data = gzip.decompress(pf.payload)
            except OSError:
                data = pf.payload
            resp = Response(); resp.ParseFromString(data)
            if resp.needAck:
                ack = PushFrame(payloadType="ack", payload=resp.internalExt.encode(), logId=pf.logId)
                await ws.send(ack.SerializeToString())
            for m in resp.messagesList:
                kind = INTEREST.get(m.method)
                if not kind:
                    continue
                counts[kind] = counts.get(kind, 0) + 1
                if len(samples[kind]) < 6:
                    samples[kind].append(base64.b64encode(m.payload).decode())
    out = {"room_id": room_id, "captured_at": int(time.time()), "counts": counts, "samples": samples}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("counts:", counts)
    print("saved ->", args.out)


if __name__ == "__main__":
    main()
