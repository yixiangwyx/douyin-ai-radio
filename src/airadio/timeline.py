"""时间线模型与调度器（纯逻辑，无外部依赖，可单测）。

核心思想：稿子编译成 Segment 序列（句/停顿/保护块/必播标记），
互动只能放在**句边界**（Segment.can_interrupt_after）上——句子中间永远不会被打断。

标记语法（行内或独立行均可）：
  [[no interrupt]] ... [[/no interrupt]]   保护块：块内不插播（报价/政策/金句）
  [[pause:500]]                            定点静音 500ms
  [[待插:感谢{gift}的朋友]]                固定位置必播话术（可用 {nick}/{gift} 占位符）
  以 # 开头的行 → 段标题，渲染成"接下来，{标题}。"
  普通行按中英文句末标点切句；一行内不写句末标点 = 一整句（整段不可插播）。
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Optional

from datetime import datetime

from .events import InteractionQueue, InteractionEvent
from .placeholders import fill as fill_placeholders, needs_data, speakable_nick

SENT_SPLIT_RE = re.compile(r"([。！？!?…]+[”’\"']?)")
CTRL_TOKEN_RE = re.compile(
    r"\[\[pause:(\d+)\]\]"                      # 1: ms
    r"|\[\[(no interrupt)\]\]"                  # 2: tag
    r"|\[\[(/no interrupt)\]\]"                 # 3: tag
    r"|\[\[待插(?::([^\]]*))?\]\]"             # 4: text
)

_DEFAULT_TEMPLATES = {
    "member": [
        "欢迎 [昵称] 进入我们的直播间。",
        "[昵称] 来了，欢迎欢迎。",
    ],
    "follow": [
        "[昵称] 关注了我们，谢谢支持。",
        "感谢 [昵称] 的关注。",
    ],
    "chat": [
        "[昵称] 说：[内容]",
        "看到 [昵称] 说，[内容]",
    ],
    "like": [
        "看到直播间好多朋友在点赞，感谢大家的支持。",
    ],
    "badge": [
        "[昵称] 点亮了灯牌，谢谢你的支持。",
        "感谢 [昵称] 加入粉丝团，灯牌一挂咱们就是一家人了。",
    ],
    "gift": [
        "感谢 [昵称] 送的[礼物][数量]。",
        "[昵称] 送了[礼物][数量]，有心了。",
    ],
}

# 默认报时话术（随机报时用，占位符同上）
DEFAULT_TIME_TEMPLATES = [
    "现在是[时点][时分]，刚进直播间的朋友先点个关注。",
    "提醒一下，现在是[月]月[日]日[时段][时]点左右。",
    "现在是[时点][时分]，咱们接着讲重点。",
]

NICK_MAX_DEFAULT = 8
INTERACTION_KINDS = ("member", "follow", "chat", "like", "gift", "badge")

MAX_LINE_CHARS = 400   # 单行超过这个长度才按句号细分（一次合成太长不利于插播与超时控制）


@dataclass
class Segment:
    kind: str  # speech | pause | mark
    text: str = ""
    duration_ms: int = 0
    protected: bool = False
    can_interrupt_after: bool = False
    line_no: int = -1    # 来自稿子的第几行（按行播放/按行轮换用）


def parse_script(text: str, line_units: bool = True) -> list[Segment]:
    """把稿子解析成可播的段。

    line_units=True（默认）：**一行 = 一个播放/合成单元**，行内不再按句号拆碎。
      上传的实录稿一行就是一段话，按句拆会产生几百个碎块（每块自带边界与停顿）。
      行内含控制标记（[[pause:…]] / [[no interrupt]] / [[待插:…]]）时仍按标记切分。
      单行超过 MAX_LINE_CHARS 才退回按句号细分。
    line_units=False：旧行为，按句号逐句切。
    """
    segs: list[Segment] = []
    protected = False
    for lineno, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if not line or line.startswith("<!--"):
            continue

        def add(kind: str, body: str, duration_ms: int = 0, prot: bool = False) -> None:
            if not body and kind != "pause":
                return
            segs.append(Segment(kind, body, duration_ms, prot, not prot, lineno))

        m_title = re.match(r"^#+\s*(.+)$", line)
        if m_title:
            add("speech", f"接下来，{m_title.group(1).strip()}。", prot=protected)
            continue

        def emit_plain(frag: str) -> None:
            frag = frag.strip()
            if not frag:
                return
            if line_units and len(frag) <= MAX_LINE_CHARS:
                add("speech", frag, prot=protected)
            else:
                for s in _split_sentences(frag, protected):
                    s.line_no = lineno
                    segs.append(s)

        pos = 0
        for m in CTRL_TOKEN_RE.finditer(line):
            if m.start() > pos:
                emit_plain(line[pos:m.start()])
            if m.group(1) is not None:
                add("pause", "", int(m.group(1)))
            elif m.group(2) is not None:
                protected = True
            elif m.group(3) is not None:
                protected = False
            elif m.group(4) is not None:
                add("mark", m.group(4).strip() or "感谢各位的支持", prot=True)
            pos = m.end()
        if pos < len(line):
            emit_plain(line[pos:])
    return segs


def _split_sentences(frag: str, protected: bool) -> list[Segment]:
    out: list[Segment] = []
    buf = ""
    for part in SENT_SPLIT_RE.split(frag):
        if not part:
            continue
        if SENT_SPLIT_RE.fullmatch(part):
            buf += part
            if buf.strip():
                out.append(Segment("speech", buf.strip(), protected=protected,
                                   can_interrupt_after=not protected))
            buf = ""
        else:
            buf += part
    if buf.strip():
        out.append(Segment("speech", buf.strip(), protected=protected,
                           can_interrupt_after=not protected))
    return out


@dataclass
class ScheduleConfig:
    min_gap_s: float = 45.0          # 两次插播的最小间隔
    window_s: tuple = (60.0, 150.0)  # 全局默认随机插播窗口（未在 per_kind 里单独配置的类别用它）
    # 每种互动独立的"事件→计划插播"随机延迟区间（秒）。缺省 None=用 window_s。
    per_kind_windows: dict = field(default_factory=dict)
    # 插播优先级：数字越大越优先（同一到期批次里先选它，够 max_events 就不带别的）
    kind_priority: dict = field(default_factory=lambda: {
        "gift": 100, "chat": 80, "badge": 70, "follow": 60, "member": 40, "like": 10})
    max_events: int = 3              # 单次插播最多合并几条
    urgent_value: int = 100          # 礼物价值≥此钻石数可插队
    urgent_min_gap_s: float = 25.0
    silence_fallback_s: float = 180.0  # 长时间无任何互动时的兜底话术
    chat_max_len: int = 40
    templates: dict = field(default_factory=lambda: dict(_DEFAULT_TEMPLATES))
    # 昵称播报：只读汉字（过滤 emoji/字母/符号），以及最多读几个字
    nick_han_only: bool = True
    nick_max_chars: int = 8
    # 随机报时：开关 + 最小/最大间隔秒 + 话术（占位符全局通用）
    announce_time: bool = False
    announce_interval_s: tuple = (900.0, 1800.0)
    time_templates: list = field(default_factory=lambda: list(DEFAULT_TIME_TEMPLATES))
    # 长时间没有任何互动时的兜底话术（也可多条随机）
    fallback_templates: list = field(default_factory=lambda: [
        "直播间的朋友们，如果觉得有收获，点赞分享收藏走一波，有问题直接评论区留言，我看到都会聊。",
    ])

    def window_for(self, kind: str) -> tuple:
        w = self.per_kind_windows.get(kind)
        if w:
            lo = max(0.0, float(w[0]))
            return (lo, max(lo + 1.0, float(w[1])))
        return self.window_s


class Scheduler:
    """在句边界被询问：这里要不要插播互动？返回要插的话术或 None。

    每个事件首次被看见时分配一个"计划插播时刻" = 事件时间 + random(window_s)，
    到点的边界才插——这就是"定时随机插播"的实现，间隔天然随机。
    高价值礼物可插队（不等窗口，只受 urgent_min_gap_s 约束）。
    """

    def __init__(self, cfg: ScheduleConfig, queue: InteractionQueue,
                 rng: random.Random | None = None):
        self.cfg = cfg
        self.q = queue
        self.rng = rng or random.Random()
        self.last_burst_at = 0.0
        self._pending: list[InteractionEvent] = []
        self._planned: dict[int, float] = {}
        self._spoken: set[str] = set()   # 已播过的签名，避免 TTL 内重播
        self.inserted_count = 0
        self.announced_count = 0
        self._next_announce_at = 0.0     # 0 = 首次边界即安排

    def _short(self, nick: str) -> str:
        return speakable_nick(nick, self.cfg.nick_han_only, self.cfg.nick_max_chars)

    def _render(self, ev: InteractionEvent) -> str:
        tpls = [t.strip() for t in (self.cfg.templates.get(ev.kind) or []) if t and t.strip()]
        if not tpls:
            tpls = [t.strip() for t in (self.cfg.fallback_templates or []) if t and t.strip()]
        if not tpls:
            return ""
        content = ev.content or ""
        if len(content) > self.cfg.chat_max_len:
            content = content[: self.cfg.chat_max_len] + "，这个我们后面详细聊"
        values = {"nick": self._short(ev.nick), "content": content,
                  "gift": ev.gift or "礼物",
                  "count": f"{ev.count}个" if (ev.count or 1) > 1 else ""}
        # 多条话术随机选；若选中的话术需要而本次事件没有的数据（如礼物名），换一条不需要它的
        order = list(tpls)
        self.rng.shuffle(order)
        chosen = order[0]
        for cand in order:
            need = needs_data(cand)
            if not need or (need != "gift" or values["gift"]) or (need == "content" and values["content"]):
                chosen = cand
                break
        return fill_placeholders(chosen, values, datetime.now())

    def _gap_ok(self, now: float) -> bool:
        return not self.last_burst_at or (now - self.last_burst_at) >= self.cfg.min_gap_s

    def _time_announcement_due(self, now: float) -> bool:
        if not self.cfg.announce_time:
            return False
        if self._next_announce_at == 0.0:
            lo, hi = self.cfg.announce_interval_s
            self._next_announce_at = now + self.rng.uniform(max(0.0, lo), max(lo + 1.0, hi))
            return False
        return now >= self._next_announce_at

    def fallback_text(self) -> str:
        """无互动兜底话术：多条时随机选一条，同样支持占位符。"""
        tpls = [t for t in (self.cfg.fallback_templates or []) if str(t).strip()]
        if not tpls:
            return ""
        return fill_placeholders(self.rng.choice(tpls).strip())

    def _make_time_announcement(self, now: float) -> str:
        tpls = self.cfg.time_templates or list(DEFAULT_TIME_TEMPLATES)
        text = fill_placeholders(self.rng.choice(tpls), {}, datetime.fromtimestamp(now))
        lo, hi = self.cfg.announce_interval_s
        self._next_announce_at = now + self.rng.uniform(max(0.0, lo), max(lo + 1.0, hi))
        self.announced_count += 1
        self.last_burst_at = now
        self.inserted_count += 1
        return text

    def on_boundary(self, now: float, protected_after: bool) -> Optional[str]:
        """在句边界调用；protected_after=True 表示下一个边界在保护块内、这里必须保持干净。"""
        announce_due = False if protected_after else self._time_announcement_due(now)
        if protected_after:
            return None
        known = {id(e) for e in self._pending}
        for e in self.q.drain(now):
            if id(e) not in known and e.signature() not in self._spoken:
                self._pending.append(e)
                if e.kind == "gift" and (e.value or 0) >= self.cfg.urgent_value:
                    self._planned[id(e)] = e.t + self.cfg.urgent_min_gap_s * 0.5
                else:
                    lo, hi = self.cfg.window_for(e.kind)
                    self._planned[id(e)] = e.t + self.rng.uniform(lo, hi)
        self._pending = [e for e in self._pending if now - e.t <= self.q.ttl]
        if not self._pending:
            if announce_due and self._gap_ok(now):
                return self._make_time_announcement(now)
            last_event_at = self.q.stats.get("last_event_at", 0.0)
            if (self.inserted_count == 0 and last_event_at
                    and now - last_event_at > self.cfg.silence_fallback_s):
                self.last_burst_at = now
                self.inserted_count += 1
                return self.fallback_text()
            return None

        due = [e for e in self._pending if self._planned.get(id(e), 1e18) <= now]
        if not due:
            if announce_due and self._gap_ok(now):
                return self._make_time_announcement(now)
            return None
        # 按优先级降序（礼物>弹幕>关注>进场>点赞）；同级内只取最新数据（计划时刻新的在前）
        pri = self.cfg.kind_priority
        due.sort(key=lambda e: (-pri.get(e.kind, 0), -(self._planned.get(id(e), 0))))
        candidates = due
        urgent = [e for e in due if e.kind == "gift" and (e.value or 0) >= self.cfg.urgent_value]
        gap = now - self.last_burst_at
        min_gap = self.cfg.urgent_min_gap_s if urgent else self.cfg.min_gap_s
        if self.last_burst_at and gap < min_gap:
            return None

        picked = candidates[:self.cfg.max_events]
        picked_ids = {id(e) for e in picked}
        self._pending = [e for e in self._pending if id(e) not in picked_ids]
        for e in picked:
            self._planned.pop(id(e), None)
            self._spoken.add(e.signature())
            if len(self._spoken) > 5000:
                self._spoken.clear()
        parts: list[str] = []
        said_like = False
        for ev in picked:
            if ev.kind == "like":
                if said_like:
                    continue
                said_like = True
            parts.append(self._render(ev))
        # 报时到点就报：热门直播间互动一直排队，否则报时永远抢不到边界 → 跟这次插播一起说
        if announce_due and self._gap_ok(now):
            parts.append(self._make_time_announcement(now))
        self.last_burst_at = now
        self.inserted_count += 1
        return " ".join(parts)

    def render_mark(self, text: str, latest: InteractionEvent | None) -> str:
        """[[待插:...]] 固定话术：用与互动话术同一套全局占位符渲染。"""
        values = {}
        if latest:
            values = {"nick": self._short(latest.nick),
                      "content": (latest.content or "")[: self.cfg.chat_max_len],
                      "gift": latest.gift or "礼物",
                      "count": f"×{latest.count}" if (latest.count or 1) > 1 else ""}
        return fill_placeholders(text, values)
