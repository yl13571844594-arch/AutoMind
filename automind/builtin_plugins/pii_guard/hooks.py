"""pii_guard —— 检测任务输入与工具输出中的密钥/令牌，落审计日志告警。

复用 ``automind/core/redact.py`` 的识别规则（OpenAI/Anthropic/AWS/GitHub/
Slack token、Bearer、私钥块等）。钩子机制只允许**副作用**（返回值被忽略、
异常被吞掉），因此本插件不做改写，而是**发现即告警**：把脱敏后的样本写进
``~/.automind/pii_audit.log``，并在服务日志里打一条 warning。

挂接的钩子：
- ``before_run``：扫描用户输入文本；
- ``before_tool``：扫描工具调用参数；
- ``after_tool``：扫描工具返回的 output / error。

这样即便模型把密钥带进了某个工具调用或输出，也能在审计日志里留痕。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

from automind.core.hooks import AgentHooks
from automind.core.redact import has_secret, redact_secrets

logger = logging.getLogger("automind.plugin.pii_guard")

_AUDIT_PATH = Path("~/.automind/pii_audit.log").expanduser()
_lock = threading.Lock()


def _scan(source: str, sample: str) -> None:
    """若样本含密钥则告警并留痕（失败吞掉，不影响主流程）。"""
    if not sample or not isinstance(sample, str):
        return
    if not has_secret(sample):
        return
    masked = redact_secrets(sample)
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {source}: {masked[:200]}\n"
        with _lock, _AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:  # pragma: no cover
        logger.warning("pii_guard audit write failed", error=str(e))
    logger.warning("pii_guard detected secret", source=source)


def _flatten(value) -> str:
    """把任意结构拍平成可扫描的字符串（限长，避免大对象拖慢）。"""
    if isinstance(value, str):
        return value
    try:
        import json
        return json.dumps(value, ensure_ascii=False, default=str)[:20000]
    except Exception:
        return str(value)[:20000]


def get_hooks() -> AgentHooks:
    """返回敏感信息守卫钩子。"""

    async def before_run(user_input: str) -> None:
        _scan("input", user_input)

    async def before_tool(tool_name: str, params: dict) -> None:
        _scan(f"tool:{tool_name} params", _flatten(params))

    async def after_tool(tool_name: str, result) -> None:
        out = getattr(result, "output", None)
        err = getattr(result, "error", None)
        if out is not None:
            _scan(f"tool:{tool_name} output", _flatten(out))
        if err:
            _scan(f"tool:{tool_name} error", str(err))

    return AgentHooks(
        before_run=before_run,
        before_tool=before_tool,
        after_tool=after_tool,
    )
