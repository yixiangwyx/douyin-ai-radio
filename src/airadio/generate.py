"""AI 生成新稿：话题 → 口播稿（百炼文本模型），带内置直播教学话题库作起点。

生成只写文字稿，不碰 TTS；生成后存进稿子库，由你决定要不要立刻播放。
"""
from __future__ import annotations

import os
import re
import random
import time

# 内置起点话题库（直播教学向；要点来自公开复盘方法论：停留时长、GPM、四看盯盘、
# 关注/加团率、开场预期管理等）。你可以自己在页面上增删。
STARTER_TOPICS = [
    ("新手开播前 30 秒该说什么", "开场别急着讲产品；先给观众一个留下来的理由。"),
    ("直播间留不住人怎么办", "平均停留 30–60 秒是普遍水平；先做观众预期管理。"),
    ("怎么看懂直播间的四组数据", "人气、互动、商品、交易四类指标分别说明什么。"),
    ("GPM 是什么，为什么它决定推流", "千次观看成交额＝点击率×转化率×客单价×1000。"),
    ("开播前 30 分钟要盯哪几个数", "曝光进入率、第一波推流承接、每 5 分钟停留、成交密度。"),
    ("弹幕没人发怎么办", "提问式话术、二选一互动、点名回应，比喊'扣个 1'有效。"),
    ("关注率和加团率怎么做上去", "新号先做关注/加团；给一个具体的关注理由。"),
    ("一条 150 字的口播脚本怎么写", "3 秒抛问题、中间给方法、结尾给行动指令。"),
    ("下播后 15 分钟复盘做什么", "对比上一场、找出断点、写下一场的三个动作。"),
    ("直播话术怎么不显得念稿", "短句、口语连接词、每句只讲一件事。"),
    ("新号开播第一波流量怎么接", "第一波推流能不能接住，决定整场的流量水平。"),
    ("憋单节奏怎么把握", "单次憋单 5–10 分钟内要放单，超时容易被判虚假营销。"),
    ("留人的福利为什么要反复说", "新进粉丝不知道你前面讲的福利，5–10 分钟重复一次。"),
    ("哪些词在直播间不能说", "极限词、绝对化表述、功效承诺是高风险区。"),
    ("促单怎么不让人反感", "场景化描述代替催单口号，减少冲动单和退款。"),
    ("AI 电台怎么不被判低质循环", "语速有变化、句间有自然停顿、话术不逐字重复。"),
]

_SYSTEM = (
    "你是直播培训讲师的稿件助手，写的是要直接朗读出来的口播稿。要求：\n"
    "1) 每句一行，一句话只讲一件事；短句、口语化，不用书面语。\n"
    "2) 不要编号、不要标题、不要表情符号、不要markdown符号。\n"
    "3) 内容要具体可操作（说清怎么做、看哪个数、阈值大概多少），不要空话口号。\n"
    "4) 不编造具体账号数据、不承诺收益。\n"
    "5) 只输出稿子正文，不输出解释。"
)


def _models() -> list[str]:
    import os
    pref = os.environ.get("AIRADIO_TEXT_MODEL", "qwen-turbo")
    return [m for m in [pref, "qwen-turbo", "qwen-plus", "qwen-flash"] if m]


def generate_script(topic: str, note: str = "", sentences: int = 10,
                    api_key: str | None = None) -> tuple[str, str]:
    """返回 (稿子正文, 使用的模型名)。失败抛 RuntimeError（带可读原因）。"""
    try:
        from dashscope import Generation
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"dashscope 不可用: {e}")
    if api_key:
        import dashscope
        dashscope.api_key = api_key
    sentences = max(3, min(40, int(sentences or 10)))
    user = f"话题：{topic}\n" + (f"要点提示：{note}\n" if note else "")
    user += f"请写成 {sentences} 句口播稿，每句一行。"
    last_err = ""
    for model in _models():
        for attempt in range(3):
            try:
                r = Generation.call(model=model,
                                    messages=[{"role": "system", "content": _SYSTEM},
                                              {"role": "user", "content": user}],
                                    result_format="message", temperature=0.8)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                break
            if r.status_code == 200:
                try:
                    raw = r.output.choices[0].message.content or ""
                except Exception:
                    raw = ""
                text = _clean_draft(raw, sentences)
                if text:
                    return text, model
                last_err = "模型返回空稿"
                break
            last_err = f"{r.status_code} {getattr(r, 'code', '')} {getattr(r, 'message', '')[:120]}"
            if getattr(r, "code", "") == "Throttling.RateQuota":
                time.sleep(2.0 + attempt * 2.0)
                continue
            break   # 额度/权限类错误换模型试
    raise RuntimeError(f"生成失败：{last_err}")


