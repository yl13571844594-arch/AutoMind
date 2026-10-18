"""fs_lock_guard —— 文件被占用时的退避重试（内置插件）。

**为什么需要它**：Windows 上最常见的一类"写不进去"是**文件正被别的程序打开**
（Excel/WPS 看表、看图软件、OneDrive 同步、杀软扫描）。工具会收到
``PermissionError [WinError 32] 另一个程序正在使用此文件``，或者内部被
``except`` 吞成一句 ``Permission denied`` 的失败结果 —— 于是整步白跑，模型
往往原样重试一次，又撞上同一把锁。

**做法**：分派中间件对写类工具做短退避重试（0 / 0.4 / 1.2 秒，最多多等 1.6 秒），
同时处理两种形态：工具**抛异常**、以及工具把错误**吞成失败结果**。
只重试"看起来是占用/锁"的错误，其它失败立刻放行，不做无意义重试。

**边界**：重试解决"占用"，解决不了"跨进程并发覆盖"（那需要工具内加锁）；
也不做"原子写"（写了一半的文件由 artifact_selfcheck 负责发现）。

v1.11.9：本插件成为文件锁重试的**唯一**负责方（core/netresilience 的同名
逻辑已移除，避免两层退避叠乘成最多 12 次执行的重试风暴）；file_read 也纳入
重试范围。
"""

from __future__ import annotations

import asyncio
from typing import Any

from automind.core.hooks import AgentHooks
from automind.core.logging import get_logger
from automind.core.netresilience import is_transient_lock

logger = get_logger("automind.plugin.fs_lock_guard")

#: 会写盘的工具（读类只在 file_read / db_query 上补一个"被锁/库被锁"）。
#: file_read 是 v1.11.9 补上的：core/netresilience 移除自带的文件锁重试后，
#: "读一个正被别的程序短暂锁住的文件"这条路径没有任何插件兜底了。
_RETRY_TOOLS = frozenset({
    "file_write", "file_edit", "file_multi_edit", "file_read",
    "excel_tool", "word_tool", "ppt_tool", "pdf_tool", "csv_tool",
    "image_tool", "chart_tool", "archive", "db_query",
})

#: 每次尝试前等多久（第一次不等待）；总追加等待 1.6 秒
_DELAYS = (0.0, 0.4, 1.2)


def _looks_locked(text: str) -> bool:
    """是否"看起来是占用/锁"—— 判定统一走 core/netresilience.is_transient_lock
    （单一事实来源，中英文都认），本插件不再自维护一份模式清单。"""
    return is_transient_lock(text)


def _result_text(result: Any) -> str:
    """把失败结果的 error / output 拍成一段文本（工具可能只把原因放 output 里）。"""
    parts = [str(getattr(result, "error", "") or "")]
    out = getattr(result, "output", None)
    if out is not None:
        parts.append(str(out)[:2000])
    return " ".join(parts)


def get_middleware() -> Any:
    """写类工具遇到"占用类"失败时短退避重试。"""

    async def mw(tool_name: str, kwargs: dict[str, Any], call_next: Any) -> Any:
        if tool_name not in _RETRY_TOOLS:
            return await call_next(kwargs)

        last_result: Any = None
        for attempt, delay in enumerate(_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                result = await call_next(kwargs)
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except Exception as e:
                # 工具抛异常：只有"像占用"才重试，其它立刻抛给分派层翻译
                if not _looks_locked(f"{type(e).__name__}: {e}") or attempt == len(_DELAYS) - 1:
                    raise
                logger.info("fs_lock_guard retry_after_exception", tool=tool_name,
                            attempt=attempt + 1, error=f"{type(e).__name__}: {e}"[:160])
                continue

            # 工具把错误吞成了失败结果：同样按"像占用"判断
            if getattr(result, "success", False) or not _looks_locked(_result_text(result)):
                return result
            last_result = result
            if attempt == len(_DELAYS) - 1:
                break
            logger.info("fs_lock_guard retry_after_result", tool=tool_name,
                        attempt=attempt + 1)

        # 重试用尽：把失败结果换成一条"可操作"的说明（保留原始输出供排查）
        if last_result is None:                       # pragma: no cover - 防御
            return await call_next(kwargs)
        try:
            from automind.core.types import ToolResult

            return ToolResult(
                tool_name=getattr(last_result, "tool_name", "") or tool_name,
                success=False,
                output=getattr(last_result, "output", None),
                error=(f"{str(getattr(last_result, 'error', '') or '')}"
                       f"\n（已自动退避重试 {len(_DELAYS) - 1} 次仍失败：目标文件很可能正被"
                       f"Excel / WPS / 看图软件 / 同步盘占用。请先关闭打开该文件的程序，"
                       f"或换个输出文件名后重试。）"),
                metadata={"fs_lock_guard": True, "retried": len(_DELAYS) - 1,
                          **dict(getattr(last_result, "metadata", {}) or {})},
            )
        except Exception:                             # pragma: no cover - 防御
            return last_result

    return mw


def get_hooks() -> AgentHooks:
    return AgentHooks()
