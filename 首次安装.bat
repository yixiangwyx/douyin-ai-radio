@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AI电台 - 首次安装

rem ---- 1) 确保有 uv（没有就用官方脚本装；装不了就退回 python -m pip）----
where uv >nul 2>nul
if errorlevel 1 (
  echo 正在安装 uv（第一次需要，之后不再装）...
  curl -fsSL https://astral.sh/uv/install.cmd -o "%TEMP%\uv_install.cmd" && call "%TEMP%\uv_install.cmd"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)
where uv >nul 2>nul
if errorlevel 1 (
  echo [!] 自动装 uv 失败。请装 Python 3.13 后运行：python -m pip install uv -i https://mirrors.aliyun.com/pypi/simple/
  pause
  exit /b 1
)

rem ---- 2) 装项目依赖（国内走阿里云镜像，超时放宽）----
set UV_HTTP_TIMEOUT=180
set UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/
echo 正在装依赖...
uv sync || (echo [!] 装依赖失败，看上面报错再重试 & pause & exit /b 1)

rem ---- 3) 建一个 .env（如果还没有）----
if not exist .env (
  set /p KEY=粘贴你的百炼 API Key（sk-... 直接回车可跳过，之后在页面里填也行）: 
  echo DASHSCOPE_API_KEY=%KEY%> .env
)

echo.
echo 安装完成。以后开播只要双击「启动电台.bat」
pause
