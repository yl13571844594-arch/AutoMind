"""内置工具的公共基座 —— 可选依赖懒加载 + 社区/专业能力分级。

两个反复出现的需求集中放在这里，避免每个工具各写一遍：

1. **可选依赖懒加载**。办公类工具要用 openpyxl / python-docx / pypdf 这些
   第三方库，但它们不该成为 ``pip install automind-agent`` 的硬依赖 ——
   只想跑对话的用户没道理被拖上一堆 Office 解析库。所以：工具**照常注册**
   （模型能看到、能被规划到），只有真正调用时才 import；缺库时返回一句
   可照抄的安装命令，而不是抛一个让模型无从下手的 ImportError。

2. **能力分级**。社区版开放"基础能力"，进阶能力留给专业版/企业版。
   分级的粒度是**动作（action）**而不是整个工具 —— 让社区版用户能真正用起来
   Excel/Word/PDF，只是碰不到样式引擎、OCR、批量流水线这些进阶动作。
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import shutil
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from automind.core.edition import (
    FeatureNotAvailable,
    get_feature,
    has_feature,
    upgrade_hint,
)
from automind.core.types import ToolResult

# ── 可选依赖 ────────────────────────────────────────────────

#: 模块名 -> (pip 包名, 用途说明)
OPTIONAL_DEPS: dict[str, tuple[str, str]] = {
    "openpyxl": ("openpyxl>=3.1", "Excel 读写"),
    "docx": ("python-docx>=1.1", "Word 读写"),
    "pypdf": ("pypdf>=4.0", "PDF 解析与合并"),
    "icalendar": ("icalendar>=5.0", "ICS 日历读写"),
    "httpx": ("httpx>=0.27", "HTTP 请求与网页搜索"),
    "win32com": ("pywin32>=306", "Windows Outlook / COM 集成"),
    # v1.6.0 内置多媒体 / 系统工具（同样可选依赖、缺库给安装提示）
    "PIL": ("pillow>=10.0", "图像处理 / 截屏 / 图表导出"),
    "pptx": ("python-pptx>=0.6.21", "PowerPoint 生成"),
    "matplotlib": ("matplotlib>=3.7", "数据图表绘制"),
    "psutil": ("psutil>=5.9", "进程与端口管理"),
    "pyperclip": ("pyperclip>=1.8", "剪贴板读写"),
    "pytesseract": ("pytesseract>=0.3.10", "OCR 文字识别（还需系统里的 tesseract 引擎）"),
    "mutagen": ("mutagen>=1.47", "音频元信息解析"),
}


class MissingDependency(RuntimeError):
    """可选依赖未安装。消息里直接给出可照抄的安装命令。"""

    def __init__(self, module: str) -> None:
        pkg, purpose = OPTIONAL_DEPS.get(module, (module, module))
        self.module, self.package = module, pkg
        super().__init__(
            f"缺少「{purpose}」所需的依赖 {pkg}。请先安装：pip install {pkg}"
            f"（或一次装齐办公套件：pip install 'automind-agent[office]'）")


# ── 外部可执行文件（pip 装不到的那一半）──────────────────────

#: pip 模块 -> 该模块**运行时还需要**的外部命令。
#: pytesseract 是最典型的坑：它是 tesseract 引擎的**壳**，`pip install` 只装壳，
#: 引擎本身要单独装。旧提示让人去 `pip install pytesseract`，照做之后调用
#: 依然失败（TesseractNotFoundError），而且失败原因看起来和"没装"一模一样。
MODULE_BINARIES: dict[str, tuple[str, ...]] = {
    "pytesseract": ("tesseract",),
}

#: 外部命令 -> (用途, {平台: 安装命令})
_EXTERNAL: dict[str, tuple[str, dict[str, str]]] = {
    "ffmpeg": ("音视频转码 / 抽帧 / 合流", {
        "win32": "winget install --id Gyan.FFmpeg -e"
                 "（或 choco install ffmpeg / scoop install ffmpeg）",
        "darwin": "brew install ffmpeg",
        "linux": "sudo apt install ffmpeg（Debian/Ubuntu），或 sudo dnf install ffmpeg",
    }),
    "ffprobe": ("读取音视频元信息（随 ffmpeg 一起发布）", {
        "win32": "winget install --id Gyan.FFmpeg -e（ffprobe 与 ffmpeg 同一个包）",
        "darwin": "brew install ffmpeg",
        "linux": "sudo apt install ffmpeg",
    }),
    "tesseract": ("OCR 文字识别引擎", {
        "win32": "winget install --id UB-Mannheim.TesseractOCR -e"
                 "（或从 https://github.com/UB-Mannheim/tesseract/wiki 下载安装包）",
        "darwin": "brew install tesseract tesseract-lang",
        "linux": "sudo apt install tesseract-ocr tesseract-ocr-chi-sim",
    }),
    "git": ("版本控制（git 工具）", {
        "win32": "winget install --id Git.Git -e",
        "darwin": "brew install git",
        "linux": "sudo apt install git",
    }),
    "notify-send": ("Linux 桌面通知（libnotify）", {
        "win32": "Windows 走 PowerShell toast，不需要该命令",
        "darwin": "macOS 走 osascript，不需要该命令",
        "linux": "sudo apt install libnotify-bin",
    }),
}

#: Windows 上"装了但没进 PATH"的高频位置 —— 安装器默认目录，兜底找一遍。
_WINDOWS_FALLBACK: dict[str, tuple[str, ...]] = {
    "tesseract": (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe",
        r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe",
    ),
    "ffmpeg": (
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe",
    ),
    "ffprobe": (
        r"C:\ffmpeg\bin\ffprobe.exe",
        r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffprobe.exe",
    ),
    "git": (
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
    ),
}


def _env_override(binary: str) -> str:
    """该命令的"手动指定路径"环境变量名（如 AUTOMIND_TESSERACT_CMD）。"""
    return "AUTOMIND_" + binary.upper().replace("-", "_") + "_CMD"


def find_binary(binary: str) -> str | None:
    """在 PATH（及 Windows 常见安装目录）里找这个外部命令，返回其路径。

    找不到返回 None。**不抛异常** —— "有没有"和"没有怎么办"是两件事：
    前者界面自检也要用（``/api/browser/status`` 那类），不该被迫 try/except。
    """
    override = os.environ.get(_env_override(binary), "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return str(p)
        found = shutil.which(override)
        if found:
            return found
    found = shutil.which(binary)
    if found:
        return found
    if os.name == "nt":
        for raw in _WINDOWS_FALLBACK.get(binary, ()):
            cand = Path(os.path.expandvars(raw))
            if cand.is_file():
                return str(cand)
    return None


def binary_install_hint(binary: str) -> str:
    """该命令在当前平台上的安装办法（一行，可直接照抄）。"""
    purpose, per_os = _EXTERNAL.get(binary, (binary, {}))
    key = "win32" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "linux")
    how = per_os.get(key) or per_os.get("linux") or "请查阅其官方文档安装后加入 PATH"
    return f"{binary}（{purpose}）：{how}"


class MissingBinary(RuntimeError):
    """外部可执行文件缺失 —— 消息里给的是**该平台的安装办法**，不是 pip 命令。"""

    def __init__(self, binary: str, *, module: str = "") -> None:
        self.binary, self.module = binary, module
        prefix = (f"pip 里的 {module} 已经装上，但它只是个壳，"
                  f"真正干活的外部程序 {binary} 不在系统里。"
                  if module else f"缺少外部程序 {binary}。")
        super().__init__(
            f"{prefix}\n"
            f"  这不是 Python 包，pip install 装不了它。安装办法：\n"
            f"    · {binary_install_hint(binary)}\n"
            f"  装完请**重开终端/重启本程序**让 PATH 生效；"
            f"若已装在非标准目录，可用环境变量 {_env_override(binary)} "
            f"直接指定完整路径（例如 {_env_override(binary)}=C:\\path\\to\\{binary}.exe）。")


def need_binary(binary: str) -> str:
    """按需取一个外部命令的路径；缺失时抛 MissingBinary（含该平台安装办法）。"""
    found = find_binary(binary)
    if not found:
        raise MissingBinary(binary)
    return found


def need(module: str) -> Any:
    """按需导入可选依赖；缺失时抛 MissingDependency。

    v1.7.2：导入成功**之后**还要检查它依赖的外部命令（见 ``MODULE_BINARIES``）。
    否则 ``pip install pytesseract`` 会得到一个"装好了却依然报缺依赖"的错觉 ——
    用户按提示装完，OCR 仍然失败，而失败信息还是那句 pip 命令，形成死循环。
    """
    try:
        mod = __import__(module)
    except ImportError as e:
        raise MissingDependency(module) from e
    for binary in MODULE_BINARIES.get(module, ()):
        if find_binary(binary) is None:
            raise MissingBinary(binary, module=module)
    return mod


# ── 能力分级 ────────────────────────────────────────────────

#: 工具内的进阶动作 -> 所需特性键。未授权时该动作被拒，其余动作照常可用。
def require(feature: str) -> None:
    """进阶动作的门控；未授权抛 FeatureNotAvailable（消息含升级引导）。"""
    if not has_feature(feature):
        raise FeatureNotAvailable(feature)


def gated(feature: str, actions: set[str], action: str) -> None:
    """若 ``action`` 属于进阶动作集合，则要求 ``feature`` 已授权。"""
    if action in actions:
        require(feature)


def delegate_pro(feature: str, tool: str, action: str, kwargs: dict) -> dict:
    """把进阶动作委派给商业版特性对象执行，返回其 output 字典。

    社区版**不实现**这些动作，只在此处转交 —— 避免出现"授权了却提示不支持"
    的死路，也保证社区版代码里没有永远走不到的分支。未授权时抛
    FeatureNotAvailable（消息含升级引导）。
    """
    impl = get_feature(feature)
    if impl is None:
        raise FeatureNotAvailable(feature)
    if not impl.supports(tool, action):
        raise ValueError(f"{feature} 暂不支持 {tool}.{action}")
    return impl.handle(tool, action, kwargs)


def err(tool: str, exc: Exception) -> ToolResult:
    """把异常统一转成"模型能看懂并据此改正"的 ToolResult。"""
    if isinstance(exc, MissingDependency):
        return ToolResult(tool_name=tool, success=False, error=str(exc),
                          output={"missing_dependency": exc.package})
    if isinstance(exc, MissingBinary):
        # 与缺 Python 包分开报：这两者的修复动作完全不同（一个 pip，一个装系统程序），
        # 混成一个 "missing_dependency" 会让人照着 pip 命令白跑一趟。
        return ToolResult(tool_name=tool, success=False, error=str(exc),
                          output={"missing_binary": exc.binary,
                                  "install_hint": binary_install_hint(exc.binary)})
    if isinstance(exc, FeatureNotAvailable):
        return ToolResult(tool_name=tool, success=False, error=str(exc),
                          output={"upgrade_required": exc.feature,
                                  "hint": upgrade_hint(exc.feature)})
    return ToolResult(tool_name=tool, success=False,
                      error=f"{type(exc).__name__}: {exc}")


def ok(tool: str, **output: Any) -> ToolResult:
    return ToolResult(tool_name=tool, success=True, output=output)


def bad(tool: str, message: str, **output: Any) -> ToolResult:
    return ToolResult(tool_name=tool, success=False, error=message, output=output)


# ── 出网安全（SSRF 防护） ───────────────────────────────────

_BLOCKED_SCHEMES = {"file", "ftp", "gopher", "data", "dict", "sftp", "ldap"}

#: 云厂商元数据服务 —— SSRF 最经典的目标，拿到就等于拿到实例凭据
_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal", "100.100.100.200"}


class BlockedTarget(ValueError):
    """请求目标落在禁止范围内（私网 / 回环 / 元数据服务 / 危险协议）。"""


def check_url(url: str, allow_private: bool = False) -> str:
    """校验外发 URL；返回规范化后的 URL，越界抛 BlockedTarget。

    默认**禁止访问私网与回环地址**。这不是多余的谨慎：模型可被网页内容或
    用户文档诱导去请求 ``http://127.0.0.1:8765/api/...``（本机的 AutoMind
    自己）或 ``http://169.254.169.254/``（云上实例元数据），从而把内网接口
    和临时凭据带出来。要访问内网请显式传 allow_private=True。
    """
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise BlockedTarget(
            f"不支持的协议 '{u.scheme or '(空)'}'，仅允许 http/https"
            if u.scheme in _BLOCKED_SCHEMES or not u.scheme
            else f"不支持的协议 '{u.scheme}'")
    host = (u.hostname or "").lower()
    if not host:
        raise BlockedTarget("URL 缺少主机名")
    if host in _METADATA_HOSTS:
        raise BlockedTarget(f"禁止访问云元数据服务 {host}")
    if allow_private:
        return url

    # 逐个解析结果都要检查：DNS 可能同时返回公网与私网地址
    try:
        infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise BlockedTarget(f"无法解析主机 {host}：{e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            raise BlockedTarget(
                f"目标 {host} 解析到非公网地址 {ip}，已拒绝"
                "（如确需访问内网，请显式设置 allow_private=true）")
    return url


# ── 路径安全 ────────────────────────────────────────────────

def safe_extract_path(root: Any, member: str) -> Any:
    """解压时校验单个成员路径，防 zip-slip（``../../etc/passwd``）。

    归档里的成员名是**攻击者可控**的：不校验就直接 join，一个 ``..`` 就能
    把文件写到解压目录之外。这里要求最终路径必须仍在 root 之内。
    """
    from pathlib import Path
    root = Path(root).resolve()
    target = (root / member).resolve()
    if target != root and root not in target.parents:
        raise BlockedTarget(f"归档成员路径越界，已拒绝解压：{member}")
    return target


# ── 阻塞调用挪出事件循环 ─────────────────────────────────────

async def run_blocking(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """在线程池里执行一个**会阻塞**的同步调用，返回其结果。

    工具都是 ``async def execute``，但函数体里往往藏着同步调用：``pyperclip``
    读写剪贴板、``subprocess.run`` 起 PowerShell/osascript/ffmpeg。它们的共同
    特点是**等待期间不释放事件循环**，后果不只是"这个工具慢"：

      · 整个进程的 asyncio 循环被占死 —— 其它会话的任务、审批弹窗推送、
        心跳与进度条、``/api/health`` 全部一起冻住；
      · 任何 ``asyncio.wait_for`` 超时都**不会触发**（定时器压根轮不到执行），
        也就是"给这一步设了上限"其实形同虚设。

    实测（v1.7.0 之前）：一个 ``time.sleep(20)`` 的同步工具配 ``goal_timeout=2``，
    实际耗时 20.0 秒、超时未生效。

    用法::

        r = await run_blocking(subprocess.run, cmd, capture_output=True,
                              text=True, timeout=20)

    注意：``timeout`` 参数仍然要给（子进程要真的被杀掉），``run_blocking``
    解决的是"不要把事件循环一起等死"，不是"不用设超时"。
    """
    return await asyncio.to_thread(fn, *args, **kwargs)
