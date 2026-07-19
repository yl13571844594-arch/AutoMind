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

# ── 标准流兜底（双击启动的根因修复）────────────────────
# console=False 的冻结 GUI 进程从资源管理器双击启动时 sys.stdout/stderr
# 为 None：uvicorn 配置日志会调 sys.stdout.isatty() → AttributeError，
# logging 的 StreamHandler 写 stderr 也会炸。装一个行为完整的空流桩
# （isatty=False / write 丢弃），所有第三方库的隐式假设即恢复成立。
# （从终端/管道启动时句柄存在，不走此分支 —— 这正是测试没暴露的原因。）
import io as _io


class _NullStdIO(_io.TextIOBase):
    encoding = "utf-8"

    def write(self, s: str) -> int:  # noqa: D102
        return len(s)

    def flush(self) -> None:  # noqa: D102
        pass

    def isatty(self) -> bool:  # noqa: D102
        return False


if sys.stdout is None:
    sys.stdout = _NullStdIO()
if sys.stderr is None:
    sys.stderr = _NullStdIO()

# Windows GBK 控制台兜底：诊断输出统一 UTF-8
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
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


# ── 单实例互斥（Windows 命名互斥体）─────────────────────
# 用户在"没反应"的几十秒里往往会连点数次 → 多实例抢端口、互相拖慢。
# 重复启动时：读取上个实例记录的地址，直接用浏览器打开它并退出。
_MUTEX_NAME = "Local\\AutoMindDesktopSingleton"
_ERROR_ALREADY_EXISTS = 183


def _acquire_single_instance() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        return ctypes.windll.kernel32.GetLastError() != _ERROR_ALREADY_EXISTS
    except Exception:
        return True   # 互斥体异常不阻断启动


def _instance_file(data_dir: str) -> str:
    return os.path.join(data_dir, "instance.json")  # noqa: PTH118


def _write_instance(data_dir: str, port: int) -> None:
    try:
        import json
        with open(_instance_file(data_dir), "w", encoding="utf-8") as f:
            json.dump({"port": port, "pid": os.getpid()}, f)
    except Exception:
        pass


def _read_instance_url(data_dir: str) -> str | None:
    """读取已运行实例的地址并确认健康（直连探测）；不健康返回 None。"""
    try:
        import json
        with open(_instance_file(data_dir), encoding="utf-8") as f:
            port = int(json.load(f).get("port", 0))
        if port:
            url = f"http://127.0.0.1:{port}"
            if _probe(url):
                return url
    except Exception:
        pass
    return None


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


_server_error: list[str] = []   # 服务线程内异常（供就绪等待快速失败与留痕）


def _start_server(port: int) -> threading.Thread:
    def _run() -> None:
        try:
            import uvicorn

            from automind.server import app

            # use_colors=False：显式关闭彩色，避免 uvicorn 探测终端（isatty）
            config = uvicorn.Config(app, host="127.0.0.1", port=port,
                                    log_level="warning", access_log=False,
                                    use_colors=False)
            uvicorn.Server(config).run()
        except Exception:
            import traceback
            detail = traceback.format_exc()
            _server_error.append(detail)
            _log("!! 服务线程异常:\n" + detail)

    t = threading.Thread(target=_run, name="automind-server", daemon=True)
    t.start()
    return t


# 冷启动（尤其杀软首次逐文件扫描 onedir 的上千个模块）可能远超 30s
_READY_TIMEOUT = 90.0

