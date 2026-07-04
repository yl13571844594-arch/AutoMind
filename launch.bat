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
echo     [5] 退出
echo.
set /p choice="请输入选项 (1-5): "

if "%choice%"=="1" goto web
if "%choice%"=="2" goto web_browser
if "%choice%"=="3" goto cli
if "%choice%"=="4" goto demo
if "%choice%"=="5" exit /b 0
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

:end
