"""探针3：v3.5-flash 能不能用别的模型（v2）建的音色（用项目自己的 TTS 封装实测）。"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from airadio.tts import TTSStreamer  # noqa: E402

key = os.environ.get("DASHSCOPE_API_KEY", "")
cases = [
    ("cosyvoice-v3.5-flash", "cosyvoice-v2-vd-male55071-3dd2c7524bdc433e8214aa477a9ac3b3"),
    ("cosyvoice-v2", "cosyvoice-v2-vd-male55071-3dd2c7524bdc433e8214aa477a9ac3b3"),
    ("cosyvoice-v3.5-flash", "cosyvoice-v3.5-flash-vd-casi12313-fab98a1f7958405c8430e0a20d9fcbd5"),
    ("cosyvoice-v2", "cosyvoice-v3.5-flash-vd-casi12313-fab98a1f7958405c8430e0a20d9fcbd5"),
]
for model, voice in cases:
    try:
        t = TTSStreamer(api_key=key, model=model, voice=voice)
        audio = t.synth(["测试一句话，看看这个音色能不能用。"])
        print("OK  ", model, "<-", voice[:42], "bytes:", len(audio or b""))
    except Exception as e:
        print("FAIL", model, "<-", voice[:42], type(e).__name__, str(e)[:180])
