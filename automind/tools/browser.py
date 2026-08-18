"""浏览器自动化工具 — Playwright 异步封装。

浏览器从哪来（§按此顺序回退）
-----------------------------
Playwright 的 Python 包**不含浏览器本体**，要另跑一次 ``playwright install``
下载约 150MB 的 Chromium。装了包却没下载二进制时，用户会撞上一大段英文：

    BrowserType.launch: Executable doesn't exist at ...\\chrome-headless-shell.exe
    ╔═══ Looks like Playwright was just installed or updated. ═══╗
    ║     playwright install                                     ║

对桌面版用户尤其难办 —— 冻结包里没有可用的 ``playwright`` 命令行，那句提示
照着做也做不了。

所以改成三级回退：
  1. Playwright 自带的 Chromium（``playwright install`` 下载过就用它）；
  2. **系统已装的 Edge / Chrome**（Windows 上 Edge 是系统组件，几乎必然存在，
     零下载即可用；macOS/Linux 上有 Chrome 也能命中）；
  3. 三者都没有 → 给一句中文说明 + 可照抄的安装命令。

`browser_status()` 供界面做"浏览器就绪状态"自检。
"""

from __future__ import annotations

from typing import Any

from automind.core.logging import get_logger
from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool

logger = get_logger("automind.tools.browser")

#: 系统浏览器回退通道，按优先级。msedge 在 Win10/11 上是系统自带组件。
SYSTEM_CHANNELS: tuple[str, ...] = ("msedge", "chrome", "chrome-beta", "msedge-beta")

_MISSING_HINT = (
    "浏览器自动化不可用：既没有 Playwright 下载的 Chromium，也没有检测到系统"
    "已安装的 Edge / Chrome。\n"
    "解决办法（任选其一）：\n"
    "  · 装一个 Chrome 或 Edge（Windows 10/11 自带 Edge，通常无需此步）；\n"
    "  · 下载 Playwright 自带的 Chromium：python -m playwright install chromium"
)


def _is_missing_executable(exc: Exception) -> bool:
    """判断异常是不是"浏览器二进制不存在"（而非启动参数/权限等其它错）。"""
    msg = str(exc)
    return ("Executable doesn't exist" in msg
            or "playwright install" in msg
            or "Chromium distribution" in msg)


async def browser_status() -> dict:
    """探测浏览器可用性，供界面自检与 /api/browser/status 使用。

    Returns:
        ``{"ready": bool, "source": "bundled"|"msedge"|..., "detail": str}``
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"ready": False, "source": "", "sdk": False,
                "detail": "未安装 playwright 库：pip install playwright"}

    pw = None
    try:
        pw = await async_playwright().start()
        for source, kwargs in _launch_candidates():
            try:
                b = await pw.chromium.launch(headless=True, **kwargs)
                await b.close()
                return {"ready": True, "source": source, "sdk": True,
                        "detail": "自带 Chromium" if source == "bundled"
                                  else f"系统浏览器（{source}）"}
            except Exception:
                continue
        return {"ready": False, "source": "", "sdk": True, "detail": _MISSING_HINT}
    except Exception as e:
        return {"ready": False, "source": "", "sdk": True, "detail": str(e)[:300]}
    finally:
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass


def _launch_candidates() -> list[tuple[str, dict]]:
    """按优先级列出启动方案：自带 Chromium → 系统 Edge/Chrome。"""
    return [("bundled", {})] + [(c, {"channel": c}) for c in SYSTEM_CHANNELS]


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
        #: 本次实际用上的浏览器来源（bundled / msedge / chrome…），供结果标注
        self.browser_source: str = ""

    async def _ensure_browser(self) -> None:
        """延迟初始化浏览器：自带 Chromium → 系统 Edge/Chrome → 明确报错。"""
        if self._page is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise ImportError(
                "未安装 playwright 库。请执行：pip install playwright"
            ) from e

        try:
            self._playwright = await async_playwright().start()
        except Exception:
            await self._cleanup()
            raise

        last_exc: Exception | None = None
        for source, kwargs in _launch_candidates():
            try:
                self._browser = await self._playwright.chromium.launch(
                    headless=self._headless, **kwargs)
                self._page = await self._browser.new_page()
                self.browser_source = source
                if source != "bundled":
                    # 用的是系统浏览器而非自带 Chromium，记一笔便于排查行为差异
                    logger.info("browser_fallback_to_system", channel=source)
                return
            except Exception as e:
                last_exc = e
                if not _is_missing_executable(e):
                    # 不是"没这个浏览器"，而是启动本身出错（权限/沙箱/参数）——
                    # 继续换通道只会掩盖真正的原因
                    await self._cleanup()
                    raise
                continue

        await self._cleanup()
        raise RuntimeError(_MISSING_HINT + (
            f"\n（最后一次尝试的报错：{str(last_exc)[:200]}）" if last_exc else ""))

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
