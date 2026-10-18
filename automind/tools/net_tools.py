"""网络工具 —— HTTP 请求与网页搜索。

两者都会把数据带出本机，因此共用 ``_toolkit.check_url`` 的 SSRF 防护：
**默认拒绝私网、回环与云元数据地址**。这不是过度设计 —— 模型完全可能被网页
内容或用户文档诱导去请求 ``http://127.0.0.1:8765/api/...``（本机的 AutoMind
自己）或 ``169.254.169.254``（云上实例凭据），把内网接口和临时密钥带出去。
"""

from __future__ import annotations

import base64
import html as _html
import json as _json
import os
import re
from typing import Any

from automind.core.types import PermissionTier, ToolResult
from automind.tools._toolkit import BlockedTarget, bad, check_url, err, need, ok
from automind.tools.base import AbstractTool

#: 会改变服务端状态的方法 —— 需要更高权限档位
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_MAX_BODY = 200_000


class HttpRequestTool(AbstractTool):
    """发起 HTTP 请求。"""

    name = "http_request"
    description = (
        "Make an HTTP request to a public URL. Supports GET/HEAD/POST/PUT/PATCH/DELETE, "
        "custom headers, query params, JSON or form body. "
        "Private/loopback/link-local addresses and cloud metadata endpoints are blocked "
        "by default; set allow_private=true to reach intranet hosts on purpose."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Target URL (http/https)."},
            "method": {"type": "string", "description": "HTTP method (default GET)."},
            "headers": {"type": "object", "description": "Extra request headers."},
            "params": {"type": "object", "description": "Query-string parameters."},
            "json_body": {"type": "object", "description": "JSON request body."},
            "data": {"type": "string", "description": "Raw/form request body."},
            "timeout": {"type": "number", "description": "Seconds (default 30, max 120)."},
            "allow_private": {
                "type": "boolean",
                "description": "Explicitly permit private/intranet addresses. Default false.",
            },
            "max_chars": {"type": "number", "description": "Cap on returned body (default 200000)."},
        },
        "required": ["url"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 45

    async def execute(self, **kwargs: Any) -> ToolResult:
        url = str(kwargs.get("url", ""))
        method = str(kwargs.get("method") or "GET").upper()
        try:
            need("httpx")
            import httpx  # noqa: PLC0415 - 懒加载

            allow_private = bool(kwargs.get("allow_private"))
            try:
                check_url(url, allow_private=allow_private)
            except BlockedTarget as e:
                return bad(self.name, str(e), blocked=True)

            timeout = max(1.0, min(float(kwargs.get("timeout") or 30), 120))
            cap = int(kwargs.get("max_chars") or _MAX_BODY)
            headers = {str(k): str(v) for k, v in (kwargs.get("headers") or {}).items()}
            headers.setdefault("User-Agent", "AutoMind/1.5 (+https://github.com/)")

            req: dict[str, Any] = {
                "params": kwargs.get("params") or None,
                "headers": headers,
                "timeout": timeout,
            }
            if kwargs.get("json_body") is not None:
                req["json"] = kwargs["json_body"]
            elif kwargs.get("data") is not None:
                req["content"] = str(kwargs["data"]).encode()

            # follow_redirects=False：跟随重定向会绕过上面的 SSRF 校验
            # （公网 URL 可以 302 到 127.0.0.1）。改为手动逐跳校验。
            async with httpx.AsyncClient(follow_redirects=False) as client:
                hops, current = [], url
                for _ in range(5):
                    r = await client.request(method, current, **req)
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        nxt = str(httpx.URL(current).join(r.headers["location"]))
                        try:
                            check_url(nxt, allow_private=allow_private)
                        except BlockedTarget as e:
                            return bad(self.name,
                                       f"重定向目标被拒绝（{e}）。原始地址：{current}",
                                       blocked=True, redirects=hops)
                        hops.append(nxt)
                        current = nxt
                        continue
                    break
                else:
                    return bad(self.name, "重定向次数过多（>5）", redirects=hops)

            text = r.text or ""
            body: Any = text[:cap]
            parsed = None
            if "application/json" in (r.headers.get("content-type") or ""):
                try:
                    parsed = _json.loads(text)
                except ValueError:
                    parsed = None
            return ok(self.name, status=r.status_code, url=str(r.url),
                      headers=dict(r.headers), body=body, json=parsed,
                      truncated=len(text) > cap, redirects=hops,
                      ok=200 <= r.status_code < 400)
        except Exception as e:
            return err(self.name, e)


# ═══════════════════════════════════════════════════════════
# 网页搜索（v1.11.12 修复：开箱即用，不再要求手动配搜索 Key）
# ═══════════════════════════════════════════════════════════
#
# 旧实现只认 AUTOMIND_SEARCH_PROVIDER + AUTOMIND_SEARCH_API_KEY：没配就返回
# 「未配置搜索服务」—— 而绝大多数用户根本没听说过 Tavily/Serper，于是
# web_search 在界面上等于不可用。现在按「有 Key 用官方 API、没 Key 用免密钥
# 网页检索」的顺序自动选择：
#
#   1. 显式配置（向后兼容）：AUTOMIND_SEARCH_PROVIDER=tavily|serper|brave|searxng；
#   2. DeepSeek Search API：用户只要在「API Keys」里配过 DeepSeek（或环境变量
#      DEEPSEEK_API_KEY），就自动复用同一把 Key 调用官方结构化搜索接口
#      （https://api.deepseek.com/v1/tools/search，返回 title/url/snippet/date）；
#   3. 免密钥兜底：抓取 Bing 搜索结果页并解析（cn.bing.com → www.bing.com）。
#      只作为最后兜底，标注 provider="bing"、keyless=true，并提示结果质量可能
#      受页面改版/人机验证影响。
#
# 三层都失败才返回错误，且错误里逐条说明每个后端为什么失败 —— 不再是
# 一句让用户不知道该干什么的「未配置搜索服务」。

_SEARCH_TIMEOUT = 20.0
_DEEPSEEK_SEARCH_DEFAULT_BASE = "https://api.deepseek.com/v1/tools"
_BING_ENDPOINTS = ("https://cn.bing.com/search", "https://www.bing.com/search")
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

#: 测试可替换的 httpx.AsyncClient 工厂（避免真联网）。
_client_factory: Any = None

#: 显式搜索服务的 endpoint（与旧实现一致）
_EXPLICIT_PROVIDERS = {
    "tavily": "https://api.tavily.com/search",
    "serper": "https://google.serper.dev/search",
    "brave": "https://api.search.brave.com/res/v1/web/search",
}

_BING_BLOCK_RE = re.compile(r'<li class="b_algo".*?</li>', re.S | re.I)
_BING_TITLE_RE = re.compile(
    r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_BING_SNIPPET_RE = re.compile(r'<p[^>]*>(.*?)</p>', re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _saved_api_keys() -> dict[str, str]:
    """读用户已保存的 API Key（设置面板写入的 .automind_config.json）。

    搜索工具与 Web 服务不在同一进程层，直接读同一份配置文件即可；文件缺失或
    损坏都按"没配"处理 —— 搜索不可用不能反过来把工具调用弄崩。
    """
    try:
        from automind.core import paths

        path = paths.config_file()
        if not path.is_file():
            return {}
        data = _json.loads(path.read_text(encoding="utf-8"))
        keys = data.get("api_keys") if isinstance(data, dict) else None
        return {str(k): str(v) for k, v in (keys or {}).items() if v}
    except Exception:
        return {}


def _deepseek_search_key() -> str:
    """按 环境变量 → 已保存 Key 的顺序取 DeepSeek Key。"""
    for env in ("DEEPSEEK_API_KEY", "AUTOMIND_DEEPSEEK_API_KEY"):
        value = (os.environ.get(env) or "").strip()
        if value:
            return value
    return _saved_api_keys().get("deepseek", "").strip()


def _deepseek_search_base() -> str:
    raw = (os.environ.get("DEEPSEEK_SEARCH_BASE_URL")
           or os.environ.get("AUTOMIND_DEEPSEEK_SEARCH_BASE")
           or _DEEPSEEK_SEARCH_DEFAULT_BASE).strip().rstrip("/")
    if raw.endswith("/search"):
        raw = raw[: -len("/search")]
    return raw


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub(" ", text or ""))).strip()


def _decode_bing_url(url: str) -> str:
    """解开 Bing 的跳转链接（``/ck/a?...&u=a1<base64url>``），拿真实目标。"""
    url = _html.unescape(url or "").strip()
    if "bing.com/ck/a" not in url:
        return url
    match = re.search(r"[?&]u=a1([^&]+)", url)
    if not match:
        return url
    try:
        payload = match.group(1)
        padded = payload + "=" * (-len(payload) % 4)
        target = base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
        return target if target.startswith(("http://", "https://")) else url
    except Exception:
        return url


def _parse_bing(html_text: str, limit: int) -> list[dict[str, Any]]:
    """从 Bing 搜索结果页解析 title/url/snippet（结构变化时返回空，不抛）。"""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in _BING_BLOCK_RE.findall(html_text or ""):
        title_m = _BING_TITLE_RE.search(block)
        if not title_m:
            continue
        url = _decode_bing_url(title_m.group(1))
        title = _strip_html(title_m.group(2))
        if not url.startswith(("http://", "https://")) or not title:
            continue
        if url in seen:
            continue
        seen.add(url)
        snippet_m = _BING_SNIPPET_RE.search(block)
        items.append({
            "title": title,
            "url": url,
            "snippet": _strip_html(snippet_m.group(1))[:500] if snippet_m else "",
        })
        if len(items) >= limit:
            break
    return items


async def _search_explicit(client: Any, provider: str, key: str,
                           endpoint: str, query: str,
                           n: int) -> tuple[list[dict[str, Any]] | None, str]:
    """旧的四家显式搜索服务（保持原行为）。"""
    try:
        if provider == "tavily":
            r = await client.post(_EXPLICIT_PROVIDERS["tavily"],
                                  json={"api_key": key, "query": query,
                                        "max_results": n}, timeout=_SEARCH_TIMEOUT)
            items = [{"title": x.get("title"), "url": x.get("url"),
                      "snippet": x.get("content")}
                     for x in (r.json().get("results") or [])]
        elif provider == "serper":
            r = await client.post(_EXPLICIT_PROVIDERS["serper"],
                                  json={"q": query, "num": n},
                                  headers={"X-API-KEY": key},
                                  timeout=_SEARCH_TIMEOUT)
            items = [{"title": x.get("title"), "url": x.get("link"),
                      "snippet": x.get("snippet")}
                     for x in (r.json().get("organic") or [])]
        elif provider == "brave":
            r = await client.get(_EXPLICIT_PROVIDERS["brave"],
                                 params={"q": query, "count": n},
                                 headers={"X-Subscription-Token": key,
                                          "Accept": "application/json"},
                                 timeout=_SEARCH_TIMEOUT)
            items = [{"title": x.get("title"), "url": x.get("url"),
                      "snippet": x.get("description")}
                     for x in ((r.json().get("web") or {}).get("results") or [])]
        elif provider == "searxng":
            if not endpoint:
                return None, "searxng 需要 AUTOMIND_SEARCH_ENDPOINT"
            check_url(endpoint, allow_private=True)   # 自建实例常在内网
            r = await client.get(endpoint, params={"q": query, "format": "json"},
                                 timeout=_SEARCH_TIMEOUT)
            items = [{"title": x.get("title"), "url": x.get("url"),
                      "snippet": x.get("content")}
                     for x in (r.json().get("results") or [])][:n]
        else:
            return None, f"不支持的搜索服务：{provider}"
        if r.status_code >= 400:
            return None, f"HTTP {r.status_code}：{(r.text or '')[:160]}"
        return items[:n], ""
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:160]}"