# 本机探测必须绕过系统代理：用户开着代理/VPN 时（国内极常见），
# urllib 默认走系统代理 → 代理回连不了用户本机 127.0.0.1 →
# 服务明明健康、探测却全部失败，表现为"启动失败/界面打不开"。
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _probe(url: str, timeout: float = 2.0) -> bool:
    try:
        with _DIRECT.open(url + "/api/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_ready(url: str, timeout: float = _READY_TIMEOUT,
                thread: threading.Thread | None = None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _probe(url):
            return True
        # 服务线程已死（端口被抢 / import 失败）→ 立即失败，不空等
        if _server_error or (thread is not None and not thread.is_alive()):
            return False
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


# 启动画面：双击后窗口立即出现（冷启动 + 杀软扫描可能要几十秒，
# 没有即时反馈用户会以为没点上而反复双击 → 多实例雪崩）
_SPLASH_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
html,body{height:100%;margin:0;background:#060913;color:#e8ecf7;
  font-family:'Segoe UI','Microsoft YaHei',sans-serif;overflow:hidden}
.wrap{height:100%;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:18px}
.dot{width:52px;height:52px;border-radius:50%;
  background:linear-gradient(135deg,#7b9fff,#b594ff);
  box-shadow:0 0 44px rgba(123,159,255,.55);animation:pulse 1.6s ease infinite}
@keyframes pulse{50%{transform:scale(.86);box-shadow:0 0 20px rgba(123,159,255,.35)}}
h1{font-size:1.25em;font-weight:600;margin:0}
p{color:#8e9abb;font-size:.85em;margin:0}
.bar{width:220px;height:4px;border-radius:2px;background:#1f2740;overflow:hidden}
.bar i{display:block;height:100%;width:40%;border-radius:2px;
  background:linear-gradient(90deg,#7b9fff,#b594ff);animation:slide 1.4s ease-in-out infinite}
@keyframes slide{0%{margin-left:-40%}100%{margin-left:100%}}
</style></head><body><div class="wrap">
<div class="dot"></div><h1>AutoMind 正在启动</h1>
<div class="bar"><i></i></div>
<p>首次启动可能需要一分钟（安全软件扫描），请稍候…</p>
</div></body></html>"""


def _fail_html(data_dir: str, reason: str) -> str:
    import html as _html
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
html,body{{height:100%;margin:0;background:#060913;color:#e8ecf7;
  font-family:'Segoe UI','Microsoft YaHei',sans-serif}}
.wrap{{height:100%;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:14px;padding:0 40px;text-align:center}}
h1{{font-size:1.2em;margin:0;color:#ff6b6b}}
p{{color:#8e9abb;font-size:.86em;margin:0;line-height:1.8}}
code{{background:#1f2740;border-radius:6px;padding:2px 8px;font-size:.84em}}
</style></head><body><div class="wrap">
<h1>⚠ AutoMind 启动失败</h1>
<p>{_html.escape(reason)}</p>
<p>诊断日志：<code>{_html.escape(data_dir)}\\desktop.log</code><br>
请关闭本窗口后重试；若反复失败，把日志文件反馈给开发者。</p>
</div></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoMind Desktop")
    parser.add_argument("--server-only", action="store_true",
                        help="仅启动服务，不开窗口（冒烟测试用）")
    parser.add_argument("--browser", action="store_true",
                        help="跳过 WebView，用系统默认浏览器打开")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    data_dir = _setup_data_dir()

    # ── 单实例：已有实例在跑 → 打开它的界面后退出（多次双击不再雪崩）──
    if not args.server_only and not _acquire_single_instance():
        existing = _read_instance_url(data_dir)
        _log(f"检测到已运行实例：{existing or '(尚未就绪)'}")
        if existing:
            webbrowser.open(existing)
        else:
            _alert("AutoMind 已在运行",
                   "AutoMind 正在启动中（或驻留在系统托盘）。\n"
                   "请稍候几秒，或点击托盘图标打开主窗口。")
        return

    port = _pick_port(args.port)
    url = f"http://127.0.0.1:{port}"

    _log(f"AutoMind Desktop v{_app_version()} — 数据目录: {data_dir}")
    _log(f"启动服务: {url}")
    # 系统代理留痕（本机探测已强制直连；WebView2 亦强制 direct://）
    try:
        proxies = urllib.request.getproxies()
        if proxies:
            _log(f"检测到系统代理: {sorted(proxies)}（本机连接将绕过）")
    except Exception:
        pass
    # WebView2 只用于加载本机界面 → 强制直连，杜绝代理拦截 127.0.0.1
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                          "--proxy-server=direct://")
    server_thread = _start_server(port)

    if args.server_only:
        if not _wait_ready(url, thread=server_thread):
            _log("!! 服务启动失败")
            sys.exit(1)
        _log("服务就绪 ✓")
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
            os.remove(_instance_file(data_dir))  # noqa: PTH107
        except OSError:
            pass
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
    # 窗口立即打开显示启动画面；服务就绪后切到真实界面（后台线程驱动）。
    if not args.browser:
        try:
            import webview

            win = webview.create_window(
                APP_TITLE, html=_SPLASH_HTML, width=1360, height=860,
                min_size=(960, 640), background_color="#060913")
            window_ref.append(win)

            if tray is not None:
                # 有托盘：关窗仅隐藏，进程与任务继续
                def on_closing():
                    win.hide()
                    return False
                win.events.closing += on_closing

            def _boot(w) -> None:
                if _wait_ready(url, thread=server_thread):
                    _log("服务就绪 ✓ → 加载主界面")
                    _write_instance(data_dir, port)
                    w.load_url(url)
                    return
                # 兜底复核：超时后再直连探测一次 —— 若服务其实健康
                # （曾见探测被环境因素干扰），仍然加载主界面而非报错
                if _probe(url, timeout=4):
                    _log("超时但复核健康 → 加载主界面")
                    _write_instance(data_dir, port)
                    w.load_url(url)
                    return
                reason = ("内置服务异常退出（端口冲突或组件缺失）"
                          if _server_error or not server_thread.is_alive()
                          else f"内置服务 {int(_READY_TIMEOUT)} 秒内未就绪"
                               "（可能是安全软件扫描拖慢，可重试）")
                _log(f"!! 启动失败：{reason}")
                w.load_html(_fail_html(data_dir, reason))

            _log("打开 WebView2 窗口（启动画面）…")
            # storage_path：WebView2 用户数据固定进数据目录 ——
            # 安装到 Program Files（只读）后默认写 exe 旁目录会失败
            webview.start(_boot, win,
                          storage_path=os.path.join(data_dir, "webview"),  # noqa: PTH118
                          private_mode=False)
            quit_app()
            return
        except Exception as e:
            _log(f"WebView 不可用（{e}），改用系统浏览器")

    # 浏览器降级路径：等就绪再开，避免打开空白页
    if not _wait_ready(url, thread=server_thread):
        _log("!! 服务启动失败")
        _alert("AutoMind 启动失败",
               f"内置服务未能就绪。\n诊断日志：{data_dir}\\desktop.log")
        sys.exit(1)
    _write_instance(data_dir, port)
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
