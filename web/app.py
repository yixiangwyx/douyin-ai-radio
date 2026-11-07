#!/usr/bin/env python3
"""app.py — PC 端直接运行的 AI电台（单机模式）。

一条命令启动，自动打开本机浏览器：
  uv run python web/app.py
免登录、没有前后端部署的事：这就是一个本机进程 + 本机页面。
电台不会因「页面关了 90 秒」自动停止 —— 只有你点「停止」才停。

想要多人云端版仍用: uv run python web/console.py
"""
import sys, pathlib  # noqa: E401
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import console  # noqa: E402


def run():
    argv = ["--local", "--open"] + sys.argv[1:]
    console.main(argv)


if __name__ == "__main__":
    run()
