"""实测：本机 OpenAI 兼容模型能否按保存的「直播内容框架提示词」写出直播稿。

用法: AIRADIO_LOCAL_LLM=http://<机器>:8080/v1 uv run python scripts/probe_local_draft.py [模型名]
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airadio import generate as gen
from airadio.library import Library

BASE = os.environ.get("AIRADIO_LOCAL_LLM", "").strip()
if not BASE:
    print("先设环境变量 AIRADIO_LOCAL_LLM（例 http://<机器>:8080/v1）")
    sys.exit(1)

lib = Library(ROOT / "data" / "users" / "local" / "library.json")
snap = lib.snapshot()
templates = snap.get("gen_templates") or []
if not templates:
    print("稿子框架提示词为空，先用页面保存一套")
    sys.exit(1)
tpl = templates[0]
model = sys.argv[1] if len(sys.argv) > 1 else ""
cfg = {"backend": "openai", "base_url": BASE, "api_key": "", "model": model,
       "lines": 12, "temperature": 0.8}
text, used = gen.generate_script_llm(cfg, avoid=[],
                                     system_override=tpl.get("prompt") or tpl.get("text") or "")
print(f"框架：{tpl.get('name')} | 模型：{used}")
print(text)
