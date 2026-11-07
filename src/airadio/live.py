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
from urllib.parse import quote, urlencode

import requests

from .events import InteractionEvent, InteractionQueue
from .proto import (ChatMessage, EmojiChatMessage, FansclubMessage, GiftMessage,
                    LikeMessage, MemberMessage, PushFrame, Response,
                    UpdateFanTicketMessage, SocialMessage)
from .sign import signature_for

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 手机 UA：只有它请求 live.douyin.com/{直播间号} 才会 302 到 reflow/{room_id}?sec_user_id=...
# （电脑 UA 拿到的是渲染好的 HTML，语音/电台房那份 HTML 里没有房间数据）
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 11; SAMSUNG SM-G973U) AppleWebKit/537.36 "
             "(KHTML, like Gecko) SamsungBrowser/14.2 Chrome/87.0.4280.141 Mobile Safari/537.36")

TYPE_MAP = {
    "WebcastChatMessage": ("chat", ChatMessage),
    "WebcastEmojiChatMessage": ("chat", EmojiChatMessage),
    "WebcastSocialMessage": ("follow", SocialMessage),
    "WebcastMemberMessage": ("member", MemberMessage),
    "WebcastLikeMessage": ("like", LikeMessage),
    "WebcastGiftMessage": ("gift", GiftMessage),
    # 灯牌/粉丝团（点亮灯牌、加粉丝团、粉丝团升级）
    "WebcastFansclubMessage": ("badge", FansclubMessage),
}


@dataclass
class RoomInfo:
    web_rid: str
    room_id: str
    status: int  # 2=直播中, 4=已结束
    title: str = ""
    ttwid: str = ""
    logged_in: bool = False   # 带 sessionid_ss 等登录 Cookie ⇒ 才会收到礼物消息
    audio_room: bool = False  # 语音/电台房（手机开播）：电脑端页面不渲染房间数据
    source: str = ""          # 拿到 room_id 的途径，排查用: page / enter / reflow


LOGIN_COOKIE_KEYS = ("sessionid_ss", "sessionid", "sid_tt")


class RoomBlockedError(RuntimeError):
    """抖音风控/验证页：请求被拦，拿不到房间数据（和"没开播"是两回事，别混淆）。"""


def cookie_has_login(cookie: str) -> bool:
    """浏览器复制来的 Cookie 串里有没有抖音登录态（礼物消息必须要它）。"""
    return any(f"{k}=" in (cookie or "") for k in LOGIN_COOKIE_KEYS)


def _session(login_cookie: str = "") -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://live.douyin.com/"})
    s.cookies.set("__ac_nonce", "0" + uuid.uuid4().hex[:23])
    if login_cookie:
        # 用户提供的浏览器 Cookie 直接带上（含登录态），后面的请求与 WSS 都用它
        s.headers["Cookie"] = login_cookie
    return s


def _anchor_sec_uid(page_html: str) -> str:
    """直播间页 HTML 里主播的 sec_uid（语音/电台房只能靠它去问房间数据）。"""
    m = re.search(r'(MS4wLjABAAAA[A-Za-z0-9_\-]{10,})', page_html)
    return m.group(1) if m else ""


def _profile_current_room(s, sec_uid: str) -> str:
    """网页版用户资料接口：返回该主播**当前**房间 id（room_id / live_status）。

    有些电台房（手机开播）连手机 UA 都不重定向（直接给 HTML，且 HTML 里
    roomStore.roomInfo 是空的、没有任何 room_id），这条接口照样给房间号，
    而且不需要 a_bogus/msToken 签名（实测：27589728361 → 7705293012108151567）。
    不在播时 room_id=0。
    """
    if not sec_uid:
        return ""
    params = {"device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
              "sec_user_id": sec_uid, "publish_video_strategy_type": "2",
              "pc_client_type": "1", "version_code": "170400", "version_name": "17.4.0"}
    try:
        r = s.get("https://www.douyin.com/aweme/v1/web/user/profile/other/?"
                  + urlencode(params), timeout=15)
        user = (r.json() or {}).get("user") or {}
    except Exception:
        return ""
    rid = str(user.get("room_id") or "")
    return rid if re.fullmatch(r"\d{15,20}", rid) else ""


