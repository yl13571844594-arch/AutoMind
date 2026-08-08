"""网络工具 —— HTTP 请求与网页搜索。

两者都会把数据带出本机，因此共用 ``_toolkit.check_url`` 的 SSRF 防护：
**默认拒绝私网、回环与云元数据地址**。这不是过度设计 —— 模型完全可能被网页
内容或用户文档诱导去请求 ``http://127.0.0.1:8765/api/...``（本机的 AutoMind
自己）或 ``169.254.169.254``（云上实例凭据），把内网接口和临时密钥带出去。
"""

from __future__ import annotations

import json as _json
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


class WebSearchTool(AbstractTool):
    """网页搜索（需配置搜索服务的 API Key）。"""

    name = "web_search"
    description = (
        "Search the web and return ranked results (title, url, snippet). "
        "Requires a search provider API key: set AUTOMIND_SEARCH_PROVIDER "
        "(tavily|serper|brave|searxng) and AUTOMIND_SEARCH_API_KEY. "
        "For self-hosted SearXNG set AUTOMIND_SEARCH_ENDPOINT instead."
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

    #: provider -> (endpoint, 是否用 Bearer 头)
    _PROVIDERS = {
        "tavily": "https://api.tavily.com/search",
        "serper": "https://google.serper.dev/search",
        "brave": "https://api.search.brave.com/res/v1/web/search",
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        import os
        query = str(kwargs.get("query", "")).strip()
        if not query:
            return bad(self.name, "query 不能为空")
        n = max(1, min(int(kwargs.get("max_results") or 5), 20))
        provider = (os.environ.get("AUTOMIND_SEARCH_PROVIDER") or "").lower()
        key = os.environ.get("AUTOMIND_SEARCH_API_KEY", "")
        endpoint = os.environ.get("AUTOMIND_SEARCH_ENDPOINT", "")

        if not provider:
            return bad(self.name,
                       "未配置搜索服务。请设置环境变量 AUTOMIND_SEARCH_PROVIDER "
                       "（tavily / serper / brave / searxng）与 AUTOMIND_SEARCH_API_KEY；"
                       "自建 SearXNG 则设置 AUTOMIND_SEARCH_ENDPOINT。",
                       needs_config=True)
        if provider != "searxng" and not key:
            return bad(self.name, f"{provider} 需要 AUTOMIND_SEARCH_API_KEY", needs_config=True)

        try:
            need("httpx")
            import httpx  # noqa: PLC0415

            async with httpx.AsyncClient(timeout=30) as c:
                if provider == "tavily":
                    r = await c.post(self._PROVIDERS["tavily"],
                                     json={"api_key": key, "query": query, "max_results": n})
                    items = [{"title": x.get("title"), "url": x.get("url"),
                              "snippet": x.get("content")}
                             for x in (r.json().get("results") or [])]
                elif provider == "serper":
                    r = await c.post(self._PROVIDERS["serper"], json={"q": query, "num": n},
                                     headers={"X-API-KEY": key})
                    items = [{"title": x.get("title"), "url": x.get("link"),
                              "snippet": x.get("snippet")}
                             for x in (r.json().get("organic") or [])]
                elif provider == "brave":
                    r = await c.get(self._PROVIDERS["brave"], params={"q": query, "count": n},
                                    headers={"X-Subscription-Token": key,
                                             "Accept": "application/json"})
                    items = [{"title": x.get("title"), "url": x.get("url"),
                              "snippet": x.get("description")}
                             for x in ((r.json().get("web") or {}).get("results") or [])]
                elif provider == "searxng":
                    if not endpoint:
                        return bad(self.name, "searxng 需要 AUTOMIND_SEARCH_ENDPOINT",
                                   needs_config=True)
                    check_url(endpoint, allow_private=True)   # 自建实例常在内网
                    r = await c.get(endpoint, params={"q": query, "format": "json"})
                    items = [{"title": x.get("title"), "url": x.get("url"),
                              "snippet": x.get("content")}
                             for x in (r.json().get("results") or [])][:n]
                else:
                    return bad(self.name, f"不支持的搜索服务：{provider}")

            if r.status_code >= 400:
                return bad(self.name,
                           f"搜索服务返回 {r.status_code}：{(r.text or '')[:200]}")
            return ok(self.name, query=query, provider=provider,
                      results=items[:n], count=len(items[:n]))
        except Exception as e:
            return err(self.name, e)
