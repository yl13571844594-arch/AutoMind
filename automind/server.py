"""AutoMind Web Server — FastAPI REST + WebSocket API。

启动方式:
    python -m automind.server              # 默认 http://localhost:8765
    python -m automind.server --port 8080  # 自定义端口
    python -m automind.server --host 0.0.0.0 --port 8765  # 允许外部访问
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:
    print("FastAPI not installed. Install with: pip install fastapi uvicorn")
    sys.exit(1)

from automind import __version__
from automind.core.logging import get_logger
from automind.server_web import apply_security_headers as _apply_security_headers
from automind.server_web import versioned_html as _versioned_html

logger = get_logger("automind.server")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="AutoMind Agent", version=__version__, docs_url="/docs")

# §14.10 前端工程化：拆分后的 css/js 模块经 /static 伺服（无需鉴权，同 CDN 语义）
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 商用安全：可选鉴权 + CORS 收紧（默认本地放开，配置后生效）
_AUTH_TOKEN = os.environ.get("AUTOMIND_AUTH_TOKEN", "")
_cors_origins = os.environ.get("AUTOMIND_CORS_ORIGINS", "*").split(",")

# 安全深度加固（§14.11）— 均默认关闭，配置环境变量后生效，不改变既有行为
#   AUTOMIND_RATE_LIMIT      : /api/run 每分钟每客户端允许次数（0=关闭）
#   AUTOMIND_REDACT_SECRETS  : 设为 1/true 时对任务输出做密钥脱敏
#   AUTOMIND_ALLOWED_ORIGINS : 逗号分隔的 WebSocket 允许来源（空=不校验）
from automind.core.ratelimit import SlidingWindowLimiter  # noqa: E402

_rate_limiter = SlidingWindowLimiter(
    max_requests=int(os.environ.get("AUTOMIND_RATE_LIMIT", "0") or "0"),
    window_seconds=60.0,
)
_REDACT_SECRETS = os.environ.get("AUTOMIND_REDACT_SECRETS", "").lower() in ("1", "true", "yes")
_WS_ALLOWED_ORIGINS = {
    o.strip() for o in os.environ.get("AUTOMIND_ALLOWED_ORIGINS", "").split(",") if o.strip()
}


def _maybe_redact(text: Any) -> Any:
    """按开关对输出做密钥脱敏；关闭或非字符串时原样返回。"""
    if not _REDACT_SECRETS or not isinstance(text, str):
        return text
    from automind.core.redact import redact_secrets
    return redact_secrets(text)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins] or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _auth_token() -> str:
    """鉴权令牌：优先环境变量，其次配置文件。空字符串表示不鉴权（本地模式）。"""
    return _AUTH_TOKEN or _read_config().get("auth_token", "")


@app.middleware("http")
async def _auth_middleware(request, call_next):
    token = _auth_token()
    path = request.url.path
    # 仅保护 /api/*；放开首页、文档、健康检查
    if token and path.startswith("/api/") and not path.startswith("/api/health"):
        sent = request.headers.get("authorization", "")
        provided = sent[7:] if sent.lower().startswith("bearer ") else \
            request.query_params.get("token", "")
        if provided != token:
            logger.warning("auth_denied", path=path,
                           client=request.client.host if request.client else "?")
            return JSONResponse({"error": "未授权：请提供有效的访问令牌"}, status_code=401)

    # 速率限制（§14.11-4）：仅作用于任务执行入口 /api/run，按客户端 IP 计数
    if _rate_limiter.enabled and request.method == "POST" and path == "/api/run":
        client_ip = request.client.host if request.client else "unknown"
        if not _rate_limiter.allow(client_ip):
            retry = _rate_limiter.retry_after(client_ip)
            return JSONResponse(
                {"error": f"请求过于频繁，请在 {retry}s 后重试。"},
                status_code=429,
                headers={"Retry-After": str(int(retry) + 1)},
            )
    response = await call_next(request)
    _apply_security_headers(response, path)  # 安全头 + 首页 CSP（server_web.py）
    return response


# ── 全局状态 ──────────────────────────────────────────────

_agent: Any = None
_active_sessions: dict[str, dict] = {}
_ws_clients: dict[str, list[WebSocket]] = {}
_task_history: list[dict] = []
_token_totals = {"prompt": 0, "completion": 0, "total": 0, "tasks": 0}
_running_tasks = {"count": 0}  # 并发任务计数（资源保护）
_MAX_CONCURRENT = int(os.environ.get("AUTOMIND_MAX_CONCURRENT", "8"))
_START_TIME = time.time()


def _accumulate_tokens(record: dict) -> None:
    _token_totals["prompt"] += record.get("prompt_tokens", 0)
    _token_totals["completion"] += record.get("completion_tokens", 0)
    _token_totals["total"] += record.get("tokens", 0)
    _token_totals["tasks"] += 1


_HISTORY_FILE = Path(".automind") / "task_history.json"
_HISTORY_CAP = 200


def _load_task_history() -> None:
    """启动时恢复任务历史（关浏览器/重启服务后仍可回溯之前的产出）。"""
    try:
        if _HISTORY_FILE.exists():
            data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _task_history.extend(data[-_HISTORY_CAP:])
    except Exception as e:
        logger.warning("history_load_failed", error=str(e))


def _save_task_history() -> None:
    """持久化任务历史（尽力而为，失败不影响主流程）。"""
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HISTORY_FILE.write_text(
            json.dumps(_task_history[-_HISTORY_CAP:], ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


def _push_history(record: dict) -> dict:
    """统一记录入口 — 按开关脱敏 output 后写入历史（§14.11-3）。"""
    if "output" in record:
        record["output"] = _maybe_redact(record["output"])
    record.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
    _task_history.append(record)
    del _task_history[:-_HISTORY_CAP]
    _save_task_history()
    return record


_load_task_history()

# 环境变量映射
_ENV_KEY_MAP = {
    "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY", "gemini": "GOOGLE_API_KEY",
    "grok": "GROK_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
    "kimi": "MOONSHOT_API_KEY", "moonshot": "MOONSHOT_API_KEY",
    "bailian": "DASHSCOPE_API_KEY", "dashscope": "DASHSCOPE_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "zhipu": "ZHIPU_API_KEY", "glm": "ZHIPU_API_KEY",
    "doubao": "DOUBAO_API_KEY", "custom": "CUSTOM_API_KEY",
}

# 交互模式 → 底层执行引擎
_INTERACTION_TO_EXECUTION = {
    "chat": "react",            # 对话模式不走引擎，仅占位
    "work": "plan_and_execute",  # 工作模式：分层规划
    "coding": "react",           # 编程模式：ReAct 循环
    "multi": "plan_and_execute",  # 多智能体协同（单独走 orchestrator）
    "loop": "react",              # 循环工程：自主迭代闭环
}


# ═══════════════════════════════════════════════════════════
# 配置 / 对话 / 会话 持久化 —— 已抽取到 server_store.Store（见 server_store.py）
# server.py 保留同名委托别名，故 40+ 路由调用点无需改动。
# ═══════════════════════════════════════════════════════════

from automind.server_store import Store as _Store  # noqa: E402

_store = _Store(env_key_map=_ENV_KEY_MAP)

# 委托别名（保持既有函数名 → 路由与测试调用点零改动）
_read_config = _store.read_config
_write_config = _store.write_config
_load_api_keys = _store.load_api_keys
_save_api_keys = _store.save_api_keys
_load_providers = _store.load_providers
_valid_api_base = _store.valid_api_base
_save_provider_cfg = _store.save_provider_cfg
_load_active = _store.load_active
_save_active = _store.save_active
_load_mode_models = _store.load_mode_models
_mode_model = _store.mode_model
_env_api_key = _store.env_api_key
_custom_models = _store.custom_models
_add_custom_model = _store.add_custom_model
_remove_custom_model = _store.remove_custom_model
_load_chat_history = _store.load_chat_history
_save_chat_history = _store.save_chat_history
_get_session_history = _store.get_session_history
_save_session_history = _store.save_session_history


def _apply_mode_model(agent, interaction: str):
    """按交互模式切换到对应模型（不改写全局默认配置）。返回最新 agent。"""
    global _agent
    mm = _mode_model(interaction)
    if not mm:
        # 该模式未单独配置 → 回退到全局默认（provider+model）
        active = _load_active()
        mm = {"provider": active.get("provider") or agent.config.llm.provider,
              "model": active.get("model") or agent.config.llm.model}
    cur = (agent.config.llm.provider, agent.config.llm.model)
    if (mm["provider"], mm["model"]) != cur:
        _rebuild_agent(provider=mm["provider"], model=mm["model"])
        return _agent
    return agent


# ═══════════════════════════════════════════════════════════
# Agent 生命周期
# ═══════════════════════════════════════════════════════════


def _rebuild_agent(provider: str | None = None, model: str | None = None):
    """完全重建 agent 实例（切换模型 / API Key / 代理时调用）。

    可传入 provider/model 覆盖（用于按交互模式切换模型），不会改写全局默认配置。
    """
    global _agent
    from automind.agent import AutoMindAgent
    from automind.core.config import AgentConfig
    from automind.core.types import ExecutionMode, InteractionMode
    from automind.tools.permissions import PermissionPolicy

    active = _load_active()
    providers = _load_providers()

    project_root = active.get("project") or "."
    if not Path(project_root).is_dir():
        project_root = "."
    config = AgentConfig.auto_load(project_root)
    config.project_root = str(Path(project_root).resolve())
    config.permissions = PermissionPolicy(auto_approve_safe=True)

    # 1) 决定当前提供商（显式覆盖 > 现有 agent > active > 默认）
    provider = (
        provider
        or (_agent.config.llm.provider if _agent is not None else None)
        or active.get("provider")
        or config.llm.provider
    )
    config.llm.provider = provider

    # 2) 应用提供商自定义的 model / api_base（中转代理）
    pcfg = providers.get(provider, {})
    same_provider = _agent is not None and _agent.config.llm.provider == provider
    if model:
        config.llm.model = model
    elif same_provider:
        config.llm.model = _agent.config.llm.model
    # 仅在同提供商时继承旧 api_base，避免跨提供商把中转地址带过去
    if same_provider:
        config.llm.api_base = _agent.config.llm.api_base
    else:
        config.llm.api_base = ""
    if not model:
        if pcfg.get("model"):
            config.llm.model = pcfg["model"]
        elif active.get("model"):
            config.llm.model = active["model"]
    # 仅当 api_base 是有效 URL 时才使用（避免误用非 URL 占位符）
    raw_base = pcfg.get("api_base", "")
    if raw_base and (raw_base.startswith("http://") or raw_base.startswith("https://")):
        config.llm.api_base = raw_base
    elif not config.llm.api_base:
        config.llm.api_base = ""

    # 3) 应用 API Key（持久化 > 环境变量）
    api_keys = _load_api_keys()
    if api_keys.get(provider):
        config.llm.api_key = api_keys[provider]
    elif not config.llm.api_key:
        config.llm.api_key = _env_api_key(provider)

    # 4) 底层执行引擎
    interaction = active.get("interaction", "chat")
    exec_mode = _INTERACTION_TO_EXECUTION.get(interaction, "plan_and_execute")
    config.execution.mode = exec_mode

    # 5) 审批模式
    config.execution.approval_mode = active.get("approval_mode", "auto")

    # 6) 自主闭环开关（默认全开，用户可在设置中关闭）
    ap = _read_config().get("autopilot", {})
    for flag in ("auto_review", "auto_verify", "auto_test",
                 "parallel_execution", "subtask_cache"):
        if flag in ap:
            setattr(config.execution, flag, bool(ap[flag]))

    _agent = AutoMindAgent(config)
    _agent._mode = ExecutionMode(exec_mode)
    try:
        _agent._interaction = InteractionMode(interaction)
    except ValueError:
        _agent._interaction = InteractionMode.CHAT

    # 恢复对话历史
    _agent._chat_history = _load_chat_history()
    # 恢复已保存的自定义技能目录（同时支持 .py 与 SKILL.md）
    for d in _read_config().get("skill_dirs", []):
        try:
            if Path(d).is_dir():
                _agent.skill_registry.discover_any(d)
        except Exception:
            pass
    # 应用已禁用的工具
    for tname in _read_config().get("disabled_tools", []):
        try:
            _agent.tool_registry.unregister(tname)
        except Exception:
            pass
    # 重连已保存的 MCP 服务器
    _reconnect_mcp_servers(_agent)
    logger.info("agent_rebuilt", provider=config.llm.provider, model=config.llm.model,
                interaction=interaction, exec_mode=exec_mode,
                llm_ready=_agent.llm is not None, project=config.project_root)
    return _agent


# 内置（不可删除）技能名单
_BUILTIN_SKILLS = {
    "project_init", "code_generator", "test_runner",
    "log_analyzer", "doc_generator", "dep_audit",
}
# 自定义技能目录
_SKILLS_DIR = Path(".automind") / "skills"


def _reconnect_mcp_servers(agent) -> None:
    """从配置恢复 MCP 服务器配置（连接按需在异步上下文中进行，避免阻塞重建）。"""
    from automind.tools.mcp_registry import MCPServerConfig

    for s in _read_config().get("mcp_servers", []):
        try:
            agent.mcp_registry.add_server(MCPServerConfig(
                name=s["name"], command=s.get("command", ""),
                args=s.get("args", []), url=s.get("url", ""),
                transport=s.get("transport", "stdio"),
            ))
        except Exception:
            pass


@asynccontextmanager
async def _lifespan(_app):
    """应用生命周期（替代已弃用的 on_event startup/shutdown）。

    启动：连接 MCP 服务器、恢复定时任务并启动调度循环。
    退出：释放 Agent 资源（MCP 连接 / ChromaDB / LLM 连接池）。
    """
    logger.info("server_startup", version=__version__,
                auth=bool(_auth_token()), max_concurrent=_MAX_CONCURRENT)
    try:
        agent = get_agent()
        if agent.mcp_registry.list_servers():
            await agent.mcp_registry.connect_all()
            agent.mcp_registry.register_to(agent.tool_registry)
            logger.info("mcp_connected", servers=agent.mcp_registry.list_servers())
    except Exception as e:
        logger.warning("startup_mcp_failed", error=str(e))
    # 定时任务调度器（专业版特性 scheduler；社区版无此能力）
    _sched = _edition.get_feature("scheduler")
    if _sched is not None:
        try:
            _sched.start()
        except Exception as e:
            logger.warning("startup_scheduler_failed", error=str(e))
    yield
    logger.info("server_shutdown")
    if _sched is not None:
        try:
            _sched.stop()
        except Exception as e:
            logger.warning("shutdown_scheduler_failed", error=str(e))
    _pool = _edition.get_feature("session_pool")
    if _pool is not None:
        try:
            await _pool.aclose_all()  # 释放全部会话 Agent（§7.4）
        except Exception as e:
            logger.warning("shutdown_pool_failed", error=str(e))
    if _agent is not None:
        try:
            await _agent.close()
        except Exception as e:
            logger.warning("shutdown_close_failed", error=str(e))


# 在所有依赖的辅助函数定义后，将 lifespan 挂到已实例化的 app 上
app.router.lifespan_context = _lifespan


def get_agent():
    global _agent
    if _agent is None:
        _rebuild_agent()
    return _agent


# ── 会话级 Agent 池（§7.4 执行态多用户隔离 — 企业版特性 session_pool）──
from automind.core import edition as _edition  # noqa: E402


def _session_agent_factory():
    """从全局 agent 克隆配置，创建一个执行态独立的会话 Agent。"""
    from automind.agent import AutoMindAgent
    base = get_agent()
    cfg = base.config.model_copy(deep=True)
    a = AutoMindAgent(cfg)
    a._interaction = base._interaction
    a._mode = base._mode
    return a


def _pool_enabled() -> bool:
    """企业版会话池是否可用且开启（社区/专业版恒为 False）。"""
    pool = _edition.get_feature("session_pool")
    return pool is not None and pool.enabled()


def _acquire_run_agent(base_agent, sid: str):
    """返回本次任务应使用的 Agent。

    企业版会话池启用时：返回该会话独立的 Agent（隔离 _current_plan/ReAct 态/
    审批回调），并同步全局的交互/执行模式与审批回调；
    否则：原样返回全局 agent（与历史行为一致，零改动）。
    """
    if not _pool_enabled():
        return base_agent
    a = _edition.get_feature("session_pool").acquire(sid)
    a._interaction = base_agent._interaction
    a._mode = base_agent._mode
    a.approval_callback = getattr(base_agent, "approval_callback", None)
    a.event_sink = getattr(base_agent, "event_sink", None)
    return a


# ═══════════════════════════════════════════════════════════
# REST API — 状态与配置
# ═══════════════════════════════════════════════════════════


@app.get("/api/health")
async def api_health():
    """健康检查（无需鉴权）— 供部署监控与负载均衡探活。"""
    return {
        "status": "ok", "version": app.version,
        "edition": _edition.get_edition(),
        "auth_required": bool(_auth_token()),
        "running_tasks": _running_tasks["count"],
        "max_concurrent": _MAX_CONCURRENT,
        "uptime_s": round(time.time() - _START_TIME, 1),
    }


@app.get("/api/status")
async def api_status(interaction: str = ""):
    agent = get_agent()
    # 若指定模式，返回该模式将使用的模型（per-mode 配置或全局默认）
    cur_interaction = interaction or agent._interaction.value
    mm = _mode_model(cur_interaction)
    eff_provider = mm["provider"] if mm else agent.config.llm.provider
    eff_model = mm["model"] if mm else agent.config.llm.model
    # 该模型是否就绪（有 key 且 LLM 初始化成功）
    keys = _load_api_keys()
    has_key = bool(keys.get(eff_provider) or _env_api_key(eff_provider))
    llm_ready = has_key and agent.llm is not None
    llm_error = ""
    if has_key and agent.llm is None:
        llm_error = getattr(agent, "_llm_init_error", "") or (
            f"LLM 后端初始化失败，请检查 {eff_provider} 的 API Key 与 api_base 是否正确"
        )
    return {
        "status": "running",
        "edition": _edition.get_edition(),
        "features": _edition.feature_flags(),
        "interaction": agent._interaction.value,
        "mode": agent._mode.value,
        "approval_mode": getattr(agent.permissions, "approval_mode", "auto"),
        "provider": eff_provider,
        "model": eff_model,
        "mode_specific": mm is not None,
        "api_base": agent.config.llm.api_base or "",
        "has_api_key": has_key,
        "llm_ready": llm_ready,
        "llm_error": llm_error,
        "project": str(agent.config.project_root),
        "tools": len(agent.tool_registry),
        "skills": len(agent.skill_registry),
        "sessions": len(_active_sessions),
        "history": len(_task_history),
        "token_totals": _token_totals,
    }


@app.get("/api/config/mode-models")
async def api_get_mode_models():
    """各模式的模型配置 + 全局默认。"""
    active = _load_active()
    return {
        "default": {"provider": active.get("provider", ""), "model": active.get("model", "")},
        "modes": _load_mode_models(),
    }


@app.post("/api/config/mode-models")
async def api_set_mode_models(data: dict):
    """为某模式设置/清除独立模型。{mode, provider, model} 或 {mode, clear:true}。"""
    mode = (data.get("mode") or "").strip()
    if mode not in ("chat", "work", "coding", "multi", "loop"):
        return JSONResponse({"error": "无效的模式"}, status_code=400)
    cfg = _read_config()
    mm = cfg.setdefault("mode_models", {})
    if data.get("clear"):
        mm.pop(mode, None)
    else:
        provider = (data.get("provider") or "").strip()
        model = (data.get("model") or "").strip()
        if not provider or not model:
            return JSONResponse({"error": "provider 和 model 必填"}, status_code=400)
        mm[mode] = {"provider": provider, "model": model}
    _write_config(cfg)
    return {"status": "ok", "modes": mm}


@app.post("/api/config/approval")
async def api_set_approval(data: dict):
    """设置审批模式：ask（询问）| auto（自动）| approve_all（全批准）。"""
    mode = (data.get("approval_mode") or "").strip()
    if mode not in ("ask", "auto", "approve_all"):
        return JSONResponse({"error": "无效的审批模式"}, status_code=400)
    _save_active(approval_mode=mode)
    agent = get_agent()
    agent.permissions.approval_mode = mode
    agent.config.execution.approval_mode = mode
    return {"status": "ok", "approval_mode": mode}


_AUTOPILOT_FLAGS = ("auto_review", "auto_verify", "auto_test",
                    "parallel_execution", "subtask_cache")


@app.get("/api/config/autopilot")
async def api_get_autopilot():
    """自主闭环开关（多Agent审查/Loop验证/TDD测试/并行执行/子任务缓存）。"""
    agent = get_agent()
    return {f: bool(getattr(agent.config.execution, f)) for f in _AUTOPILOT_FLAGS}


@app.post("/api/config/autopilot")
async def api_set_autopilot(data: dict):
    """更新自主闭环开关（持久化 + 即时生效，无需重建 Agent）。"""
    agent = get_agent()
    cfg = _read_config()
    stored = cfg.get("autopilot", {})
    for flag in _AUTOPILOT_FLAGS:
        if flag in data:
            val = bool(data[flag])
            stored[flag] = val
            setattr(agent.config.execution, flag, val)
    cfg["autopilot"] = stored
    _write_config(cfg)
    # 即时应用到执行器（并行/缓存在 PlanExecutor 实例上）
    agent.plan_executor.parallel = agent.config.execution.parallel_execution
    agent.plan_executor.use_cache = agent.config.execution.subtask_cache
    return {f: bool(getattr(agent.config.execution, f)) for f in _AUTOPILOT_FLAGS}


@app.get("/api/tokens")
async def api_tokens():
    """累计 token 用量统计。"""
    return _token_totals


@app.delete("/api/tokens")
async def api_tokens_reset():
    _token_totals.update({"prompt": 0, "completion": 0, "total": 0, "tasks": 0})
    return {"status": "ok"}


@app.get("/api/stats")
async def api_stats():
    """增强统计 — 按模式聚合任务、成功率、token、耗时与工具使用。"""
    hist = _task_history
    by_mode: dict[str, dict] = {}
    tool_usage: dict[str, int] = {}
    total_dur = 0.0
    success = 0
    for h in hist:
        m = h.get("interaction", "other")
        b = by_mode.setdefault(m, {"count": 0, "success": 0, "tokens": 0, "duration_ms": 0.0})
        b["count"] += 1
        b["success"] += 1 if h.get("success") else 0
        b["tokens"] += h.get("tokens", 0)
        b["duration_ms"] += h.get("duration_ms", 0) or 0
        total_dur += h.get("duration_ms", 0) or 0
        success += 1 if h.get("success") else 0
    # 工具使用来自审计日志（安全获取）
    agent = get_agent()
    try:
        audit_log = getattr(agent.permissions, "audit_log", [])
        for e in audit_log:
            tool_usage[e.tool_name] = tool_usage.get(e.tool_name, 0) + 1
    except Exception:
        audit_log = []
    n = len(hist)
    # round per-mode durations
    for b in by_mode.values():
        b["avg_ms"] = round(b["duration_ms"] / b["count"], 1) if b["count"] else 0
        b["duration_ms"] = round(b["duration_ms"], 1)
    try:
        audit_summary = {
            "total": len(audit_log),
            "ask_user": sum(1 for e in audit_log if getattr(e, "decision", None) and e.decision.value == "ask_user"),
            "dangerous": sum(1 for e in audit_log if getattr(e, "tier", None) and e.tier.value == "dangerous"),
        }
    except Exception:
        audit_summary = {"total": 0, "ask_user": 0, "dangerous": 0}
    return {
        "tasks_total": n,
        "success_total": success,
        "success_rate": round(success / n * 100, 1) if n else 0,
        "avg_duration_ms": round(total_dur / n, 1) if n else 0,
        "tokens": dict(_token_totals),
        "by_mode": by_mode,
        "tool_usage": dict(sorted(tool_usage.items(), key=lambda x: -x[1])),
        "audit": audit_summary,
        "scheduled_tasks": (
            _edition.get_feature("scheduler").count()
            if _edition.has_feature("scheduler") else 0),
    }


# ── 商业功能占位（专业版路由由 automind_pro 在扩展加载阶段注册）──
# 高级统计（/api/stats/detail|context|history）与定时任务（/api/schedule*）
# 已迁移至 automind-pro 包；社区版访问这些端点时由文件末尾注册的
# 降级路由返回 403 + 升级提示（若专业版已激活，其路由先注册故优先匹配）。


@app.get("/api/config/full")
async def api_config_full():
    agent = get_agent()
    saved_keys = _load_api_keys()
    providers = _load_providers()
    return {
        "interaction": agent._interaction.value,
        "provider": agent.config.llm.provider,
        "model": agent.config.llm.model,
        "api_base": agent.config.llm.api_base or "",
        "mode": agent._mode.value,
        "temperature": agent.config.llm.temperature,
        "max_tokens": agent.config.llm.max_tokens,
        "project": str(agent.config.project_root),
        "saved_api_keys": {k: bool(v) for k, v in saved_keys.items()},
        "provider_configs": providers,
    }


@app.post("/api/config")
async def api_config(data: dict):
    """更新配置：提供商 / 模型 / api_base / API Key / 交互模式 / 采样参数。"""
    global _agent
    from automind.core.types import ExecutionMode, InteractionMode

    agent = get_agent()
    changed = False

    if data.get("provider") and data["provider"] != agent.config.llm.provider:
        agent.config.llm.provider = data["provider"]
        _save_active(provider=data["provider"])
        changed = True
    if "model" in data and data["model"]:
        agent.config.llm.model = data["model"]
        _save_provider_cfg(agent.config.llm.provider, model=data["model"])
        _save_active(model=data["model"])
        changed = True
    if "api_base" in data:
        agent.config.llm.api_base = data["api_base"]
        _save_provider_cfg(agent.config.llm.provider, api_base=data["api_base"])
        changed = True
    if data.get("api_key"):
        agent.config.llm.api_key = data["api_key"]
        keys = _load_api_keys()
        keys[agent.config.llm.provider] = data["api_key"]
        _save_api_keys(keys)
        changed = True
    if data.get("interaction"):
        try:
            new_interaction = InteractionMode(data["interaction"])
            exec_mode = _INTERACTION_TO_EXECUTION.get(data["interaction"], "plan_and_execute")
            agent._interaction = new_interaction
            agent._mode = ExecutionMode(exec_mode)
            _save_active(interaction=data["interaction"])
            # 切换到该模式对应的模型
            agent = _apply_mode_model(agent, data["interaction"])
            changed = True  # 强制走重建逻辑以确保模型生效
        except ValueError:
            pass
    if "temperature" in data:
        agent.config.llm.temperature = float(data["temperature"])
    if "max_tokens" in data:
        agent.config.llm.max_tokens = int(data["max_tokens"])

    if changed:
        _rebuild_agent()
        agent = _agent

    llm_error = ""
    if agent.config.llm.api_key and agent.llm is None:
        llm_error = getattr(agent, "_llm_init_error", "")
    return {
        "status": "ok",
        "interaction": agent._interaction.value,
        "provider": agent.config.llm.provider,
        "model": agent.config.llm.model,
        "api_base": agent.config.llm.api_base or "",
        "mode": agent._mode.value,
        "has_api_key": bool(agent.config.llm.api_key),
        "llm_ready": agent.llm is not None,
        "llm_error": llm_error,
    }


@app.get("/api/config/apikeys")
async def api_get_api_keys():
    saved = _load_api_keys()
    providers = _load_providers()
    all_names = set(saved) | set(providers) | set(_ENV_KEY_MAP)
    result = {}
    for k in all_names:
        result[k] = {
            "saved": bool(saved.get(k)),
            "env": bool(_env_api_key(k)),
            "has_key": bool(saved.get(k) or _env_api_key(k)),
            "api_base": providers.get(k, {}).get("api_base", ""),
            "model": providers.get(k, {}).get("model", ""),
            "custom_models": providers.get(k, {}).get("custom_models", []),
        }
    return result


@app.post("/api/config/apikeys")
async def api_save_api_key(data: dict):
    """保存 / 删除某提供商的 API Key（可同时保存 api_base、model）。"""
    provider = data.get("provider", "")
    api_key = data.get("api_key", "")
    if not provider:
        return JSONResponse({"error": "Provider required"}, status_code=400)

    keys = _load_api_keys()
    if api_key:
        keys[provider] = api_key
    else:
        keys.pop(provider, None)
    _save_api_keys(keys)

    if "api_base" in data or "model" in data:
        _save_provider_cfg(
            provider,
            api_base=data.get("api_base"),
            model=data.get("model"),
        )

    # 若修改的是当前提供商，热重建
    agent = get_agent()
    if agent.config.llm.provider == provider:
        _rebuild_agent()

    return {"status": "ok", "provider": provider, "saved": bool(api_key)}


@app.post("/api/config/provider")
async def api_save_provider(data: dict):
    """保存某提供商的自定义 api_base 与默认模型（中转/代理）。"""
    provider = data.get("provider", "")
    if not provider:
        return JSONResponse({"error": "Provider required"}, status_code=400)
    _save_provider_cfg(
        provider,
        api_base=data.get("api_base"),
        model=data.get("model"),
    )
    agent = get_agent()
    if agent.config.llm.provider == provider:
        _rebuild_agent()
    return {"status": "ok", "provider": provider}


@app.post("/api/config/test")
async def api_config_test(data: dict):
    """测试 API 连通性 — 用给定/当前配置发起一次最小真实调用。"""
    from automind.core.config import LLMProviderConfig
    from automind.core.llm import LLMBackendFactory

    provider = (data.get("provider") or "").strip()
    if not provider:
        agent = get_agent()
        provider = agent.config.llm.provider

    providers = _load_providers()
    pcfg = providers.get(provider, {})
    model = (data.get("model") or pcfg.get("model")
             or _load_active().get("model") or "")
    api_base = data.get("api_base")
    if api_base is None:
        api_base = pcfg.get("api_base", "")
    api_key = data.get("api_key") or _load_api_keys().get(provider) or _env_api_key(provider)

    if not api_key:
        return {"success": False, "stage": "config",
                "error": f"未配置 {provider} 的 API Key"}

    # 仅当 api_base 是有效 URL 时才传入，避免误用占位符
    safe_base = api_base if (api_base and (api_base.startswith("http://") or api_base.startswith("https://"))) else ""
    cfg = LLMProviderConfig(
        provider=provider, model=model, api_key=api_key,
        api_base=safe_base, max_tokens=16, temperature=0.0, timeout=30.0,
    )
    t0 = time.perf_counter()
    try:
        backend = LLMBackendFactory.create(provider, cfg)
    except Exception as e:
        return {"success": False, "stage": "init", "error": str(e),
                "provider": provider, "model": model}
    try:
        resp = await backend.generate(
            [{"role": "user", "content": "ping，请只回复 ok"}])
        latency = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "success": True, "latency_ms": latency,
            "provider": provider, "model": resp.model or model,
            "api_base": api_base or "(默认)",
            "reply_sample": (resp.text or "")[:60],
            "tokens": resp.total_tokens,
        }
    except Exception as e:
        msg = str(e)
        hint = ""
        low = msg.lower()
        if "401" in msg or "unauthorized" in low or "invalid api key" in low or "invalid_api_key" in low:
            hint = "API Key 无效或未授权，请检查 Key 是否正确、是否过期。"
        elif "model" in low and any(w in low for w in ("not found", "not exist", "supported", "passed", "no such", "does not")):
            hint = "模型名称有误，请按服务商要求填写正确的模型名（注意大小写）。"
        elif "404" in msg or "not found" in low:
            hint = "API 地址（api_base）可能有误，请检查是否为正确的 /v1 端点。"
        elif "connect" in low or "timeout" in low or "resolve" in low or "connection" in low:
            hint = "无法连接到服务地址，请检查 api_base 与网络/代理。"
        elif "429" in msg or "rate" in low:
            hint = "请求过于频繁或额度不足。"
        return {"success": False, "stage": "request", "error": msg,
                "hint": hint, "provider": provider, "model": model,
                "api_base": api_base or "(默认)"}


# ═══════════════════════════════════════════════════════════
# REST API — 项目目录 / 文件系统浏览
# ═══════════════════════════════════════════════════════════


def _list_drives() -> list[str]:
    """Windows 盘符列表。"""
    drives = []
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            d = f"{letter}:\\"
            if Path(d).exists():
                drives.append(d)
    return drives


@app.get("/api/fs/list")
async def api_fs_list(path: str = ""):
    """浏览本地目录（仅返回子目录），用于项目目录选择器。"""
    try:
        base = Path(path).expanduser() if path else Path.cwd()
        base = base.resolve()
        if not base.is_dir():
            return JSONResponse({"error": f"非目录: {base}"}, status_code=400)
        dirs = []
        try:
            for e in sorted(base.iterdir(), key=lambda p: p.name.lower()):
                if e.is_dir() and not e.name.startswith("."):
                    dirs.append(e.name)
        except PermissionError:
            pass
        return {
            "path": str(base),
            "parent": str(base.parent) if base.parent != base else "",
            "dirs": dirs,
            "drives": _list_drives(),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/preview/file")
async def api_preview_file(path: str):
    """在项目目录范围内安全地预览一个文件（HTML 直接渲染，其它返回文本）。"""
    agent = get_agent()
    root = Path(agent.config.project_root).resolve()
    try:
        target = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    except Exception:
        return JSONResponse({"error": "非法路径"}, status_code=400)
    # 限制在项目根目录内，防止目录穿越
    if root not in target.parents and target != root:
        return JSONResponse({"error": "路径超出项目目录范围"}, status_code=403)
    if not target.is_file():
        return JSONResponse({"error": f"文件不存在: {target}"}, status_code=404)
    suffix = target.suffix.lower()
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if suffix in (".html", ".htm"):
        return HTMLResponse(content)
    media = "text/plain; charset=utf-8"
    if suffix == ".svg":
        media = "image/svg+xml"
    return HTMLResponse(content, media_type=media)


@app.post("/api/preview/render")
async def api_preview_render(data: dict):
    """渲染一段 HTML 字符串（供前端 iframe 预览使用）。"""
    return HTMLResponse(data.get("html", ""))


@app.get("/api/files/html")
async def api_list_html(limit: int = 30):
    """列出项目目录中最近修改的 HTML 文件，便于一键预览。"""
    agent = get_agent()
    root = Path(agent.config.project_root).resolve()
    files = []
    try:
        for p in root.rglob("*.htm*"):
            if any(part.startswith(".") or part in ("node_modules", "__pycache__")
                   for part in p.relative_to(root).parts):
                continue
            try:
                files.append((p.stat().st_mtime, str(p.relative_to(root))))
            except Exception:
                pass
    except Exception:
        pass
    files.sort(reverse=True)
    return [{"path": rel, "mtime": mt} for mt, rel in files[:limit]]


@app.post("/api/config/project")
async def api_set_project(data: dict):
    """设置 Agent 工作的本地项目目录。"""
    path = (data.get("path") or "").strip()
    p = Path(path).expanduser()
    if not p.is_dir():
        return JSONResponse({"error": f"目录不存在: {path}"}, status_code=400)
    _save_active(project=str(p.resolve()))
    _rebuild_agent()
    return {"status": "ok", "project": str(p.resolve())}


# ═══════════════════════════════════════════════════════════
# REST API — 工作区管理（每个工作区 = 独立目录 + 独立上下文）
# ═══════════════════════════════════════════════════════════


@app.get("/api/workspaces")
async def api_workspaces():
    """列出已保存的工作区与当前激活项。"""
    cfg = _read_config()
    active_project = str(Path(_load_active().get("project") or ".").resolve())
    return {"workspaces": cfg.get("workspaces", []), "active": active_project}


@app.post("/api/workspaces")
async def api_workspace_add(data: dict):
    """新增/更新一个命名工作区（name + 目录路径）。"""
    name = (data.get("name") or "").strip()
    path = (data.get("path") or "").strip()
    if not name:
        return JSONResponse({"error": "工作区名称必填"}, status_code=400)
    p = Path(path).expanduser()
    if not p.is_dir():
        return JSONResponse({"error": f"目录不存在: {path}"}, status_code=400)
    cfg = _read_config()
    spaces = [w for w in cfg.get("workspaces", []) if w.get("name") != name]
    spaces.append({"name": name, "path": str(p.resolve())})
    cfg["workspaces"] = spaces
    _write_config(cfg)
    return {"status": "ok", "workspaces": spaces}


@app.delete("/api/workspaces/{name}")
async def api_workspace_delete(name: str):
    """删除一个命名工作区（不删除磁盘目录）。"""
    cfg = _read_config()
    before = cfg.get("workspaces", [])
    cfg["workspaces"] = [w for w in before if w.get("name") != name]
    _write_config(cfg)
    return {"status": "ok", "deleted": len(before) - len(cfg["workspaces"])}


@app.post("/api/workspaces/switch")
async def api_workspace_switch(data: dict):
    """切换到指定工作区：Agent 在该目录下重建（记忆/索引/权限根随之切换）。

    name 为空 = 回到默认工作区（服务器启动目录）。
    """
    name = (data.get("name") or "").strip()
    if not name:
        _save_active(project=str(Path.cwd()))
        _rebuild_agent()
        logger.info("workspace_switched", name="(default)")
        return {"status": "ok", "name": "", "project": str(Path.cwd())}
    for w in _read_config().get("workspaces", []):
        if w.get("name") == name:
            p = Path(w.get("path", ""))
            if not p.is_dir():
                return JSONResponse(
                    {"error": f"工作区目录已不存在: {w.get('path')}"}, status_code=400)
            _save_active(project=str(p.resolve()))
            _rebuild_agent()
            logger.info("workspace_switched", name=name, path=str(p))
            return {"status": "ok", "name": name, "project": str(p.resolve())}
    return JSONResponse({"error": f"工作区不存在: {name}"}, status_code=404)


# ═══════════════════════════════════════════════════════════
# REST API — 文件改动日志 / 撤销回滚
# ═══════════════════════════════════════════════════════════


@app.get("/api/changes")
async def api_changes(limit: int = 30):
    """最近的文件改动记录（新→旧），供「撤销/回滚」面板展示。"""
    from automind.tools.file_editor import JOURNAL
    return {"changes": JOURNAL.entries()[:limit]}


@app.post("/api/changes/rollback")
async def api_rollback(data: dict):
    """撤销文件改动：传 path 恢复单个文件；传 all=true 恢复全部。"""
    from automind.tools.file_editor import JOURNAL
    if data.get("all"):
        n = JOURNAL.rollback_all()
        logger.info("rollback_all", restored=n)
        return {"status": "ok", "restored": n}
    path = (data.get("path") or "").strip()
    if not path:
        return JSONResponse({"error": "缺少 path 或 all 参数"}, status_code=400)
    if JOURNAL.rollback(path):
        logger.info("rollback_file", path=path)
        return {"status": "ok", "restored": 1, "path": path}
    return JSONResponse({"error": f"无该文件的改动记录或恢复失败: {path}"},
                        status_code=404)


# ═══════════════════════════════════════════════════════════
# REST API — MCP 服务器 / 技能管理
# ═══════════════════════════════════════════════════════════


@app.get("/api/mcp")
async def api_mcp_list():
    """列出已配置的 MCP 服务器及其工具。"""
    agent = get_agent()
    reg = agent.mcp_registry
    saved = _read_config().get("mcp_servers", [])
    servers = []
    for s in saved:
        name = s["name"]
        tools = [t.name for t in reg.get_all_tools() if t.name.startswith(f"mcp__{name}__")]
        servers.append({
            "name": name,
            "command": s.get("command", ""),
            "args": s.get("args", []),
            "url": s.get("url", ""),
            "transport": s.get("transport", "stdio"),
            "connected": name in getattr(reg, "_connected", set()),
            "tools": tools,
        })
    try:
        import mcp  # noqa: F401
        sdk = True
    except Exception:
        sdk = False
    return {"servers": servers, "sdk_installed": sdk}


@app.post("/api/mcp")
async def api_mcp_add(data: dict):
    """添加并连接一个 MCP 服务器。"""
    from automind.tools.mcp_registry import MCPServerConfig

    name = (data.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "服务器名称必填"}, status_code=400)

    transport = data.get("transport", "stdio")
    args = data.get("args", [])
    if isinstance(args, str):
        args = args.split()

    entry = {
        "name": name, "command": data.get("command", ""),
        "args": args, "url": data.get("url", ""), "transport": transport,
    }
    cfg = _read_config()
    servers = [s for s in cfg.get("mcp_servers", []) if s["name"] != name]
    servers.append(entry)
    cfg["mcp_servers"] = servers
    _write_config(cfg)

    agent = get_agent()
    agent.mcp_registry.add_server(MCPServerConfig(
        name=name, command=entry["command"], args=args,
        url=entry["url"], transport=transport,
    ))
    connected = False
    error = ""
    try:
        connected = await agent.mcp_registry.connect_server(name)
        if connected:
            agent.mcp_registry.register_to(agent.tool_registry)
    except Exception as e:
        error = str(e)
    if not connected and not error:
        error = "连接失败（请确认已安装 `pip install mcp` 且命令/参数正确）"
    return {"status": "ok", "connected": connected, "error": error}


@app.post("/api/mcp/import")
async def api_mcp_import(data: dict):
    """批量导入 MCP 服务器。

    支持 Claude Desktop 风格 {"mcpServers": {name: {command,args,...}}}，
    或直接传入 {name: {...}} 映射，或 [{name, command, ...}] 列表。
    """
    raw = data.get("config", data)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return JSONResponse({"error": "配置不是合法 JSON"}, status_code=400)

    servers_map: dict = {}
    if isinstance(raw, dict) and "mcpServers" in raw:
        servers_map = raw["mcpServers"] or {}
    elif isinstance(raw, dict):
        servers_map = raw
    elif isinstance(raw, list):
        servers_map = {s.get("name"): s for s in raw if s.get("name")}

    if not servers_map:
        return JSONResponse({"error": "未发现任何 MCP 服务器配置"}, status_code=400)

    from automind.tools.mcp_registry import MCPServerConfig
    agent = get_agent()
    cfg = _read_config()
    existing = {s["name"]: s for s in cfg.get("mcp_servers", [])}
    results = []
    for name, spec in servers_map.items():
        if not isinstance(spec, dict):
            continue
        args = spec.get("args", [])
        if isinstance(args, str):
            args = args.split()
        transport = spec.get("transport") or ("sse" if spec.get("url") else "stdio")
        entry = {"name": name, "command": spec.get("command", ""),
                 "args": args, "url": spec.get("url", ""), "transport": transport}
        existing[name] = entry
        agent.mcp_registry.add_server(MCPServerConfig(
            name=name, command=entry["command"], args=args,
            url=entry["url"], transport=transport))
        connected = False
        try:
            connected = await agent.mcp_registry.connect_server(name)
        except Exception:
            pass
        results.append({"name": name, "connected": connected})
    if connected_any := any(r["connected"] for r in results):
        agent.mcp_registry.register_to(agent.tool_registry)
    cfg["mcp_servers"] = list(existing.values())
    _write_config(cfg)
    return {"status": "ok", "imported": len(results), "results": results,
            "connected_any": connected_any}


@app.delete("/api/mcp/{name}")
async def api_mcp_remove(name: str):
    cfg = _read_config()
    cfg["mcp_servers"] = [s for s in cfg.get("mcp_servers", []) if s["name"] != name]
    _write_config(cfg)
    agent = get_agent()
    for t in list(agent.mcp_registry.get_all_tools()):
        if t.name.startswith(f"mcp__{name}__"):
            agent.tool_registry.unregister(t.name)
    try:
        await agent.mcp_registry.disconnect(name)
    except Exception:
        pass
    agent.mcp_registry.remove_server(name)
    return {"status": "ok"}


@app.post("/api/skills/load")
async def api_skills_load(data: dict):
    """从本地目录加载技能 — 同时支持 .py（AbstractSkill）与 SKILL.md 格式。"""
    directory = (data.get("directory") or "").strip()
    p = Path(directory).expanduser()
    if not p.is_dir():
        return JSONResponse({"error": f"目录不存在: {directory}"}, status_code=400)
    agent = get_agent()
    before = len(agent.skill_registry)
    res = agent.skill_registry.discover_any(p)
    cfg = _read_config()
    dirs = cfg.get("skill_dirs", [])
    if str(p.resolve()) not in dirs:
        dirs.append(str(p.resolve()))
        cfg["skill_dirs"] = dirs
        _write_config(cfg)
    return {"status": "ok", "loaded": res["total"], "py": res["py"],
            "markdown": res["markdown"], "total": len(agent.skill_registry),
            "before": before}


@app.post("/api/skills/import")
async def api_skills_import(data: dict):
    """导入单个技能：上传 .py 文件内容，保存到本地技能目录并加载。"""
    name = (data.get("name") or "").strip()
    code = data.get("code") or ""
    if not code.strip():
        return JSONResponse({"error": "技能代码为空"}, status_code=400)
    if "AbstractSkill" not in code:
        return JSONResponse(
            {"error": "未检测到 AbstractSkill 子类，请确认这是有效的技能文件。"},
            status_code=400)
    # 文件名安全化
    import re as _re
    stem = _re.sub(r"[^A-Za-z0-9_]", "_", (name or "skill").rsplit(".", 1)[0]) or "skill"
    _SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    fpath = _SKILLS_DIR / f"{stem}.py"
    fpath.write_text(code, encoding="utf-8")

    agent = get_agent()
    before = set(agent.skill_registry.list_names())
    try:
        count = agent.skill_registry.discover_from_directory(_SKILLS_DIR)
    except Exception as e:
        fpath.unlink(missing_ok=True)
        return JSONResponse({"error": f"加载失败: {e}"}, status_code=400)
    new = [n for n in agent.skill_registry.list_names() if n not in before]
    # 持久化技能目录
    cfg = _read_config()
    dirs = cfg.get("skill_dirs", [])
    if str(_SKILLS_DIR.resolve()) not in dirs:
        dirs.append(str(_SKILLS_DIR.resolve()))
        cfg["skill_dirs"] = dirs
        _write_config(cfg)
    if not new:
        fpath.unlink(missing_ok=True)
        return JSONResponse({"error": "未发现可注册的技能类"}, status_code=400)
    return {"status": "ok", "imported": new, "total": len(agent.skill_registry)}


@app.delete("/api/skills/{name}")
async def api_skills_delete(name: str):
    """删除一个技能（内置技能不可删除）。"""
    if name in _BUILTIN_SKILLS:
        return JSONResponse({"error": "内置技能不可删除"}, status_code=400)
    agent = get_agent()
    agent.skill_registry.unregister(name)
    # 删除对应的自定义技能文件（按 name 或文件名匹配）
    try:
        for f in _SKILLS_DIR.glob("*.py"):
            txt = f.read_text(encoding="utf-8", errors="ignore")
            if f'name = "{name}"' in txt or f"name = '{name}'" in txt or f.stem == name:
                f.unlink(missing_ok=True)
    except Exception:
        pass
    return {"status": "ok", "total": len(agent.skill_registry)}


# ═══════════════════════════════════════════════════════════
# REST API — 提供商 / 模型 / 工具 / 技能
# ═══════════════════════════════════════════════════════════


@app.get("/api/providers")
async def api_providers():
    from automind.core.llm import LLMBackendFactory
    providers = LLMBackendFactory.available_providers()
    cloud = ["openai", "anthropic", "deepseek", "kimi", "moonshot", "bailian",
             "dashscope", "qwen", "zhipu", "glm", "doubao", "google", "gemini", "grok"]
    local = ["ollama"]
    custom = ["custom"]
    return {
        "all": providers,
        "cloud": [p for p in providers if p in cloud],
        "local": [p for p in providers if p in local],
        "custom": [p for p in providers if p in custom],
        "labels": {
            "openai": "OpenAI", "anthropic": "Anthropic Claude",
            "deepseek": "DeepSeek", "kimi": "Kimi (月之暗面)",
            "moonshot": "Moonshot", "bailian": "阿里百炼", "dashscope": "DashScope",
            "qwen": "通义千问", "zhipu": "智谱 GLM", "glm": "GLM",
            "doubao": "字节豆包", "google": "Google Gemini", "gemini": "Gemini",
            "grok": "xAI Grok", "ollama": "Ollama (本地)",
            "custom": "自定义 (OpenAI 标准/中转代理)",
        },
        "defaults": {
            "openai": "gpt-4o", "anthropic": "claude-sonnet-4-20250514",
            "deepseek": "deepseek-chat", "kimi": "moonshot-v1-128k",
            "bailian": "qwen-max", "zhipu": "glm-4-plus",
            "doubao": "doubao-pro-128k", "google": "gemini-2.5-flash",
            "grok": "grok-3", "ollama": "llama3.2", "custom": "gpt-4o",
        },
    }


@app.get("/api/models")
async def api_models(provider: str = ""):
    """获取某提供商的推荐模型列表（含用户自定义保存的模型）。"""
    model_map = {
        "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4-turbo", "o1", "gpt-3.5-turbo"],
        "anthropic": ["claude-opus-4-7", "claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"],
        "deepseek": ["deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro", "deepseek-v4-flash"],
        "kimi": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "moonshot": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "bailian": ["qwen-max", "qwen-plus", "qwen-turbo"],
        "dashscope": ["qwen-max", "qwen-plus", "qwen-turbo"],
        "qwen": ["qwen-max", "qwen-plus", "qwen-turbo"],
        "zhipu": ["glm-4-plus", "glm-4-flash", "glm-4-air"],
        "glm": ["glm-4-plus", "glm-4-flash"],
        "doubao": ["doubao-pro-128k", "doubao-pro-32k"],
        "google": ["gemini-2.5-flash", "gemini-2.5-pro"],
        "gemini": ["gemini-2.5-flash", "gemini-2.5-pro"],
        "grok": ["grok-3", "grok-2"],
        "ollama": ["llama3.2", "mistral", "codellama", "qwen2.5", "deepseek-coder"],
        "custom": ["gpt-4o", "gpt-3.5-turbo"],
    }
    models = list(model_map.get(provider.lower(), []))
    # 用户自定义添加的模型置顶
    for m in reversed(_custom_models(provider.lower())):
        if m not in models:
            models.insert(0, m)
    saved = _load_providers().get(provider.lower(), {}).get("model")
    if saved and saved not in models:
        models.insert(0, saved)
    return models


@app.post("/api/models/add")
async def api_models_add(data: dict):
    """自定义添加一个模型名到某提供商。"""
    provider = (data.get("provider") or "").lower()
    model = (data.get("model") or "").strip()
    if not provider or not model:
        return JSONResponse({"error": "provider 和 model 均为必填"}, status_code=400)
    _add_custom_model(provider, model)
    return {"status": "ok", "models": await api_models(provider)}


@app.post("/api/models/remove")
async def api_models_remove(data: dict):
    """删除某提供商的一个自定义模型。"""
    provider = (data.get("provider") or "").lower()
    model = (data.get("model") or "").strip()
    _remove_custom_model(provider, model)
    return {"status": "ok", "models": await api_models(provider)}


@app.get("/api/tools")
async def api_tools():
    agent = get_agent()
    disabled = _read_config().get("disabled_tools", [])
    result = [
        {
            "name": t.name,
            "description": t.description,
            "tier": t.permission_tier.value,
            "risk": t.risk_score,
            "params": list(t.parameters.get("properties", {}).keys()),
            "required": t.parameters.get("required", []),
            "enabled": True,
            "mcp": t.name.startswith("mcp__"),
        }
        for t in agent.tool_registry.list_all()
    ]
    # 附加已禁用的工具（仅名称，重新启用后恢复完整信息）
    for name in disabled:
        result.append({
            "name": name, "description": "（已禁用）", "tier": "safe",
            "risk": 0, "params": [], "required": [], "enabled": False,
            "mcp": name.startswith("mcp__"),
        })
    return result


@app.post("/api/tools/toggle")
async def api_tools_toggle(data: dict):
    """启用/禁用某个工具（禁用后 Agent 执行时不可调用）。"""
    name = (data.get("name") or "").strip()
    enabled = bool(data.get("enabled", True))
    if not name:
        return JSONResponse({"error": "工具名必填"}, status_code=400)
    cfg = _read_config()
    disabled = cfg.get("disabled_tools", [])
    if enabled:
        disabled = [d for d in disabled if d != name]
    elif name not in disabled:
        disabled.append(name)
    cfg["disabled_tools"] = disabled
    _write_config(cfg)
    _rebuild_agent()  # 重建以一致应用启用/禁用
    return {"status": "ok", "name": name, "enabled": enabled}


@app.get("/api/skills")
async def api_skills():
    agent = get_agent()
    out = []
    for s in agent.skill_registry.list_all():
        stype = s.get("type", "python")
        out.append({
            **s,
            "type": stype,
            "emoji": s.get("emoji") or ("📦" if stype == "markdown" else "✨"),
            "builtin": s.get("name") in _BUILTIN_SKILLS,
        })
    # 内置技能在前，其余按名称排序
    out.sort(key=lambda x: (not x["builtin"], x["type"] != "markdown", x["name"]))
    return out


# ═══════════════════════════════════════════════════════════
# REST API — 插件系统（§14.7）
# ═══════════════════════════════════════════════════════════


@app.get("/api/plugins")
async def api_plugins():
    """列出已发现/已加载的插件。"""
    agent = get_agent()
    return {"plugins": agent.plugin_manager.status()}


@app.post("/api/plugins/{name}/load")
async def api_plugin_load(name: str):
    """加载插件并将其 hooks 应用到当前 Agent。"""
    agent = get_agent()
    hooks = agent.plugin_manager.load(name)
    if hooks is None:
        return JSONResponse({"error": f"插件加载失败或不存在：{name}"}, status_code=400)
    agent.apply_plugin_hooks()
    return {"ok": True, "loaded": agent.plugin_manager.loaded_names()}


@app.post("/api/plugins/{name}/unload")
async def api_plugin_unload(name: str):
    """卸载插件并刷新 Agent hooks。"""
    agent = get_agent()
    removed = agent.plugin_manager.unload(name)
    agent.apply_plugin_hooks()
    return {"ok": removed, "loaded": agent.plugin_manager.loaded_names()}


# ═══════════════════════════════════════════════════════════
# REST API — 软件审计
# ═══════════════════════════════════════════════════════════


@app.get("/api/audit")
async def api_audit(limit: int = 200):
    """权限审计日志 — 记录每次工具调用的风险评估与授权决策。"""
    agent = get_agent()
    log = getattr(agent.permissions, "audit_log", [])
    entries = []
    for e in log[-limit:]:
        entries.append({
            "timestamp": e.timestamp,
            "time": time.strftime("%H:%M:%S", time.localtime(e.timestamp)),
            "tool": e.tool_name,
            "decision": e.decision.value,
            "tier": e.tier.value,
            "risk": e.risk_score,
            "reason": e.reason,
            "params": {k: str(v)[:120] for k, v in (e.params or {}).items()},
        })
    entries.reverse()
    # 汇总
    summary = {
        "total": len(log),
        "allow": sum(1 for e in log if e.decision.value == "allow"),
        "ask_user": sum(1 for e in log if e.decision.value == "ask_user"),
        "deny": sum(1 for e in log if e.decision.value == "deny"),
        "dangerous": sum(1 for e in log if e.tier.value == "dangerous"),
        "high_risk": sum(1 for e in log if e.risk_score >= 80),
    }
    return {"summary": summary, "entries": entries}


@app.delete("/api/audit")
async def api_clear_audit():
    agent = get_agent()
    if hasattr(agent.permissions, "audit_log"):
        agent.permissions.audit_log.clear()
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════
# REST API — 任务执行
# ═══════════════════════════════════════════════════════════


@app.get("/api/history")
async def api_history(limit: int = 50):
    return _task_history[-limit:]


@app.get("/api/history/{session_id}")
async def api_history_detail(session_id: str):
    for h in _task_history:
        if h["session_id"] == session_id:
            return h
    return JSONResponse({"error": "Not found"}, status_code=404)


@app.get("/api/sessions")
async def api_sessions():
    return {
        sid: {
            "task": s.get("task", ""),
            "status": s.get("status", "unknown"),
            "steps": s.get("steps", 0),
            "created": s.get("created", ""),
        }
        for sid, s in _active_sessions.items()
    }


@app.post("/api/run")
async def api_run(data: dict):
    """执行任务 — 根据交互模式路由到 对话 / 工作 / 编程。"""
    from automind.core.types import ExecutionMode, InteractionMode

    task = (data.get("task") or "").strip()
    interaction = data.get("interaction", "")
    provider = data.get("provider", "")
    model = data.get("model", "")
    api_key = data.get("api_key", "")
    images = data.get("images") or []  # 多模态：data URL 列表
    chat_sid = data.get("session_id") or "default"  # 多用户会话隔离

    if not task and not images:
        return JSONResponse({"error": "任务内容为空"}, status_code=400)

    agent = get_agent()

    # 运行时切换提供商 / 模型 / Key
    need_rebuild = False
    if provider and provider != agent.config.llm.provider:
        agent.config.llm.provider = provider
        _save_active(provider=provider)
        need_rebuild = True
    if model and model != agent.config.llm.model:
        agent.config.llm.model = model
        need_rebuild = True
    if api_key:
        agent.config.llm.api_key = api_key
        need_rebuild = True
    if need_rebuild:
        _rebuild_agent()
        agent = _agent

    # 切换交互模式
    if interaction:
        try:
            agent._interaction = InteractionMode(interaction)
            agent._mode = ExecutionMode(
                _INTERACTION_TO_EXECUTION.get(interaction, "plan_and_execute"))
            _save_active(interaction=interaction)
        except ValueError:
            pass

    # 按交互模式应用对应模型（per-mode 配置）
    agent = _apply_mode_model(agent, agent._interaction.value)

    if agent.llm is None:
        err_detail = getattr(agent, "_llm_init_error", "")
        hint = (
            f"LLM 未初始化（{agent.config.llm.provider}/{agent.config.llm.model}）。"
            + (f" 原因: {err_detail}" if err_detail else " 请检查 API Key 与 api_base 是否正确。")
        )
        return JSONResponse({"error": hint}, status_code=400)

    # 资源保护：并发任务数上限
    if _running_tasks["count"] >= _MAX_CONCURRENT:
        return JSONResponse(
            {"error": f"当前并发任务已达上限（{_MAX_CONCURRENT}），请稍后再试。"},
            status_code=429)

    session_id = uuid.uuid4().hex[:12]
    _active_sessions[session_id] = {
        "task": task, "status": "running", "steps": 0,
        "created": str(time.time()), "provider": agent.config.llm.provider,
        "model": agent.config.llm.model, "interaction": agent._interaction.value,
    }
    _running_tasks["count"] += 1
    # §7.4：池启用时改用该会话独立的 Agent（执行态隔离）；关闭时仍是全局 agent。
    agent = _acquire_run_agent(agent, chat_sid)
    logger.info("task_start", session=session_id, interaction=agent._interaction.value,
                provider=agent.config.llm.provider, model=agent.config.llm.model,
                task=task[:120], running=_running_tasks["count"],
                pooled=_pool_enabled())

    try:
        # ── 对话模式：纯多轮对话 ──
        if agent._interaction == InteractionMode.CHAT:
            t0 = time.perf_counter()
            hist = _get_session_history(chat_sid)
            reply = await agent.chat(task, images=images, history=hist)
            _save_session_history(chat_sid)
            usage = agent.llm.usage
            _active_sessions[session_id]["status"] = "success"
            record = {
                "session_id": session_id, "task": task, "success": True,
                "output": reply, "steps": 0, "backtracks": 0,
                "errors_corrected": 0,
                "tokens": usage.total,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                "plan": None, "interaction": "chat",
            }
            _push_history(record)
            _accumulate_tokens(record)
            return record

        # ── 多智能体协同 ──
        if agent._interaction == InteractionMode.MULTI:
            t0 = time.perf_counter()
            ma = await agent.run_multi(task)
            usage = ma.get("token_usage")
            _active_sessions[session_id]["status"] = "success"
            record = {
                "session_id": session_id, "task": task, "success": True,
                "output": ma["output"][:6000], "steps": len(ma.get("steps", [])),
                "backtracks": 0, "errors_corrected": 0,
                "tokens": usage.total if usage else 0,
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                "plan": None, "interaction": "multi", "multi_steps": ma.get("steps", []),
            }
            _push_history(record)
            _accumulate_tokens(record)
            return record

        # ── 循环工程 ──
        if agent._interaction == InteractionMode.LOOP:
            t0 = time.perf_counter()
            lp = await agent.run_loop(task)
            usage = lp.get("token_usage")
            record = {
                "session_id": session_id, "task": task, "success": lp.get("success", False),
                "output": (lp.get("output") or "")[:6000], "steps": lp.get("iterations", 0),
                "backtracks": 0, "errors_corrected": 0,
                "tokens": usage.total if usage else 0,
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                "plan": None, "interaction": "loop", "stop_reason": lp.get("stop_reason", ""),
            }
            _push_history(record)
            _accumulate_tokens(record)
            return record

        # ── 工作 / 编程模式：完整 Agent 流程 ──
        result = await agent.run(task)
        _active_sessions[session_id]["status"] = "success" if result.success else "partial"
        _active_sessions[session_id]["steps"] = result.steps_executed

        record = {
            "session_id": session_id,
            "task": task,
            "success": result.success,
            "output": result.output[:5000],
            "steps": result.steps_executed,
            "backtracks": result.backtracks,
            "errors_corrected": result.errors_corrected,
            "tokens": result.token_usage.total,
            "prompt_tokens": result.token_usage.prompt_tokens,
            "completion_tokens": result.token_usage.completion_tokens,
            "duration_ms": round(result.duration_ms, 1),
            "plan": _serialize_plan(result.plan) if result.plan else None,
            "interaction": agent._interaction.value,
        }
        _push_history(record)
        _accumulate_tokens(record)

        await _broadcast({
            "type": "task_complete", "session_id": session_id,
            "success": result.success, "output": result.output,
            "steps": result.steps_executed, "backtracks": result.backtracks,
            "tokens": result.token_usage.total,
            "duration_ms": round(result.duration_ms, 1),
            "plan": record["plan"],
        })
        return record

    except _edition.FeatureNotAvailable as e:
        _active_sessions[session_id]["status"] = "error"
        _active_sessions[session_id]["error"] = str(e)
        return JSONResponse({"error": str(e), "feature": e.feature,
                             "edition": _edition.get_edition()}, status_code=403)
    except Exception as e:
        import traceback
        logger.error("task_failed", session=session_id,
                     interaction=agent._interaction.value, error=str(e),
                     trace=traceback.format_exc()[-800:])
        _active_sessions[session_id]["status"] = "error"
        _active_sessions[session_id]["error"] = str(e)
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        _running_tasks["count"] = max(0, _running_tasks["count"] - 1)
        logger.info("task_end", session=session_id,
                    status=_active_sessions.get(session_id, {}).get("status", "?"),
                    running=_running_tasks["count"])


@app.get("/api/chat/history")
async def api_chat_history(session_id: str = "default"):
    """返回某会话的对话历史（用于刷新后恢复显示，多用户隔离）。"""
    return {"messages": _get_session_history(session_id)}


@app.post("/api/chat/reset")
@app.delete("/api/chat/history")
async def api_chat_reset(session_id: str = "default"):
    """删除某会话的对话记录。"""
    _store.session_histories[session_id] = []
    _save_session_history(session_id)
    if session_id == "default":
        try:
            get_agent().reset_chat()
        except Exception:
            pass
    return {"status": "ok"}


@app.delete("/api/history/{session_id}")
async def api_delete_history(session_id: str):
    """删除单条任务历史记录。"""
    before = len(_task_history)
    _task_history[:] = [h for h in _task_history if h.get("session_id") != session_id]
    _active_sessions.pop(session_id, None)
    _save_task_history()
    return {"status": "ok", "deleted": before - len(_task_history)}


@app.delete("/api/history")
async def api_clear_history():
    _task_history.clear()
    _active_sessions.clear()
    _save_task_history()
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════
# WebSocket (实时流)
# ═══════════════════════════════════════════════════════════


_ws_tasks: dict[str, asyncio.Task] = {}
_ws_approvals: dict[str, asyncio.Future] = {}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # WebSocket 源校验（§14.11-5）：配置 AUTOMIND_ALLOWED_ORIGINS 后，
    # 拒绝来源不在白名单的连接，防止跨站 WS 劫持。默认不校验。
    if _WS_ALLOWED_ORIGINS:
        origin = ws.headers.get("origin", "")
        if origin not in _WS_ALLOWED_ORIGINS:
            await ws.close(code=4403)
            return
    # 商用鉴权：配置令牌后，WS 需带 ?token=
    token = _auth_token()
    if token and ws.query_params.get("token", "") != token:
        await ws.close(code=4401)
        return
    await ws.accept()
    client_id = uuid.uuid4().hex[:8]
    _ws_clients.setdefault("all", []).append(ws)
    try:
        await ws.send_json({"type": "connected", "client_id": client_id})
        while True:
            data = await ws.receive_json()
            action = data.get("action", "")
            if action == "ping":
                await ws.send_json({"type": "pong"})
            elif action == "run":
                old = _ws_tasks.get(client_id)
                if old and not old.done():
                    old.cancel()
                _ws_tasks[client_id] = asyncio.create_task(_ws_run(ws, client_id, data))
            elif action == "stop":
                t = _ws_tasks.get(client_id)
                if t and not t.done():
                    t.cancel()
            elif action == "approval_response":
                fut = _ws_approvals.get(data.get("approval_id", ""))
                if fut and not fut.done():
                    fut.set_result(bool(data.get("approved")))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        t = _ws_tasks.pop(client_id, None)
        if t and not t.done():
            t.cancel()
        if ws in _ws_clients.get("all", []):
            _ws_clients["all"].remove(ws)


async def _ws_run(ws: WebSocket, client_id: str, data: dict):
    """WebSocket 任务执行 — 对话流式输出，工作/编程可中断。"""
    from automind.core.types import ExecutionMode, InteractionMode, TokenUsage

    task = (data.get("task") or "").strip()
    interaction = data.get("interaction", "")
    images = data.get("images") or []
    chat_sid = data.get("session_id") or "default"  # 多用户会话隔离
    if not task and not images:
        return

    agent = get_agent()
    if interaction:
        try:
            agent._interaction = InteractionMode(interaction)
            agent._mode = ExecutionMode(
                _INTERACTION_TO_EXECUTION.get(interaction, "plan_and_execute"))
            _save_active(interaction=interaction)
        except ValueError:
            pass

    # 按交互模式应用对应模型（per-mode 配置）
    agent = _apply_mode_model(agent, agent._interaction.value)

    if agent.llm is None:
        await ws.send_json({"type": "task_error",
                            "error": "LLM 未初始化，请先为该模式配置可用模型的 API Key。"})
        return

    # 资源保护：并发任务上限
    if _running_tasks["count"] >= _MAX_CONCURRENT:
        await ws.send_json({"type": "task_error",
                            "error": f"当前并发任务已达上限（{_MAX_CONCURRENT}），请稍后再试。"})
        return

    session_id = uuid.uuid4().hex[:12]
    _running_tasks["count"] += 1

    # 注入审批回调（ask 模式下工具调用前向前端请求批准）
    async def _approval_cb(tool_name, args, tier, reason):
        approval_id = uuid.uuid4().hex[:10]
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        _ws_approvals[approval_id] = fut
        try:
            await ws.send_json({
                "type": "approval_request", "approval_id": approval_id,
                "session_id": session_id, "tool": tool_name, "tier": tier,
                "reason": reason, "params": {k: str(v)[:200] for k, v in (args or {}).items()},
            })
            return await asyncio.wait_for(fut, timeout=300)
        except TimeoutError:
            return False
        finally:
            _ws_approvals.pop(approval_id, None)

    agent.approval_callback = _approval_cb

    # 注入执行过程事件回调（实时展示思考/工具调用/计划步骤）
    async def _event_sink(ev):
        try:
            await ws.send_json({"session_id": session_id, **ev})
        except Exception:
            pass
    agent.event_sink = _event_sink

    await ws.send_json({"type": "task_start", "session_id": session_id,
                        "interaction": agent._interaction.value})
    t0 = time.perf_counter()
    try:
        # ── 对话模式：流式 ──
        if agent._interaction == InteractionMode.CHAT:
            chunks: list[str] = []
            hist = _get_session_history(chat_sid)
            async for delta in agent.chat_stream(task, images=images, history=hist):
                chunks.append(delta)
                await ws.send_json({"type": "chat_chunk", "session_id": session_id,
                                    "delta": delta})
            _save_session_history(chat_sid)
            usage = getattr(agent, "_last_stream_usage", None) or TokenUsage()
            record = {
                "session_id": session_id, "task": task, "success": True,
                "output": "".join(chunks), "steps": 0, "backtracks": 0,
                "errors_corrected": 0, "tokens": usage.total,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                "plan": None, "interaction": "chat",
            }
            _push_history(record)
            _accumulate_tokens(record)
            await ws.send_json({"type": "chat_done", "session_id": session_id,
                                "tokens": usage.total,
                                "prompt_tokens": usage.prompt_tokens,
                                "completion_tokens": usage.completion_tokens,
                                "duration_ms": record["duration_ms"]})
            return

        # ── 多智能体协同 ──
        if agent._interaction == InteractionMode.MULTI:
            async def on_ev(ev):
                try:
                    await ws.send_json({"session_id": session_id, **ev})
                except Exception:
                    pass
            ma = await agent.run_multi(task, on_event=on_ev)
            usage = ma.get("token_usage")
            record = {
                "session_id": session_id, "task": task, "success": True,
                "output": ma["output"][:6000], "steps": len(ma.get("steps", [])),
                "backtracks": 0, "errors_corrected": 0,
                "tokens": usage.total if usage else 0,
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                "plan": None, "interaction": "multi",
                "multi_steps": ma.get("steps", []),
            }
            _push_history(record)
            _accumulate_tokens(record)
            await ws.send_json({"type": "task_complete", **record})
            return

        # ── 循环工程：自主"行动-观察-修正"闭环 ──
        if agent._interaction == InteractionMode.LOOP:
            async def on_loop(ev):
                try:
                    await ws.send_json({"session_id": session_id, **ev})
                except Exception:
                    pass
            lp = await agent.run_loop(task, on_event=on_loop)
            usage = lp.get("token_usage")
            record = {
                "session_id": session_id, "task": task, "success": lp.get("success", False),
                "output": (lp.get("output") or "")[:6000], "steps": lp.get("iterations", 0),
                "backtracks": 0, "errors_corrected": 0,
                "tokens": usage.total if usage else 0,
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                "plan": None, "interaction": "loop",
                "stop_reason": lp.get("stop_reason", ""),
            }
            _push_history(record)
            _accumulate_tokens(record)
            await ws.send_json({"type": "task_complete", **record})
            return

        # ── 工作 / 编程模式：完整流程（可中断）──
        result = await agent.run(task)
        record = {
            "session_id": session_id, "task": task, "success": result.success,
            "output": result.output[:5000], "steps": result.steps_executed,
            "backtracks": result.backtracks, "errors_corrected": result.errors_corrected,
            "tokens": result.token_usage.total,
            "prompt_tokens": result.token_usage.prompt_tokens,
            "completion_tokens": result.token_usage.completion_tokens,
            "duration_ms": round(result.duration_ms, 1),
            "plan": _serialize_plan(result.plan) if result.plan else None,
            "interaction": agent._interaction.value,
        }
        _push_history(record)
        _accumulate_tokens(record)
        await ws.send_json({"type": "task_complete", **record})

    except asyncio.CancelledError:
        try:
            await ws.send_json({"type": "task_cancelled", "session_id": session_id})
        except Exception:
            pass
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            await ws.send_json({"type": "task_error", "session_id": session_id,
                                "error": str(e)})
        except Exception:
            pass
    finally:
        agent.approval_callback = None
        agent.event_sink = None
        _running_tasks["count"] = max(0, _running_tasks["count"] - 1)


async def _broadcast(data: dict):
    dead = []
    for ws in _ws_clients.get("all", []):
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _ws_clients.get("all", []):
            _ws_clients["all"].remove(ws)


# ── 辅助 ──────────────────────────────────────────────────


def _serialize_plan(plan) -> dict | None:
    if plan is None:
        return None

    def serialize_goal(g):
        return {
            "id": g.id, "description": g.description,
            "status": g.status.value,
            "preconditions": [str(p) for p in g.preconditions],
            "expected_effects": [str(p) for p in g.expected_effects],
            "action": g.assigned_action.tool_name if g.assigned_action else None,
            "children": [serialize_goal(c) for c in g.children],
        }

    return {
        "task": plan.task_description,
        "status": plan.status.value,
        "execution_order": plan.execution_order[:20],
        "root_goal": serialize_goal(plan.root_goal),
        "revision_history": plan.revision_history,
    }


# ═══════════════════════════════════════════════════════════
# 商业扩展装载（专业版/企业版）与社区版降级路由
# ═══════════════════════════════════════════════════════════
# server_ctx 是传给商业扩展 attach() 的稳定契约 v1
# （见 automind/core/edition.py 模块文档），社区版重构时保持这些键可用。


def _build_server_ctx() -> dict:
    return {
        "app": app,
        "get_agent": get_agent,
        "read_config": _read_config,
        "write_config": _write_config,
        "push_history": _push_history,
        "broadcast": _broadcast,
        "task_history": lambda: _task_history,
        "token_totals": lambda: _token_totals,
        "interaction_to_execution": _INTERACTION_TO_EXECUTION,
        "session_agent_factory": _session_agent_factory,
        "max_concurrent": _MAX_CONCURRENT,
        "version": __version__,
    }


def _attach_extensions() -> None:
    """激活商业扩展并注册其路由；随后注册社区版降级路由。

    路由匹配按注册顺序：专业版真实路由先注册故优先；降级路由仅在
    对应特性缺失时兜底命中，返回 403 + 升级提示。
    """
    _edition.load_extensions()
    ctx = _build_server_ctx()
    for name in ("scheduler", "advanced_stats", "session_pool"):
        feature = _edition.get_feature(name)
        if feature is not None and hasattr(feature, "attach"):
            try:
                feature.attach(ctx)
            except Exception as e:
                logger.warning("edition_attach_failed", feature=name, error=str(e))

    def _locked(feature: str):
        async def _handler(sid: str = "", data: dict | None = None):
            return JSONResponse(
                {"error": _edition.upgrade_hint(feature),
                 "feature": feature, "edition": _edition.get_edition()},
                status_code=403)
        return _handler

    # 定时任务（专业版）
    for method, path in (("GET", "/api/schedule"), ("POST", "/api/schedule"),
                         ("DELETE", "/api/schedule/{sid}"),
                         ("POST", "/api/schedule/{sid}/toggle"),
                         ("POST", "/api/schedule/{sid}/run")):
        app.add_api_route(path, _locked("scheduler"), methods=[method])
    # 高级统计（专业版）
    for path in ("/api/stats/detail", "/api/stats/context", "/api/stats/history"):
        app.add_api_route(path, _locked("advanced_stats"), methods=["GET"])

    logger.info("edition_ready", edition=_edition.get_edition(),
                features=[k for k, v in _edition.feature_flags().items() if v])


_attach_extensions()


@app.get("/manual")
async def manual_page():
    """内置使用手册（static/manual.html，界面右上角 📖 入口）。"""
    html_path = STATIC_DIR / "manual.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return JSONResponse({"error": "手册文件缺失"}, status_code=404)


@app.get("/")
async def index():
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        html = html_path.read_text(encoding="utf-8")
        # 版本化静态资源 URL → 版本升级即自动 cache-bust（server_web.py）
        return HTMLResponse(_versioned_html(html, __version__))
    return HTMLResponse(_fallback_html())


def _fallback_html() -> str:
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>AutoMind</title></head><body style="font-family:sans-serif;max-width:800px;margin:50px auto">
<h1>AutoMind Agent</h1><p>前端文件未找到。请确保 static/index.html 存在。</p>
<p>启动: <code>python -m automind.server --port 8765</code></p></body></html>"""


def main():
    import argparse
    parser = argparse.ArgumentParser(description="AutoMind Web Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    print(f"""
╔══════════════════════════════════════════════════╗
║         AutoMind Web UI v0.3.0                    ║
║                                                  ║
║  打开浏览器访问: http://{args.host}:{args.port}              ║
║  API 文档:      http://{args.host}:{args.port}/docs         ║
║                                                  ║
║  按 Ctrl+C 停止服务器                             ║
╚══════════════════════════════════════════════════╝
""")
    import uvicorn
    uvicorn.run("automind.server:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