async def _search_deepseek(client: Any, query: str,
                           n: int) -> tuple[list[dict[str, Any]] | None, str]:
    """DeepSeek Search API（结构化结果，不消耗模型轮次）。"""
    key = _deepseek_search_key()
    if not key:
        return None, "未配置 DeepSeek Key（在设置→API Keys 填 DeepSeek 即可自动启用）"
    base = _deepseek_search_base()
    try:
        r = await client.post(
            f"{base}/search",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"},
            json={"query": query, "count": n, "need_content": False,
                  # 官方 API 的意图信号；截到 128 字符与官方指南一致
                  "ori_question": query[:128]},
            timeout=_SEARCH_TIMEOUT,
        )
        if r.status_code >= 400:
            hint = "（Key 无效或未开通搜索权限）" if r.status_code in (401, 403) else ""
            return None, f"HTTP {r.status_code}{hint}：{(r.text or '')[:160]}"
        data = r.json()
        raw = data.get("results") if isinstance(data, dict) else data
        items: list[dict[str, Any]] = []
        for x in raw or []:
            if not isinstance(x, dict):
                continue
            url = str(x.get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            snippet = x.get("snippet") or x.get("summary") or ""
            item: dict[str, Any] = {
                "title": _strip_html(str(x.get("title") or url)),
                "url": url,
                "snippet": _strip_html(str(snippet))[:500],
            }
            if x.get("date"):
                item["date"] = str(x.get("date"))[:40]
            if x.get("site_name"):
                item["site"] = str(x.get("site_name"))[:80]
            items.append(item)
            if len(items) >= n:
                break
        return items, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:160]}"


