"""敏感信息脱敏 — 对工具/模型输出中的密钥、令牌做打码（§14.11-3）。

设计目标：在不破坏正常文本可读性的前提下，把常见的 API Key / Token /
私钥等高危字符串替换为带提示的掩码（保留首尾少量字符便于人工核对）。

纯函数实现，无外部依赖，可独立单测。默认不在主流程启用，由服务端按
环境变量 ``AUTOMIND_REDACT_SECRETS`` 选择性开启，避免误伤合法内容。
"""

from __future__ import annotations

import re

# 每个条目： (编译后的正则, 捕获组序号) ——
# 捕获组指向"需要打码的实际密钥子串"，其余上下文（如 key= 前缀）保留。
_SECRET_RULES: list[tuple[re.Pattern[str], int]] = [
    # OpenAI / 兼容服务： sk-..., sk-proj-...
    (re.compile(r"\b(sk-(?:proj-)?[A-Za-z0-9_\-]{16,})"), 1),
    # Anthropic： sk-ant-...
    (re.compile(r"\b(sk-ant-[A-Za-z0-9_\-]{16,})"), 1),
    # AWS Access Key Id： AKIA + 16
    (re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), 1),
    # Google API Key： AIza + 35（容忍更长尾部）
    (re.compile(r"\b(AIza[0-9A-Za-z_\-]{35,})"), 1),
    # GitHub token： ghp_/gho_/ghs_/ghr_ + 36
    (re.compile(r"\b((?:gh[poshr]_)[A-Za-z0-9]{30,})\b"), 1),
    # xoxb/xoxp Slack token
    (re.compile(r"\b(xox[baprs]-[A-Za-z0-9\-]{10,})\b"), 1),
    # HTTP Authorization: Bearer <token>
    (re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{16,})"), 2),
    # 通用 key/token/secret/password = "value" 或 : value
    (re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd|token)"
        r"(\s*[=:]\s*[\"']?)([A-Za-z0-9._\-/+]{8,})"
    ), 3),
]

# 私钥/证书块整体替换
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

_PLACEHOLDER = "***REDACTED***"


def _mask(secret: str) -> str:
    """保留首尾少量字符的掩码：``sk-ab…yz`` → 便于核对又不泄露。"""
    if len(secret) <= 8:
        return _PLACEHOLDER
    return f"{secret[:4]}…{secret[-2:]}[{_PLACEHOLDER}]"


def redact_secrets(text: str) -> str:
    """返回打码后的文本。非字符串原样返回。"""
    if not text or not isinstance(text, str):
        return text

    redacted = _PRIVATE_KEY_BLOCK.sub(f"-----PRIVATE KEY {_PLACEHOLDER}-----", text)

    for pattern, group in _SECRET_RULES:
        def _repl(m: re.Match[str], _g: int = group) -> str:
            secret = m.group(_g)
            return m.group(0).replace(secret, _mask(secret), 1)

        redacted = pattern.sub(_repl, redacted)

    return redacted


def has_secret(text: str) -> bool:
    """快速判断文本中是否包含可识别的密钥（用于告警/审计）。"""
    if not text or not isinstance(text, str):
        return False
    if _PRIVATE_KEY_BLOCK.search(text):
        return True
    return any(p.search(text) for p, _ in _SECRET_RULES)
