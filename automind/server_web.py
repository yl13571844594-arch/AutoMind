"""Web 层无状态辅助 — 从 server.py 抽出的纯函数（安全响应头 / 静态资源版本化）。

这些函数不依赖任何模块级可变全局，独立可测，供 server.py 导入调用。
拆分目标：将 server.py 的横切关注点（安全头、CSP、cache-bust）内聚到此。
"""

from __future__ import annotations

import re

# 首页 CSP：前端使用内联脚本/样式与内联事件处理器，故允许 'unsafe-inline'；
# 关键防线是 default-src 'self' + frame-ancestors 'self'，杜绝外部资源注入与被外站嵌套。
INDEX_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob: https:; "
    "connect-src 'self' ws: wss:; "
    "frame-src 'self' blob:; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; form-action 'self'"
)

_ASSET_RE = re.compile(r'((?:href|src)="/static/[^"]+\.(?:css|js))"')


def apply_security_headers(response, path: str) -> None:
    """为响应添加安全头（防 MIME 嗅探 / 点击劫持 / Referer 泄漏）；首页附加 CSP。"""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if path in ("/", ""):
        response.headers.setdefault("Content-Security-Policy", INDEX_CSP)


def versioned_html(html: str, version: str) -> str:
    """给首页 HTML 中的 css/js 资源 URL 追加 ?v=<version>，实现按版本 cache-bust。"""
    return _ASSET_RE.sub(rf'\1?v={version}"', html)
