#!/usr/bin/env bash
# 双击/执行这个脚本 = 启动 AI电台（PC 单机版），浏览器会自动打开
cd "$(dirname "$0")"
if command -v uv >/dev/null 2>&1; then
  exec uv run python web/app.py
else
  exec python3 web/app.py
fi
