"""会话执行上下文 —— 把「当前是哪个会话在跑」送到工具边界。

为什么需要它：工具注册表是**会话克隆之间共享**的（`clone_for_session` 只独享
执行态，共享工具/技能/记忆），所以「把 session_id 挂到工具实例上」这条路走不通
—— 后注册的会话会把先注册的覆盖掉，两个会话同时跑就又串了。

`contextvars.ContextVar` 天然是「每个任务一份」：`asyncio.create_task` 会复制当前
上下文，因此并发任务各自看到的会话身份互不干扰。写入方只有一个入口
（`_run_impl` 里包一层 :func:`bind_session`），读取方在工具内部，
不依赖任何全局可变状态。
"""

from __future__ import annotations

import itertools
from contextvars import ContextVar, Token
from typing import Any

#: 当前会话标识（None = 未归属，例如 CLI 单用户或单元测试直接调用工具）
_current_session: ContextVar[str | None] = ContextVar("automind_session_id", default=None)

#: 当前运行标识（一个会话可以有多次任务，轨迹按 run 分开落盘）
_current_run: ContextVar[str | None] = ContextVar("automind_run_id", default=None)

#: 会话私有工作目录（开启目录级隔离时非空；工具据此改写写入根）
_current_workspace: ContextVar[str | None] = ContextVar("automind_workspace", default=None)

#: 当前任务的项目根（v1.11.13）。工具/插件需要把**相对路径**落到项目里，
#: 但服务进程的 cwd 可能不是项目根（Web 端可随时切换工作区）。此前若干
#: 工具用相对 cwd 解析，导致"写到了 AutoMind 自己的安装目录/服务目录"，
#: 与 IM 机器人/隔离工作区混在一起。这里把任务真正的工作区根下发到边界。
_current_project_root: ContextVar[str | None] = ContextVar(
    "automind_project_root", default=None)

#: 当前任务的事件推送回调（v1.11.1）。工具想把"过程画面"推给界面
#: （例如浏览器每步操作后的截图）时经它走，而不必把大块 base64 塞进
#: 工具输出 —— 工具输出会进模型上下文，截图在那里只是几千 token 的噪声。
_current_event_sink: ContextVar[Any] = ContextVar("automind_event_sink", default=None)

_seq = itertools.count(1)


def event_sink() -> Any:
    """当前任务的事件回调（``async def sink(event: dict)``）；未绑定返回 None。"""
    return _current_event_sink.get()


def bind_event_sink(sink: Any) -> Token:
    """绑定事件回调，返回 token（调用方用 :func:`reset_event_sink` 恢复）。"""
    return _current_event_sink.set(sink)


def reset_event_sink(token: Token) -> None:
    try:
        _current_event_sink.reset(token)
    except (ValueError, LookupError):
        pass


async def emit_event(event: dict) -> bool:
    """向当前任务的界面推一条事件；没有绑定回调或推送失败都静默返回 False。"""
    sink = _current_event_sink.get()
    if sink is None:
        return False
    try:
        await sink(event)
        return True
    except Exception:
        return False


def new_run_id(prefix: str = "run") -> str:
    """生成一个进程内唯一的运行标识（轨迹文件名用）。"""
    import time

    return f"{prefix}{next(_seq)}-{int(time.time() * 1000)}"


def session_id() -> str | None:
    """当前会话标识；未设置返回 None。"""
    return _current_session.get()


def run_id() -> str | None:
    return _current_run.get()


def workspace() -> str | None:
    return _current_workspace.get()


def project_root() -> str | None:
    """当前任务的项目根；未绑定返回 None（工具回退到自身 project_root/cwd）。"""
    return _current_project_root.get()


# ── 当前 Agent（v1.11.11）──────────────────────────────────────
# 为什么需要：子智能体/目标这类工具要"拿到当前正在跑的那个 Agent"，而工具
# 注册表是会话克隆之间共享的，把 Agent 挂在工具实例上会被并发会话互相覆盖。
# 与 session 一样用 ContextVar：每个任务一份，工具内部读取。
_current_agent: ContextVar[Any] = ContextVar("automind_agent", default=None)

