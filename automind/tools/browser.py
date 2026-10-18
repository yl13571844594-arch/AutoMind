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


#: 导航 / 单步动作的默认超时（毫秒）。Playwright 默认 30s 对"点一个按钮"太长 ——
#: 选择器写错时模型要干等半分钟才拿到失败，一轮任务的墙钟预算被白白吃掉。
NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 8_000
#: 定位兜底时每种候选的探测超时（毫秒）：只是"看一眼在不在"，不能久等
PROBE_TIMEOUT_MS = 1_200

#: 这些报错多半是瞬时的（网络抖动 / 页面正在跳转），值得原地重试一次
_TRANSIENT_MARKERS = (
    "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED", "ERR_NETWORK_CHANGED",
    "ERR_TIMED_OUT", "ERR_EMPTY_RESPONSE", "Execution context was destroyed",
    "frame was detached",
)
#: 这些报错说明浏览器 / 页面本身已经没了，要重开而不是重试
_DEAD_MARKERS = (
    "Target page, context or browser has been closed", "Browser has been closed",
    "browser has disconnected", "Target closed", "Connection closed",
    "Navigation failed because page crashed", "Page crashed",
)

#: snapshot 动作在页面里执行的脚本：给可交互元素打上 data-am-ref 编号并列出来。
#: 模型此后用 ref=编号 定位 —— 比让模型凭空猜 CSS 选择器可靠得多
#: （猜错选择器是浏览器任务失败的头号原因）。
_SNAPSHOT_JS = r"""
(limit) => {
  const sel = 'a[href],button,input:not([type=hidden]),textarea,select,[role=button],'
    + '[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=option],[role=textbox],'
    + '[contenteditable=""],[contenteditable=true],[onclick],summary';
  document.querySelectorAll('[data-am-ref]').forEach(e => e.removeAttribute('data-am-ref'));
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (out.length >= limit) break;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
    n += 1;
    el.setAttribute('data-am-ref', String(n));
    const text = (el.innerText || el.value || el.getAttribute('aria-label')
      || el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt')
      || '').replace(/\s+/g, ' ').trim().slice(0, 60);
    out.push({
      ref: n, tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || el.getAttribute('role') || '',
      text: text, name: el.getAttribute('name') || '',
      href: el.tagName === 'A' ? (el.getAttribute('href') || '').slice(0, 120) : '',
      disabled: !!el.disabled,
      in_view: r.top < innerHeight && r.bottom > 0,
    });
  }
  return out;
}
"""


def _first_line(exc: BaseException | None, limit: int = 240) -> str:
    """Playwright 的启动报错动辄几 KB（整段命令行 + 浏览器日志），界面只放第一行。"""
    text = str(exc or "").strip()
    return (text.splitlines()[0] if text else type(exc).__name__)[:limit]


def _is_transient(exc: BaseException | None) -> bool:
    msg = str(exc or "")
    return any(m in msg for m in _TRANSIENT_MARKERS)


def _is_dead(exc: BaseException | None) -> bool:
    msg = str(exc or "")
    return any(m in msg for m in _DEAD_MARKERS)


def _ref_selector(ref: Any) -> str:
    """``ref=3`` / ``@3`` → snapshot 打上的属性选择器；不是数字返回空串。"""
    s = str(ref).strip().lstrip("@").lstrip("[").rstrip("]")
    return f'[data-am-ref="{s}"]' if s.isdigit() else ""


def _env_headless() -> bool:
    """``AUTOMIND_BROWSER_HEADLESS=0`` 时弹出可见窗口（调试 / 需要人工过验证码）。"""
    import os
    return os.environ.get("AUTOMIND_BROWSER_HEADLESS", "1").strip().lower() not in (
        "0", "false", "no", "off")


class BrowserLocateError(LookupError):
    """元素定位失败 —— 报错本身已是给模型的可执行指引，原样返回。"""


class _Session:
    """一个会话独占的浏览器上下文（cookie / 登录态 / 标签页互不串）。"""

    __slots__ = ("context", "page", "lock")

    def __init__(self, context: Any, page: Any) -> None:
        import asyncio
        self.context = context
        self.page = page
        self.lock = asyncio.Lock()