def _room_redirect(s, web_rid: str) -> tuple[str, str]:
    """手机 UA 请求 live.douyin.com/{web_rid} 会 302 到
    webcast.amemv.com/douyin/webcast/reflow/{room_id}?sec_user_id=...

    这条重定向对手机开播的语音/电台房同样有效，且给的是该直播间号**当前/最近**那一间房，
    所以它比读电脑端页面更可靠（实测：电台房 94005035096 → reflow/7705239387273038655）。
    返回 (room_id, sec_user_id)，拿不到就返回 ("", "")。
    """
    try:
        r = s.get(f"https://live.douyin.com/{web_rid}",
                  headers={"User-Agent": MOBILE_UA}, allow_redirects=False, timeout=15)
        loc = r.headers.get("Location", "") or ""
        mm = re.search(r"reflow/(\d{15,20})", loc)
        if mm:
            sm = re.search(r"sec_user_id=([\w_\-]+)", loc)
            return mm.group(1), (sm.group(1) if sm else "")
        if r.status == 200 and "验证码中间页" in r.content.decode("utf-8", "ignore")[:4000]:
            raise RoomBlockedError("抖音返回验证码中间页（请求太频繁被风控），稍后再试或带登录 Cookie")
    except RoomBlockedError:
        raise
    except Exception:
        pass
    return "", ""


def _reflow_room_info(s, room_id: str, sec_user_id: str = "") -> dict | None:
    """app 端 reflow/info：房间权威数据。

    返回 data.room：status(2=在播, 4=已结束)、title、live_type_audio（语音/电台房标记）、
    user_count、create_time/finish_time。主播不在播时对着旧房间号也是 status=4，
    因此能准确区分"真没开播"和"接口取不到数据"。
    """
    if not room_id:
        return None
    params = {"type_id": "0", "live_id": "1", "room_id": room_id,
              "version_code": "99.99.99", "app_id": "1128", "device_platform": "webcast"}
    if sec_user_id:
        params["sec_user_id"] = sec_user_id
    try:
        r = s.get("https://webcast.amemv.com/webcast/room/reflow/info/?"
                  + urlencode(params), timeout=15)
        data = (r.json() or {}).get("data") or {}
    except Exception:
        return None
    room = data.get("room")
    return room if isinstance(room, dict) and room.get("id_str") else None


def _page_room_id(html: str) -> str | None:
    """直播间页 SSR 里的真实 room_id（19 位）。

    页面有两种渲染写法：普通 JSON `\"roomId\":\"770...\"` 与转义 JSON
    `roomId\\\":\\"770...\\"`，所以只匹配 roomId 到数字之间的分隔符，不去数反斜杠。
    """
    for pat in (r'roomId["\\:\s]{0,8}(\d{15,20})',
                r'room_id["\\:\s]{0,8}(\d{15,20})',
                r'reflow/(\d{15,20})'):
        mm = re.search(pat, html)
        if mm:
            return mm.group(1)
    return None


