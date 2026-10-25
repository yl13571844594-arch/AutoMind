"""执行器 —— 把"一个评测任务"变成"一份可断言的结果"。

为什么要可注入：评测框架最容易变成"只能联网跑、只能人工看"的黑盒，于是它
永远不会被接进 CI，也永远不会被信任。因此默认执行器（真跑 AutoMindAgent）
只是一个**实现**，:class:`Executor` 协议才是接口 —— 测试里注入假 agent +
假工具即可离线覆盖"全部通过/断言失败/超时/异常"四类路径。

关于隔离：每个任务使用**自己的临时工作目录**，并把 agent 的
``config.project_root`` 指到那里（``tools/file_editor.py`` 的 ``_RootGuard``
正是以它为边界解析相对路径），因此任务之间不会互相污染，评测也不会写到
仓库里。工作目录默认在任务结束后删除（``keep_workspace`` 可留作排障）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from automind.core.logging import get_logger
from automind.eval.assertions import EvalOutcome
from automind.eval.suite import EvalCase

logger = get_logger("automind.eval.executors")

#: 交互模式名 → 需要调用的 agent 方法。
#: multi/loop 走的是另外的入口（run_multi / run_loop），刻意不在 v1 支持：
#: 它们的"成功"语义与单任务断言不同，硬套只会给出误导性的绿灯。
_MODE_METHOD = {"chat": "chat", "work": "run", "coding": "run"}


@dataclass
class LLMTarget:
    """本次评测使用的模型与凭据来源（报告里要写清"用什么跑的"）。"""

    provider: str = ""
    model: str = ""
    api_key_source: str = ""      # 例如 "env:DEEPSEEK_API_KEY" / "config"
    available: bool = False
    reason: str = ""              # 不可用时的原因（原文给出，便于照做）

    def as_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model,
                "api_key_source": self.api_key_source,
                "available": self.available, "reason": self.reason}


@dataclass
class RunContext:
    """一次任务执行的输入。"""

    case: EvalCase
    workspace: Path
    mode: str = "coding"
    timeout_seconds: float = 0.0
    #: 产物遍历忽略的目录（避免把 .git/__pycache__ 之类算进 artifacts）
    ignore_dirs: tuple[str, ...] = (".git", "__pycache__", ".automind", ".pytest_cache",
                                    "node_modules", ".ruff_cache", ".venv")
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Executor(Protocol):
    """执行器协议：实现 ``run`` 即可被 runner 使用。"""

    async def run(self, ctx: RunContext) -> EvalOutcome:  # pragma: no cover - 协议
        ...


# ═══════════════════════════════════════════════════════════════
# 工具调用记录
# ═══════════════════════════════════════════════════════════════


def record_tool_calls(agent: Any, sink: list[dict[str, Any]]) -> None:
    """把 agent 的每次工具调用追加到 ``sink``（不改任何被禁改的文件）。

    做法：在**实例上**包一层 ``tool_registry.dispatch``。全仓库所有工具执行
    都经过它（``planning/react_executor.py``、``planning/plan_executor.py``、
    技能），因此在实例上装钩子即可全量捕获，且对其它会话/其它代码零影响
    （基类 ``ToolRegistry.dispatch`` 一行未动）。
    """
    registry = getattr(agent, "tool_registry", None)
    if registry is None or getattr(registry, "_eval_recording", False):
        return
    original = registry.dispatch

    async def _dispatch(tool_name: str, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            result = await original(tool_name, **kwargs)
        except BaseException as e:               # 连异常也要记，否则"调了但炸了"看不见
            sink.append({"name": tool_name, "arguments": _jsonable(kwargs),
                         "success": False, "error": str(e)[:500],
                         "seconds": round(time.monotonic() - started, 4)})
            raise
        sink.append({"name": tool_name, "arguments": _jsonable(kwargs),
                     "success": bool(getattr(result, "success", False)),
                     "seconds": round(time.monotonic() - started, 4)})
        return result

    registry.dispatch = _dispatch
    registry._eval_recording = True


def _jsonable(value: Any) -> Any:
    """参数里可能有 Path/bytes 等不可 JSON 化的对象，转成可序列化形态。"""
    import json

    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        try:
            return json.loads(json.dumps(value, default=str))
        except Exception:
            return {"_repr": repr(value)[:500]}


def list_artifacts(root: Path, ignore_dirs: tuple[str, ...] = ()) -> list[str]:
    """列出工作区内的文件（相对路径，正斜杠），供 ``file_exists`` 之外的回显。"""
    out: list[str] = []
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in ignore_dirs for part in rel.parts):
            continue
        out.append(rel.as_posix())
        if len(out) >= 500:                      # 防御性上限：别让报告变成文件清单
            break
    return out


# ═══════════════════════════════════════════════════════════════
# 默认执行器：真跑 AutoMindAgent
# ═══════════════════════════════════════════════════════════════


def _llm_config_kwargs(provider: str, model: str) -> dict[str, Any]:
    """构造 ``AgentConfig`` 的 LLM 覆盖项。

    注意嵌套结构：``AgentConfig`` 里 provider/model 位于 ``llm`` 子模型下，
    直接平铺传参会被 pydantic 以 ``extra_forbidden`` 拒掉（评测最不该出现的
    失败形态就是"连 agent 都没建起来"）。
    """
    llm: dict[str, Any] = {}
    if provider:
        llm["provider"] = provider
    if model:
        llm["model"] = model
    return {"llm": llm} if llm else {}


def detect_llm_target(provider: str = "", model: str = "") -> LLMTarget:
    """判断"能不能真跑"：有没有凭据、用哪个模型（**不做任何网络请求**）。

    与"跑起来才发现 401"相比，提前判断的价值在于：CI 上"没配 Key"必须报成
    配置问题（退出码 2），而不是"全部任务失败"（退出码 1）—— 后者会让人去查
    模型退化，方向完全错了。
    """
    from automind.core.config import AgentConfig
    from automind.core.provider_resolver import ENV_KEY_MAP

    target = LLMTarget(provider=provider, model=model)
    try:
        cfg = AgentConfig()
        base = cfg.llm
    except Exception as e:                       # 配置本身坏了也要如实说
        target.reason = f"配置加载失败：{e}"
        return target

    target.provider = provider or base.provider
    target.model = model or base.model
    env_var = ENV_KEY_MAP.get(target.provider, "")

    if target.provider == "ollama":
        # 本地模型没有也不需要 Key —— 要求它有，会把"完全离线跑评测"这条最有
        # 价值的路误判成配置缺失
        target.api_key_source = "local (ollama)"
        target.available = True
        return target

    if base.api_key and not provider:
        target.api_key_source = "config"
        target.available = True
        return target
    if env_var and os.environ.get(env_var, "").strip():
        target.api_key_source = f"env:{env_var}"
        target.available = True
        return target
    if base.api_key and provider:
        target.api_key_source = "config"
        target.available = True
        return target

    target.reason = (
        f"LLM 未配置：provider='{target.provider}' 没有可用凭据"
        + (f"（期望环境变量 {env_var}）" if env_var else "")
        + "。请在配置里填写 API Key 或设置对应环境变量后重试；"
          "dry-run（--dry-run）不需要 Key。")
    return target


#: 评测执行器支持的审批模式（与 ``PermissionEngine.APPROVAL_MODES`` 一致）。
#: 评测是**无人值守**场景：没有任何人工审批通道。``auto`` 模式下 terminal 命令
#: 的 preflight 风险分是 60，``PermissionEngine`` 会返回 ASK_USER，而
#: ``react_executor._gate`` 对"问不到人"是 fail-closed 拒绝 —— 模型拿不到结果
#: 只会换着写法反复重试，实测把一条几秒的命令拖成 237s 超时 + 43.7 万 token。
#: 因此默认全批准（敏感工具自动放行）。
EVAL_APPROVAL_MODES = ("approve_all", "auto", "ask")
DEFAULT_EVAL_APPROVAL_MODE = "approve_all"


def make_agent_config(workspace: Path, provider: str = "", model: str = "",
                      approval_mode: str = DEFAULT_EVAL_APPROVAL_MODE) -> Any:
    """按任务工作区构造 AgentConfig（不读用户仓库里的 automind.yaml）。

    刻意不用 ``AgentConfig.auto_load``：评测要求**可复现** —— 结果不应随
    "当前目录下恰好有一个 automind.yaml"而变。

    ``approval_mode`` 默认 ``approve_all``（理由见 ``EVAL_APPROVAL_MODES``）：
    无人值守评测没有审批通道，需要人工确认的调用会被 fail-closed 拒绝，进而
    触发模型的反复重试。``auto`` / ``ask`` 只建议用于"对比审批模式影响"的实验
     —— 它们在这个环境里跑出来的耗时/token 会被"审批被拒"放大。
    """
    from automind.core.config import AgentConfig

    if approval_mode not in EVAL_APPROVAL_MODES:
        # 不静默回退到 auto：那样会把"全批准"悄悄降级成"无人可问 → 拒绝"，
        # 正是本次要消除的失效形态。宁可让配置错误在开始跑之前就暴露。
        raise ValueError(
            f"未知的 approval_mode={approval_mode!r}；"
            f"可选：{', '.join(EVAL_APPROVAL_MODES)}")

    cfg = AgentConfig(project_root=str(workspace),
                      **_llm_config_kwargs(provider, model))
    # 轨迹落盘默认就会被 AUTOMIND_DATA_DIR 隔离到临时目录（见 runner），
    # 这里不需要也不应该改其它 ExecutionConfig 字段。
    cfg.execution.approval_mode = approval_mode
    return cfg


def align_task_budget(cfg: Any, timeout_seconds: float) -> None:
    """把 agent 自己的墙钟预算（``max_task_seconds``）夹到评测任务上限内。

    为什么执行器还要再设一道：agent 的默认预算是 1800s，而评测套件给单任务的
    上限通常是 180s。只靠外层 ``asyncio.wait_for`` 硬超时的话，到点会**直接取消**
    agent —— 拿不到它已做出的部分交付，报告里只剩一句"超时"。把内部预算对齐到
    同一上限后，agent 会先走到自己的截止线、主动收尾并返回部分结果；外层硬超时
    只作为"连收尾都卡住"时的最后保险。

    取 ``min`` 而不是直接覆盖：调用方（套件/用户配置）把预算设得**更小**时那是
    更严格的约束，不能因为评测上限更大就放宽；``current <= 0`` 表示"不限"，
    这时任务上限就是唯一约束。
    """
    if timeout_seconds <= 0:
        return
    execution = getattr(cfg, "execution", None)
    if execution is None:
        return
    current = float(getattr(execution, "max_task_seconds", 0) or 0)
    execution.max_task_seconds = (min(current, timeout_seconds) if current > 0
                                  else timeout_seconds)


class AutoMindExecutor:
    """默认执行器：每个任务建一个独立的 AutoMindAgent + 独立工作目录。"""

    def __init__(self, provider: str = "", model: str = "",
                 keep_workspace: bool = False, agent_factory: Any = None,
                 approval_mode: str = DEFAULT_EVAL_APPROVAL_MODE) -> None:
        self.provider = provider
        self.model = model
        self.keep_workspace = keep_workspace
        #: 可注入的 agent 工厂（测试用）：签名 ``(config) -> agent``
        self.agent_factory = agent_factory
        #: 无人值守审批模式（默认 approve_all，理由见 make_agent_config）。
        #: runner 通过 ``--approval-mode`` 覆盖它，并写进报告 notes。
        self.approval_mode = approval_mode

    def _build_agent(self, config: Any) -> Any:
        if self.agent_factory is not None:
            return self.agent_factory(config)
        from automind.agent import AutoMindAgent

        return AutoMindAgent(config)

    async def run(self, ctx: RunContext) -> EvalOutcome:
        from automind.core.types import InteractionMode

        calls: list[dict[str, Any]] = []
        started = time.monotonic()
        agent = None
        try:
            config = make_agent_config(ctx.workspace, self.provider, self.model,
                                       self.approval_mode)
            align_task_budget(config, ctx.timeout_seconds)
            agent = self._build_agent(config)
            if getattr(agent, "llm", None) is None:
                raise RuntimeError(
                    "LLM 后端未初始化（agent.llm is None）："
                    + str(getattr(agent, "_llm_init_error", "") or "通常是缺 API Key"))
            record_tool_calls(agent, calls)
            with contextlib.suppress(Exception):
                agent._interaction = InteractionMode(ctx.mode)
            if hasattr(agent, "session_id"):
                agent.session_id = f"eval-{ctx.case.id}"

            method_name = _MODE_METHOD.get(ctx.mode, "run")
            invocation = self._invoke(agent, method_name, ctx.case.prompt)
            if ctx.timeout_seconds > 0:
                # 硬超时（无人值守评测修复）：任务内部可能卡在审批等待 / LLM
                # 重试 / 工具循环里，单靠 runner 的**事后**判定救不了"挂死"。
                # wait_for 到点会取消任务（触发 agent 的中断落盘），随后
                # finally 里的 _close 仍会执行，不会漏掉资源释放。
                result = await asyncio.wait_for(invocation,
                                                timeout=ctx.timeout_seconds)
            else:
                result = await invocation
            seconds = time.monotonic() - started
            usage = _usage_of(agent, result)
            return EvalOutcome(
                output=_output_of(result),
                success=bool(getattr(result, "success", True)),
                tool_calls=calls,
                seconds=seconds,
                artifacts=list_artifacts(ctx.workspace, ctx.ignore_dirs),
                **usage,
            )
        except TimeoutError:
            # asyncio.wait_for 的硬超时（以及 agent 自身入口抛出的 TimeoutError）。
            # 注意：agent 到点主动收尾时不会走到这里 —— 那是"返回部分结果"路径，
            # 由报告里的 max_seconds 断言判定。
            seconds = time.monotonic() - started
            limit = f"（上限 {ctx.timeout_seconds:g}s）" if ctx.timeout_seconds > 0 else ""
            return EvalOutcome(
                output="", success=False, tool_calls=calls, seconds=seconds,
                error=f"任务超时{limit}", timed_out=True,
                artifacts=list_artifacts(ctx.workspace, ctx.ignore_dirs),
                **_usage_of(agent, None))
        except Exception as e:
            # 执行器**不把异常抛给 runner**：runner 会把无法归因的异常记成
            # error 状态（"框架/环境问题"），而这里能给出更准确的原因
            # （agent 建不起来、入口不对、模型不可用…），让报告直接指向病灶。
            # 前缀"执行器异常"是给 runner 的状态判定用的：有它 = 没跑起来。
            seconds = time.monotonic() - started
            return EvalOutcome(
                output="", success=False, tool_calls=calls, seconds=seconds,
                error=f"执行器异常 {type(e).__name__}: {e}",
                artifacts=list_artifacts(ctx.workspace, ctx.ignore_dirs),
                # v1.7.5：失败也要记用量 —— 烧掉的 token 不会因为任务失败就退回来
                **_usage_of(agent, None))
        finally:
            await self._close(agent)

    async def _invoke(self, agent: Any, method_name: str, prompt: str) -> Any:
        method = getattr(agent, method_name, None)
        if method is None:
            raise RuntimeError(
                f"agent 不支持 mode 要求的入口 '{method_name}'；"
                f"chat→chat()，work/coding→run()")
        return await method(prompt)

    async def _close(self, agent: Any) -> None:
        close = getattr(agent, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                await close()


def _output_of(result: Any) -> str:
    """从 agent 返回值里取"用户可见输出"（chat 返回 str，run 返回 AgentResult）。"""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    text = getattr(result, "output", None)
    if text is not None:
        return str(text)
    return str(result)


def _usage_of(agent: Any, result: Any) -> dict[str, int]:
    """取 token 用量 —— 优先 agent 自己的累计器（含全部 LLM 调用，不只最后一次）。

    ``result`` 可以为 None（任务抛异常/超时时没有结果对象）：这时仍然应当
    从 agent 身上把已经烧掉的用量取出来 —— v1.7.5 之前异常分支直接返回空用量，
    于是**失败任务的 token 一律记成 0**，统计页少算、成本无从归因
    （实测 smoke 套件真实消耗约 35 万 token，报告里只显示 10.5 万）。
    """
    prompt = completion = 0
    total = getattr(agent, "_usage_total", None)
    if isinstance(total, dict):
        prompt = int(total.get("prompt_tokens", 0) or 0)
        completion = int(total.get("completion_tokens", 0) or 0)
    if not prompt and not completion and result is not None:
        usage = getattr(result, "token_usage", None)
        if usage is not None:
            prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion = int(getattr(usage, "completion_tokens", 0) or 0)
    if not prompt and not completion:
        llm = getattr(agent, "llm", None)
        usage = getattr(llm, "usage", None)
        if usage is not None:
            prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion = int(getattr(usage, "completion_tokens", 0) or 0)
    if not prompt and not completion:
        # 最后一道：ResourceManager 的累计器。它记的是**全部**调用，
        # 即使 _usage_total 因为回调异常没更新，这里仍能拿到。
        rm = getattr(agent, "resources", None)
        used = getattr(getattr(rm, "tokens", None), "tokens_used", None)
        if used is not None:
            prompt = int(getattr(used, "prompt_tokens", 0) or 0)
            completion = int(getattr(used, "completion_tokens", 0) or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}
