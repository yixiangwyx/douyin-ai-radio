"""话术占位符：全局通用替换（互动话术、[[待插:…]]、随机报时、兜底话术共用一套）。

时间占位符：[月] [日] [年] [时] [分] [秒] [点] [星期] [周] [时段]
            [时点][时分]（或 [H][M]）是整词写法 → 直接读成"19点05分"（分钟补零）
互动数据占位符：[昵称] [礼物] [数量] [内容]
兼容旧写法：{nick} {gift} {count} {content}
"""
from __future__ import annotations

import re
from datetime import datetime

TIME_TOKENS = {
    "[月]": lambda t: str(t.month),
    "[日]": lambda t: str(t.day),
    "[年]": lambda t: str(t.year),
    "[时]": lambda t: str(t.hour),
    "[H]": lambda t: str(t.hour),
    "[点]": lambda t: str(t.hour),
    "[分]": lambda t: str(t.minute),
    "[M]": lambda t: str(t.minute),
    "[秒]": lambda t: str(t.second),
    "[时点]": lambda t: f"{t.hour}点",
    "[时分]": lambda t: f"{t.minute:02d}分",
    "[星期]": lambda t: "周" + "日一二三四五六"[t.weekday()],
    "[周]": lambda t: "周" + "日一二三四五六"[t.weekday()],
    "[时段]": lambda t: _daypart(t.hour),
}

TIME_RE = re.compile(r"\[(月|日|年|时点|时分|时|H|分|M|秒|点|星期|周|时段)\]")

# 互动数据占位符 → 取值键（值由调用方给，缺的一律空串）
DATA_TOKENS = {
    "[昵称]": "nick",
    "[内容]": "content",
    "[礼物]": "gift",
    "[数量]": "count",
    "{nick}": "nick",
    "{content}": "content",
    "{gift}": "gift",
    "{count}": "count",
}


def _daypart(hour: int) -> str:
    if 5 <= hour < 8:
        return "早上"
    if 8 <= hour < 11:
        return "上午"
    if 11 <= hour < 13:
        return "中午"
    if 13 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    return "凌晨"


PLACEHOLDER_DOC = [
    {"token": "[昵称]", "desc": "互动用户的昵称（按'只读汉字/最多几字'规则处理）"},
    {"token": "[月]", "desc": "当前月份，如 11"},
    {"token": "[日]", "desc": "当前日期，如 6"},
    {"token": "[年]", "desc": "当前年份"},
    {"token": "[时]", "desc": "当前小时（24小时制数字），如 19"},
    {"token": "[分]", "desc": "当前分钟，如 5"},
    {"token": "[秒]", "desc": "当前秒"},
    {"token": "[时点][时分]", "desc": "整词写法，直接读成'19点05分'（分钟自动补零）"},
    {"token": "[星期]", "desc": "今天是周几，如 周四"},
    {"token": "[时段]", "desc": "早上/上午/中午/下午/晚上/凌晨"},
    {"token": "[礼物]", "desc": "礼物名称（礼物互动时）"},
    {"token": "[数量]", "desc": "礼物数量，如 3个（只有 1 个时为空）"},
    {"token": "[内容]", "desc": "弹幕内容（弹幕互动时）"},
]


def doc() -> dict:
    """给前端的占位符说明（含示例替换结果）。"""
    from types import SimpleNamespace
    now = datetime.now()
    demo = SimpleNamespace(nick="兴旺的大刘", gift="玫瑰", count=3, content="怎么接首波流量")
    values = {"nick": speakable_nick(demo.nick), "gift": demo.gift, "count": " ×3",
              "content": demo.content}
    return {
        "placeholders": [{"token": d["token"], "desc": d["desc"], "example": fill(d["token"], values, now)}
                         for d in PLACEHOLDER_DOC],
        "examples": [
            {"template": "现在是[时点][时分]，刚进直播间的朋友先点个关注。",
             "result": fill("现在是[时点][时分]，刚进直播间的朋友先点个关注。", {}, now)},
            {"template": "[月]月[日]日[星期]，[时段]好，欢迎来到直播间。",
             "result": fill("[月]月[日]日[星期]，[时段]好，欢迎来到直播间。", {}, now)},
        ],
    }


def needs_data(text: str) -> str | None:
    """话术依赖哪个互动数据（'nick'/'content'/'gift'）；纯时间话术返回 None。"""
    if "[昵称]" in text or "{nick}" in text:
        return "nick"
    if "[内容]" in text or "{content}" in text:
        return "content"
    if "[礼物]" in text or "{gift}" in text:
        return "gift"
    return None


def fill(text: str, values: dict | None = None, now: datetime | None = None) -> str:
    """替换时间占位符与互动数据占位符。values 提供 nick/content/gift/count。"""
    t = now or datetime.now()
    out = TIME_RE.sub(lambda m: TIME_TOKENS[m.group(0)](t), text)
    values = values or {}
    for token, key in DATA_TOKENS.items():
        if token in out:
            out = out.replace(token, str(values.get(key, "") or "").strip())
    return re.sub(r"\s{2,}", " ", out).strip()


def speakable_nick(nick: str, han_only: bool = True, max_chars: int = 8) -> str:
    """按配置处理昵称：只保留汉字（去 emoji/字母/数字/符号），并限制最多读几个字。

    空结果（纯 emoji 或纯字母昵称）回退成"直播间的朋友"，避免出现"欢迎 进入直播间"。
    """
    raw = (nick or "").strip()
    text = raw
    if han_only:
        # 只读汉字：emoji/字母/数字都不播
        text = "".join(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
        if not text and re.search(r"[A-Za-z0-9]", raw):
            text = raw          # 纯字母/数字昵称：没有汉字可用，还是读原来的比读"直播间的朋友"好
    text = re.sub(r"\s+", "", text)
    limit = max(1, int(max_chars or 8))
    text = text[:limit]
    return text or "直播间的朋友"   # 全 emoji / 空昵称 → 不硬念
