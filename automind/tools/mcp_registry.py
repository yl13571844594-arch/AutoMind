"""MCP 注册中心 — Model Context Protocol 服务器发现与工具代理。

v1.11.2 可靠性修复（MCP 工具调用成功率）：

* **SDK 2.x 下一个服务器都连不上**：``mcp`` 2.x 的 ``Tool`` 字段改成了
  ``input_schema``（1.x 是 ``inputSchema``），旧代码读 ``tool.inputSchema`` 抛
  AttributeError，而 ``connect_server`` 把异常一律吞掉返回 False —— 界面上只看到
  "连接失败"，查不出原因。现在两种写法都认，连接失败的原因记在 :meth:`last_error`。
* **工具报错被当成成功**：MCP 的工具错误不抛异常，而是返回 ``isError=True`` 的结果；
  旧代码一律 ``success=True``，把整个结果对象的 repr 交给模型（"meta=None
  content=[TextContent(type='text', ...)] isError=True"）。现在按 ``isError`` 判成败，
  并把内容整理成纯文本 / 结构化数据。
* **没有超时**：服务器卡住时任务永远挂着。现在单次调用默认 60 秒
  （``AUTOMIND_MCP_TIMEOUT_S`` 可调）。
* **服务器进程退出后再也调不通**：会话对象还在、管道已断。现在识别连接类错误后
  自动重连一次并重试。
* **工具清单只取第一页**：``list_tools`` 是分页的，大服务器的后半截工具从未注册。
* 模型给可选参数传 ``null`` 时原样转发，被服务器端的 schema 校验拒掉 —— 现在剥掉。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

from automind.core.logging import get_logger
from automind.core.types import PermissionTier, ToolResult, ToolSource
from automind.tools.base import AbstractTool

logger = get_logger("automind.tools.mcp")

#: 单次工具调用的超时（秒）
def _call_timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("AUTOMIND_MCP_TIMEOUT_S", "60")))
    except ValueError:
        return 60.0


#: 请求**还没发出去**就失败（会话早已断开）：重连后重试是安全的
_DEAD_BEFORE_SEND = (
    "ClosedResourceError", "BrokenResourceError", "BrokenPipe", "not connected",
    "session unavailable", "ConnectError",
)
#: 请求发出后连接才断：工具可能已经执行过一半 —— 只重连、不重试，避免重复副作用
_DIED_DURING_CALL = (
    "Connection closed", "connection closed", "Connection reset", "ConnectionResetError",
    "EndOfStream",
)


def _matches(exc: BaseException, markers: tuple[str, ...]) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return any(m in text for m in markers)


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    """跨 SDK 版本取字段（1.x camelCase / 2.x snake_case / 普通 dict）。"""
    for n in names:
        if isinstance(obj, dict):
            if n in obj:
                return obj[n]
        elif hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    return default


def normalize_result(result: Any) -> tuple[bool, Any]:
    """把 ``CallToolResult`` 整理成 ``(是否成功, 给模型看的内容)``。"""
    if result is None or isinstance(result, (str, int, float, bool)):
        return True, result
    is_error = bool(_field(result, "is_error", "isError", default=False))
    content = _field(result, "content", default=None)
    structured = _field(result, "structured_content", "structuredContent", default=None)
    parts: list[str] = []
    for item in content or []:
        kind = _field(item, "type", default="")
        if kind == "text" or _field(item, "text") is not None:
            parts.append(str(_field(item, "text", default="")))
        elif kind == "image":
            parts.append(f"[图片 {_field(item, 'mime_type', 'mimeType', default='image')}，已省略]")
        elif kind == "audio":
            parts.append("[音频，已省略]")
        elif kind in ("resource", "embedded_resource"):
            res = _field(item, "resource", default=None)
            txt = _field(res, "text", default=None) if res is not None else None
            uri = _field(res, "uri", default="") if res is not None else ""
            parts.append(str(txt) if txt is not None else f"[资源 {uri}]")
        elif kind == "resource_link":
            parts.append(f"[资源链接 {_field(item, 'uri', default='')}]")
        else:
            parts.append(str(item))
    text = "\n".join(p for p in parts if p).strip()
    if is_error:
        return False, text or "MCP 工具返回了错误（没有附带说明）"
    if structured not in (None, {}, []):
        # 很多服务器把同一个返回值既写成文本、又包一层 {"result": …}：别给模型两份
        if (text and isinstance(structured, dict) and list(structured) == ["result"]
                and str(structured["result"]) == text):
            return True, text
        return True, structured if not text else {"text": text, "data": structured}
    if text:
        return True, text
    if content is None and structured is None:
        # 不认识的结果类型（如需要额外输入的结果）：交给模型原样看
        return True, str(result)
    return True, "（工具执行成功，没有返回内容）"


def _strip_nulls(args: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """去掉值为 None、且 schema 不允许 null 的参数（模型常给可选参数传 null）。"""
    props = (schema or {}).get("properties", {}) if isinstance(schema, dict) else {}
    out = {}
    for k, v in args.items():
        if v is None:
            t = (props.get(k) or {}).get("type") if isinstance(props, dict) else None
            if t != "null" and not (isinstance(t, list) and "null" in t):
                continue
        out[k] = v
    return out


@dataclass
class MCPServerConfig:
    """MCP 服务器配置。"""

    name: str
    command: str = ""  # 例如: "python", "node"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""  # 对于 SSE 类型的 MCP 服务器
    transport: str = "stdio"  # stdio | sse
    auto_connect: bool = True


class MCPProxyTool(AbstractTool):
    """MCP 工具代理 — 将外部 MCP 服务器的工具包装为 AutoMind 工具。

    每个 MCP 工具通过此代理注册到 ToolRegistry。
    调用时，代理将参数转发到 MCP 服务器。
    """

    source = ToolSource.MCP

    def __init__(
        self,
        mcp_tool_name: str,
        mcp_tool_schema: dict[str, Any],
        server_name: str,
        session_manager: Any,  # MCPRegistry
    ) -> None:
        self.name = f"mcp__{server_name}__{mcp_tool_name}"
        desc = mcp_tool_schema.get("description") or f"MCP tool: {mcp_tool_name}"
        # 描述里带上来源：模型据此把"用 GitHub 连接器"和这个工具对上
        self.description = f"[MCP · {server_name}] {desc}"
        params = mcp_tool_schema.get("inputSchema") or mcp_tool_schema.get("parameters") or {}
        if not isinstance(params, dict) or not params:
            params = {"type": "object", "properties": {}}
        self.parameters = params
        self._mcp_tool_name = mcp_tool_name
        self._server_name = server_name
        self._session_manager = session_manager
        self.permission_tier = PermissionTier.SENSITIVE
        self.risk_score = 50

    async def execute(self, **kwargs: Any) -> ToolResult:
        args = _strip_nulls(kwargs, self.parameters)
        try:
            result = await self._session_manager.call_tool(
                self._server_name, self._mcp_tool_name, args)
        except TimeoutError:
            return ToolResult(
                tool_name=self.name, success=False, timed_out=True,
                error=f"MCP 服务器「{self._server_name}」{_call_timeout():.0f} 秒内没有响应。"
                      f"可稍后重试、缩小请求范围，或调大 AUTOMIND_MCP_TIMEOUT_S。")
        except Exception as e:
            return ToolResult(tool_name=self.name, success=False,
                              error=f"MCP 调用失败（{self._server_name}）：{type(e).__name__}: {e}")
        ok, content = normalize_result(result)
        if ok:
            return ToolResult(tool_name=self.name, success=True, output=content)
        return ToolResult(tool_name=self.name, success=False, output=None, error=str(content))


class MCPRegistry:
    """MCP 注册中心 — 管理多个 MCP 服务器的连接和工具发现。

    使用示例::

        registry = MCPRegistry()
        registry.add_server(MCPServerConfig(
            name="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/path/to/workspace"],
        ))
        await registry.connect_all()
        tools = await registry.discover_all_tools()
    """

    def __init__(self) -> None:
        self._servers: dict[str, MCPServerConfig] = {}
        self._session_manager: Any = None
        self._connected: set[str] = set()
        self._tools: dict[str, MCPProxyTool] = {}
        # 持久化的会话与其上下文栈（避免每次调用都重连）
        self._sessions: dict[str, Any] = {}
        self._exit_stacks: dict[str, Any] = {}
        #: 每个服务器最近一次连接失败的原因（界面 / 引用回执用）
        self._errors: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def add_server(self, config: MCPServerConfig) -> None:
        """添加 MCP 服务器配置。"""
        self._servers[config.name] = config

    def remove_server(self, name: str) -> None:
        """移除 MCP 服务器配置。"""
        self._servers.pop(name, None)
        self._connected.discard(name)
        self._errors.pop(name, None)
        # 移除相关的代理工具
        prefix = f"mcp__{name}__"
        keys_to_remove = [k for k in self._tools if k.startswith(prefix)]
        for k in keys_to_remove:
            del self._tools[k]

    def list_servers(self) -> list[str]:
        """列出所有已配置的服务器名称。"""
        return list(self._servers.keys())

    def last_error(self, name: str) -> str:
        """该服务器最近一次连接失败的原因（成功连接后清空）。"""
        return self._errors.get(name, "")

    def is_connected(self, name: str) -> bool:
        return name in self._connected

    def _lock(self, name: str) -> asyncio.Lock:
        lk = self._locks.get(name)
        if lk is None:
            lk = self._locks[name] = asyncio.Lock()
        return lk

    async def connect_server(self, name: str) -> bool:
        """连接到指定的 MCP 服务器并发现其工具。

        Returns:
            True 如果连接成功。失败原因见 :meth:`last_error`。
        """
        config = self._servers.get(name)
        if not config:
            self._errors[name] = "未配置该服务器"
            return False
        if name in self._connected:
            return True

        try:
            from contextlib import AsyncExitStack

            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client
        except ImportError:
            self._errors[name] = "未安装 MCP SDK：pip install mcp"
            return False

        stack = AsyncExitStack()
        try:
            if config.transport == "stdio":
                if not config.command:
                    raise ValueError("stdio 服务器需要填写启动命令（command）")
                env = None
                if config.env:
                    # 只传配置里的变量会丢掉 PATH 等，npx / uvx 直接找不到
                    env = {**os.environ, **{k: str(v) for k, v in config.env.items()}}
                params = StdioServerParameters(command=config.command, args=config.args, env=env)
                read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            elif config.transport == "sse":
                from mcp.client.sse import sse_client
                read_stream, write_stream = await stack.enter_async_context(sse_client(config.url))
            else:
                raise ValueError(f"不支持的传输方式：{config.transport}")

            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await asyncio.wait_for(session.initialize(), timeout=_call_timeout())

            tools = await self._list_all_tools(session)
            # 重连时先清掉旧代理，避免服务器删掉的工具还留在表里
            prefix = f"mcp__{name}__"
            for k in [k for k in self._tools if k.startswith(prefix)]:
                del self._tools[k]
            for tool in tools:
                proxy = MCPProxyTool(
                    mcp_tool_name=_field(tool, "name", default=""),
                    mcp_tool_schema={
                        "description": _field(tool, "description", default="") or "",
                        "inputSchema": _field(tool, "input_schema", "inputSchema", default={}) or {},
                    },
                    server_name=name,
                    session_manager=self,
                )
                self._tools[proxy.name] = proxy

            # 缓存会话，供后续 call_tool 复用
            self._sessions[name] = session
            self._exit_stacks[name] = stack
            self._connected.add(name)
            self._errors.pop(name, None)
            logger.info("mcp_server_connected", server=name, tools=len(tools))
            return True
        except BaseException as e:
            if isinstance(e, asyncio.CancelledError):
                await self._close_stack(stack)
                raise
            reason = f"{type(e).__name__}: {e}".strip()
            if isinstance(e, FileNotFoundError) or "No such file" in reason:
                reason = f"找不到启动命令「{config.command}」：请确认已安装并在 PATH 中"
            elif isinstance(e, asyncio.TimeoutError):
                reason = "服务器启动后没有完成初始化握手（超时）"
            self._errors[name] = reason[:400]
            logger.warning("mcp_server_connect_failed", server=name, error=reason[:300])
            await self._close_stack(stack)
            await self._safe_close(name)
            return False

    @staticmethod
    async def _list_all_tools(session: Any) -> list[Any]:
        """分页拉全工具清单（不同 SDK 版本的游标写法不同）。"""
        out: list[Any] = []
        cursor = None
        for _ in range(50):                               # 防御：最多 50 页
            if cursor is None:
                res = await asyncio.wait_for(session.list_tools(), timeout=_call_timeout())
            else:
                try:
                    from mcp.types import PaginatedRequestParams
                    res = await asyncio.wait_for(
                        session.list_tools(params=PaginatedRequestParams(cursor=cursor)),
                        timeout=_call_timeout())
                except (ImportError, TypeError):
                    res = await asyncio.wait_for(session.list_tools(cursor=cursor),
                                                 timeout=_call_timeout())
            out += list(_field(res, "tools", default=[]) or [])
            cursor = _field(res, "next_cursor", "nextCursor", default=None)
            if not cursor:
                break
        return out

    @staticmethod
    async def _close_stack(stack: Any) -> None:
        try:
            await stack.aclose()
        except BaseException:
            pass

    async def _safe_close(self, name: str) -> None:
        """安全关闭并清理某服务器的会话。"""
        stack = self._exit_stacks.pop(name, None)
        self._sessions.pop(name, None)
        self._connected.discard(name)
        if stack is not None:
            await self._close_stack(stack)

    async def connect_all(self) -> dict[str, bool]:
        """连接所有配置了 auto_connect 的服务器。"""
        results = {}
        for name, config in self._servers.items():
            if config.auto_connect:
                results[name] = await self.connect_server(name)
        return results

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> Any:
        """调用 MCP 服务器上的工具（复用持久会话；断线自动重连重试一次；带超时）。"""
        for attempt in range(2):
            session = self._sessions.get(server_name)
            if session is None:
                async with self._lock(server_name):
                    if self._sessions.get(server_name) is None:
                        if not await self.connect_server(server_name):
                            raise RuntimeError(
                                f"MCP 服务器「{server_name}」未连接："
                                f"{self.last_error(server_name) or '连接失败'}")
                session = self._sessions.get(server_name)
                if session is None:
                    raise RuntimeError(f"MCP 服务器「{server_name}」会话不可用")
            try:
                return await asyncio.wait_for(session.call_tool(tool_name, arguments),
                                              timeout=_call_timeout())
            except TimeoutError:
                raise
            except Exception as e:
                if attempt == 0 and _matches(e, _DEAD_BEFORE_SEND):
                    logger.warning("mcp_session_broken_reconnect", server=server_name,
                                   error=str(e)[:200])
                    await self._safe_close(server_name)
                    continue
                if _matches(e, _DIED_DURING_CALL):
                    # 先把会话清掉，下一次调用会自动重连；这一次如实报告
                    await self._safe_close(server_name)
                    raise RuntimeError(
                        f"MCP 服务器「{server_name}」在执行「{tool_name}」时断开了连接"
                        f"（已清理，下次调用会自动重连）。该操作可能没有完成，"
                        f"也可能已经部分生效：请先确认结果再决定是否重试。") from e
                raise
        raise RuntimeError(f"MCP 服务器「{server_name}」重连后仍不可用")

    async def disconnect(self, name: str) -> None:
        """断开并清理某 MCP 服务器的连接。"""
        await self._safe_close(name)

    async def disconnect_all(self) -> None:
        for name in list(self._exit_stacks.keys()):
            await self._safe_close(name)

    def get_tool(self, full_name: str) -> MCPProxyTool | None:
        """通过完整工具名获取 MCP 代理工具。"""
        return self._tools.get(full_name)

    def get_all_tools(self) -> list[MCPProxyTool]:
        """获取所有已发现的 MCP 代理工具。"""
        return list(self._tools.values())

    def register_to(self, tool_registry: Any) -> int:
        """将所有 MCP 工具注册到指定的 ToolRegistry。"""
        count = 0
        for tool in self._tools.values():
            tool_registry.register(tool)
            count += 1
        return count
