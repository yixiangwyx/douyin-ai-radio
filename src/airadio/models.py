"""可用的语音合成模型与系统音色目录（前端自由切换用）。

实测 + 官方音色表核对的事实：
- cosyvoice-v3.5-plus / cosyvoice-v3.5-flash **没有系统音色**（本账号实测 0 个可用；
  官方音色列表里也没有 v3.5 章节）。它们只能用"声音设计（填提示词）"或
  "声音复刻（录音）"生成的专属音色。填 longanyang 这类系统音色会被拒：
  `InvalidParameter: [cosyvoice:]Engine return error code: 418`。
- 音色绑定生成时的 target_model，跨模型不能复用；换模型要重新生成音色。
- 系统音色清单可用 `uv run python scripts/probe_voices.py` 重测，
  结果写 data/verified_voices.json，本模块会自动读它并只保留实测可用的。
"""
from __future__ import annotations

import json
import pathlib

# 官方音色表（名称/特质），实测脚本会把它过滤成"这个账号真正能用的"
_OFFICIAL = {
    "cosyvoice-v3-flash": [
        ("longanyang", "龙安洋", "阳光大男孩·直播/社交陪伴（标杆音色）"),
        ("longanhuan_v3", "龙安欢(V3)", "欢脱元气女·支持八种方言"),
        ("longanxuan_v3", "龙安宣", "经典直播女·直播带货"),
        ("longanran_v3", "龙安燃", "活泼质感女·直播带货"),
        ("longshuo_v3", "龙硕", "博才干练男·新闻播报"),
        ("longshu_v3", "龙书", "沉稳青年男"),
        ("loongbella_v3", "Bella3.0", "精准干练女·新闻播报"),
        ("longxiaochun_v3", "龙小淳", "知性积极女·客服"),
        ("longxiaoxia_v3", "龙小夏", "沉稳权威女·客服"),
        ("longanyun_v3", "龙安昀", "居家暖男"),
        ("longanwen_v3", "龙安温", "优雅知性女"),
        ("longanrou_v3", "龙安柔", "温柔闺蜜女"),
        ("longlaotie_v3", "龙老铁", "东北直率男"),
        ("longshange_v3", "龙陕哥", "原味陕北男"),
        ("longanyue_v3", "龙安粤", "欢脱粤语男"),
        ("longjiaxin_v3", "龙嘉欣", "优雅粤语女"),
        ("longjiayi_v3", "龙嘉怡", "知性粤语女"),
        ("longmiao_v3", "龙妙", "抑扬顿挫女·有声书"),
        ("longyuan_v3", "龙媛", "温暖治愈女·有声书"),
        ("longxiu_v3", "龙修", "博才说书男·有声书"),
        ("longtian_v3", "龙天", "磁性理智男"),
        ("longcheng_v3", "龙橙", "智慧青年男"),
        ("longwan_v3", "龙婉", "细腻柔声女"),
        ("longyan_v3", "龙颜", "温暖春风女"),
        ("longhua_v3", "龙花", "知性快捷女"),
        ("longanzhi_v3", "龙安智", "睿智轻熟男"),
        ("longyingxiao_v3", "龙应笑", "元气直播女"),
        ("longjielidou_v3", "龙杰力豆", "阳光顽皮男童声"),
        ("longhuhu_v3", "龙呼呼", "天真烂漫女童·社交陪伴"),
        ("longxian_v3", "龙仙", "豪放可爱女·少女音"),
        ("longling_v3", "龙铃", "稚气呆板女·少女音"),
        ("longpaopao_v3", "龙泡泡", "飞天泡泡音·儿童故事机"),
        ("longshanshan_v3", "龙闪闪", "戏剧化童声·儿童有声书"),
        ("longniuniu_v3", "龙牛牛", "阳光男童声"),
    ],
    "cosyvoice-v3-plus": [
        ("longanyang", "龙安洋", "阳光大男孩"),
        ("longanhuan", "龙安欢", "欢脱元气女"),
    ],
    "cosyvoice-v2": [
        ("longxiaochun_v2", "龙小淳", "知性积极女·默认音色"),
        ("longxiaoxia_v2", "龙小夏", "沉稳女"),
        ("longanxuan", "龙安宣", "经典直播女·直播带货"),
        ("longanran", "龙安燃", "活泼质感女·直播带货"),
        ("longanchong", "龙安冲", "激情推销男·直播带货"),
        ("longanping", "龙安萍", "高亢直播女·直播带货"),
        ("longxiaobai_v2", "龙小白", "清澈男"),
        ("longcheng_v2", "龙橙", "智慧青年男"),
        ("longanyun", "龙安昀", "居家暖男"),
        ("longanqin", "龙安亲", "亲和活泼女"),
        ("longanling", "龙安灵", "思维灵动女"),
        ("longanyue", "龙安粤", "欢脱粤语男"),
        ("longyan_v2", "龙颜", "温暖春风女"),
        ("longtian_v2", "龙天", "磁性理智男"),
    ],
}

