"""互动事件模型与队列（纯 stdlib）。"""
from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field

MAX_QUEUE = 50
DEFAULT_TTL = 90.0

# 常见礼物价值（钻石），用于"高价值礼物可插队"判断；不在表内按 gift.diamond_count 解析值。
KNOWN_GIFT_VALUE = {
    "玫瑰": 1, "爱心": 5, "棒棒糖": 22, "大啤酒": 66,
    "抖音1号": 10000, "火箭": 5000, "嘉年华": 66666, "私人飞机": 3000,
}

BLOCK_WORDS = {"", "在吗", "？", "?", "1", "。。。"}


@dataclass
class InteractionEvent:
    kind: str  # chat|gift|like|follow|member|stats
    nick: str = ""
    content: str = ""
    gift: str = ""
    count: int = 1
    value: int = 0
    t: float = field(default_factory=time.time)

    def signature(self) -> str:
        if self.kind == "chat":
            return f"c:{self.nick}:{self.content}"
        if self.kind == "gift":
            return f"g:{self.nick}:{self.gift}:{int(self.t // 5)}"
        if self.kind == "like":
            return f"l:{int(self.t // 30)}"
        if self.kind in ("follow", "member"):
            return f"{self.kind[0]}:{self.nick}:{int(self.t // 120)}"
        return f"s:{int(self.t // 60)}"


class InteractionQueue:
    """近端事件队列：去重、TTL 过期、统计汇总，供调度器取用。"""

    def __init__(self, ttl: float = DEFAULT_TTL):
        self.ttl = ttl
        self.items: deque[InteractionEvent] = deque(maxlen=MAX_QUEUE)
        self._seen: set[str] = set()
        self.stats = Counter()
        self.top_gifters: Counter = Counter()
        self.top_chat: Counter = Counter()
        self.online_peak = 0

    def push(self, ev: InteractionEvent) -> bool:
        if ev.kind == "chat":
            if not ev.content or ev.content in BLOCK_WORDS or len(ev.content) > 200:
                return False
        sig = ev.signature()
        if sig in self._seen:
            return False
        self._seen.add(sig)
        if len(self._seen) > 4000:
            self._seen.clear()
        self.items.append(ev)
        self.stats[ev.kind] += 1
        self.stats["last_event_at"] = ev.t
        if ev.kind == "gift":
            self.top_gifters[ev.nick] += 1
        if ev.kind == "chat":
            self.top_chat[ev.content] += 1
        return True

    def drain(self, now: float) -> list[InteractionEvent]:
        """取出未过期事件（不清除已取出的对外承诺——由调度器决定）。"""
        out = []
        for ev in list(self.items):
            if now - ev.t <= self.ttl:
                out.append(ev)
        return out

    def recent(self, n: int = 40) -> list[dict]:
        """最近事件（倒序），供 UI 展示。"""
        out = []
        for ev in list(self.items)[-n:][::-1]:
            out.append({
                "kind": ev.kind, "nick": ev.nick[:24],
                "content": (ev.content or "")[:80],
                "gift": ev.gift, "count": ev.count, "t": ev.t,
            })
        return out

    def fallback_text(self) -> str:
        return ("直播间的朋友们，如果觉得有收获，点赞分享收藏走一波，"
                "有问题直接评论区留言，我看到都会聊。")