#: 当前执行体的委派深度（0 = 用户直接对话的顶层 Agent；≥1 = 子智能体）。
#: 子智能体禁止继续委派、禁止向用户提问 —— 这些工具据此 fail-closed。
_current_subagent_depth: ContextVar[int] = ContextVar("automind_subagent_depth", default=0)


def agent() -> Any:
    """当前正在执行的 Agent 实例；未绑定返回 None。"""
    return _current_agent.get()


def subagent_depth() -> int:
    """当前执行体的委派深度（0 = 顶层）。"""
    try:
        return int(_current_subagent_depth.get() or 0)
    except (TypeError, ValueError):           # pragma: no cover - 防御性
        return 0


class _AgentBinding:
    """把当前 Agent 与委派深度绑定到上下文，退出时恢复（与 _Binding 同语义）。"""

    __slots__ = ("_agent", "_depth", "_tokens")

    def __init__(self, agent: Any, depth: int | None) -> None:
        self._agent = agent
        self._depth = depth
        self._tokens: list[tuple[ContextVar[Any], Token[Any]]] = []

    def __enter__(self) -> _AgentBinding:
        self._tokens.append((_current_agent, _current_agent.set(self._agent)))
        if self._depth is not None:
            self._tokens.append(
                (_current_subagent_depth, _current_subagent_depth.set(int(self._depth))))
        return self

    def __exit__(self, *exc: Any) -> None:
        for var, tok in reversed(self._tokens):
            try:
                var.reset(tok)
            except (ValueError, LookupError):
                pass
        self._tokens.clear()


def bind_agent(agent_obj: Any, depth: int | None = None) -> _AgentBinding:
    """在其作用域内绑定"当前 Agent"（可选同时设定委派深度）。

    用法::

        with bind_agent(self):
            await tool.execute(...)      # 工具内部 agent() 得到 self
    """
    return _AgentBinding(agent_obj, depth)



class _Binding:
    """实际的上下文管理器实现（名字按类命名规范，见 :func:`bind_session`）。

    ``ContextVar`` 的 ``Token`` 自带 ``var`` 属性，但那是实现细节；这里把
    「变量 + token」成对记下来，恢复时逐个 ``reset``，语义明确、也不依赖私有属性。
    """

    __slots__ = ("_sid", "_rid", "_ws", "_sink", "_root", "_tokens")

    def __init__(self, sid: str | None, run_id: str | None,
                 workspace: str | None, sink: Any = None,
                 project_root: str | None = None) -> None:
        self._sid = sid
        self._rid = run_id
        self._ws = workspace
        self._sink = sink
        self._root = project_root
        self._tokens: list[tuple[ContextVar[Any], Token[Any]]] = []

    def __enter__(self) -> _Binding:
        self._tokens.append((_current_session, _current_session.set(self._sid)))
        if self._rid is not None:
            self._tokens.append((_current_run, _current_run.set(self._rid)))
        if self._ws is not None:
            self._tokens.append((_current_workspace, _current_workspace.set(self._ws)))
        if self._root is not None:
            self._tokens.append(
                (_current_project_root, _current_project_root.set(self._root)))
        if self._sink is not None:
            self._tokens.append((_current_event_sink, _current_event_sink.set(self._sink)))
        return self

    def __exit__(self, *exc: Any) -> None:
        # 逆序恢复：嵌套绑定时内层先退，外层随后
        for var, tok in reversed(self._tokens):
            try:
                var.reset(tok)
            except (ValueError, LookupError):
                # 上下文管理器跨任务误用时 reset 会失败；身份残留好过抛异常
                # 打断真正的工具执行，故此处静默。
                pass
        self._tokens.clear()


def bind_session(sid: str | None, run_id: str | None = None,
                 workspace: str | None = None, sink: Any = None,
                 project_root: str | None = None) -> _Binding:
    """在其作用域内把会话身份绑定好，退出时自动恢复。

    用法::

        with bind_session("s1", run_id="run3"):
            await tool.execute(...)      # 工具内部 session_id() 得到 "s1"

    `asyncio` 任务内使用时，作用域结束即恢复原值；嵌套绑定按栈恢复。
    ``sink`` 非空时同时绑定事件回调（见 :func:`emit_event`）；
    ``project_root`` 非空时把「当前项目根」一并下发（工具/插件解析相对路径用）。
    """
    return _Binding(sid, run_id, workspace, sink, project_root)