class BrowserTool(AbstractTool):
    """Playwright 浏览器自动化工具。

    v1.11.1 可靠性改造（提升浏览器任务成功率）：
      · **按会话隔离**：工具注册表在会话克隆之间共享，此前所有会话共用同一个
        page —— 两个会话同时开网页会互相跳走。现在一个浏览器进程、每个会话一个
        独立 BrowserContext（cookie / 登录态也不串）。
      · **可定位**：新增 ``snapshot`` 动作给可交互元素编号，之后用 ``ref`` 点击 /
        输入；``selector`` 没命中时依次按按钮 / 链接名、placeholder、label、
        可见文本兜底 —— 模型写"登录"也能点到登录按钮。
      · **能自愈**：浏览器崩溃、页面被关会自动重开；网络抖动类错误原地重试一次；
        点击被遮挡时滚动 + 强制点击 + DOM 点击逐级兜底；fill 不支持的富文本
        编辑器改为逐字输入；新标签页自动接管；原生 alert/confirm 自动确认。
      · **不污染上下文**：截图落盘只回路径，base64 不再进工具输出（此前一张截图
        = 一万多字符的乱码塞给模型）；画面经事件推给界面右侧「浏览器」预览。
      · **报错可执行**：Playwright 的英文长报错换成"下一步该怎么做"。
    """

    name = "browser"
    description = (
        "Control a real web browser (Playwright). Typical loop: navigate → snapshot "
        "(lists clickable/typeable elements with numeric refs) → click/type with ref → "
        "extract_text to read results. Prefer `ref` from the latest snapshot over guessing "
        "CSS selectors; `selector` also accepts the element's visible text (e.g. '登录'). "
        "Screenshots are saved to a file and shown to the user; image data is not returned."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["navigate", "snapshot", "click", "type", "select", "hover",
                         "press", "scroll", "back", "forward", "reload", "wait_for",
                         "extract_text", "get_links", "screenshot", "evaluate", "close"],
                "description": "The browser action to perform.",
            },
            "url": {"type": "string", "description": "URL to open (navigate)."},
            "ref": {"type": "string",
                    "description": "Element number from the latest snapshot "
                                   "(click/type/select/hover/extract_text/wait_for)."},
            "selector": {"type": "string",
                         "description": "CSS selector or the element's visible text (used when no ref)."},
            "text": {"type": "string", "description": "Text to type (type) or option label/value (select)."},
            "submit": {"type": "boolean", "description": "Press Enter after typing (type)."},
            "script": {"type": "string", "description": "JavaScript expression to evaluate (evaluate)."},
            "key": {"type": "string", "description": "Keyboard key, e.g. 'Enter' (press)."},
            "direction": {"type": "string", "enum": ["down", "up", "bottom", "top"],
                          "description": "Scroll direction (scroll; default: down one screen)."},
            "full_page": {"type": "boolean", "description": "Capture the whole page (screenshot)."},
            "wait_ms": {"type": "number", "description": "Extra wait in milliseconds after the action."},
        },
        "required": ["action"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 70

    #: 不改变页面的动作：做完不必推预览截图
    _READ_ONLY = frozenset({"snapshot", "extract_text", "get_links", "evaluate",
                            "screenshot", "close", "wait_for"})
    #: 可以安全重放的动作（重试不会造成重复提交）
    _IDEMPOTENT = frozenset({"navigate", "reload", "snapshot", "extract_text",
                             "get_links", "screenshot", "wait_for", "scroll"})

    def __init__(self, headless: bool | None = None) -> None:
        self._headless = _env_headless() if headless is None else headless
        self._playwright: Any = None
        self._browser: Any = None
        self._loop: Any = None
        self._sessions: dict[str, _Session] = {}
        self._launch_lock: Any = None
        #: 本次实际用上的浏览器来源（bundled / msedge / chrome…），供结果标注
        self.browser_source: str = ""

    # ── 生命周期 ───────────────────────────────────────────

    @property
    def _page(self) -> Any:
        """兼容旧代码 / 测试：当前会话的页面（未打开时为 None）。"""
        s = self._sessions.get(self._sid())
        return s.page if s else None

    @staticmethod
    def _sid() -> str:
        try:
            from automind.core.session_ctx import session_id
            return session_id() or "default"
        except Exception:
            return "default"

    def _forget_foreign_loop(self) -> None:
        """Playwright 对象绑在创建它的事件循环上；换了循环（CLI 多次 asyncio.run、
        测试）就只能丢掉旧引用重开 —— 跨循环 await 旧对象会直接挂死。"""
        import asyncio
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            self._playwright = self._browser = None
            self._sessions.clear()
            self._launch_lock = None
        self._loop = loop

    async def _ensure_browser(self) -> None:
        """延迟启动浏览器：自带 Chromium → 系统 Edge/Chrome → 明确报错。"""
        import asyncio
        self._forget_foreign_loop()
        if self._browser is not None and self._browser.is_connected():
            return
        if self._launch_lock is None:
            self._launch_lock = asyncio.Lock()
        async with self._launch_lock:
            if self._browser is not None and self._browser.is_connected():
                return
            if self._browser is not None or self._playwright is not None:
                # 浏览器进程挂了（崩溃 / 被用户关掉）：旧会话全部作废，重开
                logger.warning("browser_disconnected_relaunch")
                await self._cleanup()
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:
                raise ImportError("未安装 playwright 库。请执行：pip install playwright") from e

            try:
                self._playwright = await async_playwright().start()
            except Exception:
                await self._cleanup()
                raise

            last_exc: Exception | None = None
            crashed: list[tuple[str, Exception]] = []
            for source, kwargs in _launch_candidates():
                try:
                    self._browser = await self._playwright.chromium.launch(
                        headless=self._headless, **kwargs)
                    self.browser_source = source
                    if source != "bundled":
                        # 用的是系统浏览器而非自带 Chromium，记一笔便于排查行为差异
                        logger.info("browser_fallback_to_system", channel=source,
                                    after_crashes=[c for c, _ in crashed])
                    return
                except Exception as e:
                    last_exc = e
                    if not _is_missing_executable(e):
                        # v1.11.1：装了但起不来（被组策略 / 安全软件拦、配置目录损坏）
                        # 时不再立刻放弃 —— 机器上往往还装着另一个浏览器，换一个多半
                        # 就能跑。真正的原因记下来，全都失败时原样报告，不会被掩盖。
                        crashed.append((source, e))
                        logger.warning("browser_launch_crashed", channel=source,
                                       error=_first_line(e))
                    continue

            await self._cleanup()
            if crashed:
                raise RuntimeError(
                    f"浏览器启动失败（已尝试 {'、'.join(c for c, _ in crashed)}）："
                    f"{_first_line(crashed[0][1])}\n"
                    "常见原因：安全软件 / 组策略拦截了浏览器进程，或浏览器配置目录不可写。"
                    "可设置 AUTOMIND_BROWSER_HEADLESS=0 弹出窗口观察，"
                    "或运行 python -m playwright install chromium 使用独立的 Chromium。")
            raise RuntimeError(_MISSING_HINT + (
                f"\n（最后一次尝试的报错：{_first_line(last_exc)}）" if last_exc else ""))

    async def _session(self) -> _Session:
        await self._ensure_browser()
        sid = self._sid()
        s = self._sessions.get(sid)
        if s is not None and not s.page.is_closed():
            return s
        if s is not None:
            # 页面被关（window.close / 弹窗收走）：沿用上下文开新页，登录态还在
            try:
                s.page = await s.context.new_page()
                self._prime_page(s.page)
                return s
            except Exception:
                self._sessions.pop(sid, None)
        ctx = await self._browser.new_context(
            viewport={"width": 1280, "height": 800}, locale="zh-CN")
        page = await ctx.new_page()
        s = _Session(ctx, page)
        self._sessions[sid] = s

        # 新标签页（target=_blank / window.open）自动接管：否则模型点了链接，
        # 后续操作还停在旧页上，看到的永远是点击之前的内容
        def _on_page(new_page: Any, _s: _Session = s) -> None:
            _s.page = new_page
            self._prime_page(new_page)

        ctx.on("page", _on_page)
        self._prime_page(page)
        return s

    def _prime_page(self, page: Any) -> None:
        page.set_default_timeout(ACTION_TIMEOUT_MS)
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        # 原生 alert / confirm 会阻塞页面上的一切后续操作 —— 一律接受
        page.on("dialog", self._on_dialog)

    @staticmethod
    def _on_dialog(dialog: Any) -> None:
        import asyncio

        async def _accept() -> None:
            try:
                await dialog.accept()
            except Exception:
                pass
        asyncio.ensure_future(_accept())

    # ── 执行入口 ───────────────────────────────────────────

    async def execute(self, **kwargs: Any) -> ToolResult:
        import asyncio
        action = str(kwargs.get("action") or "")
        if action == "close":
            await self._close_session()
            return self._ok({"closed": True})
        last: BaseException | None = None
        for attempt in range(2):
            try:
                sess = await self._session()
            except Exception as e:
                return self._fail(str(e))
            try:
                async with sess.lock:
                    result = await self._dispatch(sess.page, action, kwargs)
                    if result.success and action not in self._READ_ONLY:
                        await self._publish_preview(sess.page, action)
                    return result
            except BrowserLocateError as e:
                return self._fail(str(e))
            except Exception as e:
                last = e
                if attempt == 0 and _is_dead(e):
                    # 页面 / 浏览器已经没了：丢掉这个会话的上下文，重开后再来一次
                    logger.warning("browser_session_dead_retry", error=str(e)[:200])
                    await self._close_session()
                    continue
                if attempt == 0 and _is_transient(e) and action in self._IDEMPOTENT:
                    await asyncio.sleep(0.8)
                    continue
                break
        return self._fail(self._explain(action, kwargs, last))

    async def _dispatch(self, page: Any, action: str, kw: dict) -> ToolResult:
        wait_ms = int(kw.get("wait_ms") or 0)
        if action == "navigate":
            r = await self._navigate(page, str(kw.get("url") or ""))
        elif action == "snapshot":
            r = await self._snapshot(page)
        elif action == "click":
            loc = await self._locate(page, kw)
            await self._click(loc)
            await self._settle(page)
            r = self._ok({"clicked": self._target_desc(kw), "url": page.url})
        elif action == "type":
            loc = await self._locate(page, kw)
            text = str(kw.get("text") or "")
            await self._type(loc, text)
            if kw.get("submit"):
                await loc.press("Enter")
                await self._settle(page)
            r = self._ok({"typed": text, "into": self._target_desc(kw),
                          "submitted": bool(kw.get("submit"))})
        elif action == "select":
            loc = await self._locate(page, kw)
            val = str(kw.get("text") or "")
            try:
                picked = await loc.select_option(label=val)
            except Exception:
                picked = await loc.select_option(val)
            r = self._ok({"selected": picked, "in": self._target_desc(kw)})
        elif action == "hover":
            loc = await self._locate(page, kw)
            await loc.hover()
            r = self._ok({"hovered": self._target_desc(kw)})
        elif action == "press":
            key = str(kw.get("key") or "Enter")
            await page.keyboard.press(key)
            await self._settle(page)
            r = self._ok({"pressed": key, "url": page.url})
        elif action == "scroll":
            r = await self._scroll(page, str(kw.get("direction") or "down"))
        elif action in ("back", "forward", "reload"):
            fn = {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[action]
            await fn(wait_until="domcontentloaded")
            r = self._ok({"url": page.url, "title": await self._title(page)})
        elif action == "wait_for":
            if kw.get("ref") in (None, "") and not kw.get("selector"):
                return self._fail("wait_for 需要 selector（CSS 或可见文本）或 ref")
            loc = await self._locate(page, kw, timeout=int(kw.get("wait_ms") or 10_000))
            r = self._ok({"appeared": self._target_desc(kw), "visible": await loc.is_visible()})
            wait_ms = 0                                  # wait_ms 已用作等待超时
        elif action == "extract_text":
            r = await self._extract_text(page, kw)
        elif action == "get_links":
            r = await self._get_links(page)
        elif action == "screenshot":
            r = await self._screenshot(page, bool(kw.get("full_page")))
        elif action == "evaluate":
            script = str(kw.get("script") or "")
            if not script:
                return self._fail("evaluate 需要 script")
            r = self._ok({"result": await page.evaluate(script)})
        else:
            return self._fail(f"未知动作：{action}")
        if wait_ms and r.success:
            await page.wait_for_timeout(min(wait_ms, 30_000))
        return r

    @staticmethod
    async def _settle(page: Any) -> None:
        """点击 / 回车可能触发跳转：等 DOM 就绪，但最多等一小会，不死等。"""
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:
            pass

    # ── 结果与错误 ─────────────────────────────────────────

    def _ok(self, output: dict) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, output=output)

    def _fail(self, error: str) -> ToolResult:
        return ToolResult(tool_name=self.name, success=False, error=error)

    @staticmethod
    def _target_desc(kw: dict) -> str:
        return f"ref={kw['ref']}" if kw.get("ref") not in (None, "") else str(kw.get("selector") or "")

    @staticmethod
    def _explain(action: str, kw: dict, exc: BaseException | None) -> str:
        """把 Playwright 的英文长报错换成"下一步该怎么做"。"""
        raw = str(exc or "未知错误").strip() or type(exc).__name__
        first = raw.splitlines()[0][:300]
        if "Timeout" in raw and action in ("click", "type", "select", "hover", "wait_for"):
            return (f"{action} 超时：元素存在但不可操作（被遮挡 / 未启用 / 不可见）。"
                    f"先 action=snapshot 确认元素状态，或先关闭弹窗、滚动到可见区域再试。原始报错：{first}")
        if "Timeout" in raw and action in ("navigate", "reload"):
            return (f"页面加载超时（{NAV_TIMEOUT_MS // 1000}s）：站点慢或被拦截。"
                    f"只读需求可改用 web_fetch，或稍后重试。原始报错：{first}")
        if "ERR_NAME_NOT_RESOLVED" in raw:
            return f"域名无法解析，请检查网址是否拼写正确：{kw.get('url', '')}"
        if "ERR_CONNECTION" in raw or "ERR_INTERNET" in raw or "ERR_PROXY" in raw:
            return f"网络连接失败（{first}）。检查网络或代理；只读需求可改用 web_fetch。"
        if "ERR_CERT" in raw:
            return f"站点证书无效（{first}），出于安全考虑不会忽略证书错误继续访问。"
        return f"{action} 失败：{first}"

    # ── 定位：ref → 选择器 → 按钮 / 链接名、placeholder、label、文本兜底 ──

    async def _locate(self, page: Any, kw: dict, timeout: int = 0) -> Any:
        ref = kw.get("ref")
        if ref not in (None, ""):
            sel = _ref_selector(ref)
            if not sel:
                raise BrowserLocateError(f"ref 必须是 snapshot 返回的数字编号，收到：{ref!r}")
            loc = page.locator(sel)
            try:
                await loc.first.wait_for(state="attached", timeout=timeout or PROBE_TIMEOUT_MS)
            except Exception:
                raise BrowserLocateError(
                    f"ref={ref} 已失效（页面在 snapshot 之后发生了变化）。"
                    f"请重新 action=snapshot 获取最新编号。") from None
            return loc.first
        raw = str(kw.get("selector") or "").strip()
        if not raw:
            raise BrowserLocateError("需要 ref（snapshot 返回的编号）或 selector（CSS / 可见文本）")
        probe = timeout or PROBE_TIMEOUT_MS
        for loc in self._candidates(page, raw):
            try:
                await loc.first.wait_for(state="attached", timeout=probe)
            except Exception as e:
                if _is_dead(e):
                    raise
                probe = PROBE_TIMEOUT_MS           # 只有第一种候选按调用方的超时等
                continue
            # 多个匹配时优先可见的那个（隐藏的移动端菜单常与桌面版同名）
            try:
                n = await loc.count()
                for i in range(min(n, 5)):
                    if await loc.nth(i).is_visible():
                        return loc.nth(i)
            except Exception:
                pass
            return loc.first
        raise BrowserLocateError(
            f"页面上找不到「{raw}」（已按 CSS / 按钮与链接名 / 输入框提示 / 标签 / 可见文本依次查找）。"
            f"请先 action=snapshot 查看可交互元素，再用 ref 编号操作。")

    @staticmethod
    def _candidates(page: Any, raw: str) -> list[Any]:
        """同一个"目标描述"的几种解读，按可信度排序。"""
        out: list[Any] = []
        explicit = raw.startswith(("text=", "xpath=", "//", "css=", "role=", "id="))
        looks_css = explicit or any(c in raw for c in "#.[]>:=()*") or raw.split(" ")[0] in (
            "a", "button", "input", "textarea", "select", "div", "span", "form", "li",
            "img", "label", "h1", "h2", "h3", "p", "table", "tr", "td", "nav", "body")
        if looks_css:
            try:
                out.append(page.locator(raw))
            except Exception:
                pass
        if not raw.startswith(("xpath=", "//", "css=", "role=", "id=")):
            text = raw[5:] if raw.startswith("text=") else raw
            out.append(page.get_by_role("button", name=text))
            out.append(page.get_by_role("link", name=text))
            out.append(page.get_by_placeholder(text))
            out.append(page.get_by_label(text))
            out.append(page.get_by_text(text, exact=True))
            out.append(page.get_by_text(text))
        return out

    @staticmethod
    async def _click(loc: Any) -> None:
        try:
            await loc.scroll_into_view_if_needed(timeout=2_000)
        except Exception:
            pass
        try:
            await loc.click(timeout=ACTION_TIMEOUT_MS)
            return
        except Exception as e:
            first_err = e
            if _is_dead(e):
                raise
        # 被透明遮罩 / 动画挡住：直接在元素上派发 DOM 点击。
        # 不用 force=True —— 它按坐标点，点到的恰恰是那层遮罩，"成功"了却没点中。
        try:
            await loc.evaluate("el => el.click()")
        except Exception:
            raise first_err from None

    @staticmethod
    async def _type(loc: Any, text: str) -> None:
        try:
            await loc.fill(text, timeout=ACTION_TIMEOUT_MS)
        except Exception as e:
            if _is_dead(e):
                raise
            # 富文本编辑器 / 自定义输入组件不支持 fill：点进去全选后逐字敲
            await loc.click(timeout=ACTION_TIMEOUT_MS)
            await loc.press("Control+A")
            await loc.press_sequentially(text, delay=15)

    # ── 各动作 ─────────────────────────────────────────────

    @staticmethod
    async def _title(page: Any) -> str:
        try:
            return await page.title()
        except Exception:
            return ""

    async def _navigate(self, page: Any, url: str) -> ToolResult:
        url = url.strip()
        if not url:
            return self._fail("navigate 需要 url")
        if not url.startswith(("http://", "https://", "file://", "about:", "data:")):
            url = "https://" + url
        resp = await page.goto(url, wait_until="domcontentloaded")
        # 单页应用的内容在 DOMContentLoaded 之后才渲染：再等一小会网络空闲，但不死等
        try:
            await page.wait_for_load_state("networkidle", timeout=3_000)
        except Exception:
            pass
        out: dict[str, Any] = {"url": page.url, "title": await self._title(page)}
        if resp is not None:
            out["status"] = resp.status
            if resp.status >= 400:
                out["warning"] = f"HTTP {resp.status}：打开的可能是错误页"
        out["hint"] = "下一步：action=snapshot 查看可点击 / 可输入的元素（附编号）"
        return self._ok(out)

    async def _snapshot(self, page: Any, limit: int = 150) -> ToolResult:
        items = await page.evaluate(_SNAPSHOT_JS, limit)
        lines = []
        for it in items:
            desc = it["tag"] + (f"[{it['type']}]" if it.get("type") else "")
            extra = f" name={it['name']}" if it.get("name") else ""
            extra += f" → {it['href']}" if it.get("href") else ""
            extra += " (禁用)" if it.get("disabled") else ""
            extra += "" if it.get("in_view") else " (需滚动)"
            lines.append(f"[{it['ref']}] {desc} \"{it['text']}\"{extra}")
        return self._ok({
            "url": page.url, "title": await self._title(page),
            "elements": "\n".join(lines) or "（没有找到可交互元素）",
            "count": len(items),
            "hint": "用 ref=编号 点击 / 输入；页面变化后编号会失效，需重新 snapshot",
        })

    async def _extract_text(self, page: Any, kw: dict) -> ToolResult:
        import re
        if kw.get("ref") not in (None, "") or kw.get("selector"):
            loc = await self._locate(page, kw)
            text = await loc.inner_text()
            target = self._target_desc(kw)
        else:
            text = await page.inner_text("body")
            target = "body"
        text = re.sub(r"\n\s*\n+", "\n\n", text or "").strip()
        return self._ok({"selector": target, "url": page.url, "text": text[:8000],
                         "truncated": len(text) > 8000})

    async def _get_links(self, page: Any) -> ToolResult:
        links = await page.evaluate(
            "Array.from(document.querySelectorAll('a[href]')).slice(0,80)"
            ".map(a => ({text: (a.innerText||'').trim().slice(0,80), href: a.href}))"
        )
        return self._ok({"links": links})

    async def _scroll(self, page: Any, direction: str) -> ToolResult:
        js = {
            "up": "window.scrollBy(0, -window.innerHeight * 0.85)",
            "bottom": "window.scrollTo(0, document.body.scrollHeight)",
            "top": "window.scrollTo(0, 0)",
        }.get(direction, "window.scrollBy(0, window.innerHeight * 0.85)")
        await page.evaluate(js)
        await page.wait_for_timeout(250)                # 懒加载内容给一点时间
        pos = await page.evaluate(
            "({y: Math.round(scrollY), h: document.body.scrollHeight, vh: innerHeight})")
        return self._ok({"scrolled": direction,
                         "at_bottom": pos["y"] + pos["vh"] >= pos["h"] - 4, **pos})

    async def _screenshot(self, page: Any, full_page: bool) -> ToolResult:
        import base64
        import time as _t

        from automind.core.paths import data_dir

        data = await page.screenshot(full_page=full_page)
        path = ""
        try:
            d = data_dir() / "screenshots"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"browser_{int(_t.time() * 1000)}.png"
            p.write_bytes(data)
            path = str(p.resolve())
        except Exception as e:                          # 落盘失败不影响截图本身
            logger.warning("browser_screenshot_save_failed", error=str(e))
        # 画面给界面看（右侧浏览器预览）；模型只拿到路径 —— base64 对它只是噪声
        await self._emit_frame(page, "screenshot", base64.b64encode(data).decode("ascii"), "png")
        return ToolResult(tool_name=self.name, success=True,
                          output={"saved_to": path, "url": page.url,
                                  "title": await self._title(page),
                                  "bytes": len(data), "shown_to_user": True},
                          metadata={"screenshot_path": path})

    # ── 界面实时预览 ───────────────────────────────────────

    async def _publish_preview(self, page: Any, action: str) -> None:
        """动作做完推一帧画面给界面（右侧「浏览器」预览）。没有界面在听时不截图。"""
        try:
            from automind.core.session_ctx import event_sink
            if event_sink() is None:
                return
            import base64
            data = await page.screenshot(type="jpeg", quality=55, timeout=4_000)
            await self._emit_frame(page, action, base64.b64encode(data).decode("ascii"), "jpeg")
        except Exception as e:                          # 预览是旁路，绝不影响动作结果
            logger.debug("browser_preview_failed", error=str(e)[:200])

    async def _emit_frame(self, page: Any, action: str, b64: str, fmt: str) -> None:
        if len(b64) > 1_500_000:                        # 超大整页截图不推（WebSocket 帧太大）
            return
        try:
            from automind.core.session_ctx import emit_event
            await emit_event({
                "type": "browser_preview", "tool": self.name, "action": action,
                "url": page.url, "title": await self._title(page),
                "format": fmt, "screenshot_base64": b64,
            })
        except Exception:
            pass

    # ── 关闭 ───────────────────────────────────────────────

    async def _close_session(self) -> None:
        s = self._sessions.pop(self._sid(), None)
        if s is not None:
            try:
                await s.context.close()
            except Exception:
                pass

    async def close(self) -> None:
        """手动关闭浏览器。"""
        await self._cleanup()

    async def _cleanup(self) -> None:
        for s in list(self._sessions.values()):
            try:
                await s.context.close()
            except Exception:
                pass
        self._sessions.clear()
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
