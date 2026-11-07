"""电台房实测：轮询 web_rid，确认「真的在播」(reflow status==2) 后立刻用游客态 WSS
统计 90 秒内能拿到哪些消息类型 —— 用来判断语音/电台房到底能不能取到弹幕互动。

用法: uv run python scripts/watch_radio_room.py <总轮询秒> <每次WSS秒> <web_rid...>
"""
import asyncio
import gzip
import json
import os
import sys
import time
import urllib.parse
from collections import Counter

sys.path.insert(0, "src")
from airadio.live import (UA, RoomBlockedError, _reflow_room_info,  # noqa: E402
                          _session, build_wss_url, resolve_room)
from airadio.proto import PushFrame, Response  # noqa: E402

TOTAL = int(sys.argv[1]) if len(sys.argv) > 1 else 600
WS_SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 90
RIDS = sys.argv[3:] or ["54235570430"]

ENTER_PARAMS = {
    "aid": "6383", "app_name": "douyin_web", "live_id": "1", "device_platform": "web",
    "language": "zh-CN", "browser_language": "zh-CN", "browser_platform": "Win32",
    "browser_name": "Chrome", "browser_version": "141.0.0.0", "enter_from": "page_refresh",
}


def enter(s, web_rid):
    p = dict(ENTER_PARAMS, web_rid=web_rid)
    r = s.get("https://live.douyin.com/webcast/room/web/enter/?" + urllib.parse.urlencode(p),
             timeout=15)
    try:
        return r.json().get("data") or {}
    except ValueError:
        return {}


def reflow(s, room_id):
    p = {"type_id": "0", "live_id": "1", "room_id": room_id, "sec_user_id": "",
         "version_code": "99.99.99", "app_id": "1128", "device_platform": "webcast"}
    r = s.get("https://webcast.amemv.com/webcast/room/reflow/info/?" + urllib.parse.urlencode(p),
             timeout=15)
    try:
        return (r.json().get("data") or {}).get("room") or {}
    except ValueError:
        return {}


async def ws_probe(room_id, ttwid):
    import websockets
    counts = Counter()
    url = build_wss_url(room_id, ttwid)
    async with websockets.connect(url, max_size=None, ping_interval=25, ping_timeout=20,
                                  additional_headers={"cookie": f"ttwid={ttwid}",
                                                     "user-agent": UA}) as ws:
        end = time.time() + WS_SECONDS
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
    return counts


async def main():
    s = _session()
    s.get("https://live.douyin.com/", timeout=15)
    ttwid = s.cookies.get("ttwid", "")
    deadline = time.time() + TOTAL
    while time.time() < deadline:
        for rid in RIDS:
            ts = time.strftime("%H:%M:%S")
            # 用 resolve_room：电台/语音房走手机 UA 重定向 + reflow，enter 接口对它们不给房间
            try:
                ri = resolve_room(rid)
            except RoomBlockedError as e:
                print(f"{ts} {rid} BLOCKED 风控验证页: {e}", flush=True)
                continue
            rd = _reflow_room_info(s, ri.room_id) or {}
            print(f"{ts} {rid} status={ri.status} audio={ri.audio_room} src={ri.source} "
                  f"users={rd.get('user_count')} "
                  f"radio={'/radio/' in str((rd.get('stream_url') or {}).get('hls_pull_url'))} "
                  f"title={(ri.title or '')[:24]} room={ri.room_id}", flush=True)
            if ri.status == 2:
                try:
                    counts = await ws_probe(ri.room_id, ttwid)
                    print(f"   WSS {ri.room_id} total={sum(counts.values())} " + " ".join(
                        f"{k.replace('Webcast', '')}={v}" for k, v in counts.most_common()),
                        flush=True)
                except Exception as e:
                    print(f"   WSS ERROR {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(int(os.environ.get("AIRADIO_WATCH_INTERVAL", "60")))


if __name__ == "__main__":
    asyncio.run(main())
