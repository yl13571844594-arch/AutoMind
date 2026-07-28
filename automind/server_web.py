"""Web 层无状态辅助 — 从 server.py 抽出的纯函数（安全响应头 / 静态资源版本化）。

这些函数不依赖任何模块级可变全局，独立可测，供 server.py 导入调用。
拆分目标：将 server.py 的横切关注点（安全头、CSP、cache-bust）内聚到此。
"""

from __future__ import annotations

import re

# 首页 CSP：前端使用内联脚本/样式与内联事件处理器，故允许 'unsafe-inline'；
# 关键防线是 default-src 'self' + frame-ancestors 'self'，杜绝外部资源注入与被外站嵌套。
# jsdelivr 仅用于按需加载 Monaco Editor（📄 代码标签页）；worker 走 blob 代理。
INDEX_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
    "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net data:; "
    "img-src 'self' data: blob: https:; "
    "connect-src 'self' ws: wss: https://cdn.jsdelivr.net; "
    "worker-src 'self' blob:; "
    "frame-src 'self' blob:; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; form-action 'self'"
)

_ASSET_RE = re.compile(r'((?:href|src)="/static/[^"]+\.(?:css|js))"')

# HTML 文档路由（入口页 + 兼容版界面 + 手册）—— 一律不入盘缓存，理由见
# cache_control_for() 的说明。
HTML_DOC_PATHS = ("/", "", "/legacy", "/manual")

# 内容哈希产物目录：Vite 构建的 index-<hash>.js / .css。文件名即内容指纹，
# 内容一变文件名必变 → 可安全地长期不可变缓存。
_HASHED_ASSET_PREFIX = "/static/dist/assets/"

# HTML 文档：no-store 是关键的一条 —— 见 cache_control_for()。
_NO_STORE = "no-store, no-cache, must-revalidate, max-age=0"
_IMMUTABLE = "public, max-age=31536000, immutable"


def cache_control_for(path: str) -> str | None:
    """按路径给出 Cache-Control；None 表示不干预（交由框架/上游决定）。

    分三档，修复"升级后部分机器界面加载失败"的根因：

    1. **HTML 文档 → no-store**。入口页引用的是带内容哈希的 JS/CSS
       （``index-<hash>.js``）。老版本只下发 ``no-cache``（协商缓存）时，
       HTML 仍会被写进磁盘缓存；WebView2/Edge 在离线、启发式过期判断或
       缓存分区复用等情况下可能直接吐出**旧版本的 HTML**，而它引用的
       旧哈希 JS 在新安装包里已不存在 → 404 → 白屏兜底提示。
       ``no-store`` 从根上禁止落盘，升级后不可能再读到上一版 HTML。
    2. **内容哈希产物 → immutable 长缓存**。文件名带指纹，永不复用，
       长缓存零风险，同时省掉每次启动的一轮 304 协商。
    3. **其余静态资源 → no-cache**（协商缓存），保持原有语义：经典界面的
       css/js 无哈希，需要每次带 ETag 重验证。
    """
    if path in HTML_DOC_PATHS:
        return _NO_STORE
    if path.startswith(_HASHED_ASSET_PREFIX):
        return _IMMUTABLE
    if path.startswith("/static/"):
        return "no-cache"
    return None


def apply_security_headers(response, path: str) -> None:
    """为响应添加安全头（防 MIME 嗅探 / 点击劫持 / Referer 泄漏）+ 缓存策略。

    缓存策略见 :func:`cache_control_for`。HTML 文档用**直接赋值**而非
    setdefault：这条策略是修复升级后加载失败的根因，不允许被上游默认值
    （如 StaticFiles）覆盖或抢先占位。
    """
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    cc = cache_control_for(path)
    if cc == _NO_STORE:
        response.headers["Cache-Control"] = cc
        # 古老中间层/代理只认 HTTP/1.0 的这两个字段
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    elif cc is not None:
        response.headers.setdefault("Cache-Control", cc)
    if path in ("/", "", "/legacy"):
        response.headers.setdefault("Content-Security-Policy", INDEX_CSP)


def versioned_html(html: str, version: str) -> str:
    """给首页 HTML 中的 css/js 资源 URL 追加 ?v=<version>，实现按版本 cache-bust。"""
    return _ASSET_RE.sub(rf'\1?v={version}"', html)


_DIST_REF_RE = re.compile(r'(?:src|href)="(/static/dist/[^"?]+)"')


def dist_asset_refs(html: str) -> list[str]:
    """提取 dist 首页引用的构建产物 URL 路径（``/static/dist/...``）。

    用于**发布前/请求时的完整性自检**：若 index.html 与 assets 目录来自不同
    次构建（打包脚本漏拷、增量构建残留），引用的哈希文件会 404 —— 与其让
    用户看到白屏兜底，不如提前发现并回退到经典界面。
    """
    return _DIST_REF_RE.findall(html)
