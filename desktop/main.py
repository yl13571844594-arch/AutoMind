"""AutoMind 桌面版入口 — pywebview（WebView2）窗口壳 + 系统托盘 + 内嵌服务。

架构：
    AutoMind.exe
      ├─ 内嵌 uvicorn（127.0.0.1 空闲端口，仅本机监听，守护线程）
      ├─ WebView2 窗口加载 http://127.0.0.1:<port>（Win10/11 系统自带内核）
      ├─ 托盘：显示窗口 / 浏览器打开 / 打开数据目录 / 退出
      └─ 关窗 = 隐藏到托盘（任务与定时调度继续运行）

降级链（保证任何环境都能用）：
    pywebview/WebView2 不可用 → 打开系统默认浏览器；
    pystray/Pillow 不可用   → 无托盘，窗口关闭即退出。

调试参数：
    --server-only   仅启动服务不开窗口（打包冒烟测试 / 无 GUI 环境）
    --port N        指定端口（默认自动选空闲端口，冲突自动换）
    --browser       跳过 WebView 直接用系统浏览器
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

# 源码直跑（python desktop/main.py）时把仓库根加进 sys.path；
# 冻结（PyInstaller）模式包已收集进产物，无需处理。
if not getattr(sys, "frozen", False):
    _ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

# Windows GBK 控制台兜底：诊断输出统一 UTF-8（无控制台/冻结模式下为 None，跳过）
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

APP_TITLE = "AutoMind — 通用自动化 Agent"


def _setup_data_dir() -> str:
    """启动最早期固定数据目录（冻结环境 → %APPDATA%\\AutoMind）。

    显式写回环境变量，保证之后无论何种导入顺序、以及可能的子进程，
    都解析到同一目录。
    """
    from automind.core.paths import data_dir
    d = data_dir()
    # 仅冻结模式写回环境变量（固定 %APPDATA%\AutoMind，含未来子进程）；
    # 源码直跑保持开发语义（cwd/.automind + .automind_config.json）。
    if getattr(sys, "frozen", False):
        os.environ.setdefault("AUTOMIND_DATA_DIR", str(d))
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _pick_port(preferred: int | None) -> int:
    candidates = ([preferred] if preferred else []) + [8765, 0]
    for port in candidates:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port or 0))
                return s.getsockname()[1]
        except OSError:
            continue
    raise RuntimeError("找不到可用端口")


def _start_server(port: int) -> threading.Thread:
    import uvicorn

    from automind.server import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    t = threading.Thread(target=server.run, name="automind-server", daemon=True)
    t.start()
    return t


def _wait_ready(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def _open_folder(path: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])  # noqa: S603,S607
        else:
            subprocess.Popen(["xdg-open", path])  # noqa: S603,S607
    except Exception:
        pass


def _tray_icon_image():
    """托盘图标：优先打包内 icon；缺失时用 Pillow 现画一个渐变圆点。"""
    from pathlib import Path

    from PIL import Image, ImageDraw
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    ico = base / "icon.ico"
    if ico.exists():
        try:
            return Image.open(ico)
        except Exception:
            pass
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i in range(28, 0, -1):  # 简易径向渐变（品牌双色）
        ratio = i / 28
        color = (int(123 + (181 - 123) * (1 - ratio)),
                 int(159 + (148 - 159) * (1 - ratio)),
                 255, 255)
        d.ellipse([32 - i, 32 - i, 32 + i, 32 + i], fill=color)
    return img


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoMind Desktop")
    parser.add_argument("--server-only", action="store_true",
                        help="仅启动服务，不开窗口（冒烟测试用）")
    parser.add_argument("--browser", action="store_true",
                        help="跳过 WebView，用系统默认浏览器打开")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    data_dir = _setup_data_dir()
    port = _pick_port(args.port)
    url = f"http://127.0.0.1:{port}"

    print(f"AutoMind Desktop — 数据目录: {data_dir}")
    print(f"启动服务: {url}")
    _start_server(port)
    if not _wait_ready(url):
        print("!! 服务启动失败（30s 内未就绪），请检查数据目录下日志或以 --server-only 诊断")
        sys.exit(1)
    print("服务就绪 ✓")

    if args.server_only:
        print("--server-only 模式：按 Ctrl+C 退出")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    # ── 托盘（可选组件，失败不阻断）──
    tray = None
    window_ref: list = []   # 延迟绑定 pywebview 窗口

    def show_window(*_):
        if window_ref:
            try:
                window_ref[0].show()
                window_ref[0].restore()
            except Exception:
                webbrowser.open(url)
        else:
            webbrowser.open(url)

    def quit_app(*_):
        try:
            if tray is not None:
                tray.stop()
        except Exception:
            pass
        if window_ref:
            try:
                window_ref[0].destroy()
            except Exception:
                pass
        os._exit(0)   # 守护线程内的 uvicorn 随进程退出

    try:
        import pystray

        tray = pystray.Icon(
            "automind", _tray_icon_image(), APP_TITLE,
            menu=pystray.Menu(
                pystray.MenuItem("显示主窗口", show_window, default=True),
                pystray.MenuItem("在浏览器打开", lambda *_: webbrowser.open(url)),
                pystray.MenuItem("打开数据目录", lambda *_: _open_folder(data_dir)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出 AutoMind", quit_app),
            ))
        threading.Thread(target=tray.run, name="tray", daemon=True).start()
    except Exception as e:
        print(f"托盘不可用（{e}），跳过")
        tray = None

    # ── 窗口：pywebview（WebView2）→ 失败降级系统浏览器 ──
    if not args.browser:
        try:
            import webview

            win = webview.create_window(
                APP_TITLE, url, width=1360, height=860,
                min_size=(960, 640), background_color="#060913")
            window_ref.append(win)

            if tray is not None:
                # 有托盘：关窗仅隐藏，进程与任务继续
                def on_closing():
                    win.hide()
                    return False
                win.events.closing += on_closing

            webview.start()   # 阻塞直至窗口销毁
            quit_app()
            return
        except Exception as e:
            print(f"WebView 不可用（{e}），改用系统浏览器")

    webbrowser.open(url)
    print("已在浏览器打开；关闭本控制台即退出服务" if tray is None
          else "已在浏览器打开；可从托盘图标退出")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        quit_app()


if __name__ == "__main__":
    main()
