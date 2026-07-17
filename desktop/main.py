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

_LOG_FILE = None   # 冻结模式启动日志（%APPDATA%\AutoMind\desktop.log）


def _log(msg: str) -> None:
    """打印 + （冻结模式）落盘：无控制台时问题也能事后追溯。"""
    print(msg)
    if _LOG_FILE is not None:
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            pass


def _alert(title: str, text: str) -> None:
    """原生消息框（无控制台的冻结模式下向用户呈现错误）。"""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)  # MB_ICONERROR
            return
        except Exception:
            pass
    print(f"{title}: {text}", file=sys.stderr or sys.stdout)


def _setup_data_dir() -> str:
    """启动最早期固定数据目录（冻结环境 → %APPDATA%\\AutoMind）。

    显式写回环境变量，保证之后无论何种导入顺序、以及可能的子进程，
    都解析到同一目录。
    """
    global _LOG_FILE
    from automind.core.paths import data_dir
    d = data_dir()
    # 仅冻结模式写回环境变量（固定 %APPDATA%\AutoMind，含未来子进程）；
    # 源码直跑保持开发语义（cwd/.automind + .automind_config.json）。
    if getattr(sys, "frozen", False):
        os.environ.setdefault("AUTOMIND_DATA_DIR", str(d))
    d.mkdir(parents=True, exist_ok=True)
    if getattr(sys, "frozen", False):
        _LOG_FILE = str(d / "desktop.log")
        try:   # 日志滚动：超 512KB 重开
            if os.path.getsize(_LOG_FILE) > 512 * 1024:  # noqa: PTH202
                os.remove(_LOG_FILE)  # noqa: PTH107
        except OSError:
            pass
        # 段错误级崩溃也留痕（faulthandler 需真实文件句柄）
        try:
            import faulthandler
            global _FAULT_FH
            _FAULT_FH = open(str(d / "desktop-crash.log"), "a",  # noqa: SIM115
                             encoding="utf-8")
            faulthandler.enable(_FAULT_FH)
        except Exception:
            pass
    return str(d)


_FAULT_FH = None


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

    _log(f"AutoMind Desktop v{_app_version()} — 数据目录: {data_dir}")
    _log(f"启动服务: {url}")
    _start_server(port)
    if not _wait_ready(url):
        _log("!! 服务启动失败（30s 内未就绪）")
        _alert("AutoMind 启动失败",
               "内置服务 30 秒内未就绪。\n\n"
               f"诊断日志：{data_dir}\\desktop.log\n"
               "可尝试：命令行运行 AutoMind.exe --server-only 查看详细输出。")
        sys.exit(1)
    _log("服务就绪 ✓")

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
        _log(f"托盘不可用（{e}），跳过")
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

            _log("打开 WebView2 窗口…")
            # storage_path：WebView2 用户数据固定进数据目录 ——
            # 安装到 Program Files（只读）后默认写 exe 旁目录会失败
            webview.start(storage_path=os.path.join(data_dir, "webview"),  # noqa: PTH118
                          private_mode=False)
            quit_app()
            return
        except Exception as e:
            _log(f"WebView 不可用（{e}），改用系统浏览器")

    webbrowser.open(url)
    _log("已在浏览器打开；可从托盘图标退出" if tray is not None
         else "已在浏览器打开；关闭本进程即退出服务")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        quit_app()


def _app_version() -> str:
    try:
        from automind import __version__
        return __version__
    except Exception:
        return "?"


def _excepthook(exc_type, exc, tb) -> None:
    """未捕获异常：写错误日志 + 弹窗指引（无控制台的冻结模式不再静默死亡）。"""
    import traceback
    detail = "".join(traceback.format_exception(exc_type, exc, tb))
    _log("!! 未捕获异常:\n" + detail)
    log_hint = _LOG_FILE or "(控制台输出)"
    try:
        err_file = None
        if _LOG_FILE:
            err_file = _LOG_FILE.replace("desktop.log", "desktop-error.log")
            with open(err_file, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n{detail}\n")
            log_hint = err_file
    except Exception:
        pass
    _alert("AutoMind 遇到错误",
           f"{exc_type.__name__}: {exc}\n\n完整错误已保存到：\n{log_hint}\n\n"
           "请把该文件反馈给开发者以便修复。")


if __name__ == "__main__":
    sys.excepthook = _excepthook
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        _excepthook(*sys.exc_info())
        sys.exit(1)