async def _search_bing(client: Any, query: str,
                       n: int) -> tuple[list[dict[str, Any]] | None, str]:
    """免密钥兜底：解析 Bing 搜索结果页。"""
    last_error = ""
    for endpoint in _BING_ENDPOINTS:
        try:
            r = await client.get(
                endpoint,
                params={"q": query, "count": max(n, 10), "setlang": "zh-CN"},
                headers={"User-Agent": _BROWSER_UA,
                         "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                         "Accept": "text/html,application/xhtml+xml"},
                timeout=_SEARCH_TIMEOUT,
            )
            if r.status_code >= 400:
                last_error = f"{endpoint} HTTP {r.status_code}"
                continue
            items = _parse_bing(r.text, n)
            if items:
                return items, ""
            last_error = f"{endpoint} 未解析到结果（可能触发人机验证或页面改版）"
        except Exception as e:
            last_error = f"{endpoint} {type(e).__name__}: {str(e)[:120]}"
    return None, last_error or "没有可用的 Bing 入口"


class WebSearchTool(AbstractTool):
    """网页搜索（开箱即用：DeepSeek Key 自动启用官方搜索 API，兜底免密钥）。"""

    name = "web_search"
    description = (
        "Search the web and return ranked results (title, url, snippet). "
        "Works out of the box: when a DeepSeek API key is configured (API Keys panel "
        "or DEEPSEEK_API_KEY), it uses the DeepSeek Search API; otherwise it falls back "
        "to a keyless web search. Optional explicit providers: set "
        "AUTOMIND_SEARCH_PROVIDER (tavily|serper|brave|searxng) plus "
        "AUTOMIND_SEARCH_API_KEY (SearXNG: AUTOMIND_SEARCH_ENDPOINT)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {"type": "number", "description": "Default 5, max 20."},
            "lang": {"type": "string", "description": "Language hint, e.g. zh-CN."},
        },
        "required": ["query"],
    }
    permission_tier = PermissionTier.SAFE
    risk_score = 10

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query", "")).strip()
        if not query:
            return bad(self.name, "query 不能为空")
        try:
            n = max(1, min(int(kwargs.get("max_results") or 5), 20))
        except (TypeError, ValueError):
            n = 5

        provider = (os.environ.get("AUTOMIND_SEARCH_PROVIDER") or "").strip().lower()
        key = (os.environ.get("AUTOMIND_SEARCH_API_KEY") or "").strip()
        endpoint = (os.environ.get("AUTOMIND_SEARCH_ENDPOINT") or "").strip()

        try:
            need("httpx")
            import httpx  # noqa: PLC0415

            factory = _client_factory or httpx.AsyncClient
            attempts: list[str] = []
            async with factory(timeout=_SEARCH_TIMEOUT, follow_redirects=True) as client:
                # 1) 显式配置优先（保持旧行为；失败才降级，不直接判死）
                if provider:
                    if provider != "searxng" and not key:
                        attempts.append(f"{provider}：缺少 AUTOMIND_SEARCH_API_KEY")
                    else:
                        items, why = await _search_explicit(
                            client, provider, key, endpoint, query, n)
                        if items is not None:
                            return ok(self.name, query=query, provider=provider,
                                      results=items, count=len(items))
                        attempts.append(f"{provider}：{why}")

                # 2) DeepSeek 官方搜索 API（配了 Key 就能用）
                items, why = await _search_deepseek(client, query, n)
                if items is not None:
                    return ok(self.name, query=query, provider="deepseek",
                              keyless=False, results=items, count=len(items))
                attempts.append(f"DeepSeek 搜索 API：{why}")

                # 3) 免密钥 Bing 兜底
                items, why = await _search_bing(client, query, n)
                if items is not None:
                    return ok(self.name, query=query, provider="bing",
                              keyless=True, results=items, count=len(items),
                              note="未配置搜索 Key，已使用免密钥网页检索；结果质量可能"
                                   "受页面改版/人机验证影响，配置 DeepSeek Key 后自动升级")
                attempts.append(f"Bing 网页：{why}")

            return bad(
                self.name,
                "搜索失败（所有后端都不可用）：\n- " + "\n- ".join(attempts)
                + "\n\n可选修复：① 在「设置 → API Keys」配置 DeepSeek（最省事）；"
                "② 配 AUTOMIND_SEARCH_PROVIDER/AUTOMIND_SEARCH_API_KEY 用 "
                "tavily/serper/brave；③ 自建 SearXNG 配 AUTOMIND_SEARCH_ENDPOINT。",
                attempts=attempts,
                needs_config=not provider and not _deepseek_search_key(),
            )
        except Exception as e:
            return err(self.name, e)

