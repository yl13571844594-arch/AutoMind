"""delivery_manifest —— 给每个任务生成「交付清单」（内置插件）。

**为什么需要它**：任务结束时用户拿到的是一段模型自述——"到底改了哪几个文件、
新建还是再次写入、多大、内容指纹是什么"没有程序化来源。想从
``file_editor.JOURNAL`` 里取也不行：它是进程级单例、容量 200 条、跨会话共享
（服务端只取前 20 条），拿它当"本任务的交付清单"会把别的会话的改动算进来。

**做法**：用分派中间件在**任务内**逐个记录写类工具的调用（``before_run`` 时清零），
``after_run`` 时渲染成清单追加进 ``result.output``，并尽力落一份 markdown。

**边界**：只改文本，不动 ``result.success``——"交付闸门"是另一个插件的职责。
中间件自己要防御性：任何异常都不能影响工具调用本身（分派侧还会再兜一层）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from automind.core.artifacts import resolve_artifact_path
from automind.core.hooks import AgentHooks
from automind.core.logging import get_logger

logger = get_logger("automind.plugin.delivery_manifest")

#: 会写盘的工具：只有这些调用值得进交付清单
_WRITE_TOOLS = frozenset({
    "file_write", "file_edit", "file_multi_edit",
    "excel_tool", "word_tool", "ppt_tool", "pdf_tool", "csv_tool",
    "image_tool", "chart_tool", "archive",
})

#: 单任务最多记录多少条（防止批量写把清单本身变成噪声）
_MAX_ITEMS = 50

#: 本任务内的写操作记录（before_run 清零）
_records: list[dict[str, Any]] = []


def _size_hash(path: Path) -> tuple[int, str]:
    """读磁盘拿真实大小与内容指纹（工具自报的 size 可能只是内存长度）。"""
    try:
        if not path.exists() or not path.is_file():
            return 0, ""
        data = path.read_bytes()
        import hashlib

        return len(data), hashlib.sha256(data).hexdigest()[:12]
    except Exception:
        return 0, ""


def _human_size(size: int) -> str:
    if size <= 0:
        return "0 B"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.2f} MB"


def get_middleware() -> Any:
    """记录写类工具的调用结果；中间件必须原样返回 result。"""

    async def mw(tool_name: str, kwargs: dict[str, Any], call_next: Any) -> Any:
        result = await call_next(kwargs)
        try:
            if tool_name in _WRITE_TOOLS and getattr(result, "success", False):
                path = resolve_artifact_path(kwargs, result)
                if path is not None:
                    size, digest = _size_hash(path)
                    _records.append({
                        "tool": tool_name,
                        "path": str(path),
                        "size": size,
                        "hash": digest,
                        "ts": time.time(),
                    })
        except Exception as e:                       # 记录失败绝不影响工具调用
            logger.debug("delivery_manifest record failed", error=str(e))
        return result

    return mw


def _render(records: list[dict[str, Any]]) -> str:
    """把记录渲染成清单；同一路径多次写入合并成一条。"""
    merged: dict[str, dict[str, Any]] = {}
    for row in records:
        key = row["path"]
        if key in merged:
            merged[key]["times"] += 1
            merged[key]["size"] = row["size"]
            merged[key]["hash"] = row["hash"] or merged[key]["hash"]
        else:
            merged[key] = {**row, "times": 1}
    lines = [f"📦 交付清单（本任务实际写入 {len(merged)} 个文件，按工具调用记录）"]
    for path_text, row in list(merged.items())[:_MAX_ITEMS]:
        action = "再次写入" if row["times"] > 1 else "写入"
        digest = f" · {row['hash']}" if row["hash"] else ""
        lines.append(f"- {path_text} · {_human_size(row['size'])} · {action}"
                     f" · {row['tool']}{digest}")
    if len(merged) > _MAX_ITEMS:
        lines.append(f"- …还有 {len(merged) - _MAX_ITEMS} 个文件未列出")
    lines.append("（以上由插件按实际工具调用记录，非模型自述。）")
    return "\n".join(lines)


def _save_markdown(text: str) -> None:
    """尽力落一份清单文件；数据目录不可写就算了（不影响任务）。"""
    try:
        from automind.core.paths import data_dir

        out_dir = Path(data_dir()).expanduser().resolve() / "delivery"
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"delivery_{time.strftime('%Y%m%d_%H%M%S')}.md"
        target.write_text(text + "\n", encoding="utf-8")
    except Exception as e:                           # pragma: no cover - 纯尽力而为
        logger.debug("delivery_manifest save failed", error=str(e))


def get_hooks() -> AgentHooks:
    """任务开始清空记录，任务结束渲染清单。"""

    async def before_run(user_input: str) -> None:
        _records.clear()

    async def after_run(result: Any) -> None:
        try:
            if not _records:
                return
            text = _render(_records)
            _save_markdown(text)
            output = getattr(result, "output", "") or ""
            result.output = f"{output}\n\n---\n{text}"
        except Exception as e:                        # 插件故障不改变任务结果
            logger.warning("delivery_manifest render failed", error=str(e))

    return AgentHooks(before_run=before_run, after_run=after_run)


def reset_for_tests() -> None:
    """测试用：清空记录。"""
    _records.clear()
