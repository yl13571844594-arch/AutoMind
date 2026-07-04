"""MCP 注册中心 — Model Context Protocol 服务器发现与工具代理。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from automind.core.types import PermissionTier, ToolResult, ToolSource
from automind.tools.base import AbstractTool


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
        session_manager: Any,  # MCPClientSessionManager
    ) -> None:
        self.name = f"mcp__{server_name}__{mcp_tool_name}"
        self.description = mcp_tool_schema.get("description", f"MCP tool: {mcp_tool_name}")
        self.parameters = mcp_tool_schema.get("inputSchema", mcp_tool_schema.get("parameters", {}))
        self._mcp_tool_name = mcp_tool_name
        self._server_name = server_name
        self._session_manager = session_manager
        self.permission_tier = PermissionTier.SENSITIVE
        self.risk_score = 50

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            result = await self._session_manager.call_tool(
                self._server_name, self._mcp_tool_name, kwargs
            )
            return ToolResult(
                tool_name=self.name,
                success=True,
                output=result,
            )
        except Exception as e:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=str(e),
            )


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

    def add_server(self, config: MCPServerConfig) -> None:
        """添加 MCP 服务器配置。"""
        self._servers[config.name] = config

    def remove_server(self, name: str) -> None:
        """移除 MCP 服务器配置。"""
        self._servers.pop(name, None)
        self._connected.discard(name)
        # 移除相关的代理工具
        prefix = f"mcp__{name}__"
        keys_to_remove = [k for k in self._tools if k.startswith(prefix)]
        for k in keys_to_remove:
            del self._tools[k]

    def list_servers(self) -> list[str]:
        """列出所有已配置的服务器名称。"""
        return list(self._servers.keys())

    async def connect_server(self, name: str) -> bool:
        """连接到指定的 MCP 服务器并发现其工具。

        Returns:
            True 如果连接成功。
        """
        config = self._servers.get(name)
        if not config:
            return False
        if name in self._connected:
            return True

        try:
            from contextlib import AsyncExitStack

            from mcp import ClientSession
            from mcp.client.sse import sse_client
            from mcp.client.stdio import StdioServerParameters, stdio_client

            stack = AsyncExitStack()
            if config.transport == "stdio":
                params = StdioServerParameters(
                    command=config.command,
                    args=config.args,
                    env=config.env or None,
                )
                read_stream, write_stream = await stack.enter_async_context(
                    stdio_client(params))
            elif config.transport == "sse":
                read_stream, write_stream = await stack.enter_async_context(
                    sse_client(config.url))
            else:
                await stack.aclose()
                return False

            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream))
            await session.initialize()

            # 缓存会话，供后续 call_tool 复用
            self._sessions[name] = session
            self._exit_stacks[name] = stack

            tools_result = await session.list_tools()
            for tool in tools_result.tools:
                proxy = MCPProxyTool(
                    mcp_tool_name=tool.name,
                    mcp_tool_schema={
                        "description": tool.description or "",
                        "inputSchema": tool.inputSchema or {},
                    },
                    server_name=name,
                    session_manager=self,
                )
                self._tools[proxy.name] = proxy

            self._connected.add(name)
            return True

        except ImportError:
            return False
        except Exception:
            await self._safe_close(name)
            return False

    async def _safe_close(self, name: str) -> None:
        """安全关闭并清理某服务器的会话。"""
        stack = self._exit_stacks.pop(name, None)
        self._sessions.pop(name, None)
        self._connected.discard(name)
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                pass

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
        """调用 MCP 服务器上的工具（复用持久会话）。"""
        session = self._sessions.get(server_name)
        if session is None:
            # 尝试按需重连一次
            if not await self.connect_server(server_name):
                raise RuntimeError(f"MCP server '{server_name}' is not connected")
            session = self._sessions.get(server_name)
            if session is None:
                raise RuntimeError(f"MCP server '{server_name}' session unavailable")
        return await session.call_tool(tool_name, arguments)

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
