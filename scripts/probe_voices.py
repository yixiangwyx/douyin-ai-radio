"""probe_voices.py — 逐音色实测：哪些模型能直接用哪些系统音色（用于前端下拉目录）。

用法: set -a && . ./.env && set +a && uv run python scripts/probe_voices.py
结果写 data/verified_voices.json（供 airadio.models 兜底）。

为什么要实测：百炼的音色与模型是绑定的，官方音色表里根本没有 cosyvoice-v3.5 的系统音色，
v3.5 系列只能配合"声音设计（提示词）/声音复刻（录音）"生成的专属音色使用，
直接填 longanyang 这类系统音色会被拒：`InvalidParameter: [cosyvoice:]Engine return error code: 418`。
"""
from __future__ import annotations

import json
import os
import pathlib
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]

CANDIDATES = {
    "cosyvoice-v2": [
        "longxiaochun_v2", "longxiaoxia_v2", "longanxuan", "longanchong", "longanping", "longanran",
        "longcheng_v2", "longanyun", "longanqin", "longyan_v2", "longtian_v2", "longanling",
        "longanyue", "longxiaobai_v2", "longshu", "longshi", "longcheng", "loongstella",
    ],
    "cosyvoice-v3-flash": [
        "longanyang", "longanhuan_v3", "longhuhu_v3", "longpaopao_v3", "longxian_v3", "longling_v3",
        "longshanshan_v3", "longniuniu_v3", "longjiaxin_v3", "longjiayi_v3", "longanyue_v3",
        "longlaotie_v3", "longshange_v3", "longmiao_v3", "longyuan_v3", "longxiu_v3",
        "longjielidou_v3", "longxiaoxia_v3", "longanyun_v3", "longanwen_v3", "longanrou_v3",
        "longanxuan_v3", "longanran_v3", "longshu_v3", "loongbella_v3", "longyingxiao_v3",
        "longtian_v3", "longhua_v3", "longcheng_v3", "longyan_v3", "longwan_v3", "longanzhi_v3",
        "longxiaochun_v3", "longanyue_v3", "longshuo_v3",
    ],
    "cosyvoice-v3-plus": ["longanyang", "longanhuan"],
    "cosyvoice-v3.5-flash": ["longanyang", "longxiaochun_v2", "longanhuan_v3", "longanyue_v3"],
    "cosyvoice-v3.5-plus": ["longanyang", "longxiaochun_v2"],
    "cosyvoice-v1": ["longxiaochun", "longxiaoxia", "longlaotie", "longshu"],
}


def synth_ok(api_key: str, model: str, voice: str) -> bool:
    body = {"model": model, "input": {"text": "测试。", "voice": voice,
                                      "format": "mp3", "sample_rate": 24000}}
    req = urllib.request.Request(
        "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    last = ""
    for attempt in range(3):        # 并发太快会被限流（看起来像"音色不可用"），所以要重试
        try:
            r = json.load(urllib.request.urlopen(req, timeout=30))
            if ((r.get("output") or {}).get("audio") or {}).get("url"):
                return True
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="ignore")
            if "418" in text or "InvalidParameter" in text:
                return False       # 音色与该模型不匹配：确定不可用
            last = f"HTTP{e.code}"
        except Exception as e:
            last = type(e).__name__
        time.sleep(1.2 * (attempt + 1))
    print(f"  ? {model} {voice}: 探测失败({last})，按不可用处理")
    return False


def main() -> None:
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        raise SystemExit("需要 DASHSCOPE_API_KEY（先 . ./.env）")
    verified: dict[str, list[str]] = {}
    for model, voices in CANDIDATES.items():
        ok = [v for v in voices if synth_ok(api_key, model, v)]
        verified[model] = ok
        print(f"{model}: {len(ok)}/{len(voices)} 可用 -> {ok}")
    out = ROOT / "data" / "verified_voices.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(verified, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved ->", out)


if __name__ == "__main__":
    main()
