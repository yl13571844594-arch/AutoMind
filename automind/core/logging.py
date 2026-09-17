"""结构化日志 — 优先 structlog，缺失时优雅降级到标准库 logging。

统一调用约定（两种实现下签名一致）::

    logger = get_logger("automind.agent")
    logger.info("step_end", goal="创建文件", status="ok")
    logger.warning("backtrack", goal_id="g1", reason="...")

structlog 存在 → 真正的结构化输出（JSON / 彩色控制台）；
structlog 缺失 → 标准库 logging，kwargs 以 ``key=value`` 追加到消息尾部。
核心库因此不强依赖任何日志三方包（工业级可移植性）。

v1.7.2：**中文日志在 Windows GBK 控制台下的乱码**在这里根治，见
:func:`ensure_utf8_stdio`。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

try:
    import structlog
    _HAS_STRUCTLOG = True
except ImportError:
    structlog = None  # type: ignore[assignment]
    _HAS_STRUCTLOG = False


#: 本进程是否已经检查过 stdio 编码（避免每次 get_logger 都去 reconfigure）
_STDIO_CHECKED = False


def _stream_encoding(stream: Any) -> str:
    try:
        return str(getattr(stream, "encoding", "") or "").lower().replace("_", "-")
    except Exception:                                     # pragma: no cover - 防御性
        return ""


def _interpreter_is_utf8() -> bool:
    """解释器是否已处于 UTF-8 模式（``-X utf8`` / ``PYTHONUTF8=1``）。

    单独抽成函数有两个原因：一是"要不要动手"的判据集中在一处、好核对；
    二是它**不可被 monkeypatch 到 sys.flags 上**（``sys.flags`` 是只读的
    structseq），而测试必须能把环境钉死 —— 否则用例会随运行环境漂移：
    GitHub Actions 给 runner 设了 ``PYTHONUTF8=1``，同一份测试在 CI 上
    走进的是"跳过"分支，本地却是"修复"分支。
    """
    return bool(getattr(sys.flags, "utf8_mode", 0))


def ensure_utf8_stdio(force: bool = False) -> dict[str, str]:
    """把 stdout/stderr 的编码统一成 UTF-8 —— Windows 中文日志乱码的根治。

    **问题**（实测确认）：Windows 上 Python 的 stdio 编码取自当前代码页，简中
    环境即 **cp936(GBK)**。只要输出不是"真正的控制台"——被 IDE、CI、启动器、
    管道、桌面壳接管的那些情况——Python 写出去的就是 GBK 字节，而接收方几乎
    总是按 UTF-8 解码，于是中文日志、中文报错全部变成乱码。此前的临时解法是
    让用户自己设 ``PYTHONIOENCODING=utf-8``；用户不该为了看懂自己的日志去配环境变量。

    这里做的正是那件事，但有边界：

    * **只处理 Windows**，且当前编码确实是本地代码页（本来就是 UTF-8 的环境完全不动）；
    * 用户显式设了 ``PYTHONIOENCODING`` / ``PYTHONUTF8`` 就**完全尊重**，不覆盖；
    * 真控制台（``isatty``）**不改编码** —— 它自己按代码页渲染中文是正常的，
      改编码反而会把它弄花；只把 ``errors`` 收紧为 replace；
    * 非控制台（管道/重定向/IDE）才切成 UTF-8，因为下游解码方式几乎总是 UTF-8；
    * ``errors="replace"`` 兜底：编码不了时降级成一个替代字符，而不是抛
      ``UnicodeEncodeError`` 把**一条日志**变成**一屏堆栈**（顺带把日志系统本身弄挂）；
    * 想关掉：``AUTOMIND_UTF8_STDIO=0``。

    幂等，且只做一次（结果缓存在模块级）。返回 ``{流名: 处理结果}`` 供自检/测试断言。
    """
    global _STDIO_CHECKED
    if _STDIO_CHECKED and not force:
        return {}
    _STDIO_CHECKED = True

    result: dict[str, str] = {}
    if os.name != "nt":
        return {"skipped": "非 Windows：stdio 编码不由代码页决定"}
    if os.environ.get("PYTHONIOENCODING") or os.environ.get("PYTHONUTF8"):
        # 用户/上层已经明确指定了编码 —— 他的选择优先于我们的判断
        return {"skipped": "已显式设置 PYTHONIOENCODING/PYTHONUTF8，尊重该设置"}
    if os.environ.get("AUTOMIND_UTF8_STDIO", "1").strip().lower() in ("0", "false", "no"):
        return {"skipped": "AUTOMIND_UTF8_STDIO=0"}
    if _interpreter_is_utf8():
        return {"skipped": "解释器已处于 UTF-8 模式"}
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:                                # pythonw / 冻结包无控制台
            result[name] = "none"
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:                           # 被上层替换过的流（StringIO 等）
            result[name] = "not-reconfigurable"
            continue
        enc = _stream_encoding(stream)
        if enc.startswith("utf"):
            result[name] = "already-utf8"
            continue
        try:
            is_tty = bool(stream.isatty())
        except Exception:                                 # pragma: no cover - 防御性
            is_tty = False
        try:
            if is_tty:
                # 真控制台：保留它自己的编码（GBK 控制台渲染中文本来就正常），
                # 只保证编码失败不再抛异常
                reconfigure(errors="replace")
                result[name] = f"tty-kept({enc or '?'})"
            else:
                reconfigure(encoding="utf-8", errors="replace")
                result[name] = f"utf8(was {enc or '?'})"
        except Exception as e:                            # pragma: no cover - 极端环境
            # 改不动也不能让程序起不来：日志乱码远好过启动失败
            result[name] = f"failed: {type(e).__name__}: {e}"
    return result


def configure_logging(level: str = "INFO", debug: bool = False) -> None:
    """配置日志。structlog 可用时配置结构化管线，否则配置标准库。

    Args:
        level: 日志级别 (DEBUG, INFO, WARNING, ERROR)。
        debug: 是否启用调试模式 (美化输出)。
    """
    ensure_utf8_stdio()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    if not _HAS_STRUCTLOG:
        return

    timestamper = structlog.processors.TimeStamper(fmt="ISO")
    if debug:
        # 开发模式：彩色控制台输出
        processors: list[Any] = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.CallsiteParameterAdder(
                [structlog.processors.CallsiteParameter.FILENAME,
                 structlog.processors.CallsiteParameter.LINENO,
                 structlog.processors.CallsiteParameter.FUNC_NAME],
            ),
            timestamper,
            structlog.dev.ConsoleRenderer(),
        ]
    else:
        processors = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            timestamper,
            structlog.processors.JSONRenderer(),
        ]

    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


class _StdlibStructAdapter:
    """标准库 logging 适配器 — 兼容 structlog 的 ``logger.info(event, **kw)`` 签名。

    v1.7.3：位置参数**刻意不叫** ``event``。此前它就叫这个名字，于是
    ``logger.warning("...", event="task_start")`` 会直接
    ``TypeError: got multiple values for argument 'event'`` —— 而 ``event``
    恰恰是这套接口里最自然的结构化键名（本仓到处都在记"发生了什么事件"）。
    更糟的是异常发生在**日志调用自身**：它会把调用点所在的整段逻辑一起带走
    （实测：webhook 投递协程里的一次告警把整批投递跳过，重试逻辑根本没执行）。
    改成语义等价但不会撞名的 ``_message``，把 ``event=`` 留给业务字段。
    """

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    @staticmethod
    def _fmt(message: str, kw: dict[str, Any]) -> str:
        if not kw:
            return message
        pairs = " ".join(f"{k}={v!r}" for k, v in kw.items())
        return f"{message} {pairs}"

    def debug(self, _message: str, **kw: Any) -> None:
        self._logger.debug(self._fmt(_message, kw))

    def info(self, _message: str, **kw: Any) -> None:
        self._logger.info(self._fmt(_message, kw))

    def warning(self, _message: str, **kw: Any) -> None:
        self._logger.warning(self._fmt(_message, kw))

    def error(self, _message: str, **kw: Any) -> None:
        self._logger.error(self._fmt(_message, kw))

    def exception(self, _message: str, **kw: Any) -> None:
        self._logger.exception(self._fmt(_message, kw))

    def bind(self, **kw: Any) -> _StdlibStructAdapter:
        """structlog 兼容占位 — 标准库模式下忽略绑定上下文。"""
        return self


def get_logger(name: str | None = None) -> Any:
    """获取日志记录器（structlog 或标准库适配器，调用签名一致）。

    顺带做一次 stdio 编码检查（见 :func:`ensure_utf8_stdio`）：``get_logger``
    几乎等于"本库被使用的第一现场"（各模块都在导入期调用它），把检查挂在这里
    意味着**无论用户从哪个入口启动**（CLI / Web 服务 / 桌面壳 / 直接 import），
    中文日志都不会因为 Windows 代码页而变成乱码 —— 而不是要求每个入口各记得
    调一次。检查本身幂等且只跑一次，代价可以忽略。
    """
    ensure_utf8_stdio()
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name or "automind")
    return _StdlibStructAdapter(logging.getLogger(name or "automind"))