def _pick_pack(topic: str) -> list[str]:
    """按话题关键词选一组可操作步骤（要点来自公开复盘方法论：停留时长、四看盯盘、GPM、憋单节奏、话术合规）。"""
    t = topic
    def has(*keys): return any(k in t for k in keys)
    if has("憋单", "放单", "福利", "抽奖", "秒杀", "库存"):
        return ["憋单不是拖着不卖，是给观众一个等下去的理由。",
                "单次憋单控制在 5 到 10 分钟，到点必须放单，拖久了容易被判虚假营销。",
                "开头把福利说清楚，中间讲清楚为什么值，最后给一个明确的上车指令。",
                "福利要每 5 到 10 分钟重复一次，因为新进的人没听到你前面说的。",
                "放单以后立刻看成交密度，密度起来了平台才会推下一波。"]
    if has("促单", "逼单", "催单", "成交", "下单", "退款"):
        return ["别只喊快下单，说清买了以后会用到什么场景。",
                "把价格对比讲明白，让观众自己得出结论，比你替他下结论有效。",
                "过度承诺会带来退款，能兑现的说，兑现不了的别说。",
                "催单要有真实的依据，比如库存、时限、售后政策，说具体的那一条。",
                "成交之后看一眼退款率，退款高的话术下次直接改掉。"]
    if has("词", "违禁", "敏感", "合规", "违规", "限流"):
        return ["极限词和绝对化说法是高风险区，比如最、第一、全网最低这类。",
                "功效类承诺要有依据，没有依据的说法直接换成客观描述。",
                "别在直播间引导站外联系方式，这是平台明确的高风险动作。",
                "稿子里出现数字和承诺，先确认你能不能兑现。",
                "下播把字幕导出来过一遍，看哪句说法有问题，下次直接改。"]
    if has("AI", "人工智能", "低质", "循环", "无人", "机器人", "重复"):
        return ["语速别全程一样，关键处放慢，促单的地方紧一点。",
                "句子之间留一点自然停顿，一句接一句反而像机器。",
                "同一类话术准备几个说法轮换，听几遍就能背出来的稿子留不住人。",
                "画面和声音要对得上，说的是什么和观众看到的必须一致。",
                "开播和下场时间别每次都精确一样，太规律容易被判异常。"]
    if has("起号", "新号", "冷启动", "第一波", "推流", "自然流量", "标签"):
        return ["开播第一波推流能不能接住，直接决定这场还有没有下一波。",
                "开播前 30 分钟盯每 5 分钟的停留曲线，掉人的那段就是话术问题。",
                "新号先把关注率和加团率做出来，给观众一个具体的关注理由。",
                "先有真实的成交和互动，系统才知道把你的直播间推给谁。",
                "连续几场用同一套主线和同一类选题，标签才稳定。"]
    if has("留", "停留", "接不住", "在线", "掉人"):
        return ["平均停留时长 30 到 60 秒是多数直播间的正常水平，能到 2 分钟就算不错。",
                "停留短不是话术差，是观众进来不知道能得到什么。",
                "所以进场的第一句话，先说清楚这场能听到什么、什么时候讲重点。",
                "每 5 分钟做一次小闭环，抛问题、给结论、留一个下期要讲的东西。",
                "下播去看每 5 分钟的停留曲线，哪一段掉人，就改那一段的说法。"]
    if has("数据", "复盘", "指标", "看播", "盯盘", "罗盘", "GPM", "gpm"):
        return ["先看曝光进入率，平台给你 100 个人你能拉进来几个，低于 20% 先改画面和标题。",
                "再看第一波推流的承接，推流峰值和实际在线的差值就是留人能力。",
                "第三看人均观看时长、互动率、关注率、加团率，这是内容质量的信号。",
                "第四看成交密度和千次观看成交额，它决定平台下一波推不推你。",
                "每一场只改一个变量，下一场才有得比。"]
    if has("脚本", "稿", "文案", "话术", "口播"):
        return ["先写一句结论，你要让观众记住哪一件事。",
                "开头三秒抛观众自己的问题，不要自我介绍。",
                "中间只讲方法，一步一句，别在一句里塞两件事。",
                "结尾给一个明确的行动指令，今天该做什么。",
                "写完整段读一遍出声，拗口的地方直接删掉。"]
    if has("开场", "开播", "冷场", "刚开"):
        return ["开播前 30 分钟最重要，平台这时候在判断要不要继续给你流量。",
                "开场先说这场能得到什么，再问一个只有直播间能回答的问题。",
                "别急着讲产品，先把人留住再说货。",
                "每 10 分钟重复一次这场的主线和重点时间。"]
    if has("互动", "弹幕", "评论", "点赞", "关注", "加团", "涨粉"):
        return ["让观众二选一，比喊扣 1 有效得多，比如问 A 还是 B。",
                "评论出来就点名念出来，念名字是最低成本的留人动作。",
                "关注要给理由：关注了能拿到什么，说清楚一次。",
                "新号先做关注率和加团率，互动率 4%、关注率 2% 是常见的参考线。"]
    return ["先确认这场直播的目标是什么，拉人、成交还是练话术。",
            "目标定下来才知道该看哪个数。",
            "把今晚的做法记下来，时间点配上动作，下播才有的复盘。",
            f"关于{t}，最有效的方法是连续三场只改一个动作，看数据变化。"]

