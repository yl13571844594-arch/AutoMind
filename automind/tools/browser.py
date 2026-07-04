"""浏览器自动化工具 — Playwright 异步封装。"""

from __future__ import annotations

from typing import Any

from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool


class BrowserTool(AbstractTool):
    """Playwright 浏览器自动化工具。

    支持操作:
        - navigate: 导航到 URL
        - click: 点击元素
        - type: 输入文本
        - screenshot: 截图
        - extract_text: 提取页面文本
    """

    name = "browser"
    description = (
        "Control a web browser using Playwright. Supports navigation, clicking, "
        "typing, screenshots, and text extraction."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["navigate", "click", "type", "screenshot", "extract_text",
                         "evaluate", "wait_for", "get_links", "scroll", "back", "press"],
                "description": "The browser action to perform.",
            },
            "url": {"type": "string", "description": "URL to navigate to (for navigate action)."},
            "selector": {"type": "string", "description": "CSS selector (for click/type/wait_for/extract_text)."},
            "text": {"type": "string", "description": "Text to type (for type action)."},
            "script": {"type": "string", "description": "JavaScript to evaluate (for evaluate action)."},
            "key": {"type": "string", "description": "Keyboard key to press, e.g. 'Enter' (for press action)."},
            "wait_ms": {"type": "number", "description": "Wait time in milliseconds after action."},
        },
        "required": ["action"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 70

    def __init__(self, headless: bool = True) -> None:
        self._headless = headless
        self._playwright = None
        self._browser = None
        self._page = None

    async def _ensure_browser(self) -> None:
        """延迟初始化浏览器。"""
        if self._page is not None:
            return
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless
            )
            self._page = await self._browser.new_page()
        except ImportError:
            raise ImportError(
                "playwright is not installed. Install it with: pip install playwright && playwright install"
            )
        except Exception as e:
            await self._cleanup()
            raise e

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs["action"]
        try:
            await self._ensure_browser()
        except Exception as e:
            return ToolResult(tool_name=self.name, success=False, error=str(e))

        try:
            if action == "navigate":
                return await self._navigate(kwargs.get("url", ""), kwargs.get("wait_ms", 0))
            elif action == "click":
                return await self._click(kwargs.get("selector", ""), kwargs.get("wait_ms", 0))
            elif action == "type":
                return await self._type(
                    kwargs.get("selector", ""), kwargs.get("text", ""), kwargs.get("wait_ms", 0)
                )
            elif action == "screenshot":
                return await self._screenshot()
            elif action == "extract_text":
                return await self._extract_text(kwargs.get("selector", ""))
            elif action == "evaluate":
                return await self._evaluate(kwargs.get("script", ""))
            elif action == "wait_for":
                return await self._wait_for(kwargs.get("selector", ""), kwargs.get("wait_ms", 10000))
            elif action == "get_links":
                return await self._get_links()
            elif action == "scroll":
                return await self._scroll(kwargs.get("wait_ms", 0))
            elif action == "back":
                await self._page.go_back()
                return ToolResult(tool_name=self.name, success=True, output={"url": self._page.url})
            elif action == "press":
                await self._page.keyboard.press(kwargs.get("key", "Enter"))
                return ToolResult(tool_name=self.name, success=True, output={"pressed": kwargs.get("key", "Enter")})
            else:
                return ToolResult(tool_name=self.name, success=False, error=f"Unknown action: {action}")
        except Exception as e:
            return ToolResult(tool_name=self.name, success=False, error=str(e))

    async def _navigate(self, url: str, wait_ms: int) -> ToolResult:
        if not url:
            return ToolResult(tool_name=self.name, success=False, error="URL is required")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        await self._page.goto(url)
        if wait_ms:
            await self._page.wait_for_timeout(wait_ms)
        return ToolResult(
            tool_name=self.name,
            success=True,
            output={"url": self._page.url, "title": await self._page.title()},
        )

    async def _click(self, selector: str, wait_ms: int) -> ToolResult:
        if not selector:
            return ToolResult(tool_name=self.name, success=False, error="Selector is required")
        await self._page.click(selector)
        if wait_ms:
            await self._page.wait_for_timeout(wait_ms)
        return ToolResult(tool_name=self.name, success=True, output={"clicked": selector})

    async def _type(self, selector: str, text: str, wait_ms: int) -> ToolResult:
        if not selector or not text:
            return ToolResult(tool_name=self.name, success=False, error="Selector and text are required")
        await self._page.fill(selector, text)
        if wait_ms:
            await self._page.wait_for_timeout(wait_ms)
        return ToolResult(tool_name=self.name, success=True, output={"typed": text, "into": selector})

    async def _screenshot(self) -> ToolResult:
        import base64
        data = await self._page.screenshot()
        b64 = base64.b64encode(data).decode("ascii")
        return ToolResult(
            tool_name=self.name,
            success=True,
            output={"screenshot_base64": b64, "format": "png"},
        )

    async def _extract_text(self, selector: str = "") -> ToolResult:
        target = selector or "body"
        text = await self._page.inner_text(target)
        return ToolResult(tool_name=self.name, success=True,
                          output={"selector": target, "text": text[:8000]})

    async def _wait_for(self, selector: str, timeout_ms: int) -> ToolResult:
        if not selector:
            return ToolResult(tool_name=self.name, success=False, error="Selector is required")
        await self._page.wait_for_selector(selector, timeout=timeout_ms or 10000)
        return ToolResult(tool_name=self.name, success=True, output={"appeared": selector})

    async def _get_links(self) -> ToolResult:
        links = await self._page.evaluate(
            "Array.from(document.querySelectorAll('a[href]')).slice(0,80)"
            ".map(a => ({text: (a.innerText||'').trim().slice(0,80), href: a.href}))"
        )
        return ToolResult(tool_name=self.name, success=True, output={"links": links})

    async def _scroll(self, wait_ms: int) -> ToolResult:
        await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        if wait_ms:
            await self._page.wait_for_timeout(wait_ms)
        return ToolResult(tool_name=self.name, success=True, output={"scrolled": "bottom"})

    async def _evaluate(self, script: str) -> ToolResult:
        if not script:
            return ToolResult(tool_name=self.name, success=False, error="Script is required")
        result = await self._page.evaluate(script)
        return ToolResult(tool_name=self.name, success=True, output={"result": result})

    async def close(self) -> None:
        """手动关闭浏览器。"""
        await self._cleanup()

    async def _cleanup(self) -> None:
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._page = None

    async def __aenter__(self) -> BrowserTool:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._cleanup()


