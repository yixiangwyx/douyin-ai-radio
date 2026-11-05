"""抖音直播间互动采集：web_rid → room_id → webcast WSS → 事件队列。

对应上游协议：与 DouyinLiveWebFetcher 相同的 webcast/im/push/v2 路线，
signature 由 vendor/sign.js（mini-racer）生成。
"""
from __future__ import annotations

import asyncio
import gzip
import re
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote

import requests

from .events import InteractionEvent, InteractionQueue
from .proto import (ChatMessage, EmojiChatMessage, GiftMessage, LikeMessage,
                    MemberMessage, PushFrame, Response, SocialMessage)
from .sign import signature_for

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

TYPE_MAP = {
    "WebcastChatMessage": ("chat", ChatMessage),
    "WebcastEmojiChatMessage": ("chat", EmojiChatMessage),
    "WebcastSocialMessage": ("follow", SocialMessage),
    "WebcastMemberMessage": ("member", MemberMessage),
    "WebcastLikeMessage": ("like", LikeMessage),
    "WebcastGiftMessage": ("gift", GiftMessage),
}


@dataclass
class RoomInfo:
    web_rid: str
    room_id: str
    status: int  # 2=直播中, 4=已结束
    title: str = ""
    ttwid: str = ""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://live.douyin.com/"})
    s.cookies.set("__ac_nonce", "0" + uuid.uuid4().hex[:23])
    return s


def resolve_room(web_rid: str) -> RoomInfo:
    """输入抖音直播间号（live.douyin.com/后的数字）或直播间 URL。"""
    m = re.search(r"(\d{5,20})", web_rid)
    if not m:
        raise ValueError(f"无法从 {web_rid!r} 解析直播间号（请输入 live.douyin.com/数字 或纯数字）")
    rid = m.group(1)
    s = _session()
    r = s.get(f"https://live.douyin.com/{rid}", timeout=15)
    # SSR 页面里找真实 room_id（两种渲染格式：普通 JSON 与转义 JSON）
    room_id = None
    for pat in (r'\\"roomId\\":\\"(\d{15,20})',   # 转义 JSON（当前 SSR 格式）
                r'"roomId":"(\d{15,20})"',
                r'"room_id":"(\d{15,20})"',
                r'reflow/(\d{15,20})'):
        mm = re.search(pat, r.text)
        if mm:
            room_id = mm.group(1)
            break
    status = 2 if room_id else 0
    mm = re.search(r'"status":(\d+)', r.text)
    if mm:
        status = int(mm.group(1))
    title = ""
    mt = re.search(r'<title>([^<]+)</title>', r.text)
    if mt:
        title = mt.group(1)[:80]
    if not room_id:
        # 未开播：把 web_rid 当占位 room_id，由上层轮询重试
        room_id = rid
    return RoomInfo(web_rid=rid, room_id=room_id, status=status,
                    title=title, ttwid=s.cookies.get("ttwid", ""))


def build_wss_url(room_id: str, ttwid: str) -> str:
    ts = int(time.time() * 1000)
    uid = str(uuid.uuid4().int % 10 ** 19)
    params = [
        ("app_name", "douyin_web"), ("version_code", "180800"),
        ("webcast_sdk_version", "1.0.14-beta.0"), ("update_version_code", "1.0.14-beta.0"),
        ("compress", "gzip"), ("device_platform", "web"), ("cookie_enabled", "true"),
        ("screen_width", "1920"), ("screen_height", "1080"),
        ("browser_language", "zh-CN"), ("browser_platform", "Win32"), ("browser_name", "Mozilla"),
        ("browser_version", UA.split("Mozilla/")[1]),
        ("browser_online", "true"), ("tz_name", "Asia/Shanghai"),
        ("cursor", f"d-1_u-1_fh-7392091211001140287_t-{ts}_r-1"),
        ("internal_ext", f"internal_src:dim|wss_push_room_id:{room_id}|wss_push_did:{uid}"
                         f"|first_req_ms:{ts}|fetch_time:{ts}|seq:1|wss_info:0-{ts}-0-0|wrds_v:{ts}"),
        ("host", "https://live.douyin.com"), ("aid", "6383"), ("live_id", "1"), ("did_rule", "3"),
        ("endpoint", "live_pc"), ("support_wrds", "1"), ("user_unique_id", uid),
        ("im_path", "/webcast/im/fetch/"), ("identity", "audience"),
        ("need_persist_msg_count", "15"), ("insert_task_id", ""), ("live_reason", ""),
        ("room_id", room_id), ("heartbeatDuration", "0"),
    ]
    qs = "&".join(f"{k}={quote(v, safe='')}" for k, v in params)
    wss = f"wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/?{qs}"
    return wss + "&signature=" + quote(signature_for(wss))


class LiveCollector:
    """后台线程持续采集互动事件到 InteractionQueue。"""

    def __init__(self, web_rid: str, queue: InteractionQueue, on_status=None):
        self.web_rid = web_rid
        self.q = queue
        self.on_status = on_status or (lambda s: None)
        self.room: RoomInfo | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.room = resolve_room(self.web_rid)
                if self.room.status != 2:
                    self.on_status("not_live")
                    self._stop.wait(30)
                    continue
                self.on_status(f"live:{self.room.room_id}")
                asyncio.run(self._ws_loop())
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                self.on_status(f"error:{self.last_error}")
                self._stop.wait(15)

    async def _ws_loop(self):
        import websockets
        assert self.room
        url = build_wss_url(self.room.room_id, self.room.ttwid)
        async with websockets.connect(
                url, max_size=None, ping_interval=25, ping_timeout=20,
                additional_headers={"cookie": f"ttwid={self.room.ttwid}", "user-agent": UA}) as ws:
            self.on_status("connected")
            while not self._stop.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    hb = PushFrame(payloadType="hb")
                    await ws.send(hb.SerializeToString())
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
                    ack = PushFrame(payloadType="ack", payload=resp.internalExt.encode(),
                                   logId=pf.logId)
                    await ws.send(ack.SerializeToString())
                for m in resp.messagesList:
                    entry = TYPE_MAP.get(m.method)
                    if not entry:
                        continue
                    kind, cls = entry
                    pm = cls()
                    try:
                        pm.ParseFromString(m.payload)
                    except Exception:
                        continue
                    ev = self._to_event(kind, pm)
                    if ev:
                        self.q.push(ev)

    @staticmethod
    def _nick(pm) -> str:
        try:
            # nickname 在 User 里；frames.proto 用 string(high_tag) 承载（见协议注释）
            u = getattr(pm, "user", None)
            if u is None:
                return ""
            for field in ("nickname", "display_name"):
                v = getattr(u, field, "")
                if isinstance(v, str) and v:
                    return v
        except Exception:
            pass
        return ""

    def _to_event(self, kind: str, pm) -> InteractionEvent | None:
        if kind == "chat":
            return InteractionEvent(kind="chat", nick=self._nick(pm), content=pm.content)
        if kind == "follow":
            if getattr(pm, "action", 1) == 2:  # 取关不播
                return None
            return InteractionEvent(kind="follow", nick=self._nick(pm))
        if kind == "member":
            return InteractionEvent(kind="member", nick=self._nick(pm))
        if kind == "like":
            return InteractionEvent(kind="like", nick=self._nick(pm))
        if kind == "gift":
            gift = pm.gift.name if pm.gift else ""
            count = pm.repeatCount or 1
            from .events import KNOWN_GIFT_VALUE
            value = KNOWN_GIFT_VALUE.get(gift, 0)
            return InteractionEvent(kind="gift", nick=self._nick(pm), gift=gift,
                                   count=count, value=value)
        return None

