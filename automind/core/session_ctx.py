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

_seq = itertools.count(1)


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


class _Binding:
    """实际的上下文管理器实现（名字按类命名规范，见 :func:`bind_session`）。

    ``ContextVar`` 的 ``Token`` 自带 ``var`` 属性，但那是实现细节；这里把
    「变量 + token」成对记下来，恢复时逐个 ``reset``，语义明确、也不依赖私有属性。
    """

    __slots__ = ("_sid", "_rid", "_ws", "_tokens")

    def __init__(self, sid: str | None, run_id: str | None,
                 workspace: str | None) -> None:
        self._sid = sid
        self._rid = run_id
        self._ws = workspace
        self._tokens: list[tuple[ContextVar[str | None], Token[str | None]]] = []

    def __enter__(self) -> _Binding:
        self._tokens.append((_current_session, _current_session.set(self._sid)))
        if self._rid is not None:
            self._tokens.append((_current_run, _current_run.set(self._rid)))
        if self._ws is not None:
            self._tokens.append((_current_workspace, _current_workspace.set(self._ws)))
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
                 workspace: str | None = None) -> _Binding:
    """在其作用域内把会话身份绑定好，退出时自动恢复。

    用法::

        with bind_session("s1", run_id="run3"):
            await tool.execute(...)      # 工具内部 session_id() 得到 "s1"

    `asyncio` 任务内使用时，作用域结束即恢复原值；嵌套绑定按栈恢复。
    """
    return _Binding(sid, run_id, workspace)
