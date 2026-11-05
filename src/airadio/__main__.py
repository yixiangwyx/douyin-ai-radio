"""抖音AI电台：读稿 + 互动插播 + 百炼TTS。

用法:
  uv run python -m airadio --script scripts/demo_script.txt --room 直播间号 [--sink ffplay|file]
"""
from __future__ import annotations

import argparse
import json
import pathlib

from .orchestrator import Orchestrator
from .timeline import ScheduleConfig


def main():
    ap = argparse.ArgumentParser(description="抖音AI电台直播系统")
    ap.add_argument("--room", required=True, help="直播间号（live.douyin.com/后的数字）或直播间URL")
    ap.add_argument("--script", required=True, help="直播稿文件路径")
    ap.add_argument("--voice", default="longxiaochun_v2", help="百炼音色（cosyvoice-v2）")
    ap.add_argument("--model", default="cosyvoice-v2")
    ap.add_argument("--rate", type=float, default=1.0, help="语速 0.5-2.0")
    ap.add_argument("--sink", default="file", choices=["file", "ffplay", "dry"],
                    help="file=写 out/chunks 供浏览器播放；ffplay=本机扬声器外放；dry=不合成只验证调度")
    ap.add_argument("--min-gap", type=float, default=45.0, help="插播最小间隔秒")
    ap.add_argument("--window", default="60,150", help="随机插播窗口秒，如 60,150")
    ap.add_argument("--loops", type=int, default=0, help="稿子循环次数，0=无限")
    ap.add_argument("--out", default="out/chunks")
    args = ap.parse_args()

    script_text = pathlib.Path(args.script).read_text(encoding="utf-8")
    w0, w1 = (float(x) for x in args.window.split(","))
    cfg = ScheduleConfig(min_gap_s=args.min_gap, window_s=(w0, w1))

    orch = Orchestrator(args.room, script_text, schedule=cfg, voice=args.voice,
                        model=args.model, speech_rate=args.rate,
                        out_dir=args.out, sink=args.sink)
    print(f"room={args.room} voice={args.voice} sink={args.sink}")
    try:
        orch.run(max_loops=args.loops)
    except KeyboardInterrupt:
        pass
    print("\n--- 播放/插播日志 ---")
    for line in orch.log:
        print(line)
    print("\n--- 运行统计 ---")
    print(json.dumps({
        "status": orch.status,
        "queue_stats": dict(orch.queue.stats),
        "inserts": orch.scheduler.inserted_count,
        "tts_error": orch.tts.last_error if orch.tts else "(dry模式未合成)",
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