def resolve_room(web_rid: str, login_cookie: str = "") -> RoomInfo:
    """输入抖音直播间号（live.douyin.com/后的数字）或直播间 URL。

    手机开播的语音/电台房（抖音"电台"直播）在电脑端是拿不到房间数据的：
    页面显示"该直播类型或玩法电脑端暂未支持"，SSR 里 roomStore.roomInfo 是空的，
    web/enter 接口也不给房间 —— 老逻辑因此永远解析不到 room_id，控制台一直显示"未开播"。

    可用的两条路（都实测过）：
    1) 手机 UA 请求 live.douyin.com/{直播间号} → 302 到 webcast.amemv.com/douyin/webcast/
       reflow/{room_id}?sec_user_id=...   语音房也照常重定向，且给的就是当前那一间房；
    2) webcast/room/reflow/info/?room_id=... → 权威房间数据：status(2=在播/4=已结束)、
       title、live_type_audio、在线人数。
    status 一律以 (2) 为准：只读页面会把"上一场已结束的房间"当成在播，误报开播。
    """
    m = re.search(r"(\d{5,20})", web_rid)
    if not m:
        raise ValueError(f"无法从 {web_rid!r} 解析直播间号（请输入 live.douyin.com/数字 或纯数字）")
    rid = m.group(1)
    s = _session(login_cookie)

    room_id, sec_user_id = _room_redirect(s, rid)
    source = "redirect" if room_id else ""

    page_html = ""
    title = ""
    audio_room = False
    if not room_id:
        # 重定向没给房间（未开播、该号不是直播间号、或这类电台房连手机 UA 都不重定向）
        # 才读电脑端页面：取主播 sec_uid 走下面那条资料接口
        try:
            pr = s.get(f"https://live.douyin.com/{rid}", timeout=15)
            # 不指定 charset 时 requests 会按 latin-1 解，中文标题变乱码，风控文案也匹配不上
            pr.encoding = "utf-8"
            page_html = pr.text
        except Exception:
            pass
        if "验证码中间页" in page_html[:4000]:
            # 轮询太密会被风控拦住，返回的是验证页：既不是没开播，也拿不到房间数据
            raise RoomBlockedError("抖音返回验证码中间页（请求太频繁被风控），稍后再试或带登录 Cookie")
        room_id = _page_room_id(page_html) or ""
        source = "page" if room_id else ""
        if not sec_user_id:
            sec_user_id = _anchor_sec_uid(page_html)
        audio_room = "暂未支持" in page_html
        mt = re.search(r'<title>([^<]+)</title>', page_html)
        if mt:
            title = mt.group(1)[:80]
    status = 2 if room_id else 0

    if not room_id and sec_user_id:
        # 主播资料接口：有些手机开播的电台房只有这条能给房间号
        room_id = _profile_current_room(s, sec_user_id)
        source = "profile" if room_id else ""

    room = _reflow_room_info(s, room_id, sec_user_id)
    if room:
        room_id = str(room["id_str"])
        status = int(room.get("status") or 0)
        audio_room = bool(room.get("live_type_audio")) or audio_room
        if room.get("title"):
            title = str(room["title"])[:80]
        source = "reflow"
    if not room_id:
        # 未开播：把 web_rid 当占位 room_id，由上层轮询重试
        room_id = rid
        status = 0
    ttwid = s.cookies.get("ttwid", "")
    if not ttwid and login_cookie:
        m = re.search(r"ttwid=([^;]+)", login_cookie)
        ttwid = m.group(1) if m else ""
    return RoomInfo(web_rid=rid, room_id=room_id, status=status,
                    title=title, ttwid=ttwid, audio_room=audio_room, source=source,
                    logged_in=cookie_has_login(login_cookie) or
                    any(c.name in LOGIN_COOKIE_KEYS for c in s.cookies))


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
    """后台线程持续采集互动事件到 InteractionQueue。

    抖音服务端只向**带登录 Cookie** 的连接下发 WebcastGiftMessage：
    游客态（只有 ttwid）能收到 弹幕/进场/关注/点赞/灯牌/人数，但一条礼物都没有（实测）。
    所以想插播"谁送了礼物"，必须在 WSS 握手 header 里带上浏览器复制的 Cookie（含 sessionid_ss）。
    """

    def __init__(self, web_rid: str, queue: InteractionQueue, on_status=None,
                 login_cookie: str = ""):
        self.web_rid = web_rid
        self.login_cookie = (login_cookie or "").strip()
        self.q = queue
        self.on_status = on_status or (lambda s: None)
        self.room: RoomInfo | None = None
        self.gift_available: bool | None = None   # None=未知, False=没登录 Cookie 所以没礼物
        self._gift_merge: dict[tuple, tuple] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def set_login_cookie(self, cookie: str):
        """换/更新登录 Cookie（下一次重连生效）。"""
        self.login_cookie = (cookie or "").strip()
        if self.room is not None:
            self.room.logged_in = cookie_has_login(self.login_cookie)

    def _cookie_header(self) -> str:
        parts = []
        if self.login_cookie:
            parts.append(self.login_cookie)
        elif self.room and self.room.ttwid:
            parts.append(f"ttwid={self.room.ttwid}")
        return "; ".join(p.strip("; ") for p in parts if p)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.room = resolve_room(self.web_rid, self.login_cookie)
                if self.room.status != 2:
                    self.on_status("not_live")
                    self._stop.wait(30)
                    continue
                self.on_status(f"live:{self.room.room_id}")
                # 签名必须在 asyncio.run 之外生成：py_mini_racer 0.14 内部绑定当前
                # event loop，若在 loop 内调用，该 loop 关闭后重连会报
                # "Event loop is closed"（真实踩过的坑）。
                url = build_wss_url(self.room.room_id, self.room.ttwid)
                asyncio.run(self._ws_loop(url))
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                self.on_status(f"error:{self.last_error}")
                # 风控/验证页要退避久一点，不然越查越被拦
                self._stop.wait(60 if isinstance(e, RoomBlockedError) else 15)

    async def _ws_loop(self, url: str):
        import websockets
        async with websockets.connect(
                url, max_size=None, ping_interval=25, ping_timeout=20,
                additional_headers={"cookie": self._cookie_header(), "user-agent": UA}) as ws:
            self.on_status("connected")
            # 没带登录 Cookie 时，连上 60s 还一条礼物都没有 ⇒ 基本可判定是登录态问题
            # （不是"这房间恰好没人送礼"）。带 Cookie 时收到第一条礼物即置 True。
            gift_probe_until = time.time() + 60.0
            logged_in = cookie_has_login(self.login_cookie)
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
                    if m.method == "WebcastGiftMessage":
                        self.gift_available = True
                    entry = TYPE_MAP.get(m.method)
                    if not entry:
                        # 未映射的类型（RoomStats 等）也要参与"60s 内有没有礼物"的判断
                        if self.gift_available is None and not logged_in and time.time() > gift_probe_until:
                            self.gift_available = False
                        continue
                    if self.gift_available is None and not logged_in and time.time() > gift_probe_until:
                        # 游客态跑了 60s 都没礼物：明确告诉上层「礼物要登录 Cookie」
                        self.gift_available = False
                    kind, cls = entry
                    pm = cls()
                    try:
                        pm.ParseFromString(m.payload)
                    except Exception:
                        continue
                    ev = self._to_event(kind, pm)
                    if ev:
                        self._push(ev)

    def _push(self, ev):
        """同一次送礼抖音会连发多条 GiftMessage（连击/组合，repeat_count 递增）。
        合并成一条并把数量取最大值，避免一次送礼被插播好几遍。"""
        if ev.kind == "gift":
            key = (ev.nick, ev.gift)
            prev = self._gift_merge.get(key)
            if prev and time.time() - prev[0] <= 20.0:
                stored = prev[1]
                if ev.count > stored.count:
                    stored.count = ev.count     # 连击数量合并到已经入队的那条
                self._gift_merge[key] = (time.time(), stored)
                return
            self.q.push(ev)
            self._gift_merge[key] = (time.time(), ev)
            if len(self._gift_merge) > 500:
                self._gift_merge.clear()
            return
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
        if kind == "badge":
            # 灯牌/粉丝团：content 是抖音自带文案（如"加入了粉丝团"），action 区分加入/升级
            return InteractionEvent(kind="badge", nick=self._nick(pm),
                                   content=getattr(pm, "content", "") or "")
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
            count = int(getattr(pm, "repeat_count", 0) or getattr(pm, "combo_count", 0) or 1)
            from .events import KNOWN_GIFT_VALUE
            value = KNOWN_GIFT_VALUE.get(gift, 0)
            return InteractionEvent(kind="gift", nick=self._nick(pm), gift=gift,
                                   count=count, value=value)
        return None

