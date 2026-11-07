"""探针：一台 OpenAI 兼容的本地模型能不能直接生成直播稿（不花百炼文本额度）。

用法: AIRADIO_LOCAL_LLM=http://<你的机器>:8080/v1 uv run python scripts/probe_local_llm.py [模型名]
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from airadio import generate as gen  # noqa: E402

cfg = {
    "backend": "openai",
    "base_url": os.environ.get("AIRADIO_LOCAL_LLM", "http://127.0.0.1:8080/v1"),
    "model": sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AIRADIO_LOCAL_LLM_MODEL", ""),
    "api_key": os.environ.get("AIRADIO_LOCAL_LLM_KEY", ""),
    "lines": 12,
}
try:
    out = gen.generate_script_llm(cfg, avoid=[], system_override=None, api_key_fallback="")
    text, model = out if isinstance(out, tuple) else (out, cfg["model"])
    print("OK model:", model, "行:", len(text.splitlines()))
    print(text[:400])
except Exception as e:
    print("FAIL", type(e).__name__, str(e)[:300])