_OPENERS = [
    "今天聊一个很多主播都卡住的问题：{topic}。",
    "接着讲{topic}，这个是我见过最多主播踩的坑。",
    "有人问我{topic}，我把做法拆成几步说清楚。",
    "这一场专门讲{topic}，你听着对照自己的直播间。",
]
_CLOSERS = [
    "这个动作不用一次全做到位，下一场直播先做其中一条就行。",
    "先把上面这一条试三场，有变化再往下加。",
    "今晚下播就把这个记下来，下一场直接照着做。",
]
_CTA = [
    "觉得有用的话点个关注，我接着讲下一个话题。",
    "想继续听的点个关注，我后面按顺序讲。",
    "有问题打在公屏上，我看到就点名回答。",
]


def draft_from_topic(topic: str, note: str = "", sentences: int = 10) -> str:
    """不依赖 API 的本地生成：按话题类型套结构化段落（要点来自公开复盘方法论）。

    用于百炼文本额度不足/未开通时的兜底；生成后可在页面上直接改。
    开场/收尾用随机变体，避免多套稿循环播放时听起来一模一样。
    """
    topic = (topic or "").strip() or "直播留人"
    pack = _pick_pack(topic)
    lines: list[str] = [random.choice(_OPENERS).format(topic=topic)]
    if note:
        lines.append(note.strip())
    for step in pack:
        lines.append(step)
    lines.append(random.choice(_CLOSERS))
    lines.append(random.choice(_CTA))
    return _clean_draft("\n".join(lines), max(3, min(40, int(sentences or 10))))



# ------------------------------------------------------------------
# 按「直播内容框架提示词」生成新稿（AI生成选项卡 / 开播生成 / 半稿自动生成共用）
# ------------------------------------------------------------------

DEFAULT_TEXT_MODEL = "qwen3.5-plus"

# 内置默认框架提示词：用户没有自己的提示词时用它；页面上可以直接改/另存
DEFAULT_GEN_PROMPT = """你是电台主播的直播稿助手，按下面的直播内容框架写要直接朗读出来的口播稿。
框架：
1) 开场3-5句：先给观众一个留下来的理由，说清这场能听到什么、什么时候讲重点。
2) 正文按段落推进：每段一个主题，每段8-15行，一行只讲一件事；段落之间自然衔接，不写小标题。
3) 内容具体可操作：说清怎么做、看哪个数、阈值大概多少，不写空话口号。
4) 每5-10行做一次小闭环：抛问题、给结论、留一个下期要讲的东西。
5) 穿插互动引导：点名式提问、二选一，引导公屏弹幕。
6) 收尾3-5句：总结这场，预告下一场。
硬性要求：短句、口语化，不用书面语；不要编号、标题、markdown、表情符号；不编造具体账号数据、不承诺收益；不用极限词和功效承诺；只输出稿子正文，不输出解释。"""


