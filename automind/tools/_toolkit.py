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

import ipaddress
import socket
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
}


class MissingDependency(RuntimeError):
    """可选依赖未安装。消息里直接给出可照抄的安装命令。"""

    def __init__(self, module: str) -> None:
        pkg, purpose = OPTIONAL_DEPS.get(module, (module, module))
        self.module, self.package = module, pkg
        super().__init__(
            f"缺少「{purpose}」所需的依赖 {pkg}。请先安装：pip install {pkg}"
            f"（或一次装齐办公套件：pip install 'automind-agent[office]'）")


def need(module: str) -> Any:
    """按需导入可选依赖；缺失时抛 MissingDependency。"""
    try:
        return __import__(module)
    except ImportError as e:
        raise MissingDependency(module) from e


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