_MODELS = [
    {
        "model": "cosyvoice-v3.5-flash",
        "label": "cosyvoice-v3.5-flash（最新·只能用自建音色）",
        "needs_custom_voice": True,
        "note": "这个模型没有系统音色：必须先用「填提示词生成音色」或「录音复刻」生成音色再选它。",
    },
    {
        "model": "cosyvoice-v3.5-plus",
        "label": "cosyvoice-v3.5-plus（最新·只能用自建音色）",
        "needs_custom_voice": True,
        "note": "同上：只支持声音设计（提示词）/声音复刻（录音）生成的专属音色。",
    },
    {
        "model": "cosyvoice-v3-flash",
        "label": "cosyvoice-v3-flash（系统音色最多·支持方言/情感指令）",
        "needs_custom_voice": False,
        "note": "可直接选系统音色；也可用自建音色。",
    },
    {
        "model": "cosyvoice-v3-plus",
        "label": "cosyvoice-v3-plus",
        "needs_custom_voice": False,
        "note": "系统音色少（龙安洋/龙安欢）。",
    },
    {
        "model": "cosyvoice-v2",
        "label": "cosyvoice-v2（稳定·当前默认）",
        "needs_custom_voice": False,
        "note": "系统音色可用，含直播带货类音色。",
    },
]

_VERIFIED_PATH = pathlib.Path(__file__).resolve().parents[2] / "data" / "verified_voices.json"


def _verified() -> dict:
    try:
        return json.loads(_VERIFIED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def catalog() -> list[dict]:
    """给前端的模型/音色目录：系统音色只列实测可用的（没实测文件时用官方清单）。"""
    verified = _verified()
    out = []
    for m in _MODELS:
        name = m["model"]
        official = _OFFICIAL.get(name, [])
        if name in verified:                       # 有实测结果 → 只保留实测可用的
            ok = set(verified[name])
            voices = [{"voice": v, "name": n, "trait": t}
                      for (v, n, t) in official if v in ok]
        else:
            voices = [{"voice": v, "name": n, "trait": t} for (v, n, t) in official]
        item = dict(m)
        item["system_voices"] = voices
        out.append(item)
    return out


def model_names() -> list[str]:
    return [m["model"] for m in _MODELS]


def model_info(model: str) -> dict | None:
    for m in _MODELS:
        if m["model"] == (model or "").strip():
            return m
    return None


def default_voice_for(model: str) -> str:
    info = model_info(model)
    if not info:
        return ""
    voices = _OFFICIAL.get(model, [])
    if info["needs_custom_voice"]:
        return ""                      # v3.5：必须自建音色
    return voices[0][0] if voices else ""


def is_usable_voice(model: str, voice: str) -> bool:
    """自建（复刻/设计）音色一律放行；系统音色只认该模型清单里的。

    自建音色命名：复刻=`{model}-{prefix}-{uuid}`，设计=`{model}-vd-{prefix}-{uuid}`，
    都以 model 名开头。
    """
    voice = (voice or "").strip()
    if not voice:
        return False
    model = (model or "").strip()
    if voice.startswith(model + "-") or voice.startswith("qwen"):
        return True
    info = model_info(model)
    if not info:
        return True          # 未知模型不拦，交给百炼报错
    allowed = {v for (v, _, _) in _OFFICIAL.get(model, [])}
    verified = _verified()
    if model in verified:
        allowed |= set(verified[model])
    return voice in allowed
