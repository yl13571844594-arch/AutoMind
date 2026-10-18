"""web_search 开箱即用链路（v1.11.12）。

旧实现要求用户自己配 Tavily/Serper 的 Key，没配就返回「未配置搜索服务」——
在界面上等同于工具不可用。新实现按三层降级：

1. 显式配置的搜索服务（保持旧行为）；
2. 已保存/环境变量里的 DeepSeek Key → DeepSeek 官方 Search API；
3. 免密钥 Bing 网页检索兜底。

这里用 ``httpx.MockTransport`` 覆盖三条链路与全部失败话术，不联网。
"""

from __future__ import annotations

import base64
import json

import httpx

import automind.tools.net_tools as nt


def _install(monkeypatch, handler, *, key: str = "test-key"):
    """替换模块级 client 工厂与 DeepSeek Key 解析（杜绝真联网）。"""

    def factory(**kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(nt, "_client_factory", factory)
    monkeypatch.setattr(nt, "_deepseek_search_key", lambda: key)
    monkeypatch.delenv("AUTOMIND_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("AUTOMIND_SEARCH_API_KEY", raising=False)


async def _run(query: str = "q", **kwargs):
    return await nt.WebSearchTool().execute(query=query, **kwargs)


class TestDeepSeekBackend:
    async def test_uses_saved_deepseek_key_and_parses_results(self, monkeypatch):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"results": [
                {"title": "A <b>result</b>", "url": "https://a.example/x",
                 "snippet": "snippet A", "date": "2026-10-01",
                 "site_name": "A 站"},
                {"title": "B", "url": "https://b.example/y", "summary": "summary B"},
                {"title": "bad", "url": "not-a-url"},
            ]})

        _install(monkeypatch, handler)
        r = await _run("AutoMind", max_results=5)
        assert r.success, r.error
        assert r.output["provider"] == "deepseek"
        assert r.output["keyless"] is False
        assert r.output["count"] == 2
        assert seen["auth"] == "Bearer test-key"
        assert seen["url"].endswith("/v1/tools/search")
        assert seen["body"]["query"] == "AutoMind"
        assert seen["body"]["need_content"] is False
        first = r.output["results"][0]
        assert first["title"] == "A result"          # HTML 已清洗
        assert first["snippet"] == "snippet A"
        assert first["date"] == "2026-10-01"
        assert first["site"] == "A 站"
        assert r.output["results"][1]["snippet"] == "summary B"

    async def test_empty_result_array_is_success(self, monkeypatch):
        _install(monkeypatch, lambda _request: httpx.Response(200, json={"results": []}))
        r = await _run()
        assert r.success and r.output["provider"] == "deepseek"
        assert r.output["count"] == 0 and r.output["results"] == []

    async def test_401_falls_back_to_bing(self, monkeypatch):
        bing_html = _bing_html([
            ("https://a.example/x", "A 结果", "A 摘要"),
        ])

        def handler(request: httpx.Request) -> httpx.Response:
            if "api.deepseek.com" in str(request.url):
                return httpx.Response(401, text="invalid key")
            return httpx.Response(200, text=bing_html)

        _install(monkeypatch, handler)
        r = await _run("AutoMind")
        assert r.success, r.error
        assert r.output["provider"] == "bing"
        assert r.output["keyless"] is True
        assert r.output["results"][0]["url"] == "https://a.example/x"
        assert "DeepSeek" in (r.output.get("note") or "")

    async def test_key_from_env_overrides_saved(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
        monkeypatch.setattr(nt, "_saved_api_keys", lambda: {"deepseek": "saved-key"})
        assert nt._deepseek_search_key() == "env-key"


class TestKeylessFallback:
    async def test_no_key_uses_bing(self, monkeypatch):
        bing_html = _bing_html([
            ("https://a.example/1", "标题一", "摘要一"),
            ("https://b.example/2", "标题二", "摘要二"),
            ("https://a.example/1", "重复", "重复"),
        ])
        _install(monkeypatch, lambda _request: httpx.Response(200, text=bing_html),
                 key="")
        r = await _run("Python asyncio", max_results=5)
        assert r.success, r.error
        assert r.output["provider"] == "bing" and r.output["keyless"] is True
        assert r.output["count"] == 2          # 去重
        assert r.output["results"][0]["title"] == "标题一"

    async def test_all_backends_fail_with_actionable_error(self, monkeypatch):
        _install(monkeypatch, lambda _request: httpx.Response(503, text="down"), key="")
        r = await _run()
        assert not r.success
        assert r.output["needs_config"] is True
        assert len(r.output["attempts"]) == 2
        assert "DeepSeek" in (r.error or "")
        assert "AUTOMIND_SEARCH_PROVIDER" in (r.error or "")

    async def test_bing_captcha_page_reports_no_results(self, monkeypatch):
        _install(monkeypatch, lambda _request: httpx.Response(
            200, text="<html><body>验证码</body></html>"), key="")
        r = await _run()
        assert not r.success
        assert "未解析到结果" in (r.error or "")


class TestExplicitProvider:
    async def test_tavily_still_works(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "api.tavily.com" in str(request.url)
            return httpx.Response(200, json={"results": [
                {"title": "T", "url": "https://t.example", "content": "C"}]})

        _install(monkeypatch, handler)
        monkeypatch.setenv("AUTOMIND_SEARCH_PROVIDER", "tavily")
        monkeypatch.setenv("AUTOMIND_SEARCH_API_KEY", "tv-key")
        r = await _run()
        assert r.success and r.output["provider"] == "tavily"
        assert r.output["results"][0]["snippet"] == "C"

    async def test_explicit_failure_falls_back(self, monkeypatch):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if "api.tavily.com" in str(request.url):
                return httpx.Response(500, text="down")
            return httpx.Response(200, json={"results": [
                {"title": "D", "url": "https://d.example", "snippet": "s"}]})

        _install(monkeypatch, handler)
        monkeypatch.setenv("AUTOMIND_SEARCH_PROVIDER", "tavily")
        monkeypatch.setenv("AUTOMIND_SEARCH_API_KEY", "tv-key")
        r = await _run()
        assert r.success and r.output["provider"] == "deepseek"
        assert any("tavily" in u for u in calls)


class TestParsers:
    def test_decode_bing_redirect_url(self):
        target = "https://example.com/article?x=1"
        token = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        url = f"https://www.bing.com/ck/a?!&&p=abc&u=a1{token}&ntb=1"
        assert nt._decode_bing_url(url) == target

    def test_parse_bing_skips_invalid_and_dedupes(self):
        html = _bing_html([
            ("https://a.example", "A", "sa"),
            ("javascript:void(0)", "bad", ""),
            ("https://a.example", "A2", "dup"),
        ])
        items = nt._parse_bing(html, 10)
        assert len(items) == 1 and items[0]["url"] == "https://a.example"
        assert items[0]["title"] == "A"

    def test_strip_html_unescapes_entities(self):
        assert nt._strip_html("<b>a&amp;b</b>  c") == "a&b c"


def _bing_html(items: list[tuple[str, str, str]]) -> str:
    blocks = []
    for url, title, snippet in items:
        blocks.append(
            '<li class="b_algo"><h2><a href="' + url + '">' + title
            + "</a></h2><div class=\"b_caption\"><p>" + snippet
            + "</p></div></li>")
    return "<html><body><ol>" + "".join(blocks) + "</ol></body></html>"
