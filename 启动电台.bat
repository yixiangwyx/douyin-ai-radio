@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM 双击这个文件 = 启动 AI电台（PC 单机版），浏览器会自动打开
where uv >nul 2>nul
if %errorlevel%==0 (
  uv run python web\app.py
) else (
  python web\app.py
)
pause