def _openai_chat(cfg: dict, system: str, user: str, model: str) -> str:
    """OpenAI 兼容后端（DeepSeek / Moonshot / 本地 Ollama / vLLM / 百炼 compatible-mode 都行）。"""
    import requests
    base = (cfg.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("OpenAI 兼容后端要填 API 地址（Base URL，例 https://api.deepseek.com/v1）")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    key = cfg.get("api_key") or ""
    resp = requests.post(
        url, timeout=120,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "temperature": float(cfg.get("temperature") or 0.8),
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}]})
    if resp.status_code != 200:
        raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        raise RuntimeError("返回里没有 choices[0].message.content，检查一下模型名和地址")


def generate_script_llm(cfg: dict, *, avoid: list[str] | None = None,
                        system_override: str | None = None,
                        api_key_fallback: str | None = None) -> tuple[str, str]:
    """按用户的生成配置写一整稿。cfg: {backend, model, base_url, api_key, prompt, lines}。

    avoid = 本场已经播过的稿名，写进指令里防止同一场反复讲同一个主题。
    返回 (稿子正文, 使用的模型名)。失败抛 RuntimeError（带可读原因）。
    """
    backend = (cfg.get("backend") or "dashscope").strip()
    model = (cfg.get("model") or DEFAULT_TEXT_MODEL).strip()
    lines = max(6, min(200, int(cfg.get("lines") or 40)))
    system = (system_override or cfg.get("prompt") or DEFAULT_GEN_PROMPT).strip()
    user = f"按上面的直播内容框架，生成一场新直播的口播稿，约 {lines} 行，每行一句，主题自定。\n"
    avoid = [a for a in (avoid or []) if a]
    if avoid:
        user += "本场已经播过这些稿子：" + "；".join(avoid) + "。换一个主题和角度，内容和它们不要重复。\n"

    if backend == "openai":
        raw = _openai_chat(cfg, system, user, model)
        text = _clean_draft(raw, lines)
        if not text:
            raise RuntimeError("模型返回空稿")
        return text, model

    try:
        from dashscope import Generation
        import dashscope
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"dashscope 不可用: {e}")
    key = cfg.get("api_key") or api_key_fallback or os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("没有填文本模型的 API Key（AI生成选项卡里填，或用服务器 .env 的 DASHSCOPE_API_KEY）")
    dashscope.api_key = key
    # 首选模型不行就换备选轮一遍（免费额度到顶 / 模型改名 都不该把整条流水线卡死）
    chain = [model] + [m for m in (DEFAULT_TEXT_MODEL, "qwen-plus", "qwen-turbo", "qwen-flash")
                       if m and m != model]
    last_err = ""
    for m in chain:
        for attempt in range(2):
            try:
                r = Generation.call(model=m,
                                    messages=[{"role": "system", "content": system},
                                              {"role": "user", "content": user}],
                                    result_format="message", temperature=0.8)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                break
            if r.status_code == 200:
                try:
                    raw = r.output.choices[0].message.content or ""
                except Exception:
                    raw = ""
                text = _clean_draft(raw, lines)
                if text:
                    return text, m
                last_err = "模型返回空稿"
                break
            code = getattr(r, "code", "")
            last_err = f"{r.status_code} {code} {getattr(r, 'message', '')[:120]}"
            if code == "Throttling.RateQuota":
                time.sleep(2.0 + attempt * 2.0)
                continue
            break   # 额度/权限类错误换下一个模型
    if "AllocationQuota" in last_err or "Free quota" in last_err:
        raise RuntimeError("百炼这个模型免费额度已用完（账号要去百炼控制台充值/开通付费，"
                           "或在「AI生成」页改用别的模型 / 换成 OpenAI 兼容后端）")
    raise RuntimeError(f"生成失败：{last_err}")


def _clean_draft(raw: str, want: int) -> str:
    out: list[str] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*•]\s*", "", line)
        line = re.sub(r"^\d+[.)、]\s*", "", line)
        line = line.strip("*_`#").strip()
        if not line or len(line) < 4:
            continue
        if line.startswith(("说明", "注：", "以上", "如果需要", "希望")):
            continue
        out.append(line)
        if len(out) >= want:
            break
    return "\n".join(out)
