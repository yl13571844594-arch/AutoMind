"""写盘工具的产物路径解析（v1.11.13）。

**为什么需要独立模块**：写类工具成功后，内置插件要回读磁盘做自检/交付清单。
但插件看到的只是「工具参数 + 返回值」：

* 模型参数里的路径通常是**相对路径**；
* 相对基准可能是**会话隔离副本**（``session_ctx.workspace()``）或项目根；
* 而工具返回值里往往已经带了**解析后的绝对路径**。

此前 artifact_selfcheck 只用参数里的相对路径、按服务进程 cwd 检查 —— 在
目录级隔离模式下（Web 与 IM 机器人的工作模式默认开启）**每次文件写入都会
被误判为"文件不存在"**，紧接着 delivery_manifest 也把路径/大小记错。这里
把"产物到底落在哪"收敛成一个可复用的解析函数，插件与工具共用同一套规则。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from automind.core import session_ctx

#: 返回值里可能携带"真正写入路径"的键（优先于入参，因为入参可能是相对路径、
#: 甚至像 image_tool 的 ``path`` 其实是**输入图片**而不是输出文件）
_RESULT_KEYS = ("output", "output_file", "saved_to", "dest", "destination",
                "path", "file", "name")
#: 入参里可能携带输出路径的键（顺序：输出类在前，path 最后）
_KWARG_KEYS = ("output", "output_file", "saved_to", "dest", "destination",
               "file", "path")


def _bases() -> list[Path]:
    bases: list[Path] = []
    for value in (session_ctx.workspace(), session_ctx.project_root()):
        if not value:
            continue
        try:
            p = Path(value).expanduser().resolve()
        except OSError:
            continue
        if p not in bases:
            bases.append(p)
    return bases


def _candidate(text: Any) -> Path | None:
    if not isinstance(text, str):
        return None
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    if not value or value.lower().startswith(("http://", "https://", "data:")):
        return None
    p = Path(value).expanduser()
    if p.is_absolute():
        return p
    bases = _bases()
    if not bases:
        return p
    resolved = [base / p for base in bases]
    for cand in resolved:
        try:
            if cand.exists():
                return cand
        except OSError:
            pass
    return resolved[0]


def resolve_artifact_path(kwargs: dict[str, Any] | None,
                          result: Any = None) -> Path | None:
    """找出本次写盘实际产出的文件路径；找不到返回 None（不猜测）。

    优先使用工具返回值里的绝对路径（那是工具自己解析后的真值），其次才用
    入参；相对路径按会话副本 → 项目根的顺序解析。返回的路径**不保证存在**
    —— 自检插件正需要用它去发现"没写成功"。
    """
    candidates: list[Path] = []
    out = getattr(result, "output", None)
    if isinstance(out, dict):
        for key in _RESULT_KEYS:
            cand = _candidate(out.get(key))
            if cand is not None:
                candidates.append(cand)
    for key in _KWARG_KEYS:
        cand = _candidate((kwargs or {}).get(key))
        if cand is not None:
            candidates.append(cand)
    if not candidates:
        return None
    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            pass
    # 都不存在：返回第一个"看起来是输出路径"的候选，供自检给出准确错误
    return candidates[0]