class WebFetchTool(AbstractTool):
    """无依赖网页抓取工具 — 用 httpx 获取网页并提取正文文本/链接。

    不需要安装 Playwright，适合"读取网页内容"类任务（强化浏览器自动化的轻量补充）。
    """

    name = "web_fetch"
    description = (
        "Fetch a web page over HTTP and extract readable text and links. "
        "Use for reading article/doc/API content without a full browser."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch (http/https)."},
            "max_chars": {"type": "number", "description": "Max characters of text to return (default 4000)."},
        },
        "required": ["url"],
    }
    permission_tier = PermissionTier.SAFE
    risk_score = 20

    async def execute(self, **kwargs: Any) -> ToolResult:
        import re

        url = kwargs.get("url", "")
        max_chars = int(kwargs.get("max_chars", 4000) or 4000)
        if not url.startswith(("http://", "https://")):
            return ToolResult(tool_name=self.name, success=False,
                              error="url 必须以 http:// 或 https:// 开头")
        try:
            import httpx
        except ImportError:
            return ToolResult(tool_name=self.name, success=False,
                              error="缺少 httpx 库，请先 pip install httpx")
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True,
                                         headers={"User-Agent": "AutoMind/1.0"}) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text
        except Exception as e:
            return ToolResult(tool_name=self.name, success=False, error=str(e))

        # 标题
        title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
        # 去脚本/样式后提取纯文本
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        # 链接
        links = re.findall(r'href=["\'](https?://[^"\']+)["\']', html)[:30]
        return ToolResult(
            tool_name=self.name, success=True,
            output={"title": title, "text": text[:max_chars],
                    "links": list(dict.fromkeys(links)), "url": str(resp.url)},
        )
