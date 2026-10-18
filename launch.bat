@echo off
chcp 65001 >nul
title AutoMind Agent Server

echo.
echo ╔══════════════════════════════════════════════════╗
echo ║         AutoMind — 通用自动化 Agent              ║
echo ╚══════════════════════════════════════════════════╝
echo.
echo   启动选项:
echo     [1] 启动 Web UI 服务器 (http://localhost:8765)
echo     [2] 启动 Web UI + 自动打开浏览器
echo     [3] 进入 CLI 交互模式
echo     [4] 运行端到端演示
echo     [5] 依赖自检（缺什么、怎么装，一目了然）
echo     [6] 依赖自检 + 一键补齐缺失的 Python 库
echo     [7] 退出
echo.
set /p choice="请输入选项 (1-7): "

if "%choice%"=="1" goto web
if "%choice%"=="2" goto web_browser
if "%choice%"=="3" goto cli
if "%choice%"=="4" goto demo
if "%choice%"=="5" goto doctor
if "%choice%"=="6" goto doctor_fix
if "%choice%"=="7" exit /b 0
goto end

:web
echo.
echo 正在启动 AutoMind Web Server...
echo 打开浏览器访问: http://localhost:8765
echo 按 Ctrl+C 停止服务器
echo.
python -m automind.server --host 0.0.0.0 --port 8765
goto end

:web_browser
echo.
echo 正在启动 AutoMind Web Server...
start http://localhost:8765
python -m automind.server --host 0.0.0.0 --port 8765
goto end

:cli
echo.
echo 正在启动 AutoMind CLI...
python -m automind.cli.app --mode plan_and_execute
goto end

:demo
echo.
echo 正在运行端到端演示...
python demo/e2e_demo.py
pause
goto end

:doctor
echo.
echo 正在检查依赖...
python -m automind.core.dep_preflight
echo.
pause
goto end

:doctor_fix
echo.
echo 正在检查并补齐依赖（外部程序需按提示单独安装）...
python -m automind.core.dep_preflight --fix
echo.
pause
goto end

:end
