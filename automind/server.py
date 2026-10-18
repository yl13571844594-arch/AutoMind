"""AutoMind Web Server — FastAPI REST + WebSocket API。

启动方式:
    python -m automind.server              # 默认 http://localhost:8765
    python -m automind.server --port 8080  # 自定义端口
    python -m automind.server --host 0.0.0.0 --port 8765  # 允许外部访问
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import secrets
import sys
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:
    print("FastAPI not installed. Install with: pip install fastapi uvicorn")
    sys.exit(1)

from automind import __version__
from automind.core.chat_window import chat_history_window
from automind.core.http_guard import host_allowed as _host_allowed
from automind.core.logging import get_logger
from automind.server_web import apply_security_headers as _apply_security_headers
from automind.server_web import dist_asset_refs as _dist_asset_refs
from automind.server_web import ensure_web_mimetypes as _ensure_web_mimetypes
from automind.server_web import versioned_html as _versioned_html

logger = get_logger("automind.server")

STATIC_DIR = Path(__file__).parent / "static"

# v1.11.4：注册表里 .js 被改成 text/plain 的机器上，界面脚本会被浏览器拒绝执行
_ensure_web_mimetypes()

app = FastAPI(title="AutoMind Agent", version=__version__, docs_url="/docs")

# §14.10 前端工程化：拆分后的 css/js 模块经 /static 伺服（无需鉴权，同 CDN 语义）
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 商用安全：可选鉴权 + CORS 收紧（默认本地放开，配置后生效）
_AUTH_TOKEN = os.environ.get("AUTOMIND_AUTH_TOKEN", "")

# CORS（v1.4.5 收紧）—— 此前默认 `*` 且 allow_credentials=True。
# 这个组合比看上去危险得多：Starlette 在 credentials 打开时**不会**回 `*`，
# 而是把请求方的 Origin 原样回显。实测任意站点（如 https://evil.example.com）
# 发来的请求都会拿到 `Access-Control-Allow-Origin: <该站点>` +
# `Allow-Credentials: true` —— 即用户浏览的任何网页都能跨站调用本机 AutoMind
# 并读取响应；本地默认又不开鉴权，等于把 /api/run 之类的接口交了出去。
#
# 现在的默认：只信任本机来源（同源页面本就不需要 CORS，这里放开主要是给
# 本地开发的 vite dev server 用）。显式配置 AUTOMIND_CORS_ORIGINS 才放行其它来源；
# 若其中含 `*`，则强制关闭 credentials（浏览器规范本就不允许两者并用）。
_LOCAL_ORIGIN_RE = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"


def _ws_origin_ok(origin: str) -> bool:
    """WebSocket 的 Origin 是否属于本机页面（与 CORS 用同一套判断）。"""
    import re as _re
    return bool(_re.match(_LOCAL_ORIGIN_RE, origin))
_cors_origins = [o.strip() for o in
                 os.environ.get("AUTOMIND_CORS_ORIGINS", "").split(",") if o.strip()]

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

if "*" in _cors_origins:
    # 用户明确要求通配：允许，但绝不与 credentials 并用
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_credentials=False,
        allow_methods=["*"], allow_headers=["*"],
    )
elif _cors_origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=_cors_origins, allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware, allow_origin_regex=_LOCAL_ORIGIN_RE, allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )


def _auth_token() -> str:
    """鉴权令牌：优先环境变量，其次配置文件。空字符串表示不鉴权（本地模式）。"""
    return _AUTH_TOKEN or _read_config().get("auth_token", "")


def _admin_token() -> str:
    """管理员令牌（v1.7.3）：高危端点的第二把钥匙。空 = 未配置。"""
    return (os.environ.get("AUTOMIND_ADMIN_TOKEN", "").strip()
            or str(_read_config().get("admin_token", "") or "").strip())


#: 「管理动作」端点前缀 —— 与只读/执行动作**不同权**。
#:
#: 为什么需要这一层：此前**任何**持有访问令牌的人都能改 ``api_base``（把模型出口
#: 指向攻击者，此后每一次对话的提示词与 API Key 都从那儿过）、都能加 MCP 服务器
#: （= 以本进程身份启动任意进程）、都能加载插件/技能（= 任意代码）、都能触发静默
#: 更新与清空审计。而访问令牌的用途本该只是"让 IDE / 前端连上来聊天跑任务"。
#:
#: **边界是刻意收窄的**：只拦"能劫持平台本身、或能把密钥与代码带出去"的动作。
#: 工作区切换、团队看板、文件回滚、知识库上传这些是**内容操作**（改的是用户的
#: 数据，而 Agent 本来就能做同样的事）—— 拦下来只会让远程协作的同事处处 403，
#: 却换不到对应的安全收益。分权的目的是"把能一击致命的动作关在本机"，
#: 不是"把所有写操作都锁起来"（全量回归当场把这条教训打了出来：
#: tests/server/test_v09_features.py 与 test_workspace_and_rollback.py 一起红了）。
#:
#: 判定放在中间件里而不是逐个端点加装饰器：135 个端点里漏一个就是漏洞，
#: 而漏掉的那种情况不会有任何测试告诉你。集中一张表，新增端点时只需在此登记。
_ADMIN_PATH_PREFIXES: tuple[str, ...] = (
    "/api/config",            # api_base / API Key / 审批模式 / 工作目录
    "/api/mcp",               # 任意进程启动
    "/api/plugins",           # 加载插件 = 执行任意代码
    "/api/skills",            # 加载/导入技能 = 执行任意代码
    "/api/update/apply",      # 静默下载并安装
    "/api/tools/toggle",      # 关掉安全相关工具
    "/api/tools/reload",      # 重新扫描连接器目录（= 重新加载用户代码）
    "/api/experts/activate",  # 激活的专家提示词会注入到**所有**任务里
    "/api/experts/install",   # 从官方源安装专家（同样是注入共享提示词）
    "/api/approvals",         # 外部审批回执（v1.7.3 第 8 项）
    "/api/workflow/run",      # 工作流可携带任意工具参数（v1.7.3 第 9 项）
    "/api/workflow/runs",     # 取消 / 续跑（续跑会真的执行、花钱）/ 忽略（v1.8.0）
    "/api/workflow/save",     # 写入 workflows/ = 以后可被运行的流程定义（v1.8.0）
    "/api/workflow/delete",
    "/api/workflow/triggers",  # 启停触发器 / 重置 webhook 令牌 / 立即触发（v1.9.0）
    "/api/workflow/secrets",   # 凭据保管（v1.10.0，企业版）
    "/api/workflow/concurrency",  # 并发配额（v1.10.0，企业版）
    "/api/workflow/templates",    # 工作流模板库（v1.10.0，专业版；"用模板新建"会写 workflows/）
    "/api/workflow/samples",      # 安装内置样板 = 写 workflows/（v1.11.5）
    "/api/eval/run",          # 评测会真的执行任务、花钱
    "/api/license",           # 上传扩展包 = 加载代码；激活许可证改变全站能力（v1.11.1）
    "/api/bots",              # IM 机器人 = 让外部聊天能驱动本机 Agent（v1.11.6；回调除外）
)

#: 删除审计是"管理动作里的管理动作"：单独列出，便于测试与文档引用
_ADMIN_EXACT_PATHS: tuple[str, ...] = ("/api/audit",)


def _is_admin_path(path: str) -> bool:
    """该路径是否是管理动作端点（只看路径，方法在中间件里另判）。"""
    if path.startswith("/api/bots/hook/"):
        return False            # 平台回调：凭机器人自己的令牌与平台签名，不是管理动作
    if path in _ADMIN_EXACT_PATHS:
        return True
    return any(path == p or path.startswith(p + "/") for p in _ADMIN_PATH_PREFIXES)


#: v1.10.0：企业版开了"按工作流 RBAC"时，这些工作流动作对 **SSO 登录的用户**不再要求
#: 管理员令牌，而是交给端点里的 RBAC 判定（谁能运行 / 编辑 / 挂触发器哪一份工作流）。
#: 凭据与并发配额仍然只有管理员能动 —— 它们不是"某一份工作流"的事。
_WF_RBAC_DELEGATED: tuple[str, ...] = (
    "/api/workflow/run", "/api/workflow/runs", "/api/workflow/save", "/api/workflow/delete",
    "/api/workflow/triggers",
)


def _wf_rbac_delegated(request, path: str) -> bool:
    """该请求的管理员门是否交给按工作流 RBAC（企业版 + SSO 身份）。"""
    if not any(path == p or path.startswith(p + "/") for p in _WF_RBAC_DELEGATED):
        return False
    if not _edition.has_feature("workflow_governance"):
        return False
    return _request_identity(request).get("source") == "sso"


def _identity_from_token(token: str, *, local: bool) -> dict:
    """请求方是谁（v1.10.0）：SSO 会话 → 会话里的用户 / 角色 / 团队；否则按本机或令牌持有人。

    没有 SSO 时，能走到这里的请求要么来自本机、要么持有平台令牌 —— 两者本来就拥有
    全部权限，因此身份记为 admin（审计里写清来源：local / token）。
    """
    sso = _edition.get_feature("sso_ldap")
    if token and sso is not None and hasattr(sso, "session_of"):
        try:
            sess = sso.session_of(token)
        except Exception:                               # pragma: no cover - 扩展兜底
            sess = None
        if sess:
            return {"user": str(sess.get("user") or ""), "role": str(sess.get("role") or ""),
                    "team": str(sess.get("team") or sess.get("role") or "default"),
                    "source": "sso"}
    return {"user": "local" if local else "token-holder", "role": "admin", "team": "default",
            "source": "local" if local else "token"}


def _request_identity(request) -> dict:
    sent = request.headers.get("authorization", "")
    token = sent[7:].strip() if sent.lower().startswith("bearer ") else \
        request.query_params.get("token", "")
    return _identity_from_token(token, local=_is_local_request(request))


def _approver_matches(required: list | None, identity: dict | None) -> tuple[bool, str]:
    """这位审批人是否满足 human 步骤的 approvers 要求（v1.10.0，R-303）。"""
    if not required:
        return True, ""
    who = identity or {}
    if who.get("role") == "admin" and who.get("source") in ("local", "token"):
        # 没有登录身份的平台管理员（本机 / 持平台令牌）：本来就拥有全部权限，可以代任何角色审批
        # （审计里如实记下是谁）。要真正的多人分级审批，请启用 SSO —— 那时每个人以自己的身份批
        return True, ""
    for item in required:
        kind, _, name = str(item).partition(":")
        if (kind == "role" and who.get("role") == name) or (kind == "user" and who.get("user") == name):
            return True, ""
    return False, (f"这一级审批需要 {'、'.join(map(str, required))}，"
                   f"当前身份是 {who.get('user') or '未知'}（角色 {who.get('role') or '未知'}）")


def _admin_header_token(request) -> str:
    """从请求里取管理员令牌：``X-Admin-Token`` 优先，其次 Bearer。"""
    got = request.headers.get("x-admin-token", "").strip()
    if got:
        return got
    sent = request.headers.get("authorization", "")
    return sent[7:].strip() if sent.lower().startswith("bearer ") else ""


def _admin_ok(request) -> tuple[bool, str]:
    """管理动作是否被允许。返回 ``(允许, 拒绝原因)``。

    规则（按优先级）：

    1. **本地即管理台**：请求来自回环地址 —— 桌面版与"我就在这台机器上开浏览器"
       的场景不该被自己的安全策略挡住，而回环访问本来就等价于本机权限。
    2. **配了管理员令牌**：远程请求必须带对的管理员令牌（``X-Admin-Token``）。
    3. **没配管理员令牌**：远程请求**一律拒绝**，并明确告诉运维该怎么开 ——
       fail-closed。这里不能"没配就放行"：那等于安全策略在默认配置下失效，
       而默认配置恰恰是最多人用的那一份。
    """
    if _is_local_request(request):
        return True, ""
    admin = _admin_token()
    if not admin:
        return False, (
            "该操作属于**管理动作**（改配置 / 加 MCP / 加载插件技能 / 触发更新 / "
            "回执审批 / 清空审计），默认只允许在本机执行。远程执行请设置环境变量 "
            "AUTOMIND_ADMIN_TOKEN，并在请求头带上 X-Admin-Token。")
    provided = _admin_header_token(request)
    if provided and secrets.compare_digest(provided, admin):
        return True, ""
    logger.warning("admin_denied", path=request.url.path,
                   client=request.client.host if request.client else "?")
    return False, "未授权：该操作需要管理员令牌（请求头 X-Admin-Token）。"


# 扩展令牌校验器（扩展契约 v1）：企业版 SSO 等通过 register_token_validator
# 注册回调，静态令牌不匹配时逐个询问 —— 会话令牌等动态凭据由此通行。
_token_validators: list = []


def _register_token_validator(fn) -> None:
    if callable(fn) and fn not in _token_validators:
        _token_validators.append(fn)


def _token_ok(provided: str, static_token: str) -> bool:
    # 常量时间比较：令牌比较不该泄露"前几位对上了"这种信息
    if provided and static_token and secrets.compare_digest(provided, static_token):
        return True
    for v in _token_validators:
        try:
            if v(provided):
                return True
        except Exception:
            continue
    return False


def _host_ok(request) -> tuple[bool, str]:
    """Host 头准入（v1.7.4）—— 判定逻辑与理由见 ``automind/core/http_guard.py``。

    这里只做一件事：把请求头里的 Host 交给守卫。**优先看 ``Host`` 而不是
    ``X-Forwarded-Host``** —— 后者是客户端可任意伪造的头，拿它当判据等于没有判据。
    """
    return _host_allowed(request.headers.get("host", ""))


@app.middleware("http")
async def _auth_middleware(request, call_next):
    path = request.url.path

    # Host 判定必须在令牌之前（v1.7.4）：它挡的是"外部域名被解析到本机"的
    # DNS 重绑定 —— 那种请求的客户端 IP 是回环、Host 是攻击者域名，
    # 而下游的管理动作放行、目录浏览放行、令牌端点全都在拿"回环 IP"当信任依据。
    # 只挡 /api/ 会漏掉"重绑定页面直接开首页 → 前端脚本自己调 /api"这条路：
    # 首页不是 /api/，但它的脚本是。故放在最前面，按整站判。
    # WS 不受影响（本中间件只处理 HTTP；WS 有自己的 Origin 判定）。
    host_ok, host_why = _host_ok(request)
    # v1.11.6：IM 平台回调经公网域名 / 内网穿透进来，Host 必然不是本机名。它不依赖
    # "回环来源"这类信任（凭的是 URL 里每个机器人自己的随机令牌 + 平台签名），放行。
    if not host_ok and path.startswith("/api/bots/hook/"):
        host_ok = True
    if not host_ok:
        logger.warning("host_denied", host=request.headers.get("host", "")[:120],
                       client=request.client.host if request.client else "?")
        return JSONResponse({"error": host_why}, status_code=403)

    token = _auth_token()
    # 仅保护 /api/*；放开首页、文档、健康检查
    # /api/auth/login 放行：SSO 登录本身不能要求已持有令牌；
    # /v1/*（OpenAI 兼容，IDE 集成）与 /api/* 同等保护。
    # /metrics（v1.7.3）：与 /api/* 同等保护 —— 任务数与失败率属于运行信息，
    # 绑到 0.0.0.0 时不该对全网公开。Prometheus 抓取可用 ?token= 或 Authorization 头。
    # /api/workflow/hooks/*（v1.9.0，入站 webhook 触发器）放行：调用方是外部系统，
    # 凭的是**每个触发器各自的令牌**（由专业版特性校验，错了一律 401）—— 让它再持有
    # 整个平台的访问令牌，等于为了触发一条流程把全部接口的钥匙交出去。
    if (token and (path.startswith("/api/") or path.startswith("/v1/") or path == "/metrics")
            and not path.startswith("/api/health")
            and not path.startswith("/api/workflow/hooks/")
            and not path.startswith("/api/bots/hook/")
            and path != "/api/auth/login"):
        sent = request.headers.get("authorization", "")
        provided = sent[7:] if sent.lower().startswith("bearer ") else \
            request.query_params.get("token", "")
        if not _token_ok(provided, token):
            logger.warning("auth_denied", path=path,
                           client=request.client.host if request.client else "?")
            return JSONResponse({"error": "未授权：请提供有效的访问令牌"}, status_code=401)

    # 管理动作的第二道门（v1.7.3）：与只读/执行动作不同权。
    # 独立于上面的令牌检查 —— 即使没配访问令牌（本地模式），管理动作仍然
    # 只对回环开放；反过来，"有访问令牌"也不再等于"能改平台配置"。
    if (_is_admin_path(path) and request.method in ("POST", "PUT", "PATCH", "DELETE")
            and not _wf_rbac_delegated(request, path)):
        allowed, why = _admin_ok(request)
        if not allowed:
            return JSONResponse({"error": why}, status_code=403)

    # 速率限制（§14.11-4）：任务执行入口 + OpenAI 兼容补全入口，按客户端 IP 计数
    if (_rate_limiter.enabled and request.method == "POST"
            and (path in ("/api/run", "/v1/chat/completions")
                 or path.startswith("/api/workflow/hooks/")
                 or path.startswith("/api/bots/hook/"))):
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
#: 本次运行（进程生命周期）的累计用量。v1.11.10 起补记提示缓存与上下文峰值；
#: /api/status 直接透传这个 dict，所以新增键会"自然"出现在状态里，不必改结构。
_token_totals = {
    "prompt": 0, "completion": 0, "total": 0, "tasks": 0,
    # 提示缓存：cached = 命中复用的输入 token，input = 含命中部分的总输入（分母）
    "cached": 0, "input": 0,
    # 调用次数与其中命中缓存的次数：回答"几次调用里命中几次"
    "calls": 0, "cache_hit_calls": 0,
    # 其中**上报了**缓存口径的调用次数（0 = 该模型不提供缓存统计，
    # 命中率应显示"—"而不是 0%）
    "cache_reported_calls": 0,
    # 最近一次任务/调用的单请求输入峰值（上下文占用的 used），不是累加值
    "peak_prompt_tokens": 0,
}
_running_tasks = {"count": 0}  # 并发任务计数（资源保护）
#: v1.11.6：服务端内部调用 /api/run 时按会话登记的回调（IM 机器人：事件回传 + 聊天内审批）
_run_hooks: dict[str, dict] = {}
_MAX_CONCURRENT = int(os.environ.get("AUTOMIND_MAX_CONCURRENT", "8"))
#: 正在等待人工审批的任务数（这些任务已把并发槽让出来，单独计数便于诊断）
_approval_waiting = {"count": 0}
#: 待决审批：approval_id → (Future, client_id, session_id, tool)。
#: 记 client_id 是为了在**连接断开时立刻终结**该连接上的审批等待 ——
#: 否则回调要一直挂到超时才返回，用户在别的标签页里看着任务"没反应"。
_pending_approvals: dict[str, dict] = {}
#: WebSocket 连接 → 连接方身份（v1.10.0：按角色审批要知道"是谁点的批准"）
_ws_identity: dict[str, dict] = {}
#: 「询问」模式下等待人工审批的上限（秒）。超时按配置处置，并**明确告知前端**。
#: 前端拿它做倒计时，用户能看见还剩多久，而不是对着一个看似能永远等的弹窗。
#: v1.6.4 起可在 ExecutionConfig 里显式配置
#: （``approval_timeout_seconds`` / ``approval_timeout_action``），
#: 环境变量 ``AUTOMIND_APPROVAL_TIMEOUT`` 仍然作为兜底。
_APPROVAL_TIMEOUT_S = int(os.environ.get("AUTOMIND_APPROVAL_TIMEOUT", "300"))
_START_TIME = time.time()


def _approval_timeout_seconds(agent: Any) -> float:
    """本次任务审批等待上限：ExecutionConfig 优先，环境变量兜底。"""
    try:
        v = float(getattr(agent.config.execution, "approval_timeout_seconds", 0) or 0)
        if v > 0:
            return v
    except Exception:
        pass
    return float(_APPROVAL_TIMEOUT_S)


def _approval_timeout_action(agent: Any) -> str:
    """超时处置：``reject``（默认，fail-closed）或 ``approve``。"""
    try:
        a = str(getattr(agent.config.execution, "approval_timeout_action", "") or "").lower()
        if a in ("approve", "reject"):
            return a
    except Exception:
        pass
    return "reject"


def _usage_record_fields(usage: Any) -> dict:
    """把 TokenUsage（或 LLMResponse / None）摊平成任务记录的用量字段。

    任务历史有近十处 record 构造点，以前各自手抄 tokens / prompt_tokens。
    每新增一个指标就要改十遍 —— 漏掉一处的表现是"有的记录带缓存数据、
    有的没有"，历史聚合出的命中率会莫名偏低，且没有任何报错能提示。
    统一在这个入口摊平，调用方传 None 时得到一整套 0，旧记录结构与新记录一致。
    """
    def _int(name: str) -> int:
        try:
            return int(getattr(usage, name, 0) or 0)
        except (TypeError, ValueError):
            return 0

    prompt = _int("prompt_tokens")
    completion = _int("completion_tokens")
    cached = _int("cached_tokens")
    # 老提供商只回 prompt_tokens；input_tokens 才是含缓存命中的真实输入量，
    # 命中率的分母必须用它，否则命中部分被重复计入分母、比率被压低。
    total_input = _int("input_tokens") or prompt
    # 没有 calls 字段的用量对象（如 LLMResponse）本身就代表一次调用
    calls = _int("calls")
    if usage is not None and calls <= 0 and not hasattr(usage, "calls"):
        calls = 1
    hit_calls = _int("cache_hit_calls")
    if usage is not None and cached > 0 and not hasattr(usage, "cache_hit_calls"):
        hit_calls = 1
    # 是否上报了缓存口径：TokenUsage 自己记调用次数；LLMResponse 只有布尔标记
    reported_calls = _int("cache_reported_calls")
    if not hasattr(usage, "cache_reported_calls") and getattr(
            usage, "cache_reported", False):
        reported_calls = 1
    return {
        # total 是 TokenUsage 的属性；LLMResponse 只有 total_tokens，回退加和
        "tokens": _int("total") or (prompt + completion),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "input_tokens": total_input,
        "llm_calls": calls,
        "cache_hit_calls": hit_calls,
        "cache_reported_calls": reported_calls,
        # 单请求输入峰值：TokenUsage 自己会记；只有一次调用的响应，
        # 本次输入本身就是峰值
        "peak_prompt_tokens": _int("peak_prompt_tokens") or total_input,
    }


def _accumulate_tokens(record: dict) -> None:
    """把一条任务记录（或一次 IDE 调用）的用量累进本次运行的计数器。

    v1.11.10：新增缓存与上下文指标。record 可能来自只带旧四个键的调用方
    （含既有测试的 fixture），所以 record 与 `_token_totals` 两侧都用 .get
    容错；旧记录没有 input_tokens 时回退 prompt_tokens —— 否则命中率的
    分母会凭空少一块。缓存相关字段缺失按 0 处理（= 该调用没有缓存数据）。
    """
    prompt = int(record.get("prompt_tokens", 0) or 0)
    deltas = {
        "prompt": prompt,
        "completion": int(record.get("completion_tokens", 0) or 0),
        "total": int(record.get("tokens", 0) or 0),
        "tasks": 1,
        "cached": int(record.get("cached_tokens", 0) or 0),
        "input": int(record.get("input_tokens", 0) or prompt),
        "calls": int(record.get("llm_calls", 0) or 0),
        "cache_hit_calls": int(record.get("cache_hit_calls", 0) or 0),
        "cache_reported_calls": int(record.get("cache_reported_calls", 0) or 0),
    }
    for key, delta in deltas.items():
        _token_totals[key] = int(_token_totals.get(key, 0) or 0) + delta
    # 上下文占用看"最近一次"单请求峰值：语义是覆盖，不是累加/取最大
    _token_totals["peak_prompt_tokens"] = int(
        record.get("peak_prompt_tokens", 0)
        or record.get("input_tokens", 0) or prompt)


# v1.1：任务历史持久化迁移至 SQLite（automind/core/db.py）；
# 旧 JSON 首次启动自动导入并保留原文件作备份。
from automind.core import db as _db_mod  # noqa: E402
from automind.core.paths import legacy_file as _legacy_file  # noqa: E402
from automind.core.paths import skills_dir as _skills_dir  # noqa: E402

_HISTORY_FILE = _legacy_file("task_history.json")   # 旧文件（仅迁移用）
_HISTORY_CAP = 200


def _load_task_history() -> None:
    """启动时恢复任务历史（SQLite；旧 JSON 自动一次性迁移）。"""
    try:
        db = _db_mod.get_db()
        _db_mod.migrate_json_once(
            db, "task_history", _HISTORY_FILE,
            lambda data: db.history_replace(data[-_HISTORY_CAP:])
            if isinstance(data, list) else None)
        _task_history.extend(db.history_load()[-_HISTORY_CAP:])
    except Exception as e:
        logger.warning("history_load_failed", error=str(e))


def _save_task_history() -> None:
    """全量同步内存历史到 SQLite（删除/清空后调用；追加走 _push_history）。"""
    try:
        _db_mod.get_db().history_replace(_task_history[-_HISTORY_CAP:])
    except Exception as e:
        # 静默失败 = 任务历史悄悄丢了，用户下次打开发现记录不见却无从查起
        logger.error("task_history_replace_failed",
                     count=len(_task_history), error=str(e))


def _push_history(record: dict) -> dict:
    """统一记录入口 — 按开关脱敏 output 后写入历史（§14.11-3）。"""
    if "output" in record:
        record["output"] = _maybe_redact(record["output"])
    record.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
    _task_history.append(record)
    del _task_history[:-_HISTORY_CAP]
    try:
        _db_mod.get_db().history_append(record, cap=_HISTORY_CAP)
    except Exception as e:
        logger.error("task_history_append_failed",
                     session=record.get("session_id"), error=str(e))
    return record


_load_task_history()

# 环境变量映射（单一数据源：core.provider_resolver —— 此前 config.py /
# server_store.py / 这里各写一份，加一家提供商就得改三处且极易漏）
from automind.core.provider_resolver import ENV_KEY_MAP as _ENV_KEY_MAP  # noqa: E402

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


async def _save_history_notify(sid: str, ws: Any = None) -> bool:
    """保存会话历史；失败时记日志并（有 ws 时）告诉前端。

    落库失败此前是 `except: pass` —— 聊天记录悄悄丢了，用户下次打开才发现
    对话没了。现在 store 层会重试并抛 SessionSaveError，这里负责把它变成
    用户看得见的一条提示，同时**不让它中断正在进行的任务**
    （回答已经生成出来了，不该因为存档失败就整个失败）。
    """
    from automind.server_store import SessionSaveError
    try:
        _save_session_history(sid)
        return True
    except SessionSaveError as e:
        logger.error("chat_history_persist_failed", sid=sid, error=str(e))
        if ws is not None:
            try:
                await ws.send_json({
                    "type": "history_save_failed",
                    "error": "聊天记录保存失败，本次对话可能不会留存。"
                             "请检查磁盘空间与数据目录写权限。",
                    "detail": str(e)[:300],
                })
            except Exception:
                pass
        return False


# 面向用户的模型选路提示（自动 fallback / 未配 Key），由 /api/status 展示
_llm_notice: str = ""


def _set_llm_notice(note: str) -> None:
    global _llm_notice
    _llm_notice = note or ""


def _resolve_provider_fallback(config, *, explicit: bool,
                               saved_keys: dict[str, str]) -> str:
    """当前提供商没有 Key 时，对齐到实际配了 Key 的那家。返回中文提示（无则空串）。

    Args:
        config: 待写入的 AgentConfig（原地修改 ``config.llm``）。
        explicit: 提供商是否由用户/调用方显式指定。显式指定时**不**自动改换
            —— 用户刚在设置里选了 OpenAI 却被悄悄切成 DeepSeek 更莫名其妙；
            此时只回一句提示，告诉他 Key 还没填。
        saved_keys: 配置文件中已保存的 provider → key。
    """
    from automind.core import provider_resolver as _pr

    new_provider, new_model, note = _pr.resolve(
        config.llm.provider, config.llm.model, config.llm.api_key, saved_keys)
    if not note:
        return ""
    if explicit or new_provider == config.llm.provider:
        logger.warning("llm_key_missing", provider=config.llm.provider, note=note)
        return note
    config.llm.provider = new_provider
    config.llm.model = new_model
    config.llm.api_base = ""
    config.llm.api_key = (saved_keys.get(new_provider)
                          or _pr.env_api_key(new_provider) or "")
    logger.info("llm_provider_auto_switched",
                to_provider=new_provider, to_model=new_model)
    return note


def _llm_config_for(agent, provider: str, model: str = ""):
    """按持久化配置为 (provider, model) 组装一份完整的 LLMProviderConfig。

    解析顺序与 `_rebuild_agent` 一致：模型 = 显式 > 提供商配置 > 内置默认；
    api_base 仅接受合法 URL；Key = 已保存 > 环境变量 >（同提供商时）当前值。
    采样参数（temperature / max_tokens / timeout）从当前配置继承。
    """
    from automind.core import provider_resolver as _pr

    provider = (provider or agent.config.llm.provider).lower()
    pcfg = _load_providers().get(provider, {})
    cfg = agent.config.llm.model_copy(deep=True)
    cfg.provider = provider
    cfg.model = (model or pcfg.get("model")
                 or _pr.default_model(provider) or cfg.model)
    raw_base = pcfg.get("api_base", "")
    cfg.api_base = raw_base if _valid_api_base(raw_base) else ""
    key = _load_api_keys().get(provider) or _env_api_key(provider)
    if not key and provider == agent.config.llm.provider:
        key = agent.config.llm.api_key
    cfg.api_key = key or ""
    return cfg


def _apply_mode_model(agent, interaction: str):
    """按交互模式切换到对应模型（不改写全局默认配置）。返回最新 agent。

    只换 LLM，不重建整个 Agent —— 重建会重新 `AgentConfig.auto_load`、
    重扫项目索引、重建 ChromaDB、重注册全部工具与技能，用户在 Web 上点一下
    模式切换就要卡 2~3 秒，而真正变化的只有一个模型名。
    """
    mm = _mode_model(interaction)
    if not mm:
        # 该模式未单独配置 → 回退到全局默认（provider+model）
        active = _load_active()
        mm = {"provider": active.get("provider") or agent.config.llm.provider,
              "model": active.get("model") or agent.config.llm.model}
    cur = (agent.config.llm.provider, agent.config.llm.model)
    if (mm["provider"], mm["model"]) != cur:
        agent.switch_llm(_llm_config_for(agent, mm["provider"], mm["model"]))
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

    provider_arg = provider     # 调用方是否显式指定（决定能否自动换提供商）
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

    # 3.5) 默认模型与实际 Key 对齐（§开箱即用）
    # 代码默认 openai/gpt-4o，而用户往往只配了 DeepSeek 一家的 Key —— 此前的
    # 结果是启动即 llm_init_failed，界面只说"模型连接失败"，用户根本不知道
    # 要去设置里换一家。这里在**用户尚未显式选过提供商**时自动对齐到配得起
    # 的那家，并留一句中文提示给 /api/status 展示。
    _set_llm_notice(_resolve_provider_fallback(
        config, explicit=bool(provider_arg or active.get("provider")),
        saved_keys=api_keys))

    # 4) 底层执行引擎
    interaction = active.get("interaction", "chat")
    exec_mode = _INTERACTION_TO_EXECUTION.get(interaction, "plan_and_execute")
    config.execution.mode = exec_mode

    # 5) 审批模式
    config.execution.approval_mode = active.get("approval_mode", "auto")

    # 6) 自主闭环开关（默认全开，用户可在设置中关闭）
    ap = _read_config().get("autopilot", {})
    for flag in ("auto_review", "auto_verify", "auto_test",
                 "parallel_execution", "subtask_cache",
                 # v1.6.4：这些也在界面上可调，重启/重建后必须恢复，
                 # 否则"我在设置里关了隔离/调大了超时"重启就白设了
                 "isolate_workspace", "terminal_background_enabled",
                 "release_slot_on_approval_wait", "trace_enabled"):
        if flag in ap:
            setattr(config.execution, flag, bool(ap[flag]))
    # 与设置面板共用同一份清单（_AUTOPILOT_NUMS）—— 此前两处各列一遍，
    # 面板上新加的参数重启后不恢复；整数型按整数写回，不再把 8 存成 8.0
    for num_flag in _AUTOPILOT_NUMS:
        if num_flag in ap:
            try:
                v = float(ap[num_flag])
                setattr(config.execution, num_flag,
                        int(v) if num_flag in _AUTOPILOT_INT_NUMS else v)
            except (TypeError, ValueError):
                pass
            except Exception as e:      # 旧配置里的非法值不该拖垮启动
                logger.warning("autopilot_restore_failed", key=num_flag, error=str(e))
    for str_flag in ("approval_timeout_action", "write_conflict_policy"):
        if str_flag in ap and isinstance(ap[str_flag], str):
            setattr(config.execution, str_flag, ap[str_flag])

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
        except Exception as e:
            # 用户在界面上禁用了工具却没生效，是安全相关的错觉，必须记
            logger.warning("disable_tool_failed", tool=tname, error=str(e))
    # 恢复已启用的插件（内置 + 用户）—— 此前"启用"只活在当前进程的内存里，
    # 一次改模型触发的重建、或重启服务，插件就悄悄失效了
    _restore_plugins(_agent)
    # 重连已保存的 MCP 服务器
    _reconnect_mcp_servers(_agent)
    # 旧的会话克隆挂在上一代 agent 的工具/记忆上，必须一并作废
    _drop_session_clones()
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
_SKILLS_DIR = _skills_dir()


#: v1.11.6 之前随包分发的内置插件（没有 known_plugins 记录的旧配置按"都见过"处理）
_LEGACY_BUILTIN_PLUGINS = ("cost_tracker", "hello_hooks", "pii_guard", "task_notify")


def _restore_plugins(agent) -> None:
    """把插件的启用状态对齐到配置（既补加载，也补卸载）。

    只"补加载"是不够的：内置插件在 `AutoMindAgent.__init__` 里默认全部自动
    加载，所以用户在界面上关掉一个内置插件后，下一次 Agent 重建（改个模型就
    会触发）又会把它悄悄装回来 —— 开关看起来能点，实际按不住。
    """
    cfg = _read_config()
    if "enabled_plugins" not in cfg:
        return                              # 用户从未调整过 → 保持默认（内置全开）
    want = set(cfg.get("enabled_plugins") or [])
    metas = agent.plugin_manager.discover()  # 先建立元信息缓存，否则 load() 找不到
    # v1.11.6：新版本带来的**新内置插件**默认开启。此前 enabled_plugins 一旦存在，
    # 名单外的一律卸载 —— 升级后新增的内置插件（如 net_resilience）永远不会生效。
    # 用 known_plugins 记"用户已经见过哪些内置插件"：见过且不在名单里 = 用户关掉的，保持关闭。
    builtins = {m.name for m in metas if agent.plugin_manager.is_builtin(m)}
    known = set(cfg.get("known_plugins") or _LEGACY_BUILTIN_PLUGINS)
    fresh = builtins - known
    if fresh or "known_plugins" not in cfg:
        want |= fresh
        cfg["known_plugins"] = sorted(known | builtins)
        cfg["enabled_plugins"] = sorted(want)
        _write_config(cfg)

    loaded, unloaded = [], []
    for name in sorted(want - set(agent.plugin_manager.loaded_names())):
        try:
            if agent.plugin_manager.load(name) is not None:
                loaded.append(name)
            else:
                logger.warning("plugin_restore_missing", plugin=name)
        except Exception as e:
            logger.warning("plugin_restore_failed", plugin=name, error=str(e))
    for name in sorted(set(agent.plugin_manager.loaded_names()) - want):
        try:
            agent.plugin_manager.unload(name)
            unloaded.append(name)
        except Exception as e:
            logger.warning("plugin_disable_failed", plugin=name, error=str(e))

    if loaded or unloaded:
        agent.apply_plugin_hooks()
        logger.info("plugins_reconciled", loaded=loaded, unloaded=unloaded)


def _persist_enabled_plugins(agent) -> None:
    """把当前已加载的插件名单落盘（下次启动/重建自动恢复）。"""
    cfg = _read_config()
    cfg["enabled_plugins"] = agent.plugin_manager.loaded_names()
    _write_config(cfg)


def _reconnect_mcp_servers(agent) -> None:
    """从配置恢复 MCP 服务器配置（连接按需在异步上下文中进行，避免阻塞重建）。"""
    from automind.tools.mcp_registry import MCPServerConfig

    for s in _read_config().get("mcp_servers", []):
        try:
            agent.mcp_registry.add_server(MCPServerConfig(
                name=s["name"], command=s.get("command", ""),
                args=s.get("args", []), url=s.get("url", ""),
                transport=s.get("transport", "stdio"),
                env=s.get("env") if isinstance(s.get("env"), dict) else {},
            ))
        except Exception as e:
            logger.warning("mcp_server_restore_failed",
                           server=s.get("name"), error=str(e))


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
    _janitor = asyncio.create_task(_workspace_janitor())
    # v1.8.0（D5）：上次进程退出时没跑完的工作流运行 → 标成 interrupted，
    # 只在界面上提示可续跑，**不自动续跑**（续跑会真的执行、花钱）
    stale: list[str] = []
    try:
        stale = _wf_store().recover_stale(set(_wf_live))
        if stale:
            logger.info("workflow_runs_interrupted_on_restart", runs=stale)
    except Exception as e:
        logger.warning("workflow_recover_failed", error=str(e))
    # v1.9.0（专业版 workflow_triggers）：定时 / webhook 触发器调度；被重启打断的
    # **触发运行**按工作流的 on_restart 处置（resume 自动续跑 / notify 只通知，R-208）
    _wf_trig = _edition.get_feature("workflow_triggers")
    if _wf_trig is not None:
        try:
            _wf_trig.start()
            if stale and hasattr(_wf_trig, "on_restart"):
                records = [r for r in (_wf_store().load(i) for i in stale) if r]
                await _maybe_await(_wf_trig.on_restart(records))
        except Exception as e:
            logger.warning("startup_workflow_triggers_failed", error=str(e))
    # v1.11.7：IM 机器人长连接（本机主动连平台，不需要公网地址）
    if os.environ.get("AUTOMIND_BOTS_AUTOSTART", "1") != "0":
        try:
            _bots_manager().start()
        except Exception as e:
            logger.warning("startup_bots_failed", error=str(e))
    yield
    _janitor.cancel()
    if _bots_mgr is not None:
        try:
            await _bots_mgr.stop()
        except Exception as e:                       # pragma: no cover - 退出旁路
            logger.warning("shutdown_bots_failed", error=str(e))
    logger.info("server_shutdown")
    # 退出前取消仍在跑的工作流：各自的收尾会把状态记成 interrupted（可续跑）
    for _live in list(_wf_live.values()):
        _t = _live.get("task")
        if _t is not None and not _t.done():
            _t.cancel()
    _wf_tasks = [lv["task"] for lv in _wf_live.values()
                 if lv.get("task") is not None and not lv["task"].done()]
    if _wf_tasks:
        with suppress(Exception):
            await asyncio.wait(_wf_tasks, timeout=5.0)
    # 出站事件排空（v1.7.3 第 8 项）：不 flush 的话，进程退出时队列里
    # 还没发出去的事件会静默消失 —— 而"任务完成了"这种通知丢一次，
    # 客户侧的自动化流程就会永远少一个触发点。
    try:
        from automind.core import webhooks

        if webhooks.stats().get("enabled"):
            ok = await webhooks.flush(timeout=5.0)
            logger.info("webhook_flushed", complete=ok, **{
                k: v for k, v in webhooks.stats().items()
                if k in ("delivered", "failed", "dropped")})
        # 排空之后连后台投递协程一起停掉：只 flush 的话协程还挂在事件循环里，
        # 收尾时又要走"取消一个正在等待的后台协程"这条脆弱的路
        # （py3.11 上真挂过，见 core/webhooks.py 的 _idle 注释）。
        await webhooks.aclose(timeout=1.0)
    except Exception as e:
        logger.warning("webhook_flush_failed", reason=str(e))
    if _sched is not None:
        try:
            _sched.stop()
        except Exception as e:
            logger.warning("shutdown_scheduler_failed", error=str(e))
    if _wf_trig is not None:
        try:
            _wf_trig.stop()
        except Exception as e:
            logger.warning("shutdown_workflow_triggers_failed", error=str(e))
    _pool = _edition.get_feature("session_pool")
    if _pool is not None:
        try:
            await _pool.aclose_all()  # 释放全部会话 Agent（§7.4）
        except Exception as e:
            logger.warning("shutdown_pool_failed", error=str(e))
    for _clone in list(_session_clones.values()):   # 社区版轻量克隆的 LLM 连接
        try:
            await _clone.close()
        except Exception as e:
            logger.warning("shutdown_clone_failed", error=str(e))
    _session_clones.clear()
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
from automind.core import observability as _observability  # noqa: E402


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


# ── 社区/专业版：轻量会话克隆（§7.4 的免费档实现）─────────────────
# 没有企业版会话池时，此前**所有并发任务共用同一个全局 Agent**：
# `_interaction`（交互模式）、`_mode`、`context_mgr`（上下文消息）、
# `_current_plan`（规划状态）、`llm.usage`（token 计数）全是共享可变状态。
# 开两个标签页同时跑，就会出现「A 的对话模式被 B 的循环编程覆盖」
# 「两边上下文互相串」「token 数被对方 reset() 清零」，而且一声不吭。
# 克隆共享重资源（工具/技能注册表、记忆库、项目索引），只独享执行态，
# 因此代价是毫秒级，不重扫项目也不重建 ChromaDB。
_session_clones: OrderedDict[str, Any] = OrderedDict()
_SESSION_CLONE_MAX = 32


def _drop_session_clones() -> None:
    """全局 agent 被重建后，旧克隆全部作废（其 LLM 连接稍后异步关闭）。"""
    stale = list(_session_clones.values())
    _session_clones.clear()
    for a in stale:
        _close_clone_later(a)


def _clone_is_busy(agent) -> bool:
    """该会话克隆是否**正在跑任务**（v1.7.5）。

    为什么需要：LRU 淘汰到上限时会 ``close()`` 掉最旧的克隆，而 ``close()``
    会关闭它独享的 LLM 客户端 —— 如果那个会话的任务还在飞行中，表现是
    "任务毫无征兆地报连接错误"。这种事只在 >32 个活跃会话时才发生，
    因此极难复现，也更值得在源头挡掉。

    判据取 ReAct 执行器的 ``running`` 标志（工作/编程/对话增强路径都经过它）。
    计划路径没有等价的长期标志，这里不假装覆盖 —— 宁可如实少挡，
    也不要用一个猜出来的条件把淘汰逻辑弄成"有时不淘汰、有时淘汰错人"。
    """
    ex = getattr(agent, "react_executor", None)
    # v1.8.0：工作流运行用的克隆不经过 agent.react_executor（agent 步骤自建执行器），
    # 由运行管理器显式挂一个忙标记，防止运行途中被 LRU 淘汰、LLM 连接被关掉
    return bool((ex is not None and getattr(ex, "running", False))
                or getattr(agent, "_workflow_busy", False))


def _close_clone_later(agent) -> None:
    """尽力释放克隆独享的 LLM 连接；没有事件循环时静默跳过。"""
    try:
        asyncio.get_running_loop().create_task(agent.close())
    except RuntimeError:
        pass
    except Exception as e:
        logger.warning("session_clone_close_failed", error=str(e))


def _acquire_run_agent(base_agent, sid: str):
    """返回本次任务应使用的 Agent（执行态与其它会话隔离）。

    企业版会话池启用时：走池（带配额、超时回收与跨进程语义）；
    否则：按会话缓存一个轻量克隆，同一会话的多轮复用同一实例，
    不同会话彼此隔离。全局 agent 只作为「配置模板」，不再直接执行任务。
    """
    if _pool_enabled():
        a = _edition.get_feature("session_pool").acquire(sid)
    else:
        a = _session_clones.get(sid)
        if a is None or getattr(a, "_clone_origin", None) is not base_agent:
            if a is not None:
                _close_clone_later(a)
            a = base_agent.clone_for_session()
            a._clone_origin = base_agent
            _session_clones[sid] = a
            while len(_session_clones) > _SESSION_CLONE_MAX:
                # v1.7.5：淘汰时**跳过正在跑任务的会话**。
                # 淘汰会 close() 掉那个克隆独享的 LLM 客户端，而它的任务可能
                # 还在飞行中 —— 表现是"任务毫无征兆地报连接错误"。这只在
                # 活跃会话超过上限（默认 32）时才发生，因此极难复现。
                # 全都在跑时宁可暂时超限：掐断正在跑的任务比多留几个克隆更糟。
                victim = next((k for k, v in _session_clones.items()
                               if not _clone_is_busy(v)), None)
                if victim is None:
                    logger.warning("session_clone_over_limit",
                                   limit=_SESSION_CLONE_MAX,
                                   live=len(_session_clones),
                                   reason="所有会话都有任务在跑，暂不淘汰")
                    break
                _close_clone_later(_session_clones.pop(victim))
        # 最近使用的会话排到最后（LRU 淘汰时先丢最旧的）。dict 子类可能没有
        # move_to_end，缺了也不影响正确性 —— 不能因为"排序失败"把任务搞挂。
        try:
            _session_clones.move_to_end(sid)
        except AttributeError:
            pass
    a._interaction = base_agent._interaction
    a._mode = base_agent._mode
    a.approval_callback = getattr(base_agent, "approval_callback", None)
    a.event_sink = getattr(base_agent, "event_sink", None)
    # 审批模式是**用户随时会改**的设置（顶栏下拉、REST），而会话克隆在创建时
    # 就固化了当时的模式。不同步的话会出现"界面上写着「询问」，实际那个会话
    # 一直在自动放行/一直不弹窗" —— 安全设置与真实行为不一致，且毫无提示。
    _sync_approval_mode(a, base_agent)
    return a


def _sync_approval_mode(agent: Any, base_agent: Any) -> None:
    """把 base agent 的审批模式同步到会话实例（幂等，便宜的属性比较）。"""
    try:
        mode = getattr(base_agent.permissions, "approval_mode", "auto")
        if getattr(agent.permissions, "approval_mode", None) != mode:
            agent.permissions.approval_mode = mode
        agent.config.execution.approval_mode = mode
    except Exception as e:            # pragma: no cover - 配置不可读不该拖垮任务
        logger.warning("approval_mode_sync_failed", error=str(e))


# ═══════════════════════════════════════════════════════════
# REST API — 状态与配置
# ═══════════════════════════════════════════════════════════


@app.get("/api/observe/dag")
async def api_observe_dag(session_id: str = "default"):
    """当前任务的实时执行 DAG —— **社区版即可用**（只读、仅当前任务）。

    社区版语义：每个会话只保留最近一次任务的图，新任务开始即替换，不落盘、
    不提供历史与聚合。历史留存、实时看板与导出属专业版「观测中心」
    （见 /api/observe/runs、/api/observe/dashboard）。
    """
    graph = _observability.snapshot(session_id)
    return {
        "available": graph is not None,
        "edition": _edition.get_edition(),
        # 社区版为 True：前端据此显示"仅当前任务"的只读提示与升级入口
        "limited": not _edition.has_feature("observability"),
        "graph": graph,
    }


@app.get("/api/observe/traces")
async def api_observe_traces(session_id: str = "default", limit: int = 50):
    """该会话已落盘的执行轨迹清单（社区版即可用 —— 进程重启后仍可取证）。

    实时 DAG 是内存视图（重启即失），轨迹是磁盘证据（JSONL，按 run 切文件）。
    B 端排障 / SLA 举证 / 失败归因靠的是后者：能直接看出这次任务调了哪些工具、
    哪一步开始不对、模型收到了什么。
    """
    limit = max(1, min(int(limit or 50), 500))
    runs = _observability.trace_runs(session_id, limit=limit)
    stats = _observability.trace_stats()
    current = _observability.current_run_id(session_id)
    return {
        "session_id": session_id,
        "current_run": current,
        "current_path": _observability.trace_path(session_id),
        "runs": runs,
        "stats": stats,
        # 社区版不隐藏该能力（本地落盘、零额外成本），但如实标注来源
        "source": "session_trace",
    }


@app.get("/api/observe/traces/{session_id}/{run_id}")
async def api_observe_trace_read(session_id: str, run_id: str,
                                 limit: int = 200, types: str = ""):
    """读取某次运行轨迹的尾部事件（``types`` 逗号分隔可过滤，如 ``step_action``）。"""
    limit = max(1, min(int(limit or 200), 2000))
    event_types = [t.strip() for t in (types or "").split(",") if t.strip()]
    events = _observability.trace_read(session_id, run_id, limit=limit,
                                       event_types=event_types)
    path = _observability.trace_path(session_id) if run_id == _observability.current_run_id(
        session_id) else ""
    return {"session_id": session_id, "run_id": run_id, "count": len(events),
            "events": events, "path": path}


@app.get("/api/observe/traces/{session_id}/{run_id}/download")
async def api_observe_trace_download(session_id: str, run_id: str):
    """下载原始 JSONL 轨迹文件（排障时可直接发给同事/贴到工单里）。"""
    from fastapi.responses import FileResponse, JSONResponse

    from automind.core.trace import get_recorder

    path = get_recorder().path_for(session_id, run_id)
    if not path.is_file():
        return JSONResponse({"error": f"轨迹不存在：{session_id}/{run_id}"},
                            status_code=404)
    return FileResponse(str(path), media_type="application/x-ndjson",
                        filename=f"{session_id}-{run_id}.jsonl")


@app.get("/api/health")
async def api_health():
    """健康检查（无需鉴权）— 供部署监控与负载均衡探活。

    ``running_tasks`` 只计**正在执行**的任务数；等待人工审批的任务已按配置
    让出并发槽，单独以 ``approval_waiting`` 暴露 —— 两者相加才是"占用中的
    会话数"，监控据此可区分"在算"和"在等人"。
    """
    from automind.core.paths import describe as _paths_describe
    return {
        "status": "ok", "version": app.version,
        "edition": _edition.get_edition(),
        "auth_required": bool(_auth_token()),
        "running_tasks": _running_tasks["count"],
        "approval_waiting": _approval_waiting["count"],
        "approval_pending": len(_pending_approvals),
        "max_concurrent": _MAX_CONCURRENT,
        "uptime_s": round(time.time() - _START_TIME, 1),
        "paths": _paths_describe(),
    }


def _readiness_checks() -> dict[str, dict]:
    """逐项探活（v1.7.3）：把"进程还在"与"真的能干活"分开。

    此前 ``/api/health`` 无论数据库打不开、磁盘满了还是 LLM 没配，都返回
    ``status: ok`` —— 编排系统据此把流量导进来，用户拿到的是一个个失败任务。
    这里逐项去**真的碰一下**依赖（写测试文件、打开 SQLite、看磁盘余量）。
    """
    import shutil

    checks: dict[str, dict] = {}

    # 1) 数据目录可写 —— 会话、轨迹、检查点都写在这里
    try:
        from automind.core.paths import data_dir

        d = Path(data_dir())
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".readiness_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        checks["data_dir"] = {"ok": True, "path": str(d)}
    except Exception as e:
        checks["data_dir"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 2) SQLite 可读写 —— 任务历史 / 会话 / 知识库都在里面
    try:
        from automind.core.db import get_db

        db = get_db()
        db.kv_set(".readiness_probe", {"t": time.time()})
        db.kv_get(".readiness_probe")
        checks["database"] = {"ok": True}
    except Exception as e:
        checks["database"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 3) 项目目录可写 —— Agent 的产物落在这里
    try:
        root = Path(get_agent().config.project_root)
        if not root.is_dir():
            raise FileNotFoundError(f"项目目录不存在：{root}")
        if not os.access(root, os.W_OK):
            raise PermissionError(f"项目目录不可写：{root}")
        checks["project_root"] = {"ok": True, "path": str(root)}
    except Exception as e:
        checks["project_root"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 4) 磁盘余量 —— 满了之后会先丢轨迹、再写不进检查点
    try:
        usage = shutil.disk_usage(str(Path(get_agent().config.project_root)))
        free_mb = usage.free / 1024 / 1024
        checks["disk"] = {"ok": free_mb >= 200, "free_mb": round(free_mb, 1)}
        if free_mb < 200:
            checks["disk"]["error"] = "可用磁盘不足 200MB —— 轨迹与检查点写入会失败"
    except Exception as e:
        checks["disk"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 5) LLM 是否配置 —— **不算致命**：界面与配置功能在没有 Key 时依然可用，
    #    把它记成"就绪失败"会让刚部署还没配 Key 的实例被编排系统反复重启。
    try:
        llm_ready = get_agent().llm is not None
        checks["llm"] = {
            "ok": True, "configured": llm_ready,
            "note": "" if llm_ready else "未配置任何提供商的 API Key：界面可用，但任务无法执行",
        }
    except Exception as e:
        checks["llm"] = {"ok": True, "configured": False,
                         "note": f"无法判定：{type(e).__name__}: {e}"}
    return checks


@app.get("/api/health/ready")
async def api_health_ready():
    """就绪探针：依赖真的可用才返回 200，否则 503。

    与 ``/api/health``（存活探针）的分工：存活探针回答"进程还在吗"，
    就绪探针回答"现在把流量导进来会不会失败"。k8s 的 liveness / readiness
    分别打这两个，才不会出现"数据库坏了但 Pod 一直显示健康"。
    """
    checks = await asyncio.to_thread(_readiness_checks)
    critical = ("data_dir", "database", "project_root", "disk")
    failed = [name for name in critical if not checks.get(name, {}).get("ok")]
    ready = not failed
    body = {
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "failed": failed,
        "checks": checks,
        "version": app.version,
    }
    if not ready:
        logger.warning("readiness_failed", failed=failed)
    return JSONResponse(body, status_code=200 if ready else 503)


@app.get("/metrics")
async def api_metrics():
    """Prometheus 文本指标（v1.7.3）。

    配置了访问令牌时同样受保护（中间件里把 ``/metrics`` 与 ``/api/*`` 同等对待），
    Prometheus 可在 scrape 配置里用 ``?token=`` 或 ``Authorization`` 头。
    """
    from automind.core.metrics import METRICS
    from automind.core.quota import snapshot as _quota_snapshot

    # 仪表类指标在抓取时现算 —— 它们本来就是"当前值"，没必要每次变化都写一遍
    try:
        agent = get_agent()
        METRICS.gauge("automind_tools", len(agent.tool_registry.list_names()))
        METRICS.gauge("automind_tool_group_failures", len(agent.tool_group_failures()))
        METRICS.gauge("automind_interjections_pending", agent.pending_interjections())
    except Exception as e:                              # pragma: no cover - 防御性
        logger.warning("metrics_gauge_failed", error=str(e))
    try:
        q = _quota_snapshot()
        METRICS.gauge("automind_quota_tasks_used", q["daily_tasks"]["used"])
        limit = q["daily_tasks"]["limit"]
        if limit is not None:
            METRICS.gauge("automind_quota_tasks_limit", limit)
    except Exception:                                   # pragma: no cover - 防御性
        pass
    METRICS.gauge("automind_info", 1, version=app.version, edition=_edition.get_edition())
    METRICS.gauge("automind_uptime_seconds", round(time.time() - _START_TIME, 1))
    METRICS.gauge("automind_running_tasks", _running_tasks["count"])
    METRICS.gauge("automind_approval_waiting", _approval_waiting["count"])
    # 出站事件投递账目（v1.7.3 第 8 项）：failed/dropped 是最该告警的两个 ——
    # dropped > 0 意味着**已经在丢事件**，而调用方永远不会知道。
    try:
        from automind.core import webhooks

        st = webhooks.stats()
        METRICS.gauge("automind_webhook_enabled", 1 if st.get("enabled") else 0)
        METRICS.gauge("automind_webhook_targets", st.get("targets", 0))
        METRICS.gauge("automind_webhook_queue_depth", st.get("queue_depth", 0))
        for key in ("delivered", "failed", "dropped", "queued", "retried",
                    "skipped", "rejected"):
            METRICS.gauge(f"automind_webhook_{key}", st.get(key, 0))
    except Exception as e:                              # pragma: no cover - 防御性
        logger.warning("webhook_stats_unavailable", reason=str(e))
    # 工作流（第 9 项）与评测（第 7 项）的运行账目由各自模块提供
    try:
        from automind.workflow import stats as _wf_stats

        wf = _wf_stats()
        for key, value in (wf or {}).items():
            METRICS.gauge(f"automind_workflow_{key}", value)
    except Exception:
        pass
    try:
        from automind.eval import stats as _eval_stats

        ev = _eval_stats()
        for key, value in (ev or {}).items():
            METRICS.gauge(f"automind_eval_{key}", value)
    except Exception:
        pass

    return PlainTextResponse(METRICS.render(),
                             media_type="text/plain; version=0.0.4; charset=utf-8")


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
        # 自动选路提示（如"已检测到 DeepSeek Key，将使用 deepseek-chat"），
        # 前端在模型面板顶部原样展示；无提示时为空串
        "llm_notice": _llm_notice,
        "project": str(agent.config.project_root),
        "tools": len(agent.tool_registry),
        "skills": len(agent.skill_registry),
        "sessions": len(_active_sessions),
        "history": len(_task_history),
        "token_totals": _token_totals,
        "quota": _quota_status(),
    }


def _quota_status() -> dict:
    """限额摘要（内嵌于 /api/status，避免前端额外请求）。"""
    from automind.core import quota as q
    return {"daily_used": q.tasks_used_today(), "daily_limit": q.daily_limit(),
            "workspace_limit": q.workspace_limit()}


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
    """设置审批模式：ask（询问）| auto（自动）| approve_all（全批准）。

    持久化到 active 配置 + 即时应用到**全局与已存在的会话实例** ——
    只改全局时，正在用的那个标签页（会话克隆）仍旧按老模式跑，
    用户则会以为"我明明设成询问了，它怎么没问我"。
    """
    mode = (data.get("approval_mode") or "").strip()
    if mode not in ("ask", "auto", "approve_all"):
        return JSONResponse({"error": "无效的审批模式"}, status_code=400)
    _save_active(approval_mode=mode)
    agent = get_agent()
    agent.permissions.approval_mode = mode
    agent.config.execution.approval_mode = mode
    synced = 0
    for clone in list(_session_clones.values()):
        try:
            _sync_approval_mode(clone, agent)
            synced += 1
        except Exception as e:        # pragma: no cover
            logger.warning("approval_mode_apply_failed", error=str(e))
    logger.info("approval_mode_changed", mode=mode, sessions_synced=synced)
    return {"status": "ok", "approval_mode": mode, "sessions_synced": synced}


_AUTOPILOT_FLAGS = ("auto_review", "auto_verify", "auto_test",
                    "parallel_execution", "subtask_cache")
#: 只有布尔值的执行配置（可随 autopilot 一起开关，且无需重建 Agent）
_AUTOPILOT_BOOL_EXTRAS = ("isolate_workspace", "terminal_background_enabled",
                          "release_slot_on_approval_wait", "trace_enabled")
#: 数值型执行参数（设置面板「执行治理」里的输入框）
_AUTOPILOT_NUMS = ("tool_timeout_seconds", "tool_timeout_max_seconds",
                   "approval_timeout_seconds", "tool_output_max_chars",
                   "auto_verify_max_rounds", "trace_max_runs",
                   "goal_timeout_seconds", "react_tool_timeout_seconds",
                   "max_task_seconds", "react_tool_budget",
                   "react_repeat_action_threshold", "react_no_progress_limit",
                   "task_token_budget", "max_iterations", "budget_auto_continue")
#: 其中取整数的那些（其余保留小数）
_AUTOPILOT_INT_NUMS = frozenset({
    "tool_output_max_chars", "auto_verify_max_rounds", "trace_max_runs",
    "tool_timeout_seconds", "goal_timeout_seconds", "react_tool_budget",
    "react_tool_timeout_seconds", "max_task_seconds",
    "react_repeat_action_threshold", "react_no_progress_limit",
    "task_token_budget", "max_iterations", "budget_auto_continue"})
_AUTOPILOT_STRS = {"approval_timeout_action": ("approve", "reject"),
                   "write_conflict_policy": ("off", "warn", "block")}


def _autopilot_snapshot(agent) -> dict:
    """设置面板要的**完整**当前值。

    v1.7.6 修复：此前 GET 只返回布尔开关 —— 「执行治理」里的 11 个数值
    输入框与两个策略下拉框拿不到当前值，界面上全是空白 / 写死的默认项，
    用户既看不到现在是多少，也无从判断改动是否生效。
    """
    ex = agent.config.execution
    out: dict = {f: bool(getattr(ex, f)) for f in _AUTOPILOT_FLAGS}
    out.update({f: bool(getattr(ex, f, False)) for f in _AUTOPILOT_BOOL_EXTRAS})
    for n in _AUTOPILOT_NUMS:
        v = getattr(ex, n, None)
        if isinstance(v, (int, float)):
            out[n] = int(v) if n in _AUTOPILOT_INT_NUMS else float(v)
    for k in _AUTOPILOT_STRS:
        v = getattr(ex, k, None)
        if isinstance(v, str):
            out[k] = v
    return out


def _propagate_execution(fields: dict) -> int:
    """把执行配置的改动同步到**已存在的会话克隆**（返回同步了几个）。

    v1.7.6 修复：会话克隆创建时 ``config.model_copy(deep=True)`` 了一份，
    此前在设置里改超时/上限只改到全局实例 —— 正在用的那个会话要等重启
    才生效，表现为"设置改了没用"。
    """
    n = 0
    for clone in list(_session_clones.values()):
        try:
            for k, v in fields.items():
                setattr(clone.config.execution, k, v)
            pe = getattr(clone, "plan_executor", None)
            if pe is not None:
                if "parallel_execution" in fields:
                    pe.parallel = fields["parallel_execution"]
                if "subtask_cache" in fields:
                    pe.use_cache = fields["subtask_cache"]
                if "goal_timeout_seconds" in fields:
                    pe.goal_timeout = float(fields["goal_timeout_seconds"])
            n += 1
        except Exception as e:          # pragma: no cover - 单个克隆失败不影响其余
            logger.warning("execution_propagate_failed", error=str(e))
    return n


@app.get("/api/config/autopilot")
async def api_get_autopilot():
    """自主闭环开关 + 执行治理参数的当前值（设置面板用）。"""
    return _autopilot_snapshot(get_agent())


@app.post("/api/config/autopilot")
async def api_set_autopilot(data: dict):
    """更新自主闭环开关 / 执行治理参数（持久化 + 即时生效，无需重建 Agent）。

    返回完整快照；另附 ``rejected``：被忽略的键及原因（此前非法值被静默
    丢弃，界面却提示"已保存"）。
    """
    agent = get_agent()
    cfg = _read_config()
    stored = cfg.get("autopilot", {})
    applied: dict = {}
    rejected: dict = {}
    for flag in tuple(_AUTOPILOT_FLAGS) + _AUTOPILOT_BOOL_EXTRAS:
        if flag in data:
            val = bool(data[flag])
            stored[flag] = val
            setattr(agent.config.execution, flag, val)
            applied[flag] = val
    # 数值型执行参数（超时/上限）也允许在此调整：长任务排障时不必重建 Agent
    for num in _AUTOPILOT_NUMS:
        if num not in data:
            continue
        try:
            val = float(data[num])
        except (TypeError, ValueError):
            rejected[num] = "不是数字"
            continue
        if val < 0:
            rejected[num] = "不能为负数"
            continue
        if num == "max_iterations" and val < 1:
            rejected[num] = "至少为 1"
            continue
        try:
            setattr(agent.config.execution, num,
                    int(val) if num in _AUTOPILOT_INT_NUMS else val)
        except Exception as e:          # pydantic 校验失败等
            rejected[num] = str(e)[:120]
            continue
        stored[num] = getattr(agent.config.execution, num)
        applied[num] = stored[num]
        # v1.7.0：单步执行上限即时生效（改完就管住正在跑的计划，
        # 不必等重建 Agent —— 排障时正是"它现在卡住了"才要调这个值）
        if num == "goal_timeout_seconds":
            try:
                agent.plan_executor.goal_timeout = float(val)
            except Exception as e:      # pragma: no cover
                logger.warning("goal_timeout_apply_failed", error=str(e))
    for key, allowed in _AUTOPILOT_STRS.items():
        if key in data:
            val = str(data[key]).lower()
            if val in allowed:
                setattr(agent.config.execution, key, val)
                stored[key] = val
                applied[key] = val
            else:
                rejected[key] = f"可选值：{' / '.join(allowed)}"
    cfg["autopilot"] = stored
    _write_config(cfg)
    # 即时应用到执行器（并行/缓存在 PlanExecutor 实例上）
    agent.plan_executor.parallel = agent.config.execution.parallel_execution
    agent.plan_executor.use_cache = agent.config.execution.subtask_cache
    synced = _propagate_execution(applied) if applied else 0
    out = _autopilot_snapshot(agent)
    if rejected:
        out["rejected"] = rejected
    out["sessions_synced"] = synced
    return out


#: 上下文占用百分比的分母兜底：128k（主流模型常见窗口）。
#: 读不到 Agent 配置时宁可给一个常见缺省值，也不要让右栏显示 NaN% / 0%。
_CONTEXT_MAX_FALLBACK = 128000


def _context_max_tokens() -> int:
    """上下文窗口上限 = 短期记忆预算（``memory.short_term_max_tokens``）。"""
    try:
        limit = int(getattr(get_agent().config.memory,
                            "short_term_max_tokens", 0) or 0)
    except Exception:
        limit = 0
    return limit if limit > 0 else _CONTEXT_MAX_FALLBACK


def _tokens_view() -> dict:
    """本次运行累计用量的对外只读视图（/api/tokens 与 /api/stats 共用）。

    命中率、上下文占用百分比只在这一处计算：两个端点各算一遍的话，
    口径一旦分叉就会同时出现"右栏命中率 40%、统计页 0%"这类矛盾数字，
    而两边都不报错，用户无从判断该信哪个。
    """
    cached = int(_token_totals.get("cached", 0) or 0)
    total_input = int(_token_totals.get("input", 0) or 0)
    reported = int(_token_totals.get("cache_reported_calls", 0) or 0)
    used = int(_token_totals.get("peak_prompt_tokens", 0) or 0)
    limit = _context_max_tokens()
    return {
        # 原有四个字段保持不变（旧的集成脚本仍在读它们）
        "prompt": int(_token_totals.get("prompt", 0) or 0),
        "completion": int(_token_totals.get("completion", 0) or 0),
        "total": int(_token_totals.get("total", 0) or 0),
        "tasks": int(_token_totals.get("tasks", 0) or 0),
        # v1.11.10：提示缓存 / 调用次数 / 上下文占用
        "cached": cached,
        "input": total_input,
        "calls": int(_token_totals.get("calls", 0) or 0),
        "cache_hit_calls": int(_token_totals.get("cache_hit_calls", 0) or 0),
        # 只有"有输入 **且** 提供商确实上报过缓存口径"才算得出命中率：
        #   上报了且 cached=0 → 0.0%（这次确实没命中）；
        #   压根不报（Ollama 等）→ None（界面显示"—"，不能拿 0% 冒充）。
        "cache_rate": (round(cached / total_input * 100, 1)
                       if total_input > 0 and reported > 0 else None),
        "context": {
            "used": used,      # 最近一次任务的单请求输入峰值
            "max": limit,
            "pct": round(used / limit * 100, 1) if limit else 0.0,
        },
    }


@app.get("/api/tokens")
async def api_tokens():
    """累计 token 用量统计（含提示缓存命中与上下文占用）。"""
    return _tokens_view()


@app.delete("/api/tokens")
async def api_tokens_reset():
    # 新指标一并清零：只清旧四个键的话，右栏会继续显示上一次的命中率 /
    # 上下文占用，用户点完"清空"看到数字没变，只会以为按钮坏了。
    for key in _token_totals:
        _token_totals[key] = 0
    return {"status": "ok"}


@app.get("/api/stats")
async def api_stats():
    """增强统计 — 按模式聚合任务、成功率、token、耗时与工具使用。"""
    hist = _task_history
    by_mode: dict[str, dict] = {}
    tool_usage: dict[str, int] = {}
    total_dur = 0.0
    success = 0
    # Token 合计与 by_mode 必须出自同一份数据。此前合计取的是进程内存计数器
    # `_token_totals`（重启即清零），而 by_mode 取的是持久化的任务历史 ——
    # 于是重启后同一屏上会出现「累计 Token 0」与「编程模式 108,203 tk」
    # 这种自相矛盾的数字，且不会报任何错。
    tokens = {"prompt": 0, "completion": 0, "total": 0, "tasks": 0,
              "cached": 0, "input": 0, "calls": 0, "cache_hit_calls": 0}
    reported = 0            # 上报过缓存口径的记录数（决定 cache_rate 是否为 None）
    for h in hist:
        m = h.get("interaction") or "other"
        b = by_mode.setdefault(m, {"count": 0, "success": 0, "tokens": 0, "duration_ms": 0.0})
        b["count"] += 1
        b["success"] += 1 if h.get("success") else 0
        b["tokens"] += h.get("tokens", 0)
        b["duration_ms"] += h.get("duration_ms", 0) or 0
        total_dur += h.get("duration_ms", 0) or 0
        success += 1 if h.get("success") else 0
        prompt_tk = h.get("prompt_tokens", 0) or 0
        tokens["prompt"] += prompt_tk
        tokens["completion"] += h.get("completion_tokens", 0) or 0
        tokens["total"] += h.get("tokens", 0) or 0
        tokens["tasks"] += 1
        # v1.11.10：缓存/上下文指标按同一份历史聚合。旧记录没有这些键
        # （也没有 input_tokens）—— input 回退 prompt，命中率分母才完整。
        tokens["cached"] += h.get("cached_tokens", 0) or 0
        tokens["input"] += h.get("input_tokens", 0) or prompt_tk
        tokens["calls"] += h.get("llm_calls", 0) or 0
        tokens["cache_hit_calls"] += h.get("cache_hit_calls", 0) or 0
        # 旧记录没有 cache_reported_calls（按 0）→ 命中率保持 None，
        # 不会把"从来没统计过"误报成 0% 命中
        reported += h.get("cache_reported_calls", 0) or 0
    # 命中率只在"有输入且提供商确实上报过缓存口径"时给数字；否则 None
    tokens["cache_rate"] = (round(tokens["cached"] / tokens["input"] * 100, 1)
                            if tokens["input"] and reported else None)
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
        "tokens": tokens,
        # 本次运行的实时增量（右栏"Token 用量统计"用），与上面的历史合计区分开。
        # 与 /api/tokens 共用 _tokens_view：命中率/上下文占用在两端必须一致
        "tokens_session": _tokens_view(),
        "by_mode": by_mode,
        "tool_usage": dict(sorted(tool_usage.items(), key=lambda x: -x[1])),
        "audit": audit_summary,
        "scheduled_tasks": (
            _edition.get_feature("scheduler").count()
            if _edition.has_feature("scheduler") else 0),
    }


# ── 会话待办清单（v1.11.11）────────────────────────────────
# 模型在多步任务中维护的 todo 列表。这里只提供「只读查询」与「手动清空」，
# 不触发任何任务逻辑 —— todo 的写入入口在 Agent 工具侧，端点不参与执行。


def _todo_session(session_id: str) -> str:
    """归一化会话标识：空值 → "default"（与其它会话级端点保持一致）。"""
    return (session_id or "").strip() or "default"


@app.get("/api/todo")
async def api_todo(session_id: str = ""):
    """返回该会话的待办清单与状态计数（前端右栏「待办」区块）。"""
    from automind.core import todo_store

    todos = todo_store.load(_todo_session(session_id))
    return {"todos": todos, "counts": todo_store.counts(todos)}


@app.delete("/api/todo")
async def api_todo_clear(session_id: str = ""):
    """清空该会话的待办（用户在界面上手动清屏）。"""
    from automind.core import todo_store

    todo_store.clear(_todo_session(session_id))
    return {"status": "ok"}


# ── 商业功能占位（专业版路由由 automind_pro 在扩展加载阶段注册）──
# 高级统计（/api/stats/detail|context|history）与定时任务（/api/schedule*）
# 已迁移至 automind-pro 包；社区版访问这些端点时由文件末尾注册的
# 降级路由返回 403 + 升级提示（若专业版已激活，其路由先注册故优先匹配）。


@app.post("/api/config/ui")
async def api_config_ui(data: dict):
    """保存界面偏好（目前只有主题）。

    桌面版的启动画面在浏览器起来之前就要决定用深色还是浅色，它读不到
    localStorage，只能读配置文件 —— 否则浅色主题用户每次启动都会先看到
    一屏近黑的启动画面再跳成白色，就是所谓"启动黑屏闪烁"。
    """
    theme = str(data.get("theme") or "").lower()
    if theme not in ("dark", "light"):
        return JSONResponse({"error": "theme 只能是 dark 或 light"}, status_code=400)

    def _save() -> None:
        cfg = _read_config()
        cfg.setdefault("ui", {})["theme"] = theme
        _write_config(cfg)

    await asyncio.to_thread(_save)
    return {"status": "ok", "theme": theme}


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
    from automind.core.types import ExecutionMode, InteractionMode

    agent = get_agent()
    llm_changed = False   # 提供商 / 模型 / Key / 地址 是否变化
    changed = False       # 是否有任何影响会话执行的变化（用于作废会话克隆）
    new_model = data.get("model") or ""

    # 配置落盘是同步 IO —— 一律丢线程池，别让设置面板的一次保存
    # 把整个事件循环（含正在流式输出的会话）顶住
    if data.get("provider") and data["provider"] != agent.config.llm.provider:
        agent.config.llm.provider = data["provider"]
        await asyncio.to_thread(_save_active, provider=data["provider"])
        llm_changed = True
    if new_model:
        agent.config.llm.model = new_model
        await asyncio.to_thread(_save_provider_cfg, agent.config.llm.provider,
                                model=new_model)
        await asyncio.to_thread(_save_active, model=new_model)
        llm_changed = True
    if "api_base" in data:
        agent.config.llm.api_base = data["api_base"]
        await asyncio.to_thread(_save_provider_cfg, agent.config.llm.provider,
                                api_base=data["api_base"])
        llm_changed = True
    if data.get("api_key"):
        agent.config.llm.api_key = data["api_key"]
        keys = _load_api_keys()
        keys[agent.config.llm.provider] = data["api_key"]
        await asyncio.to_thread(_save_api_keys, keys)
        llm_changed = True

    if llm_changed:
        # 只换 LLM。此前这里走的是整个 Agent 重建（重扫项目索引、重建
        # ChromaDB、重注册全部工具与技能），在设置里点一下模型/模式就要
        # 卡 2~3 秒 —— 而真正变化的只有一个模型名。
        agent.switch_llm(_llm_config_for(agent, agent.config.llm.provider, new_model))
        changed = True

    if data.get("interaction"):
        try:
            new_interaction = InteractionMode(data["interaction"])
            exec_mode = _INTERACTION_TO_EXECUTION.get(data["interaction"], "plan_and_execute")
            agent._interaction = new_interaction
            agent._mode = ExecutionMode(exec_mode)
            agent.config.execution.mode = exec_mode
            await asyncio.to_thread(_save_active, interaction=data["interaction"])
            # 切换到该模式对应的模型（同样只换 LLM）
            agent = _apply_mode_model(agent, data["interaction"])
            changed = True
        except ValueError:
            pass
    if "temperature" in data:
        agent.config.llm.temperature = float(data["temperature"])
    if "max_tokens" in data:
        agent.config.llm.max_tokens = int(data["max_tokens"])

    if changed:
        # 会话克隆持有旧的模型/模式配置，需在下次任务时重新派生
        _drop_session_clones()

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
    # 测试连接要点：
    # - timeout 12s + max_retries 0：快速失败，别再让前端"转圈圈"几十秒；
    # - max_tokens 128：推理模型（如 deepseek-v4-flash）会先输出 reasoning，
    #   token 太少会把 content 挤成空串，reply_sample 看起来像"没反应"。
    cfg = LLMProviderConfig(
        provider=provider, model=model, api_key=api_key,
        api_base=safe_base, max_tokens=128, temperature=0.0,
        timeout=12.0, max_retries=0,
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
        elapsed = round(time.perf_counter() - t0, 1)
        low = msg.lower()
        # ⚠️ 超时必须**最先判断**，而且不能只认 "timeout" 这一个拼写。
        # 此前判据是 `"timeout" in low`，可 SDK 真正抛出的是
        # "APITimeoutError: Request timed out." —— 里面是 "timed out"
        # （中间有空格），**不含 "timeout" 这个子串**，于是最常见的那类失败
        # 恰好一条提示都拿不到，用户只看到一句光秃秃的报错。
        timed_out = any(k in low for k in (
            "timed out", "timeout", "readtimeout", "apitimeouterror",
            "deadline exceeded", "temporarily unavailable", "connect error"))
        if timed_out:
            hint = (f"等待 {elapsed:g} 秒未收到响应（本次上限 {cfg.timeout:g} 秒）："
                    f"多为网络/代理不通、api_base 填错，或模型侧排队。"
                    f"推理类模型（如 deepseek-reasoner / *-flash）首字延迟明显更长，"
                    f"可先换成非推理模型验证连通性。")
        elif "401" in msg or "403" in msg or "unauthorized" in low \
                or "invalid api key" in low or "invalid_api_key" in low:
            hint = "API Key 无效或未授权，请检查 Key 是否正确、是否过期。"
        elif "model" in low and any(w in low for w in ("not found", "not exist", "supported", "passed", "no such", "does not")):
            hint = "模型名称有误，请按服务商要求填写正确的模型名（注意大小写）。"
        elif "404" in msg or "not found" in low:
            hint = "API 地址（api_base）可能有误，请检查是否为正确的 /v1 端点。"
        elif "connect" in low or "resolve" in low or "connection" in low:
            hint = "无法连接到服务地址，请检查 api_base 与网络/代理。"
        elif "429" in msg or "rate" in low:
            hint = "请求过于频繁或额度不足。"
        return {"success": False, "stage": "timeout" if timed_out else "request",
                "error": msg, "hint": hint, "elapsed_s": elapsed,
                "timeout_s": cfg.timeout,
                "provider": provider, "model": model,
                "api_base": api_base or "(默认)"}
    finally:
        # 测试连接是一次性探测，用完即关，别留下连接池（每次测试都会新建一个）
        with suppress(Exception):
            await backend.close()


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


_LOOPBACK = ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")


def _is_local_request(request) -> bool:
    return ((request.client.host if request.client else "") or "") in _LOOPBACK


def _fs_browse_denied(request) -> str | None:
    """目录浏览的准入判断；返回拒绝原因，None 表示放行。

    安全修复（v1.4.4）：此前任何人都能用绝对路径列举服务器上的任意目录。
    本地单机使用时这不算问题（调用方本来就是这台机器的用户，权限一样大），
    但一旦绑到 0.0.0.0 又没配 auth_token，局域网内任何人都能拿它枚举整个磁盘。
    因此：**配了令牌 → 已鉴权，放行；没配令牌 → 只允许来自本机的请求。**
    """
    if _auth_token():
        return None
    if _is_local_request(request):
        return None
    return ("目录浏览仅限本机访问。若确需远程使用，请先配置 auth_token "
            "（设置 → 安全）后携带令牌访问。")


def _fs_roots() -> list[Path]:
    """允许浏览的锚点根目录。

    v1.4.5：此前只要通过了来源检查，就能用绝对路径漫游整块磁盘（`C:\\`、`/`
    都能列）。目录选择器的真实用途只是"挑一个放项目的地方"，没必要给到全盘。
    锚定到：用户主目录、当前项目根、已配置的工作区目录、以及显式配置的额外根
    （AUTOMIND_FS_ROOTS，冒号/分号分隔）。路径必须落在某个锚点之内。
    """
    roots: list[Path] = []

    def _add(p: Any) -> None:
        try:
            rp = Path(str(p)).expanduser().resolve()
            if rp.is_dir() and rp not in roots:
                roots.append(rp)
        except Exception:
            pass

    _add(Path.home())
    _add(Path.cwd())
    try:
        agent = get_agent()
        if agent:
            _add(agent.config.project_root)
    except Exception:
        pass
    try:
        for w in (_read_config().get("workspaces") or {}).values():
            _add(w.get("path") if isinstance(w, dict) else w)
    except Exception:
        pass
    for extra in os.environ.get("AUTOMIND_FS_ROOTS", "").replace(";", os.pathsep).split(os.pathsep):
        if extra.strip():
            _add(extra.strip())
    return roots


def _fs_within_roots(target: Path, roots: list[Path]) -> bool:
    """target 是否落在任一锚点之内（或本身就是锚点/锚点的父级）。

    允许"锚点的父级"是为了让选择器能向上导航到锚点 —— 但向上只会看到
    通往锚点的路径，列出的子目录仍会被逐一过滤，不会因此漫游到别处。
    """
    for r in roots:
        if target == r or r in target.parents or target in r.parents:
            return True
    return False


# 无论本地还是远程都不给列的系统目录 —— 目录选择器没有任何理由进这些地方，
# 挡一道可以降低"浏览器被诱导发起本机请求"这类场景的信息泄露面。
_FS_DENY_PARTS: tuple[str, ...] = (
    "windows\\system32", "windows\\syswow64", "/etc", "/root", "/proc", "/sys",
    "/var/lib", "\\appdata\\roaming\\microsoft\\crypto", "/.ssh", "\\.ssh",
)


def _fs_path_denied(p: Path) -> bool:
    s = str(p).lower().replace("/", os.sep).replace("\\", os.sep)
    if any(part.replace("/", os.sep).replace("\\", os.sep) in s
           for part in _FS_DENY_PARTS):
        return True
    # v1.11.5 放开全盘浏览后再加一道"系统目录整棵子树"：项目不可能放在这些地方
    return any(p == r or r in p.parents for r in _fs_system_roots())


def _fs_system_roots() -> list[Path]:
    """整棵子树都不给浏览的系统目录（Windows 目录 / POSIX 系统目录）。"""
    raw: list[Path] = []
    if os.name == "nt":
        for env in ("SystemRoot", "WINDIR"):
            v = os.environ.get(env)
            if v:
                raw.append(Path(v))
    else:
        raw += [Path(x) for x in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot",
                                  "/dev", "/etc", "/proc", "/sys", "/root", "/var/lib")]
    out: list[Path] = []
    for r in raw:
        try:
            r = r.resolve()
        except Exception:
            pass
        if r not in out:
            out.append(r)
    return out


def _fs_full_disk_allowed(request) -> bool:
    """v1.11.5：本机访问时，目录选择器可以浏览整块磁盘（桌面、D 盘……）。

    v1.4.5 把浏览锚定在主目录 / 项目 / 工作区，防的是"局域网 + 无令牌"时被枚举全盘；
    而本机用户本来就能在资源管理器里看到这些目录，锚定只带来"项目放在 D 盘就选不到"。
    所以**只有来自本机的请求**放开到全盘（系统目录仍然拒绝）；远程（带令牌）仍按锚点。
    设 AUTOMIND_FS_FULL_DISK=0 可恢复锚定行为。
    """
    flag = os.environ.get("AUTOMIND_FS_FULL_DISK", "1").strip().lower()
    if flag in ("0", "false", "off", "no"):
        return False
    return _is_local_request(request)


def _fs_full_disk_roots() -> list[Path]:
    if os.name == "nt":
        return [Path(d) for d in _list_drives()]
    return [Path("/")]


def _windows_known_folder(csidl: int) -> str:
    """SHGetFolderPathW：桌面 / 文档被重定向到 OneDrive 或别的盘时，只有它拿得准。"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        if shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
            return buf.value
    except Exception:
        pass
    return ""


def _fs_places() -> list[dict]:
    """快捷位置：桌面 / 文档 / 下载 / 主目录 / 当前项目（只给真实存在的）。"""
    home = Path.home()
    cands: list[tuple[str, str, list[Any]]] = []
    if os.name == "nt":
        cands.append(("desktop", "桌面", [_windows_known_folder(0x10), home / "Desktop",
                                          home / "OneDrive" / "Desktop", home / "OneDrive" / "桌面"]))
        cands.append(("documents", "文档", [_windows_known_folder(0x05), home / "Documents"]))
    else:
        cands.append(("desktop", "桌面", [home / "Desktop", home / "桌面"]))
        cands.append(("documents", "文档", [home / "Documents", home / "文档"]))
    cands.append(("downloads", "下载", [home / "Downloads", home / "下载"]))
    cands.append(("home", "主目录", [home]))
    try:
        agent = get_agent()
        if agent:
            cands.append(("project", "当前项目", [agent.config.project_root]))
    except Exception:
        pass
    out: list[dict] = []
    seen: set[str] = set()
    for key, label, paths in cands:
        for raw in paths:
            if not raw:
                continue
            try:
                rp = Path(str(raw)).expanduser().resolve()
            except Exception:
                continue
            if rp.is_dir() and str(rp) not in seen:
                seen.add(str(rp))
                out.append({"key": key, "label": label, "path": str(rp)})
                break
    return out


#: 一次最多列这么多项：一个塞了几万张照片的目录不该把选择器卡死
_FS_LIST_MAX = 500
#: Windows 每个盘符下都有、对选项目毫无意义的系统项
_FS_HIDDEN_NAMES = frozenset({"$RECYCLE.BIN", "System Volume Information", "$WinREAgent",
                              "Config.Msi", "Recovery", "PerfLogs"})


def _fs_list_sync(path: str, *, full_disk: bool = False, files: bool = False) -> dict:
    """目录浏览的同步实现（由 api_fs_list 丢进线程池执行）。

    ``iterdir`` + 逐项 ``is_dir`` 在网络盘或大目录上可能几百毫秒，
    直接跑在 async 端点里会**堵住整个事件循环**：此时所有其它 API、
    正在流式输出的 WebSocket 一起卡住。
    """
    roots = _fs_full_disk_roots() if full_disk else _fs_roots()
    base = (Path(path).expanduser() if path else Path.home()).resolve()
    if _fs_path_denied(base):
        return {"_error": "该目录不允许浏览（系统目录）", "_status": 403}
    # 根锚定：路径必须与某个锚点根相关联，不能拿绝对路径漫游全盘
    if not _fs_within_roots(base, roots):
        return {"_error": "该目录不在允许浏览的范围内（仅限主目录、项目目录与已配置的工作区）",
                "_status": 403, "roots": [str(r) for r in roots]}
    if not base.is_dir():
        return {"_error": f"目录不存在：{base}", "_status": 404}
    dirs: list[str] = []
    names: list[str] = []
    truncated = False
    try:
        for e in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if len(dirs) + len(names) >= _FS_LIST_MAX:
                truncated = True
                break
            if e.name.startswith(".") or e.name in _FS_HIDDEN_NAMES:
                continue
            try:
                is_dir = e.is_dir()
            except OSError:
                continue
            # 子项同样要过锚点与黑名单：向上导航到锚点父级时，
            # 只应看到通往锚点的那条路，而不是该层的所有目录
            if is_dir:
                if not _fs_path_denied(e) and _fs_within_roots(e, roots):
                    dirs.append(e.name)
            elif files:
                names.append(e.name)
    except PermissionError:
        return {"_error": f"没有权限读取该目录：{base}", "_status": 403}
    parent = str(base.parent) if base.parent != base else ""
    # 已经在最外层锚点上时不再给 parent，避免界面诱导用户往上点却总是 403
    if parent and not _fs_within_roots(Path(parent), roots):
        parent = ""
    drives = _list_drives()
    if not full_disk:
        # 锚定模式：drives 仅在确实有锚点落在该盘符下时才给出
        drives = [d for d in drives if any(str(r).upper().startswith(d.upper()) for r in roots)]
    return {
        "path": str(base),
        "parent": parent,
        "dirs": dirs,
        "files": names,
        "truncated": truncated,
        "roots": [str(r) for r in roots],
        "drives": drives,
        "places": _fs_places(),
        "full_disk": full_disk,
    }


@app.get("/api/fs/list")
async def api_fs_list(request: Request, path: str = "", files: bool = False):
    """浏览本地目录，用于项目目录选择器（files=true 时附带文件名，仅供辨认）。"""
    denied = _fs_browse_denied(request)
    if denied:
        logger.warning("fs_list_denied", path=path,
                       client=request.client.host if request.client else "?")
        return JSONResponse({"error": denied}, status_code=403)
    try:
        result = await asyncio.to_thread(
            _fs_list_sync, path, full_disk=_fs_full_disk_allowed(request), files=files)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if "_error" in result:
        status = result.pop("_status", 400)
        result["error"] = result.pop("_error")
        return JSONResponse(result, status_code=status)
    return result


_FS_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@app.post("/api/fs/mkdir")
async def api_fs_mkdir(request: Request, data: dict):
    """在目录选择器里新建文件夹（v1.11.5）。准入与浏览完全一致，且只建一层。"""
    denied = _fs_browse_denied(request)
    if denied:
        return JSONResponse({"error": denied}, status_code=403)
    name = str(data.get("name") or "").strip()
    if (not name or name in (".", "..") or name.endswith((".", " "))
            or _FS_BAD_NAME.search(name) or len(name) > 120):
        return JSONResponse({"error": "文件夹名不合法"}, status_code=400)
    full = _fs_full_disk_allowed(request)

    def _mk() -> dict:
        roots = _fs_full_disk_roots() if full else _fs_roots()
        try:
            parent = Path(str(data.get("path") or "")).expanduser().resolve()
        except Exception:
            return {"_error": "非法路径", "_status": 400}
        target = parent / name
        if (not parent.is_dir() or _fs_path_denied(target)
                or not _fs_within_roots(parent, roots) or not _fs_within_roots(target, roots)):
            return {"_error": "不能在该位置新建文件夹", "_status": 403}
        if target.exists():
            return {"_error": f"已存在同名项：{name}", "_status": 409}
        try:
            target.mkdir()
        except OSError as e:
            return {"_error": f"新建失败：{e}", "_status": 400}
        return {"status": "ok", "path": str(target)}

    r = await asyncio.to_thread(_mk)
    if "_error" in r:
        return JSONResponse({"error": r["_error"]}, status_code=r["_status"])
    logger.info("fs_mkdir", path=r["path"])
    return r


def _preview_file_sync(root: Path, path: str) -> dict:
    """预览文件的同步实现（路径校验 + 读盘，交由线程池执行）。"""
    try:
        target = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    except Exception:
        return {"_error": "非法路径", "_status": 400}
    # 限制在项目根目录内，防止目录穿越
    if root not in target.parents and target != root:
        return {"_error": "路径超出项目目录范围", "_status": 403}
    # 与编辑器同一道判定：预览同样能读到密钥与数据文件（v1.7.3）
    from automind.core.sensitive import SCOPE_WEB
    from automind.core.sensitive import reason as _sensitive_reason
    denied = _sensitive_reason(target, SCOPE_WEB)
    if denied:
        return {"_error": denied, "_status": 403}
    if not target.is_file():
        return {"_error": f"文件不存在: {target}", "_status": 404}
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"_error": str(e), "_status": 400}
    return {"content": content, "suffix": target.suffix.lower()}


@app.get("/api/preview/file")
async def api_preview_file(path: str):
    """在项目目录范围内安全地预览一个文件（HTML 直接渲染，其它返回文本）。"""
    agent = get_agent()
    root = Path(agent.config.project_root).resolve()
    # 读盘不能跑在事件循环上 —— 预览一个大文件会让所有会话一起卡住
    r = await asyncio.to_thread(_preview_file_sync, root, path)
    if "_error" in r:
        return JSONResponse({"error": r["_error"]}, status_code=r["_status"])
    if r["suffix"] in (".html", ".htm"):
        return HTMLResponse(r["content"])
    media = "text/plain; charset=utf-8"
    if r["suffix"] == ".svg":
        media = "image/svg+xml"
    return HTMLResponse(r["content"], media_type=media)


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


# ═══════════════════════════════════════════════════════════
# REST API — 专家系统（官方精选 / 自建 / 激活；进阶能力按版本门控）
# ═══════════════════════════════════════════════════════════

from automind.core.experts import COMMUNITY_MAX_CUSTOM as _EXPERT_LIMIT  # noqa: E402
from automind.core.experts import STORE as _experts  # noqa: E402, N811


def _expert_visible(e: dict, sid: str) -> bool:
    """企业审批流开启时：他人分享的专家须已审批才可见。"""
    if e.get("owner") in ("official", sid) or not _edition.has_feature("expert_approval"):
        return True
    return bool(e.get("shared")) and bool(e.get("approved"))


@app.get("/api/experts")
async def api_experts(session_id: str = "default"):
    """已安装专家 + 官方目录 + 当前激活项与版本能力开关。"""
    installed = [e for e in _experts.installed() if _expert_visible(e, session_id)]
    return {
        "installed": installed,
        "official": _experts.official_catalog(),
        "active": _read_config().get("active_expert", ""),
        "custom_count": _experts.custom_count(),
        "custom_limit": (None if _edition.has_feature("experts_pro")
                         else _EXPERT_LIMIT),
        "pro": _edition.has_feature("experts_pro"),
        "approval": _edition.has_feature("expert_approval"),
    }


@app.post("/api/experts/install")
async def api_experts_install(data: dict):
    """一键安装官方精选专家。"""
    expert, err = _experts.install_official((data.get("id") or "").strip())
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"status": "ok", "expert": expert}


@app.post("/api/experts")
async def api_experts_create(data: dict):
    """创建自己的专家（社区版最多 3 个；experts_pro 无限）。"""
    sid = data.get("session_id") or "default"
    if data.get("shared") and not _edition.has_feature("experts_pro"):
        return JSONResponse({"error": _edition.upgrade_hint("experts_pro"),
                             "feature": "experts_pro"}, status_code=403)
    expert, err = _experts.create(
        data, owner=sid,
        unlimited=_edition.has_feature("experts_pro"),
        needs_approval=_edition.has_feature("expert_approval") and bool(data.get("shared")))
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"status": "ok", "expert": expert}


@app.delete("/api/experts/{eid}")
async def api_experts_delete(eid: str):
    cfg = _read_config()
    if cfg.get("active_expert") == eid:
        cfg["active_expert"] = ""
        _write_config(cfg)
    if _experts.delete(eid):
        return {"status": "ok"}
    return JSONResponse({"error": f"专家不存在: {eid}"}, status_code=404)


@app.post("/api/experts/activate")
async def api_experts_activate(data: dict):
    """激活/取消激活专家（激活后所有任务带该角色设定执行）。"""
    eid = (data.get("id") or "").strip()
    if eid and _experts.get(eid) is None:
        return JSONResponse({"error": f"专家不存在: {eid}"}, status_code=404)
    cfg = _read_config()
    cfg["active_expert"] = eid
    _write_config(cfg)
    return {"status": "ok", "active": eid}


def _apply_expert(task: str) -> str:
    """任务执行前注入当前激活专家的角色设定（所有交互模式生效）。"""
    eid = _read_config().get("active_expert", "")
    if not eid:
        return task
    expert = _experts.get(eid)
    if expert is None:
        return task
    _experts.bump_usage(eid)
    return (f"【专家模式 · {expert['icon']} {expert['name']}】\n"
            f"{expert['prompt']}\n---\n{task}")


# ═══════════════════════════════════════════════════════════
# REST API — RAG 知识库（社区版基础；进阶路由由 rag_pro 扩展注册）
# ═══════════════════════════════════════════════════════════

from automind.core import quota as _quota  # noqa: E402
from automind.rag import get_store as _kb_store  # noqa: E402

# 社区版限额：5 个文档 / 10MB 总量 / 仅默认知识库
_KB_FREE_DOCS = 5
_KB_FREE_SIZE = 10 * 1024 * 1024


#: 单条消息附件上限（字符）——超出的截断并在回执里说明
_ATTACH_MAX_CHARS = 200_000
_ATTACH_TOTAL_CHARS = 400_000
_ATTACH_MAX_FILES = 10
_OVERRIDE_MODES = {"chat", "work", "coding", "multi", "loop"}
_MODE_FEATURES = {"multi": "multi_agent", "loop": "loop_engine"}


def _resolve_overrides(data: dict, *, allow_plan: bool = True) -> tuple[str, bool, dict]:
    """解析一条消息携带的 overrides（v1.7.7 输入框加号菜单协议）。

    协议（WS ``run`` / REST ``/api/run`` 的可选字段）::

        overrides: {
          mode?: "chat"|"work"|"coding"|"multi"|"loop",  # 只对这一条生效，不改默认模式
          expert?: "<专家 id>" | "",                     # 只对这一条生效；"" = 这条不用专家
          attachments?: [{name, text, size?}]            # 文本附件，随任务一起交给模型
          plan_review?: bool,          # v1.11.0 计划模式：先出计划、人批准才执行
          goal?: "<目标 id>",           # v1.11.0 目标交付模式：本条服务于该长期目标
          mcp?: ["<服务器名>"],          # v1.11.0 引用 MCP 连接器：其工具必下发
          skills?: ["<技能名>"],         # v1.11.0 引用技能：注入指令 / 可经 skill_invoke 调用
        }

    返回 ``(本次交互模式, 是否把模式写成默认, 回执)``。回执随 ``task_start``
    推回前端，逐项写明**生效了没有、为什么没生效** —— 用户选了"用编程模式
    发这条"，结果服务端没权限用某模式，就必须明说，而不是悄悄用别的模式跑。
    """
    ov = data.get("overrides") if isinstance(data.get("overrides"), dict) else {}
    interaction = data.get("interaction", "") or ""
    persist = True
    receipt: dict = {}
    m = ov.get("mode")
    if m:
        m = str(m)
        feat = _MODE_FEATURES.get(m)
        if m not in _OVERRIDE_MODES:
            receipt["mode"] = {"value": m, "applied": False, "reason": "未知模式"}
        elif feat and not _edition.has_feature(feat):
            receipt["mode"] = {"value": m, "applied": False,
                               "reason": "该模式需要专业版，已按当前模式执行"}
        else:
            interaction, persist = m, False
            receipt["mode"] = {"value": m, "applied": True}
    if ov.get("plan_review"):
        # 计划模式 = 走"先规划"的工作模式 + 执行前的人工审批闸门。
        # 多智能体 / 循环工程有各自的编排器，没有"整份计划"可审，如实说明不生效。
        if not allow_plan:
            receipt["plan"] = {"applied": False,
                               "reason": "计划模式需要在界面（WebSocket）里审批，HTTP 接口不支持"}
        elif interaction in ("multi", "loop"):
            receipt["plan"] = {"applied": False,
                               "reason": "多智能体 / 循环工程模式没有可审批的整份计划，已按该模式直接执行"}
        else:
            note = "" if interaction in ("", "work") else \
                f"（本条由{ {'chat': '对话', 'coding': '编程'}.get(interaction, interaction)}模式改为工作模式：计划模式需要先生成整份计划）"
            interaction, persist = "work", False
            receipt["plan"] = {"applied": True, "note": "计划生成后等待你批准才会执行" + note}
    return interaction, persist, receipt


#: 单条消息最多引用的 MCP 连接器 / 技能数，以及注入的技能指令总量
_REF_MAX = 8
_SKILL_INJECT_CHARS = 12_000


def _apply_references(task: str, data: dict, receipt: dict) -> tuple[str, list[str]]:
    """按 overrides.mcp / overrides.skills 注入引用（v1.11.0，与专家引用同构）。

    返回 ``(新任务, 必须下发给模型的工具名)``。

    * **MCP 连接器**：展开成该服务器的全部工具名 —— ReAct 必下发（不再看关键词
      打分），计划模式下规划器本就看得到全部工具，这里再在任务前写明"优先用它"；
    * **Markdown 技能**：把 ``instructions()`` 正文注入上下文（这个接口此前写好了
      却**零调用者**，技能正文从未进过任何提示词）；
    * **Python 技能**：注入名字、用途与调用方式，并把 ``skill_invoke`` 列为必下发。

    逐项写回执：不存在 / 未连接 / 被截断都要说清 —— "我引用了却不生效"是最糟的体验。
    """
    ov = data.get("overrides") if isinstance(data.get("overrides"), dict) else {}
    mcp_refs = ov.get("mcp") if isinstance(ov.get("mcp"), list) else []
    skill_refs = ov.get("skills") if isinstance(ov.get("skills"), list) else []
    tool_refs = ov.get("tools") if isinstance(ov.get("tools"), list) else []
    if not mcp_refs and not skill_refs and not tool_refs:
        return task, []
    agent = get_agent()
    prefer: list[str] = []
    blocks: list[str] = []
    if tool_refs:
        # v1.11.2：输入框 / 指令点名的单个工具（含插件工具）—— 必下发，并写明"优先用它"
        from automind.tools.base import resolve_name
        names = agent.tool_registry.list_names()
        applied_t, dropped_t, rows_t = [], [], []
        for asked in [str(x) for x in tool_refs][:_REF_MAX]:
            real, tips = resolve_name(asked, names)
            if real is None:
                dropped_t.append({"name": asked, "reason": "工具不存在或已停用"
                                  + (f"（是不是：{'、'.join(tips)}）" if tips else "")})
                continue
            prefer.append(real)
            applied_t.append({"name": asked, "tool": real})
            try:
                desc = (agent.tool_registry.get(real).description or "").strip().splitlines()[0][:80]
            except Exception:
                desc = ""
            rows_t.append(f"- 🔧 {real}" + (f"：{desc}" if desc else ""))
        receipt["tools"] = {"applied": applied_t, "dropped": dropped_t}
        if rows_t:
            blocks.append("【指定工具】用户希望本条优先使用以下工具完成任务：\n" + "\n".join(rows_t))
    if mcp_refs:
        all_tools = [t.name for t in agent.mcp_registry.get_all_tools()]
        configured = {s.get("name") for s in _read_config().get("mcp_servers", [])
                      if isinstance(s, dict)}
        applied, dropped, rows = [], [], []
        for name in [str(x) for x in mcp_refs][:_REF_MAX]:
            tools = [t for t in all_tools if t.startswith(f"mcp__{name}__")]
            if not tools:
                dropped.append({"name": name, "reason": "连接器未连接或没有可用工具"
                                if name in configured else "连接器不存在或已删除"})
                continue
            prefer += tools
            applied.append({"name": name, "tools": len(tools)})
            rows.append(f"- 🔌 {name}：{', '.join(tools[:30])}"
                        + (f" 等 {len(tools)} 个" if len(tools) > 30 else ""))
        for name in [str(x) for x in mcp_refs][_REF_MAX:]:
            dropped.append({"name": name, "reason": f"单条消息最多引用 {_REF_MAX} 个连接器"})
        receipt["mcp"] = {"applied": applied, "dropped": dropped,
                          "note": "MCP 工具属敏感级，按当前审批模式可能需要你批准"}
        if rows:
            blocks.append("【引用 MCP 连接器】用户希望本条优先使用以下连接器提供的工具完成任务：\n"
                          + "\n".join(rows))
    if skill_refs:
        reg = agent.skill_registry
        applied, dropped, parts = [], [], []
        budget = _SKILL_INJECT_CHARS
        for name in [str(x) for x in skill_refs][:_REF_MAX]:
            if name not in reg:
                dropped.append({"name": name, "reason": "技能不存在或已卸载"})
                continue
            skill = reg.get(name)
            if hasattr(skill, "instructions"):
                text = skill.instructions(max_chars=max(500, min(4000, budget)))
                kind = "markdown"
            else:
                text = (f"【技能: {name}】{getattr(skill, 'description', '') or ''}\n"
                        f"调用方式：skill_invoke(name=\"{name}\", input=<该技能的输入>)")
                schema = getattr(skill, "input_schema", None)
                if schema is not None:
                    try:
                        props = list(schema.model_json_schema().get("properties", {}))
                        if props:
                            text += f"；输入字段：{', '.join(props[:12])}"
                    except Exception:
                        pass
                kind = "python"
            if budget <= 0:
                dropped.append({"name": name, "reason": "本条引用的技能指令总量已达上限"})
                continue
            text = text[:budget]
            budget -= len(text)
            parts.append(text)
            applied.append({"name": name, "kind": kind})
        for name in [str(x) for x in skill_refs][_REF_MAX:]:
            dropped.append({"name": name, "reason": f"单条消息最多引用 {_REF_MAX} 个技能"})
        if applied:
            prefer.append("skill_invoke")
            blocks.append("【引用技能】用户为本条指定了以下技能，请按技能说明完成任务"
                          "（需要执行技能时调用 skill_invoke 工具）：\n" + "\n\n".join(parts))
        receipt["skills"] = {"applied": applied, "dropped": dropped}
    if not blocks:
        return task, prefer
    return "\n\n".join(blocks) + f"\n---\n{task}", prefer


def _apply_goal(task: str, data: dict, receipt: dict) -> tuple[str, str]:
    """按 overrides.goal 注入长期目标上下文（v1.11.0 目标交付模式）。

    返回 ``(新任务, 目标 id)``；目标 id 非空时，任务结束后要把这一轮记进目标。
    """
    ov = data.get("overrides") if isinstance(data.get("overrides"), dict) else {}
    gid = str(ov.get("goal") or "").strip()
    if not gid:
        return task, ""
    from automind import goals as _goals
    try:
        goal = _goals.get_store().get(gid)
    except Exception as e:
        logger.warning("goal_load_failed", goal=gid, error=str(e))
        goal = None
    if goal is None:
        receipt["goal"] = {"value": gid, "applied": False, "reason": "目标不存在或已删除"}
        return task, ""
    if goal.get("status") == "archived":
        receipt["goal"] = {"value": gid, "applied": False,
                           "reason": "目标已归档，请先在目标面板里恢复"}
        return task, ""
    receipt["goal"] = {"value": gid, "applied": True, "name": goal.get("title", ""),
                       "runs": int(goal.get("run_count") or 0)}
    return f"{_goals.GoalStore.context_block(goal)}\n---\n{task}", gid


def _goal_finish(goal_ctx: dict, session_id: str, raw_task: str) -> dict | None:
    """挂在目标下的一轮任务结束 —— 把成败、结果与「目标进度」记进目标。

    成功 / 失败从本轮的历史记录取（各执行分支都会 ``_push_history``）；
    找不到记录 = 被停止或执行出错。目标是增强项，任何异常只记日志。
    """
    gid = (goal_ctx or {}).get("id")
    if not gid:
        return None
    from automind import goals as _goals
    rec = next((r for r in reversed(_task_history)
                if r.get("session_id") == session_id), None)
    try:
        if rec is not None:
            return _goals.get_store().record_run(
                gid, task=raw_task, success=bool(rec.get("success")),
                output=str(goal_ctx.get("output") or rec.get("output") or ""),
                interaction=str(rec.get("interaction") or ""),
                tokens=int(rec.get("tokens") or 0), session_id=session_id,
                duration_ms=float(rec.get("duration_ms") or 0))
        return _goals.get_store().record_run(
            gid, task=raw_task, success=False, status="interrupted",
            output="本轮任务未正常完成（被停止或执行出错）", session_id=session_id)
    except Exception as e:
        logger.warning("goal_record_failed", goal=gid, error=str(e))
        return None


def _apply_expert_override(task: str, data: dict, receipt: dict) -> str:
    """按 overrides.expert 注入专家（缺省则沿用全局激活的专家）。"""
    ov = data.get("overrides") if isinstance(data.get("overrides"), dict) else {}
    if "expert" not in ov or ov.get("expert") is None:
        return _apply_expert(task)
    eid = str(ov.get("expert") or "")
    if not eid:
        receipt["expert"] = {"value": "", "applied": True, "name": "不使用专家"}
        return task
    expert = _experts.get(eid)
    if expert is None:
        receipt["expert"] = {"value": eid, "applied": False,
                             "reason": "专家不存在或已卸载，本条未使用专家"}
        return task
    _experts.bump_usage(eid)
    receipt["expert"] = {"value": eid, "applied": True,
                         "name": f"{expert.get('icon', '🎓')} {expert.get('name', eid)}"}
    return (f"【专家模式 · {expert.get('icon', '🎓')} {expert.get('name', eid)}】\n"
            f"{expert.get('prompt', '')}\n---\n{task}")


def _apply_attachments(task: str, data: dict, receipt: dict) -> tuple[str, str]:
    """把文本附件拼进任务；返回 (新任务, 用于语义缓存键的附件指纹)。"""
    ov = data.get("overrides") if isinstance(data.get("overrides"), dict) else {}
    atts = ov.get("attachments") or []
    if not isinstance(atts, list) or not atts:
        return task, ""
    import hashlib
    blocks: list[str] = []
    applied: list[str] = []
    dropped: list[dict] = []
    total = 0
    for a in atts[:_ATTACH_MAX_FILES]:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "附件")[:120]
        text = a.get("text")
        if not isinstance(text, str) or not text.strip():
            dropped.append({"name": name, "reason": "不是文本文件或内容为空"})
            continue
        if total >= _ATTACH_TOTAL_CHARS:
            dropped.append({"name": name, "reason": "超出单条消息附件总量上限"})
            continue
        cut = text[:min(_ATTACH_MAX_CHARS, _ATTACH_TOTAL_CHARS - total)]
        total += len(cut)
        note = "（内容过长，已截断）" if len(cut) < len(text) else ""
        blocks.append(f"=== 附件：{name}{note} ===\n{cut}\n=== 附件结束：{name} ===")
        applied.append(name)
    for a in atts[_ATTACH_MAX_FILES:]:
        dropped.append({"name": str((a or {}).get("name") or "附件")[:120],
                        "reason": f"单条消息最多 {_ATTACH_MAX_FILES} 个附件"})
    receipt["attachments"] = {"applied": applied, "dropped": dropped}
    if not blocks:
        return task, ""
    digest = hashlib.sha1("\n".join(blocks).encode("utf-8", "ignore")).hexdigest()[:16]
    return (f"{task}\n\n【用户附件】以下是用户随消息附上的文件内容：\n"
            + "\n\n".join(blocks)), f"\n[attachments:{digest}]"


# ═══════════════════════════════════════════════════════════
# v1.11.2 输入框 @ 引用：@文件 / @目录 / @对话
# ═══════════════════════════════════════════════════════════

#: 单条消息最多 @ 几项、单个文件注入多少字符、全部引用合计多少字符
_MENTION_MAX = 10
_MENTION_FILE_CHARS = 60_000
_MENTION_TOTAL_CHARS = 200_000
_MENTION_CONV_CHARS = 12_000
#: 正文里手打的 @路径（没从弹出框里选）也认：只认真实存在的项目内文件
_MENTION_RE = re.compile(r"(?:^|(?<=\s))@([^\s@]{1,240})")


def _files_search_sync(root: Path, q: str, limit: int, dirs: bool) -> list[dict]:
    """项目内按路径模糊查找（@ 弹出框用）。

    排序：文件名以查询开头 > 文件名包含 > 路径包含 > 按字符顺序出现（子序列）。
    最多扫 20000 个条目，排除依赖 / 缓存 / 隐藏目录。
    """
    ql = q.strip().lower().replace("\\", "/").lstrip("./")
    hits: list[tuple[int, int, str, bool]] = []
    seen = 0

    def _subseq(needle: str, hay: str) -> bool:
        it = iter(hay)
        return all(ch in it for ch in needle)

    for base, dnames, fnames in os.walk(root):
        dnames[:] = sorted(d for d in dnames if d not in _TREE_EXCLUDE and not d.startswith("."))
        rel_base = os.path.relpath(base, root).replace("\\", "/")
        rel_base = "" if rel_base == "." else rel_base + "/"
        entries = [(d, True) for d in dnames] if dirs else []
        entries += [(f, False) for f in sorted(fnames) if not f.startswith(".")]
        for nm, is_dir in entries:
            seen += 1
            if seen > 20000:
                break
            rel = rel_base + nm
            low, name_low = rel.lower(), nm.lower()
            if not ql:
                score = 3
            elif name_low.startswith(ql):
                score = 0
            elif ql in name_low:
                score = 1
            elif ql in low:
                score = 2
            elif len(ql) >= 2 and _subseq(ql, low):
                score = 4
            else:
                continue
            hits.append((score, rel.count("/"), rel, is_dir))
        if seen > 20000:
            break
    hits.sort(key=lambda h: (h[0], h[1], len(h[2]), h[2]))
    out = []
    for score, _, rel, is_dir in hits[:limit]:
        item: dict[str, Any] = {"path": rel, "dir": is_dir}
        if not is_dir:
            try:
                item["size"] = (root / rel).stat().st_size
            except OSError:
                pass
        out.append(item)
    return out


@app.get("/api/files/search")
async def api_files_search(q: str = "", limit: int = 30, dirs: bool = True):
    """项目内文件模糊查找（v1.11.2，输入框 @ 引用的候选）。"""
    limit = max(1, min(int(limit or 30), 100))
    items = await asyncio.to_thread(_files_search_sync, _editor_root(), q, limit, dirs)
    return {"items": items, "root": str(_editor_root())}


def _read_mention_file(path: str, budget: int) -> tuple[str, str]:
    """读一个被 @ 的文件 / 目录，返回 (注入块, 失败原因)。"""
    target, err = _editor_target(path)
    if target is None:
        return "", err
    if target.is_dir():
        rows: list[str] = []
        for p in sorted(target.rglob("*"))[:400]:
            parts = p.relative_to(target).parts
            if any(x in _TREE_EXCLUDE or x.startswith(".") for x in parts):
                continue
            rows.append(("  " * (len(parts) - 1)) + p.name + ("/" if p.is_dir() else ""))
            if len(rows) >= 200:
                rows.append("…（目录较大，只列出前 200 项；需要哪个文件请再 @ 它）")
                break
        return (f"=== 引用目录：{path}/ ===\n" + ("\n".join(rows) or "（空目录）")
                + f"\n=== 目录结束：{path}/ ==="), ""
    if not target.is_file():
        return "", "文件不存在"
    try:
        raw = target.read_bytes()
    except OSError as e:
        return "", f"读取失败：{e}"
    if b"\0" in raw[:4096]:
        return "", "二进制文件不能直接引用（图片请用附件，文档请上传到知识库）"
    text = raw.decode("utf-8", errors="replace")
    cap = min(_MENTION_FILE_CHARS, budget)
    note = f"（共 {len(text)} 字符，只注入了前 {cap} 字符；需要后面的内容请用 file_read 分段读取）" \
        if len(text) > cap else ""
    return (f"=== 引用文件：{path}{note} ===\n{text[:cap]}\n=== 文件结束：{path} ==="), ""


def _conversation_block(m: dict, budget: int) -> tuple[str, str]:
    """被 @ 的对话：优先用界面带来的记录，其次服务端历史，再其次会话摘要。"""
    sid = str(m.get("sid") or "")[:160]
    title = str(m.get("title") or sid or "对话")[:80]
    lines: list[str] = []
    transcript = m.get("transcript")
    if isinstance(transcript, list):
        for t in transcript[-60:]:
            if isinstance(t, dict) and str(t.get("content") or "").strip():
                who = "用户" if t.get("role") == "user" else "助手"
                lines.append(f"{who}：{str(t['content'])[:1500]}")
    if not lines and sid:
        try:
            for t in _get_session_history(sid)[-40:]:
                c = str((t or {}).get("content") or "").strip()
                if c:
                    lines.append(f"{'用户' if t.get('role') == 'user' else '助手'}：{c[:1500]}")
        except Exception:
            pass
    summary = ""
    if sid:
        try:
            c = _conv_store().get(sid) or {}
            summary = str(c.get("summary") or "")
            title = str(c.get("title") or title)[:80]
        except Exception:
            pass
    if not lines and not summary:
        return "", "这个对话还没有内容"
    body = "\n".join(lines)
    cap = min(_MENTION_CONV_CHARS, budget)
    if len(body) > cap:
        body = "…（更早的内容已省略）\n" + body[-cap:]
    head = f"要点：{summary}\n" if summary else ""
    return f"=== 引用对话：{title} ===\n{head}{body}\n=== 对话结束：{title} ===", ""


def _apply_mentions(task: str, data: dict, receipt: dict) -> tuple[str, str]:
    """按 overrides.mentions（以及正文里手打的 @路径）注入引用内容（v1.11.2）。

    返回 ``(新任务, 用于语义缓存键的指纹)``。逐项回执：不存在 / 越界 / 二进制 /
    超量都要说清 —— "@ 了却没生效"不能悄悄发生。
    """
    import hashlib

    ov = data.get("overrides") if isinstance(data.get("overrides"), dict) else {}
    raw = ov.get("mentions") if isinstance(ov.get("mentions"), list) else []
    items: list[dict] = [m for m in raw if isinstance(m, dict)]
    picked = {str(m.get("path") or "") for m in items if m.get("type") in ("file", "dir")}
    # 手打的 @src/app.py：只有真实存在的项目内文件才当引用，其余（@某人）原样保留
    for tok in _MENTION_RE.findall(str(data.get("task") or "")):
        tok = tok.rstrip("，。,.;；:：!！?？)）]】'\"")
        if tok in picked or len(items) >= _MENTION_MAX * 2:
            continue
        target, _ = _editor_target(tok)
        if target is not None and target.exists():
            items.append({"type": "dir" if target.is_dir() else "file", "path": tok})
            picked.add(tok)
    if not items:
        return task, ""
    applied: list[dict] = []
    dropped: list[dict] = []
    blocks: list[str] = []
    budget = _MENTION_TOTAL_CHARS
    for m in items[:_MENTION_MAX]:
        kind = str(m.get("type") or "")
        label = str(m.get("path") or m.get("title") or m.get("sid") or "?")[:200]
        if budget <= 0:
            dropped.append({"type": kind, "name": label, "reason": "本条引用的内容总量已达上限"})
            continue
        if kind in ("file", "dir"):
            block, err = _read_mention_file(str(m.get("path") or ""), budget)
        elif kind == "conversation":
            block, err = _conversation_block(m, budget)
        else:
            block, err = "", f"不支持的引用类型：{kind}"
        if err:
            dropped.append({"type": kind, "name": label, "reason": err})
            continue
        budget -= len(block)
        blocks.append(block)
        applied.append({"type": kind, "name": label})
    for m in items[_MENTION_MAX:]:
        dropped.append({"type": str(m.get("type") or ""),
                        "name": str(m.get("path") or m.get("title") or "?")[:200],
                        "reason": f"单条消息最多引用 {_MENTION_MAX} 项"})
    receipt["mentions"] = {"applied": applied, "dropped": dropped}
    if not blocks:
        return task, ""
    digest = hashlib.sha1("\n".join(blocks).encode("utf-8", "ignore")).hexdigest()[:16]
    return (f"{task}\n\n【用户引用】消息里用 @ 引用了以下内容（已读取，可直接使用，"
            f"不必再用工具重新读取）：\n" + "\n\n".join(blocks)), f"\n[mentions:{digest}]"


def _resume_target(data: dict, chat_sid: str) -> tuple[dict | None, str]:
    """校验 WS/REST 请求里的 resume_checkpoint：必须属于本会话且确实可续跑。"""
    cid = str(data.get("resume_checkpoint") or "")
    if not cid:
        return None, ""
    from automind.state.checkpoint import session_of
    if session_of(cid) != chat_sid:
        return None, "该断点不属于当前会话"
    try:
        info = get_agent().checkpoint_mgr.resumable_for_session(chat_sid)
    except Exception as e:
        return None, f"读取断点失败：{e}"
    if info is None or info.get("id") != cid:
        return None, "该断点已失效（任务已完成、已续跑过或已被更新的任务取代）"
    return info, ""


def _apply_conversation_context(task: str, chat_sid: str) -> str:
    """会话首轮注入"延续上下文 + 项目记忆"，并刷新会话目录（v1.7.6）。

    只在会话**第一轮**注入：之后这些内容已经在会话历史 / Agent 上下文里了，
    每轮再发一遍就是重复付费。任何异常都只记日志 —— 会话目录是增强项，
    不能因为它让任务跑不起来。
    """
    try:
        from automind.conversations import get_store
        store = get_store()
        prefix = store.context_prefix(chat_sid)
        store.touch(chat_sid, task)
        if prefix:
            logger.info("conversation_context_injected", sid=chat_sid,
                        chars=len(prefix))
            return f"{prefix}\n---\n{task}"
    except Exception as e:
        logger.warning("conversation_context_failed", sid=chat_sid, error=str(e))
    return task


def _kb_limits() -> dict:
    """当前版本的知识库限额（None=不限）。"""
    rag_pro = _edition.get_feature("rag_pro")
    if rag_pro is not None:
        return {"docs": None, "size": rag_pro.size_limit(), "multi_kb": True}
    return {"docs": _KB_FREE_DOCS, "size": _KB_FREE_SIZE, "multi_kb": False}


@app.get("/api/kb")
async def api_kb_list():
    """知识库总览：库列表 + 文档 + 限额 + 设置。"""
    store = _kb_store()
    limits = _kb_limits()
    return {
        "kbs": store.list_kbs(),
        "docs": store.list_docs(),
        "total_size": store.total_size(),
        "limits": limits,
        "pro": limits["multi_kb"],
        "enterprise": _edition.has_feature("kb_enterprise"),
        "auto_retrieve": bool(_read_config().get("kb_auto", True)),
        "settings": store.get_settings(),
    }


@app.post("/api/kb/upload")
async def api_kb_upload(data: dict):
    """上传文档（JSON：{name, content_b64, kb?}），解析 → 分段 → embedding。"""
    import base64

    name = (data.get("name") or "").strip()
    b64 = data.get("content_b64") or ""
    kb_id = (data.get("kb") or "default").strip() or "default"
    if not name or not b64:
        return JSONResponse({"error": "name 与 content_b64 必填"}, status_code=400)
    try:
        raw = base64.b64decode(b64.split(",", 1)[-1])
    except Exception:
        return JSONResponse({"error": "content_b64 解码失败"}, status_code=400)

    store = _kb_store()
    limits = _kb_limits()
    if not limits["multi_kb"] and kb_id != "default":
        return JSONResponse({"error": _edition.upgrade_hint("rag_pro"),
                             "feature": "rag_pro"}, status_code=403)
    if limits["docs"] is not None and store.doc_count() >= limits["docs"]:
        return JSONResponse(
            {"error": f"社区版知识库最多 {limits['docs']} 个文档。"
                      "升级专业版可解锁无限文档 / 200MB / 多知识库。"},
            status_code=403)
    if limits["size"] is not None and store.total_size() + len(raw) > limits["size"]:
        mb = limits["size"] // (1024 * 1024)
        return JSONResponse(
            {"error": f"知识库总量将超过 {mb}MB 上限，请删除部分文档或升级版本。"},
            status_code=403)
    try:
        meta = store.add_document(name, raw, kb_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"status": "ok", "doc": meta}


@app.delete("/api/kb/doc/{doc_id}")
async def api_kb_doc_delete(doc_id: str):
    if _kb_store().delete_document(doc_id):
        return {"status": "ok"}
    return JSONResponse({"error": f"文档不存在: {doc_id}"}, status_code=404)


@app.post("/api/kb/search")
async def api_kb_search(data: dict):
    """检索知识库（专业版启用 Reranker；企业版再叠加混合检索 + 审计日志）。"""
    query = (data.get("query") or "").strip()
    if not query:
        return JSONResponse({"error": "query 必填"}, status_code=400)
    rerank = _edition.has_feature("rag_pro")
    hybrid = _edition.has_feature("kb_enterprise")
    store = _kb_store()
    results = store.search(query, top_k=int(data.get("top_k", 5)),
                           kb_id=data.get("kb") or None,
                           rerank=rerank, hybrid=hybrid)
    if hybrid:
        store.log_search(query, results, source="api")
    return {"results": results, "reranked": rerank, "hybrid": hybrid}


@app.post("/api/kb/auto")
async def api_kb_auto(data: dict):
    """开关：对话中自动检索知识库。"""
    cfg = _read_config()
    cfg["kb_auto"] = bool(data.get("enabled", True))
    _write_config(cfg)
    return {"status": "ok", "auto_retrieve": cfg["kb_auto"]}


#: 用片段开头这么多字符做"已在场"的判定指纹
_KB_FINGERPRINT = 80


def _kb_already_visible(hist: list, text: str) -> bool:
    """该片段是否已经在模型看得见的窗口里。

    窗口长度不再硬编码：必须与 Agent.chat / chat_stream 真正发出去的历史
    窗口同源（``core.chat_window.chat_history_window``，按格点裁剪以便前缀
    缓存命中）。两边各写一份必然会漂移 —— 服务端以为"还看得见"而跳过注入，
    模型其实早被挤出去了，用户看到的就是"知识库明明有这段，它却答不出来"。
    """
    fp = (text or "")[:_KB_FINGERPRINT]
    if not fp:
        return True
    for m in chat_history_window(hist):
        c = m.get("content")
        if isinstance(c, str) and fp in c:
            return True
    return False


def _apply_kb(task: str, hist: list | None = None) -> str:
    """对话前自动检索知识库，把**尚未在场**的片段注入对话上下文。

    专业版（rag_pro）：启用 Reranker，注入内容带编号引用（引用溯源），
    并要求模型回答时标注来源；社区版注入纯参考片段。

    v1.6.3 之前的做法是：把片段拼进用户这句话前面发出去，生成完再把历史里的
    那条还原成用户原话。后果是**每一轮都要重新注入一遍**同样的片段 ——
    连问五个关于同一份文档的问题，同样那 500~900 token 就付了五遍，
    而模型在第二轮开始其实已经看不到第一轮的片段了（被还原掉了），
    所以还非注入不可。

    现在改成：片段作为一条独立的 system 消息**留在会话历史里**，并按
    "这段是否已经在窗口内"逐条去重。同一份资料只付一次钱；换了话题、
    检索到新片段时才会再注入新的那几段。用户原话始终原样保留（不再需要
    事后还原），历史看起来也和用户实际说的一致。
    """
    try:
        if not _read_config().get("kb_auto", True):
            return task
        store = _kb_store()
        if store.doc_count() == 0:
            return task
        pro = _edition.has_feature("rag_pro")
        ent = _edition.has_feature("kb_enterprise")
        hits = store.search(task, top_k=3, rerank=pro, hybrid=ent, min_score=0.18)
        if not hits:
            return task
        if ent:
            store.log_search(task, hits, source="chat")
        h = hist if isinstance(hist, list) else []
        fresh = [x for x in hits if not _kb_already_visible(h, x["text"][:600])]
        if not fresh:
            # 命中的片段模型已经看得见了 —— 再发一遍纯属重复付费
            logger.info("kb_injection_skipped", hits=len(hits), reason="already_in_context")
            return task
        if pro:
            refs = "\n".join(
                f"[{i+1}]（{x['doc_name']} · 第{x['seq']+1}段）{x['text'][:600]}"
                for i, x in enumerate(fresh))
            block = (f"【知识库参考】以下片段检索自用户知识库，回答时优先依据这些内容，"
                     f"引用处请标注编号（如 [1]）与来源：\n{refs}")
        else:
            refs = "\n".join(f"-（{x['doc_name']}）{x['text'][:600]}" for x in fresh)
            block = (f"【知识库参考】以下片段检索自用户知识库，回答时优先参考：\n{refs}")
        if hist is None:
            # 没有历史可挂（老调用方）→ 退回旧的"拼进本轮提问"方式
            return f"{block}\n---\n{task}"
        hist.append({"role": "system", "content": block})
        logger.info("kb_injected", fresh=len(fresh), hits=len(hits))
        return task
    except Exception as e:
        logger.warning("kb_apply_failed", error=str(e))
        return task


def _apply_router(agent, raw_task: str, images: list | None):
    """模型智能路由（专业版特性 model_router）：按复杂度切换本次任务的模型。"""
    router = _edition.get_feature("model_router")
    if router is None:
        return agent
    try:
        sel = router.select(raw_task, interaction=agent._interaction.value,
                            has_images=bool(images))
    except Exception as e:
        logger.warning("router_select_failed", error=str(e))
        return agent
    if not sel:
        return agent
    if (sel["provider"], sel["model"]) != (agent.config.llm.provider,
                                           agent.config.llm.model):
        # 只换本会话 agent 的 LLM —— 路由是逐任务决策，绝不能改写全局实例，
        # 否则并发任务会互相把对方路由到的模型顶掉
        agent.switch_llm(_llm_config_for(agent, sel["provider"], sel["model"]))
        logger.info("router_switched", tier=sel.get("tier"), score=sel.get("score"),
                    provider=sel["provider"], model=sel["model"])
    return agent


def _cache_lookup(raw_task: str, images: list | None) -> dict | None:
    """语义缓存查询（专业版特性 semantic_cache；多模态输入不缓存）。"""
    if images:
        return None
    cache = _edition.get_feature("semantic_cache")
    if cache is None:
        return None
    try:
        return cache.lookup(raw_task)
    except Exception as e:
        logger.warning("cache_lookup_failed", error=str(e))
        return None


def _cache_store(raw_task: str, images: list | None, reply: str,
                 tokens: int) -> None:
    if images:
        return
    cache = _edition.get_feature("semantic_cache")
    if cache is None:
        return
    try:
        cache.store(raw_task, reply, tokens)
    except Exception as e:
        logger.warning("cache_store_failed", error=str(e))


# ── 自动更新（GitHub Releases；桌面版可一键静默升级）──


@app.get("/api/update/check")
async def api_update_check(force: bool = False):
    """检查新版本（6h 缓存；force=true 强制刷新）。"""
    import asyncio as _aio

    from automind.core import updater
    return await _aio.to_thread(updater.check, force)


@app.post("/api/update/apply")
async def api_update_apply():
    """桌面版：下载 → 签名校验 → 静默升级并自动重启；pip 模式返回升级命令提示。"""
    from automind.core import updater
    return updater.apply_update()


@app.get("/api/update/state")
async def api_update_state():
    """升级进度（downloading/verifying/installing/error + progress%）。"""
    from automind.core import updater
    return updater.state()


@app.get("/api/quota")
async def api_quota():
    """版本限额快照：每日任务用量 / 工作区上限 / 知识库限额。"""
    snap = _quota.snapshot()
    snap["workspaces"]["used"] = len(_read_config().get("workspaces", []))
    limits = _kb_limits()
    store = _kb_store()
    snap["kb"] = {"docs_used": store.doc_count(), "docs_limit": limits["docs"],
                  "size_used": store.total_size(), "size_limit": limits["size"]}
    return snap


# ═══════════════════════════════════════════════════════════
# REST API — 团队协作（任务分配 / 活动通知；共享语义见手册 8.14）
# ═══════════════════════════════════════════════════════════

_TEAM_FILE = _legacy_file("team_tasks.json")   # 旧文件（仅迁移用）


def _team_load() -> list[dict]:
    """团队任务（SQLite；旧 JSON 自动一次性迁移）。"""
    try:
        db = _db_mod.get_db()
        _db_mod.migrate_json_once(
            db, "team_tasks", _TEAM_FILE,
            lambda data: db.team_replace(data) if isinstance(data, list) else None)
        return db.team_load()
    except Exception:
        return []


def _team_save(items: list[dict]) -> None:
    try:
        _db_mod.get_db().team_replace(items[-200:])
    except Exception:
        pass


def _team_event(sid: str, task: str, record: dict) -> dict:
    """构造任务完成的团队活动事件（含本次改动的文件数）。"""
    from automind.tools.file_editor import JOURNAL
    return {"type": "team_activity", "kind": "task_done", "sid": sid,
            "task": (task or "")[:80], "success": bool(record.get("success")),
            "interaction": record.get("interaction", ""),
            "changed_files": len({c["path"] for c in JOURNAL.entries()[:20]}),
            "time": time.strftime("%H:%M:%S")}


@app.get("/api/team/tasks")
async def api_team_tasks():
    return {"tasks": _team_load()}


@app.post("/api/team/tasks")
async def api_team_task_add(data: dict):
    """分配一个团队任务（title 必填；assignee 为成员名/会话名）。"""
    title = (data.get("title") or "").strip()[:120]
    if not title:
        return JSONResponse({"error": "title 必填"}, status_code=400)
    items = _team_load()
    task = {"id": uuid.uuid4().hex[:8], "title": title,
            "assignee": (data.get("assignee") or "").strip()[:40],
            "desc": (data.get("desc") or "").strip()[:500],
            "status": "todo", "creator": (data.get("creator") or "").strip()[:40],
            "created": time.strftime("%Y-%m-%d %H:%M")}
    items.append(task)
    _team_save(items)
    await _broadcast({"type": "team_activity", "kind": "task_assigned",
                      "title": title, "assignee": task["assignee"],
                      "sid": data.get("session_id") or ""})
    return {"status": "ok", "task": task}


@app.post("/api/team/tasks/{tid}")
async def api_team_task_update(tid: str, data: dict):
    """更新团队任务状态：todo / doing / done。"""
    status = (data.get("status") or "").strip()
    if status not in ("todo", "doing", "done"):
        return JSONResponse({"error": "status 必须是 todo/doing/done"}, status_code=400)
    items = _team_load()
    for t in items:
        if t["id"] == tid:
            t["status"] = status
            _team_save(items)
            return {"status": "ok", "task": t}
    return JSONResponse({"error": f"任务不存在: {tid}"}, status_code=404)


@app.delete("/api/team/tasks/{tid}")
async def api_team_task_delete(tid: str):
    items = _team_load()
    kept = [t for t in items if t["id"] != tid]
    if len(kept) == len(items):
        return JSONResponse({"error": f"任务不存在: {tid}"}, status_code=404)
    _team_save(kept)
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════
# REST API — 代码编辑器（Web IDE：文件树 / 读写 / Diff 预览）
# ═══════════════════════════════════════════════════════════

_TREE_EXCLUDE = {".git", "__pycache__", "node_modules", ".automind", ".reasonix",
                 ".venv", "venv", "dist", "build", ".idea", ".vscode",
                 ".pytest_cache", ".ruff_cache", ".mypy_cache", ".claude"}
_EDITOR_MAX_BYTES = 1_500_000  # 编辑器单文件上限（约 1.5MB 文本）


def _editor_root() -> Path:
    return Path(get_agent().config.project_root).resolve()


def _editor_target(path: str) -> tuple[Path | None, str]:
    """把（相对/绝对）路径解析到项目根内；越界返回错误。"""
    root = _editor_root()
    try:
        p = Path(path)
        target = (root / p).resolve() if not p.is_absolute() else p.resolve()
    except Exception:
        return None, "非法路径"
    if root not in target.parents and target != root:
        return None, "路径超出项目目录范围"
    # v1.7.3：仅校验"在项目目录内"是不够的 —— 平台的密钥与数据目录就在项目里
    # （默认 project_root 就是启动目录）。见 automind/core/sensitive.py。
    from automind.core.sensitive import SCOPE_WEB
    from automind.core.sensitive import reason as _sensitive_reason
    denied = _sensitive_reason(target, SCOPE_WEB)
    if denied:
        logger.warning("sensitive_file_access_denied", path=str(target))
        return None, denied
    return target, ""


def _files_tree_sync(root: Path, limit: int, depth: int) -> dict:
    """项目文件树的同步实现（递归 iterdir + stat，必须放到线程池）。"""
    entries: list[dict] = []

    def _walk(base: Path, level: int) -> None:
        if level > depth or len(entries) >= limit:
            return
        try:
            children = sorted(base.iterdir(),
                              key=lambda p: (not p.is_dir(), p.name.lower()))
        except Exception:
            return
        for p in children:
            if len(entries) >= limit:
                return
            if p.name in _TREE_EXCLUDE or p.name.startswith("."):
                continue
            rel = str(p.relative_to(root)).replace("\\", "/")
            if p.is_dir():
                entries.append({"path": rel, "dir": True, "level": level})
                _walk(p, level + 1)
            else:
                entries.append({"path": rel, "dir": False, "level": level,
                                "size": p.stat().st_size})

    _walk(root, 0)
    return {"root": str(root), "entries": entries,
            "truncated": len(entries) >= limit}


@app.get("/api/files/tree")
async def api_files_tree(limit: int = 800, depth: int = 6):
    """项目文件树（扁平列表，前端折叠渲染）；排除依赖/缓存目录。"""
    return await asyncio.to_thread(_files_tree_sync, _editor_root(), limit, depth)


def _files_read_sync(target: Path, path: str) -> dict:
    """编辑器读文件的同步实现（stat + 读盘，交由线程池执行）。"""
    if not target.is_file():
        return {"_error": f"文件不存在: {path}", "_status": 404}
    st = target.stat()
    if st.st_size > _EDITOR_MAX_BYTES:
        return {"_error": "文件过大，编辑器仅支持 1.5MB 以内的文本文件", "_status": 413}
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"_error": str(e), "_status": 400}
    return {"path": path, "content": content,
            "size": st.st_size, "mtime": st.st_mtime}


@app.get("/api/files/read")
async def api_files_read(path: str):
    """读取项目内文本文件（供编辑器打开）。"""
    target, err = _editor_target(path)
    if target is None:
        return JSONResponse({"error": err}, status_code=403)
    r = await asyncio.to_thread(_files_read_sync, target, path)
    if "_error" in r:
        return JSONResponse({"error": r["_error"]}, status_code=r["_status"])
    return r


def _files_write_sync(target: Path, content: str) -> dict:
    """编辑器写文件的同步实现（读前像 + 落盘，交由线程池执行）。"""
    from automind.tools.file_editor import JOURNAL
    try:
        existed = target.exists()
        if existed:
            try:
                JOURNAL.record(str(target),
                               target.read_text(encoding="utf-8"), "editor")
            except Exception:
                pass  # 前像读不出（二进制等）则不记录，仍允许保存
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            JOURNAL.record(str(target), None, "editor")
        target.write_text(content, encoding="utf-8")
    except Exception as e:
        return {"_error": str(e), "_status": 400}
    return {"created": not existed}


@app.post("/api/files/write")
async def api_files_write(data: dict):
    """保存编辑器内容（前像记入改动日志 → 右栏「文件改动」可撤销）。"""
    path = (data.get("path") or "").strip()
    content = data.get("content")
    if not path or not isinstance(content, str):
        return JSONResponse({"error": "path 与 content 必填"}, status_code=400)
    if len(content.encode("utf-8", errors="ignore")) > _EDITOR_MAX_BYTES:
        return JSONResponse({"error": "内容过大（上限 1.5MB）"}, status_code=413)
    target, err = _editor_target(path)
    if target is None:
        return JSONResponse({"error": err}, status_code=403)
    r = await asyncio.to_thread(_files_write_sync, target, content)
    if "_error" in r:
        return JSONResponse({"error": r["_error"]}, status_code=r["_status"])
    logger.info("editor_saved", path=path, size=len(content))
    return {"status": "ok", "path": path, "size": len(content),
            "created": r["created"]}


def _read_text_safe(target: Path) -> str:
    """尽力读出文本；读不出（不存在/二进制/无权限）返回空串。"""
    if not target.is_file():
        return ""
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


@app.get("/api/changes/diff")
async def api_changes_diff(path: str):
    """Diff 预览数据：该文件的最早前像（改动前）与当前内容。"""
    from automind.tools.file_editor import JOURNAL
    target, err = _editor_target(path)
    if target is None:
        return JSONResponse({"error": err}, status_code=403)
    pre = JOURNAL.pre_image(str(target))
    if pre is None:
        return JSONResponse({"error": "该文件没有记录在案的改动"}, status_code=404)
    current = await asyncio.to_thread(_read_text_safe, target)
    return {"path": path, "before": pre["before"] or "",
            "after": current, "created": pre["created"]}


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
    # 版本限额：工作区数量（社区 3 / 专业 30 / 企业不限）；同名更新不占新额度
    ok, reason = _quota.check_workspace(len(spaces))
    if not ok:
        return JSONResponse({"error": reason, "quota": _quota.snapshot()},
                            status_code=403)
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
# REST API — 会话目录：多会话 / 归档 / 上下文延续（v1.7.6）
# ═══════════════════════════════════════════════════════════


def _conv_store():
    from automind.conversations import get_store
    return get_store()


def _conv_owned(conv: dict | None, owner: str) -> bool:
    """会话只能由创建它的浏览器会话操作（多用户隔离）。"""
    from automind.conversations import _clean
    return conv is not None and conv.get("owner", "") == _clean(owner)


@app.get("/api/conversations")
async def api_conversations(owner: str = "", workspace: str = ""):
    """列出本工作区的会话（含已归档），并确保旧版"默认会话"在列。"""
    from automind.conversations import _clean
    store = _conv_store()
    await asyncio.to_thread(store.ensure_default, owner, workspace)
    items = await asyncio.to_thread(store.list, _clean(owner), _clean(workspace))
    for c in items:
        c["running"] = c.get("sid") in _live_runs
        c.pop("seed", None)             # 种子可能很长，列表里用不到
    return {"conversations": items}


@app.post("/api/conversations")
async def api_conversation_create(data: dict):
    """新建会话；带 ``continue_from`` 时以源会话摘要为种子"接着聊"。"""
    from automind.conversations import extractive_summary
    store = _conv_store()
    owner = str(data.get("owner") or "")
    workspace = str(data.get("workspace") or "")
    seed, seed_from, title = "", "", str(data.get("title") or "")
    src_sid = str(data.get("continue_from") or "")
    if src_sid:
        src = store.get(src_sid)
        if not _conv_owned(src, owner):
            return JSONResponse({"error": "源会话不存在"}, status_code=404)
        seed = src.get("summary") or ""
        if not seed:
            transcript = data.get("transcript") or _get_session_history(src_sid)
            seed = extractive_summary(transcript)
        seed_from = src_sid
        title = title or f"续：{src.get('title') or '会话'}"[:60]
    conv = await asyncio.to_thread(store.create, owner, workspace, title, seed, seed_from)
    conv.pop("seed", None)
    conv["seeded"] = bool(seed)
    return {"status": "ok", "conversation": conv}


@app.patch("/api/conversations/{sid}")
async def api_conversation_update(sid: str, data: dict):
    """重命名 / 置顶 / 取消归档。归档走专门的 /archive（要生成摘要）。"""
    store = _conv_store()
    if not _conv_owned(store.get(sid), str(data.get("owner") or "")):
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    fields = {k: data[k] for k in ("title", "pinned") if k in data}
    if data.get("archived") is False:
        fields["archived"] = False
    conv = store.update(sid, **fields)
    return {"status": "ok", "conversation": conv}


async def _summarize_conversation(transcript: list[dict]) -> tuple[str, str]:
    """生成归档摘要：优先让模型写（限时 45 秒），失败回退抽取式。"""
    from automind.conversations import SUMMARY_CHARS, extractive_summary
    fallback = extractive_summary(transcript)
    agent = get_agent()
    if agent is None or getattr(agent, "llm", None) is None or not transcript:
        return fallback, "extractive"
    lines = []
    for m in transcript[-40:]:
        role = "用户" if m.get("role") == "user" else "助手"
        lines.append(f"{role}：{' '.join(str(m.get('content') or '').split())[:600]}")
    prompt = ("把下面这段对话整理成一份供日后延续工作用的归档摘要（不超过 300 字）。"
              "按「目标 / 已完成 / 关键结论与决定 / 未完成与下一步」四项写，"
              "保留文件名、命令、参数等具体信息，不要寒暄。\n\n" + "\n".join(lines))
    try:
        resp = await asyncio.wait_for(
            agent.llm.generate([{"role": "user", "content": prompt}]), timeout=45)
        text = (getattr(resp, "text", "") or "").strip()
        if text:
            return text[:SUMMARY_CHARS], "llm"
    except Exception as e:
        logger.warning("conversation_summary_llm_failed", error=str(e)[:200])
    return fallback, "extractive"


@app.post("/api/conversation-summary")
async def api_conversation_summary(data: dict):
    """右栏「摘要」的要点总结（v1.11.5）：不归档、不落库，只把当前对话浓缩成几行。

    与归档摘要同一套实现（模型限时 45 秒，失败回退抽取式），所以没配模型也有结果。
    """
    raw = data.get("transcript")
    if not isinstance(raw, list):
        return JSONResponse({"error": "transcript 必须是 [{role, content}] 列表"}, status_code=400)
    transcript = [{"role": "user" if str(m.get("role")) == "user" else "assistant",
                   "content": str(m.get("content") or "")[:1500]}
                  for m in raw[-60:] if isinstance(m, dict) and str(m.get("content") or "").strip()]
    if not transcript:
        return {"summary": "", "by": "none", "turns": 0}
    summary, how = await _summarize_conversation(transcript)
    return {"summary": summary, "by": how,
            "turns": sum(1 for m in transcript if m["role"] == "user")}


@app.post("/api/conversations/{sid}/archive")
async def api_conversation_archive(sid: str, data: dict):
    """归档会话：生成摘要 → 写入工作区长期记忆 → 标记归档。

    前端会随请求带上它本地的消息记录（工作/编程模式的过程只存在前端），
    服务端没有时再退回服务端的对话历史。
    """
    store = _conv_store()
    conv = store.get(sid)
    if not _conv_owned(conv, str(data.get("owner") or "")):
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    if sid in _live_runs:
        return JSONResponse({"error": "该会话有任务正在执行，请先停止再归档"},
                            status_code=409)
    transcript = [m for m in (data.get("transcript") or []) if isinstance(m, dict)]
    if not transcript:
        transcript = [m for m in _get_session_history(sid)
                      if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    summary, how = "", "none"
    if data.get("summarize", True):
        summary, how = await _summarize_conversation(transcript)
    memorized = False
    if summary:
        try:
            agent = get_agent()
            await agent.memory.long_term.add(
                documents=[f"[归档会话]《{conv.get('title')}》{summary}"],
                metadatas=[{"type": "conversation_summary", "sid": sid,
                            "workspace": conv.get("workspace", "")}],
                ids=[f"conv_{uuid.uuid4().hex[:12]}"])
            memorized = True
        except Exception as e:
            logger.warning("conversation_memory_store_failed", sid=sid, error=str(e)[:200])
    extra: dict = {}
    if conv.get("title_auto"):
        # 还是占位标题（没在服务端跑过任务）→ 用第一条诉求命名，归档列表里才认得出
        from automind.conversations import auto_title
        first = next((m for m in transcript if m.get("role") == "user"), None)
        if first:
            extra["title"] = auto_title(str(first.get("content") or ""))
    conv = store.update(sid, archived=True, summary=summary or conv.get("summary", ""),
                        archived_at=time.time(), **extra)
    logger.info("conversation_archived", sid=sid, summary=how, memorized=memorized)
    return {"status": "ok", "conversation": conv, "summary_by": how,
            "memorized": memorized}


@app.delete("/api/conversations/{sid}")
async def api_conversation_delete(sid: str, owner: str = ""):
    """删除会话：目录记录 + 服务端对话历史 + 该会话的 Agent 克隆。"""
    store = _conv_store()
    conv = store.get(sid)
    if not _conv_owned(conv, owner):
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    if sid in _live_runs:
        return JSONResponse({"error": "该会话有任务正在执行，请先停止再删除"},
                            status_code=409)
    store.delete(sid)
    _store.session_histories.pop(sid, None)
    try:
        from automind.core.db import get_db
        get_db().session_delete(sid)
    except Exception as e:
        logger.warning("conversation_history_delete_failed", sid=sid, error=str(e))
    clone = _session_clones.pop(sid, None)
    if clone is not None:
        _close_clone_later(clone)
    try:                                   # v1.7.7：会话删了，它的隔离副本一并清掉
        from automind.core import workspace as _wsm
        await asyncio.to_thread(_wsm.remove, sid, _project_root())
    except Exception as e:
        logger.warning("conversation_workspace_remove_failed", sid=sid, error=str(e))
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════
# REST API — 长期目标（v1.11.0 目标交付模式）
# ═══════════════════════════════════════════════════════════


def _goal_store():
    from automind.goals import get_store
    return get_store()


def _goal_owned(goal: dict | None, owner: str) -> bool:
    """目标只能由创建它的浏览器会话操作（与会话目录同一隔离口径）。"""
    from automind.goals import _clean
    return goal is not None and goal.get("owner", "") == _clean(owner)


def _goal_brief(goal: dict) -> dict:
    """列表用的精简版：推进记录只带条数与最近一条，详情接口再给全量。"""
    out = {k: v for k, v in goal.items() if k != "runs"}
    runs = goal.get("runs") or []
    out["last_run"] = runs[-1] if runs else None
    return out


@app.get("/api/goals")
async def api_goals(owner: str = "", workspace: str = ""):
    """本工作区的长期目标（进行中在前）。"""
    items = await asyncio.to_thread(_goal_store().list, owner, workspace)
    return {"goals": [_goal_brief(g) for g in items]}


@app.post("/api/goals")
async def api_goal_create(data: dict):
    """新建目标：标题必填，期望交付物 / 验收标准可选（写清楚了推进才有方向）。"""
    try:
        goal = await asyncio.to_thread(
            _goal_store().create, str(data.get("owner") or ""),
            str(data.get("workspace") or ""), str(data.get("title") or ""),
            str(data.get("deliverable") or ""), str(data.get("criteria") or ""))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"status": "ok", "goal": goal}


@app.get("/api/goals/{gid}")
async def api_goal_get(gid: str, owner: str = ""):
    """目标详情（含完整推进记录时间线）。"""
    goal = _goal_store().get(gid)
    if not _goal_owned(goal, owner):
        return JSONResponse({"error": "目标不存在"}, status_code=404)
    return {"goal": goal}


@app.patch("/api/goals/{gid}")
async def api_goal_update(gid: str, data: dict):
    """改标题 / 交付物 / 验收标准 / 备注 / 状态（active | paused | done | archived）。"""
    store = _goal_store()
    if not _goal_owned(store.get(gid), str(data.get("owner") or "")):
        return JSONResponse({"error": "目标不存在"}, status_code=404)
    fields = {k: data[k] for k in ("title", "deliverable", "criteria", "notes",
                                   "progress", "status") if k in data}
    try:
        goal = await asyncio.to_thread(store.update, gid, **fields)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"status": "ok", "goal": goal}


@app.delete("/api/goals/{gid}")
async def api_goal_delete(gid: str, owner: str = ""):
    store = _goal_store()
    if not _goal_owned(store.get(gid), owner):
        return JSONResponse({"error": "目标不存在"}, status_code=404)
    await asyncio.to_thread(store.delete, gid)
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════
# REST API — 断点续跑（v1.7.7）
# ═══════════════════════════════════════════════════════════


@app.get("/api/checkpoints/resumable")
async def api_checkpoint_resumable(session_id: str = "default"):
    """该会话最近一次任务若没跑完，返回断点摘要（前端据此显示「从断点续跑」）。"""
    try:
        info = await asyncio.to_thread(
            get_agent().checkpoint_mgr.resumable_for_session, session_id)
    except Exception as e:
        logger.warning("resumable_lookup_failed", session=session_id, error=str(e))
        info = None
    return {"resumable": info}


@app.post("/api/checkpoints/{checkpoint_id}/dismiss")
async def api_checkpoint_dismiss(checkpoint_id: str, data: dict):
    """用户选择「不用续跑」：标记断点为 dismissed，不再提示。"""
    from automind.state.checkpoint import session_of
    sid = str(data.get("session_id") or "")
    if not sid or session_of(checkpoint_id) != sid:
        return JSONResponse({"error": "断点不属于该会话"}, status_code=404)
    ok = get_agent().checkpoint_mgr.mark(checkpoint_id, "dismissed")
    return {"status": "ok" if ok else "missing"}


# ═══════════════════════════════════════════════════════════
# REST API — 会话隔离工作副本：查看改动 / 应用到项目 / 清理（v1.7.7）
# ═══════════════════════════════════════════════════════════


def _ws_owned(sid: str, owner: str) -> bool:
    """副本按会话归属：会话 id 以浏览器级 owner 开头（多用户隔离）。"""
    return bool(sid) and bool(owner) and sid.startswith(owner)


def _project_root() -> Path:
    return Path(get_agent().config.project_root).resolve()


def _busy_sessions() -> set[str]:
    return set(_live_runs.keys())


@app.get("/api/session-workspaces")
async def api_session_workspaces(owner: str = ""):
    """当前项目下的会话副本（只列本浏览器的；本机管理员可见全部）。"""
    from automind.core import workspace as _wsm
    items = await asyncio.to_thread(_wsm.list_copies, _project_root())
    if owner:
        items = [i for i in items if i["session"].startswith(owner)]
    busy = _busy_sessions()
    for i in items:
        i["running"] = i["session"] in busy
    ex = get_agent().config.execution
    return {"copies": items, "enabled": _wsm.enabled(ex),   # 含环境变量覆盖
            "total_bytes": sum(i["bytes"] for i in items),
            "ttl_hours": getattr(ex, "workspace_ttl_hours", 72.0),
            "keep": getattr(ex, "workspace_keep", 8)}


@app.get("/api/session-workspaces/{sid}/changes")
async def api_session_workspace_changes(sid: str, owner: str = ""):
    from automind.core import workspace as _wsm
    if not _ws_owned(sid, owner):
        return JSONResponse({"error": "副本不属于当前会话"}, status_code=404)
    return await asyncio.to_thread(_wsm.changes, sid, _project_root())


@app.get("/api/session-workspaces/{sid}/apply-plan")
async def api_session_workspace_apply_plan(sid: str, owner: str = ""):
    """应用前预览（v1.11.6）：目标项目根 + 每个文件是新建还是覆盖。"""
    from automind.core import workspace as _wsm
    if not _ws_owned(sid, owner):
        return JSONResponse({"error": "副本不属于当前会话"}, status_code=404)
    return await asyncio.to_thread(_wsm.apply_plan, sid, _project_root())


@app.post("/api/session-workspaces/{sid}/apply")
async def api_session_workspace_apply(sid: str, data: dict):
    """把副本快照按相对路径覆盖回项目根（v1.11.6：同名文件直接覆盖，覆盖前备份，可撤销）。

    ``skip_conflicts: true`` 时跳过"任务期间项目里也被改过"的文件（旧行为）。
    """
    from automind.core import workspace as _wsm
    from automind.tools.file_editor import JOURNAL
    if not _ws_owned(sid, str(data.get("owner") or "")):
        return JSONResponse({"error": "副本不属于当前会话"}, status_code=404)
    if sid in _live_runs:
        return JSONResponse({"error": "该会话任务仍在执行，请结束后再应用"}, status_code=409)
    files = data.get("files")
    res = await asyncio.to_thread(
        lambda: _wsm.apply_to_project(
            sid, _project_root(), files if isinstance(files, list) else None,
            journal=JOURNAL, skip_conflicts=bool(data.get("skip_conflicts"))))
    return res


@app.post("/api/session-workspaces/{sid}/export")
async def api_session_workspace_export(sid: str, data: dict):
    from automind.core import workspace as _wsm
    if not _ws_owned(sid, str(data.get("owner") or "")):
        return JSONResponse({"error": "副本不属于当前会话"}, status_code=404)
    return await asyncio.to_thread(_wsm.export_results, sid, _project_root())


@app.delete("/api/session-workspaces/{sid}")
async def api_session_workspace_delete(sid: str, owner: str = ""):
    from automind.core import workspace as _wsm
    if not _ws_owned(sid, owner):
        return JSONResponse({"error": "副本不属于当前会话"}, status_code=404)
    if sid in _live_runs:
        return JSONResponse({"error": "该会话任务仍在执行，不能删除副本"}, status_code=409)
    ok = await asyncio.to_thread(_wsm.remove, sid, _project_root())
    return {"status": "ok" if ok else "missing"}


@app.post("/api/session-workspaces/cleanup")
async def api_session_workspace_cleanup(data: dict | None = None):
    """立即按 TTL/数量清理闲置副本（正在跑任务的不动）。"""
    removed = await asyncio.to_thread(_cleanup_workspaces_once)
    return {"status": "ok", "removed": removed}


def _cleanup_workspaces_once() -> list[str]:
    from automind.core import workspace as _wsm
    ex = get_agent().config.execution
    return _wsm.cleanup(_project_root(),
                        ttl_hours=float(getattr(ex, "workspace_ttl_hours", 72.0) or 0),
                        keep=int(getattr(ex, "workspace_keep", 8) or 8),
                        busy=_busy_sessions())


async def _workspace_janitor(interval: float = 3600.0) -> None:
    """后台定时清理闲置副本（此前只在"下一次跑任务"时顺带淘汰）。"""
    while True:
        try:
            removed = await asyncio.to_thread(_cleanup_workspaces_once)
            if removed:
                logger.info("workspace_janitor_cleaned", removed=len(removed))
        except Exception as e:
            logger.warning("workspace_janitor_failed", error=str(e))
        await asyncio.sleep(interval)


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


@app.get("/api/mcp/presets")
async def api_mcp_presets():
    """返回内置 MCP 官方适配预设（一键添加用，见 tools/mcp_presets.py）。"""
    from automind.tools.mcp_presets import MCP_PRESETS
    return {"presets": MCP_PRESETS}


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
            "error": reg.last_error(name) if hasattr(reg, "last_error") else "",
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

    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    entry = {
        "name": name, "command": data.get("command", ""),
        "args": args, "url": data.get("url", ""), "transport": transport,
        **({"env": env} if env else {}),
    }
    cfg = _read_config()
    servers = [s for s in cfg.get("mcp_servers", []) if s["name"] != name]
    servers.append(entry)
    cfg["mcp_servers"] = servers
    _write_config(cfg)

    agent = get_agent()
    agent.mcp_registry.add_server(MCPServerConfig(
        name=name, command=entry["command"], args=args,
        url=entry["url"], transport=transport, env=env,
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
        # v1.11.2：给出真实原因（找不到命令 / 握手超时 / SDK 未装…），而不是一句笼统的"连接失败"
        error = (agent.mcp_registry.last_error(name)
                 or "连接失败（请确认已安装 `pip install mcp` 且命令/参数正确）")
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
        # Claude Desktop 配置里的 env（API Key 等）此前被丢掉，需要密钥的服务器起不来
        raw_env = spec.get("env")
        env = ({str(k): str(v) for k, v in raw_env.items()}
               if isinstance(raw_env, dict) else {})
        entry = {"name": name, "command": spec.get("command", ""),
                 "args": args, "url": spec.get("url", ""), "transport": transport,
                 **({"env": env} if env else {})}
        existing[name] = entry
        agent.mcp_registry.add_server(MCPServerConfig(
            name=name, command=entry["command"], args=args,
            url=entry["url"], transport=transport, env=env))
        connected = False
        try:
            connected = await agent.mcp_registry.connect_server(name)
        except Exception:
            pass
        results.append({"name": name, "connected": connected,
                         "error": "" if connected else agent.mcp_registry.last_error(name)})
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
    builtin = getattr(agent, "builtin_tool_names", frozenset())

    def _source(name: str) -> str:
        """工具来源：内置 / MCP / 插件（界面据此打标签）。"""
        if name.startswith("mcp__"):
            return "mcp"
        return "builtin" if name in builtin else "plugin"

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
            "source": _source(t.name),
        }
        for t in agent.tool_registry.list_all()
    ]
    # 附加已禁用的工具（仅名称，重新启用后恢复完整信息）
    for name in disabled:
        result.append({
            "name": name, "description": "（已禁用）", "tier": "safe",
            "risk": 0, "params": [], "required": [], "enabled": False,
            "mcp": name.startswith("mcp__"),
            # 已禁用的工具不在注册表里，按名字兜底判断（内置名单是构造期快照，
            # 禁用后仍保留该名字，故这里判断依然准确）
            "source": _source(name),
        })
    return result


@app.get("/api/tools/registration")
async def api_tools_registration():
    """工具分组的注册失败账目（v1.7.2）。

    为什么单独开一个端点而不是塞进 ``/api/tools`` 的返回值：那个端点的返回值
    是一个数组，界面直接当列表渲染，改成对象会把所有调用方一起弄坏。

    为什么必须暴露出来：注册失败以前只写一行 ``logger.warning``（浏览器组
    甚至是裸的 ``except: pass``），而日志默认只进 stderr —— 桌面版/Web 版
    用户根本看不到，"少了六个工具"这件事在用户视角里从未发生过。这里让
    界面能问出"你到底少了哪些能力、怎么补回来"。
    """
    agent = get_agent()
    failures = agent.tool_group_failures() if hasattr(agent, "tool_group_failures") else []
    # 用户连接器（v1.7.3）：它们的加载失败也走同一条通道 ——
    # "我放进去的连接器为什么没生效"必须在同一个地方能查到，
    # 而不是去翻 stderr。
    try:
        from automind.tools.connectors import describe_dirs, load_failures

        failures = [dict(f, group="connector") for f in load_failures()] + failures
        dirs = describe_dirs()
    except Exception as e:                                  # pragma: no cover - 防御性
        logger.warning("connector_failures_unavailable", reason=str(e))
        dirs = {}
    return {
        "ok": not failures,
        "failures": failures,
        "connector_dirs": dirs,
        # 让界面不用自己拼中文：措辞与任务前自检保持一致
        "message": ("；".join(f"工具组「{f['group']}」未注册成功（{f['error']}）"
                              + (f"，修复：{f['hint']}" if f.get("hint") else "")
                              for f in failures) or "全部工具组注册正常"),
    }


@app.post("/api/tools/reload")
async def api_tools_reload():
    """重载用户连接器（``~/.automind/connectors/*.py``）—— 不必重启服务。

    v1.7.3 第 6 项：这是"接客户系统"的运行时入口 —— 现场写完一个连接器就能
    立刻在模型的可选工具里看到，不用停下服务、不用改源码。
    属于管理动作（中间件按 ``/api/tools/reload`` 前缀要求管理员权限）。
    """
    from automind.tools.connectors import load_failures, reload_connectors

    agent = get_agent()
    result = reload_connectors(agent.tool_registry)
    failed = load_failures()
    # 加载失败必须让调用方**看得见**（返回体里给全，而不是只写日志）
    result["failures"] = [dict(f, group="connector") for f in failed]
    result["tools_total"] = len(agent.tool_registry.list_names())
    if result.get("failed"):
        logger.warning("connector_reload_partial", loaded=result.get("loaded"),
                       failed=result.get("failed"))
    return result


# ── 评测（v1.7.3 第 7 项）────────────────────────────────────
#
# 为什么走"后台任务 + 轮询"而不是直接 await：一次套件会**真的跑 N 个任务、
# 花真金白银**，几分钟到几十分钟都正常。同步等待会把 HTTP 请求挂在那里，
# 也会把前端的加载态一直转下去。
#
# 为什么保留 dry-run：不联网、不需要 Key，就能回答"这套要跑什么、要多少断言" ——
# 交付现场最常见的问题正是"我不知道它会干什么就点了运行"。
_eval_jobs: dict[str, dict] = {}


@app.get("/api/eval/suites")
async def api_eval_suites():
    """列出可用的评测套件（含任务数与断言数；不加载模型、不发请求）。"""
    try:
        from automind.eval.suite import SuiteError, find_suites, load_suite
    except Exception as e:                                  # pragma: no cover - 防御性
        return JSONResponse({"error": f"评测模块不可用：{e}"}, status_code=503)

    out = []
    for p in find_suites():
        try:
            s = load_suite(p)
            out.append({"path": str(p), "name": s.name, "cases": len(s.cases),
                        "assertions": s.total_assertions(),
                        "description": s.description, "valid": True})
        except SuiteError as e:
            # 套件写错要在界面上看得见，而不是让整页 500
            out.append({"path": str(p), "name": p.stem, "valid": False,
                        "error": str(e)})
    return {"suites": out}


@app.post("/api/eval/run")
async def api_eval_run(data: dict):
    """跑一个评测套件（body: ``{suite, provider?, model?, include?, limit?, dry_run?}``）。"""
    import asyncio as _aio

    from automind.eval.executors import AutoMindExecutor, detect_llm_target
    from automind.eval.runner import run_suite
    from automind.eval.suite import SuiteError, find_suites, load_suite

    want = str(data.get("suite") or "smoke").strip()
    path = want if want.endswith((".yml", ".yaml")) else ""
    if not path:
        hit = [p for p in find_suites() if p.stem == want or p.name == want]
        if not hit:
            return JSONResponse({"error": f"找不到套件：{want}"}, status_code=404)
        path = str(hit[0])
    try:
        suite = load_suite(path)
    except SuiteError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    provider = str(data.get("provider") or "").strip()
    model = str(data.get("model") or "").strip()
    target = detect_llm_target(provider, model)

    if data.get("dry_run"):
        return {"dry_run": True, "suite": suite.name, "path": path,
                "cases": [{"id": c.id, "mode": c.mode,
                           "assertions": [a.type for a in c.assertions]}
                          for c in suite.cases],
                "target": target.as_dict()}
    if not target.available:
        # 与"模型退化"区分开：前端据此提示"去配 Key"，而不是"模型不行"
        return JSONResponse({"error": target.reason, "code": "llm_not_configured"},
                            status_code=409)

    job_id = uuid.uuid4().hex[:12]
    _eval_jobs[job_id] = {"status": "running", "suite": suite.name,
                          "model": target.model, "report": None, "error": ""}
    from automind.core.metrics import METRICS

    METRICS.inc("automind_eval_runs_total", status="started")

    async def _run() -> None:
        try:
            report = await run_suite(
                suite, AutoMindExecutor(provider=provider, model=model),
                model=target.model, provider=target.provider,
                include=data.get("include") or [],
                limit=int(data.get("limit") or 0),
                default_timeout=float(data.get("timeout") or 0.0),
                target=target)
            payload = report.as_dict()
            passed = bool(payload.get("passed"))
            _eval_jobs[job_id].update(status="done", report=payload)
            METRICS.inc("automind_eval_runs_total",
                        status="passed" if passed else "failed")
        except Exception as e:
            # 后台任务里的异常必须落进 job —— 否则前端只会一直轮询"运行中"
            _eval_jobs[job_id].update(status="error", error=str(e))
            METRICS.inc("automind_eval_runs_total", status="error")

    _aio.create_task(_run())
    return {"job_id": job_id, "status": "running", "suite": suite.name,
            "total": len(suite.cases), "model": target.model}


@app.get("/api/eval/report/{job_id}")
async def api_eval_report(job_id: str):
    """取评测结果（前端轮询这个）。"""
    job = _eval_jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "job 不存在或已被清理"}, status_code=404)
    return job


# ── 工作流即代码（v1.7.3 第 9 项）─────────────────────────────
#
# 与 ReAct / Plan 的分工：那两条路由 LLM 在**运行时**决定步骤；工作流是
# **确定的**流程（YAML 里写死了每一步），可评审、可版本化、可 diff、可
# 进 CI。FDE 交付给客户的正是后者。


def _resolve_workflow_path(name: str) -> Path:
    """把 ``file`` 参数解析成项目内的工作流文件路径（**白名单**）。

    为什么必须限制：一份工作流 YAML 能配任意工具 + 任意参数（外发 URL、
    命令），**等同于执行权限**。接受任意绝对路径等于把"读文件"升级成
    "以服务身份执行任意动作"，而请求参数是模型/前端可控的。

    规则：只允许 ``<project_root>/workflows/`` 之内、只允许 .yaml/.yml/.json，
    解析后用 ``is_relative_to`` 判穿越（``..`` 与符号链接都在 resolve 之后暴露）。
    """
    root = (Path(get_agent().config.project_root) / "workflows").resolve()
    raw = str(name or "").strip()
    if not raw:
        raise FileNotFoundError("未指定工作流文件")
    p = Path(raw)
    target = (root / p).resolve() if not p.is_absolute() else p.resolve()
    if not target.is_relative_to(root):
        raise PermissionError(
            f"工作流文件必须放在项目目录的 workflows/ 下：{root}（已拒绝 {raw}）")
    if target.suffix.lower() not in (".yaml", ".yml", ".json"):
        raise PermissionError("工作流文件只支持 .yaml / .yml / .json")
    if not target.is_file():
        raise FileNotFoundError(f"工作流文件不存在：{target}")
    return target


def _workflow_loader(tool_names: list[str], unknown_fields: str = "error"):
    from automind.workflow import WorkflowLoader

    return WorkflowLoader(tool_names=tool_names, unknown_fields=unknown_fields)


_WF_SUFFIXES = (".yaml", ".yml", ".json")

#: 进行中（以及最近结束、仍在内存里）的工作流运行：run_id → 运行上下文。
#: 结束后的完整记录以 ``WorkflowRunStore``（按步落盘的 JSON）为准，这里只是
#: "正在跑的那几个"的句柄（任务、执行器）与实时进度。
_wf_live: dict[str, dict] = {}
_WF_LIVE_KEEP = 50
_wf_store_obj: Any = None


def _wf_store():
    """工作流运行记录存储（懒加载：数据目录可能在测试里被重定向）。"""
    global _wf_store_obj
    if _wf_store_obj is None:
        from automind.workflow.runs import WorkflowRunStore

        keep, days = _wf_retention()
        _wf_store_obj = WorkflowRunStore(keep=keep, keep_days=days)
    return _wf_store_obj


def _wf_retention() -> tuple[int | None, float | None]:
    """运行记录保留口径（v1.9.0，R-207）：``(按条数, 按天数)``。

    社区版最近 50 次；专业版 90 天；企业版按配置 ``workflow_history_days``
    （缺省 365 天）。两项都不限时兜底 1000 条 —— 磁盘不能无限涨。
    """
    keep = _quota.workflow_limit("workflow_history")
    days = _quota.workflow_limit("workflow_history_days")
    if keep is None and days is None:
        try:
            days = float(_read_config().get("workflow_history_days") or 365)
        except (TypeError, ValueError):
            days = 365.0
    if keep is None and days is None:            # pragma: no cover - 防御性
        keep = 1000
    return keep, days


def _wf_session_for(file_name: str) -> str:
    """触发运行的专用会话（D4）：``wf_<文件名>``，与任何人的对话会话都不混。"""
    stem = Path(file_name or "inline").stem
    safe = "".join(ch if (ch.isascii() and ch.isalnum()) or ch in "-_" else "_" for ch in stem)
    if not safe.strip("_"):                      # 纯中文文件名 → 用摘要，保持稳定且可区分
        import hashlib

        safe = hashlib.sha1(stem.encode("utf-8")).hexdigest()[:10]
    return f"wf_{safe[:60] or 'inline'}"


def _wf_running(file_name: str, trigger_key: str = "") -> str:
    """该工作流（可限定某个触发器）正在跑的运行 id；没有返回空串。

    触发器到点时若上一次还没跑完就跳过这一次 —— 同一份流程两次叠着跑，
    中间产物与外部系统的状态都会互相踩。
    """
    for lv in _wf_live.values():
        if lv.get("status") != "running" or lv.get("file") != file_name:
            continue
        if trigger_key and lv.get("trigger_key") != trigger_key:
            continue
        return str(lv.get("run_id") or "")
    return ""


def _wf_gov():
    """企业版工作流治理特性（发布审批 / RBAC / 凭据 / 并发配额 / 审计）；社区 / 专业版为 None。"""
    return _edition.get_feature("workflow_governance")


def _wf_can(identity: dict, verb: str, file_name: str = "") -> tuple[bool, str]:
    gov = _wf_gov()
    if gov is None or not hasattr(gov, "authorize"):
        return True, ""
    return gov.authorize(identity, verb, file_name)


def _wf_authorize(request, verb: str, file_name: str = "") -> JSONResponse | None:
    """按工作流 RBAC（v1.10.0，R-302）：view / edit / run / trigger / approve。

    没有治理特性时一律放行（与此前行为完全一致）；有则拒绝时返回 403。
    """
    ok, why = _wf_can(_request_identity(request), verb, file_name)
    if ok:
        return None
    return JSONResponse({"ok": False, "type": "forbidden", "error": why}, status_code=403)


def _wf_policy(file_name: str, schema: Any, *, trigger: str) -> str:
    """运行前的发布策略检查（v1.10.0，R-301）：返回拒绝原因，空串 = 可以运行。"""
    gov = _wf_gov()
    if gov is None or not hasattr(gov, "check_run"):
        return ""
    try:
        ok, why = gov.check_run(file_name, schema, trigger=trigger)
    except Exception as e:                              # pragma: no cover - 扩展兜底
        return f"发布策略检查失败：{e}"
    return "" if ok else why


def _wf_secrets(schema: Any) -> dict[str, str] | None:
    """本次运行要用到的凭据（v1.10.0，R-305）；没有治理特性时为 None。"""
    gov = _wf_gov()
    if gov is None or not hasattr(gov, "secrets_for"):
        return None
    try:
        return gov.secrets_for(schema.secret_names())
    except Exception as e:                              # pragma: no cover - 扩展兜底
        logger.warning("workflow_secrets_unavailable", error=str(e))
        return {}


def _workflow_dir() -> Path:
    return Path(get_agent().config.project_root) / "workflows"


def _workflow_catalog(loader) -> dict[str, dict]:
    """``workflows/`` 下每份文件的校验结果 + 限额位次（R-110）。

    计数口径（与路线图 §2.3 一致）：按文件名排序、**只数校验通过的**；
    超出上限的文件照常列出、校验、dry-run，只是不能运行。
    """
    root = _workflow_dir()
    out: dict[str, dict] = {}
    if not root.is_dir():
        return out
    position = 0
    for p in sorted(root.glob("*")):
        if not p.is_file() or p.suffix.lower() not in _WF_SUFFIXES:
            continue
        try:
            schema, errors, warnings = loader.try_load(p)
        except Exception as e:                        # pragma: no cover - 防御性
            out[p.name] = {"path": p, "schema": None, "errors": [{"message": str(e)}],
                           "warnings": [], "runnable": False, "blocked_reason": ""}
            continue
        runnable, reason = False, ""
        if schema is not None:
            position += 1
            runnable, reason = _quota.check_workflow_slot(position)
        out[p.name] = {"path": p, "schema": schema,
                       "errors": [e.as_dict() for e in errors],
                       "warnings": [w.as_dict() for w in warnings],
                       "runnable": runnable, "blocked_reason": reason}
    return out


def _workflow_file_name_safe(raw: Any) -> str:
    try:
        return _workflow_file_name(raw)
    except PermissionError:
        return ""


def _workflow_file_name(raw: Any) -> str:
    """把用户给的文件名规整成 ``workflows/`` 下的**纯文件名**（不许带目录）。"""
    name = str(raw or "").strip().replace("\\", "/").split("/")[-1]
    if not name or name.startswith("."):
        raise PermissionError("文件名不合法")
    if Path(name).suffix.lower() not in _WF_SUFFIXES:
        name += ".yaml"
    if any(ch in name for ch in '<>:"|?*') or len(name) > 120:
        raise PermissionError("文件名包含非法字符或过长")
    return name


@app.get("/api/workflow/list")
async def api_workflow_list(request: Request):
    """列出项目 ``workflows/`` 目录下的工作流（供界面选择）。

    每份文件都带上：校验结果（载入期就能看出"写得对不对"）、入参声明（界面据此
    生成表单）、能否运行（社区版超过 20 个的标注原因）、最近一次运行。
    """
    loader = _workflow_loader(get_agent().tool_registry.list_names())
    catalog = await asyncio.to_thread(_workflow_catalog, loader)
    latest = await asyncio.to_thread(_wf_store().latest_by_file)
    out: list[dict[str, Any]] = []
    identity = _request_identity(request)
    gov = _wf_gov()
    for name, item in catalog.items():
        if not _wf_can(identity, "view", name)[0]:
            continue                                    # 没有查看权限的工作流不出现在列表里
        schema = item["schema"]
        row: dict[str, Any] = {
            "file": name, "valid": schema is not None,
            "name": getattr(schema, "name", Path(name).stem),
            "description": getattr(schema, "description", ""),
            "steps": len(getattr(schema, "steps", []) or []),
            "step_types": sorted({s.type for s in (getattr(schema, "steps", []) or [])}),
            "inputs": ({k: v.as_dict() for k, v in schema.inputs.items()}
                       if schema is not None else {}),
            "source_digest": getattr(schema, "source_digest", ""),
            "errors": item["errors"], "warnings": item["warnings"],
            "runnable": item["runnable"], "blocked_reason": item["blocked_reason"],
            "last_run": latest.get(name),
            # v1.9.0：触发器的声明（运行时状态见 GET /api/workflow/triggers，专业版）
            "triggers": ([t.as_dict() for t in schema.triggers] if schema is not None else []),
            # v1.10.0：引用了哪些凭据、发布状态（企业版）、当前身份能做什么
            "secrets": schema.secret_names() if schema is not None else [],
            "can": {verb: _wf_can(identity, verb, name)[0]
                    for verb in ("edit", "run", "trigger", "approve")},
        }
        if gov is not None and hasattr(gov, "publish_state"):
            row["publish"] = gov.publish_state(name, getattr(schema, "source_digest", ""))
        live = next((lv for lv in _wf_live.values()
                     if lv.get("file") == name and lv.get("status") == "running"), None)
        if live is not None:
            row["running_run_id"] = live["run_id"]
        out.append(row)
    return {"dir": str(_workflow_dir()), "workflows": out,
            "limits": _quota.snapshot().get("workflows", {}), "identity": identity}


@app.get("/api/workflow/source")
async def api_workflow_source(file: str, request: Request):
    """读一份工作流的原文（界面在线编辑用）。"""
    denied = _wf_authorize(request, "view", _workflow_file_name_safe(file))
    if denied is not None:
        return denied
    try:
        path = _resolve_workflow_path(file)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    return {"file": path.name, "yaml": path.read_text(encoding="utf-8")}


@app.post("/api/workflow/save")
async def api_workflow_save(payload: dict, request: Request):
    """保存（新建或覆盖）一份工作流到 ``workflows/``，并回带校验结果。

    **校验不通过也允许保存**：编辑到一半的草稿要能存下来；能不能跑由列表与
    运行接口判定。覆盖已有文件必须显式 ``overwrite: true`` —— 新建时撞名
    不该静默覆盖掉别人的流程。
    """
    try:
        name = _workflow_file_name(payload.get("file"))
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    denied = _wf_authorize(request, "edit", name)
    if denied is not None:
        return denied
    text = payload.get("yaml")
    if not isinstance(text, str) or not text.strip():
        return JSONResponse({"ok": False, "error": "内容为空"}, status_code=400)
    if len(text.encode("utf-8")) > 512 * 1024:
        return JSONResponse({"ok": False, "error": "工作流文件过大（上限 512KB）"},
                            status_code=413)
    result = _save_workflow_file(name, text, overwrite=bool(payload.get("overwrite")))
    if not result.get("ok"):
        status = int(result.pop("status", 400))
        return JSONResponse(result, status_code=status)
    return result


def _save_workflow_file(name: str, text: str, *, overwrite: bool = False) -> dict:
    """把一份工作流写进 ``workflows/`` 并回带校验结果（保存 / 模板新建 / 轨迹草稿入库共用）。"""
    try:
        name = _workflow_file_name(name)
    except PermissionError as e:
        return {"ok": False, "status": 400, "error": str(e)}
    root = _workflow_dir()
    target = (root / name).resolve()
    if not target.is_relative_to(root.resolve()):
        return {"ok": False, "status": 400, "error": "文件名不合法"}
    if target.exists() and not overwrite:
        return {"ok": False, "status": 409, "error": f"文件已存在：{name}", "type": "exists"}
    root.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    loader = _workflow_loader(get_agent().tool_registry.list_names())
    schema, errors, warnings = loader.try_load(target)
    logger.info("workflow_saved", file=name, valid=schema is not None)
    return {"ok": True, "file": name, "valid": schema is not None,
            "schema": schema.as_dict() if schema is not None else None,
            "errors": [e.as_dict() for e in errors],
            "warnings": [w.as_dict() for w in warnings]}


# v1.11.5：随包分发的可运行样板（社区版可用）。与专业版的"模板库"不同：样板除了 YAML
# 还可能带配套文件（如客服知识库），所以"用它新建"是一次安装 —— 把整套文件写进 workflows/。
_WF_SAMPLES_DIR = Path(__file__).resolve().parent / "workflow" / "samples"
_WF_SAMPLES: dict[str, dict[str, Any]] = {
    "customer_service": {
        "file": "customer_service.yaml",
        "extras": ["customer-service-kb.md"],
        "tags": ["客服", "分支路由", "人工接管", "Agent"],
    },
}


def _workflow_samples_sync() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    loader = _workflow_loader(get_agent().tool_registry.list_names())
    root = _workflow_dir()
    for sid, meta in _WF_SAMPLES.items():
        src = _WF_SAMPLES_DIR / meta["file"]
        if not src.is_file():
            continue
        schema, _errors, _warnings = loader.try_load(src)
        out.append({
            "id": sid, "file": meta["file"], "extras": list(meta["extras"]),
            "tags": list(meta["tags"]),
            "name": getattr(schema, "name", sid), "description": getattr(schema, "description", ""),
            "yaml": src.read_text(encoding="utf-8"),
            "installed": (root / meta["file"]).is_file(),
        })
    return out


@app.get("/api/workflow/samples")
async def api_workflow_samples():
    """内置可运行样板（v1.11.5）：列表 + YAML 原文 + 是否已装到 workflows/。"""
    return {"samples": await asyncio.to_thread(_workflow_samples_sync)}


@app.post("/api/workflow/samples/{sample_id}/install")
async def api_workflow_sample_install(sample_id: str, request: Request, payload: dict | None = None):
    """把样板（YAML + 配套文件）写进 ``workflows/``。已存在的文件默认不覆盖。"""
    meta = _WF_SAMPLES.get(sample_id)
    if meta is None:
        return JSONResponse({"ok": False, "error": f"没有这个样板：{sample_id}"}, status_code=404)
    denied = _wf_authorize(request, "edit", meta["file"])
    if denied is not None:
        return denied
    overwrite = bool((payload or {}).get("overwrite"))

    def _install() -> dict:
        root = _workflow_dir()
        root.mkdir(parents=True, exist_ok=True)
        extras: list[str] = []
        # 配套文件先落：YAML 一出现在列表里就可能被点运行，那时知识库必须已经在
        for extra in meta["extras"]:
            target = root / extra
            if target.exists() and not overwrite:
                continue                        # 用户改过的知识库不被样板冲掉
            target.write_text((_WF_SAMPLES_DIR / extra).read_text(encoding="utf-8"),
                              encoding="utf-8")
            extras.append(extra)
        text = (_WF_SAMPLES_DIR / meta["file"]).read_text(encoding="utf-8")
        result = _save_workflow_file(meta["file"], text, overwrite=overwrite)
        result["extras"] = extras
        return result

    result = await asyncio.to_thread(_install)
    if not result.get("ok"):
        return JSONResponse(result, status_code=int(result.pop("status", 400)))
    logger.info("workflow_sample_installed", sample=sample_id, extras=result.get("extras"))
    return result


@app.post("/api/workflow/delete")
async def api_workflow_delete(payload: dict, request: Request):
    """删除一份工作流文件（运行记录保留，便于事后审计）。"""
    denied = _wf_authorize(request, "edit", _workflow_file_name_safe(payload.get("file")))
    if denied is not None:
        return denied
    try:
        path = _resolve_workflow_path(payload.get("file"))
    except FileNotFoundError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=403)
    if any(lv.get("file") == path.name and lv.get("status") == "running"
           for lv in _wf_live.values()):
        return JSONResponse({"ok": False, "error": "该工作流正在运行，请先停止"},
                            status_code=409)
    path.unlink()
    logger.info("workflow_deleted", file=path.name)
    return {"ok": True, "file": path.name}


@app.post("/api/workflow/validate")
async def api_workflow_validate(payload: dict):
    """校验一份工作流定义（前端编辑器实时校验用；不执行任何东西）。"""
    text = payload.get("yaml")
    source = str(payload.get("source") or "<inline>")
    if text is None:
        try:
            text = _resolve_workflow_path(payload.get("file")).read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError) as e:
            return JSONResponse({"ok": False, "errors": [{"message": str(e)}]},
                                status_code=404 if isinstance(e, FileNotFoundError) else 403)
    loader = _workflow_loader(get_agent().tool_registry.list_names(),
                              str(payload.get("unknown_fields") or "error"))
    schema, errors, warnings = loader.try_parse(str(text), source=source)
    return {"ok": schema is not None,
            "schema": schema.as_dict() if schema is not None else None,
            "errors": [e.as_dict() for e in errors],
            "warnings": [w.as_dict() for w in warnings]}


def _load_workflow_for_run(payload: dict) -> tuple[Any, str, str, JSONResponse | None]:
    """按 ``file`` 或 ``yaml`` 载入并校验，返回 ``(schema, 文件名, 原文, 错误响应)``。"""
    agent = get_agent()
    loader = _workflow_loader(agent.tool_registry.list_names(),
                              str(payload.get("unknown_fields") or "error"))
    text = payload.get("yaml")
    file_name = ""
    if text is None:
        try:
            path = _resolve_workflow_path(payload.get("file"))
        except FileNotFoundError as e:
            return None, "", "", JSONResponse({"error": str(e)}, status_code=404)
        except PermissionError as e:
            return None, "", "", JSONResponse({"error": str(e)}, status_code=403)
        file_name = path.name
        text = path.read_text(encoding="utf-8")
        schema, errors, warnings = loader.try_load(path)
    else:
        schema, errors, warnings = loader.try_parse(
            str(text), source=str(payload.get("source") or "<inline>"))
    if schema is None:
        return None, file_name, str(text), JSONResponse(
            {"ok": False, "errors": [e.as_dict() for e in errors],
             "warnings": [w.as_dict() for w in warnings]}, status_code=422)
    return schema, file_name, str(text), None


def _workflow_slot_error(tool_names: list[str], file_name: str) -> str:
    """文件型运行的限额位次检查；返回拒绝原因（空串 = 可运行）。"""
    if not file_name:
        return ""
    catalog = _workflow_catalog(_workflow_loader(tool_names))
    item = catalog.get(file_name)
    if item is None or item["runnable"]:
        return ""
    return item["blocked_reason"] or "该工作流当前不能运行"


@app.post("/api/workflow/run")
async def api_workflow_run(payload: dict, request: Request):
    """运行一份工作流（body: ``{file|yaml, inputs, dry_run, wait, session_id}``）。

    v1.8.0（R-101）：**立即返回 run_id**，执行放后台；进度经 WebSocket 推送
    （``wf_run_start`` / ``wf_step_start`` / ``wf_step_done`` / ``wf_run_done``），
    也可轮询 ``GET /api/workflow/runs/{run_id}``。``wait: true`` 保留旧的同步语义
    （CI 调用方要的是"跑完再返回报告"）；dry-run 始终同步返回（瞬时、无副作用）。

    属于管理动作（中间件按 ``/api/workflow/run`` 前缀要求管理员权限）——
    工作流能配任意工具与参数，等同于执行权限。
    """
    from automind.workflow import check_inputs, report_to_json, run_workflow

    schema, file_name, text, err = _load_workflow_for_run(payload)
    if err is not None:
        return err
    # v1.10.0（R-302）：试运行只要查看权限（没有副作用）；真跑要运行权限。
    # 直接贴 YAML（不来自 workflows/）的运行没有"哪一份工作流"可言，按 edit 权限判
    denied = _wf_authorize(request, "view" if payload.get("dry_run") else
                           ("run" if file_name else "edit"), file_name)
    if denied is not None:
        return denied

    # 入参与 CLI 共用同一套规则（否则会出现"命令行说缺参数、接口却能跑"）
    final_inputs, input_errors, input_warnings = check_inputs(
        schema, payload.get("inputs") or {})
    if input_errors:
        return JSONResponse({"ok": False,
                             "errors": [{"message": m, "path": "inputs"} for m in input_errors],
                             "warnings": [{"message": m} for m in input_warnings]},
                            status_code=422)
    chat_sid = str(payload.get("session_id") or "default")

    if payload.get("dry_run"):
        agent = get_agent()
        run = await run_workflow(schema, final_inputs, registry=agent.tool_registry,
                                 llm=agent.llm, approval=None, dry_run=True)
        report = json.loads(report_to_json(run))
        report["input_warnings"] = input_warnings
        return report

    agent = get_agent()
    blocked = await asyncio.to_thread(_workflow_slot_error,
                                      agent.tool_registry.list_names(), file_name)
    if blocked:
        return JSONResponse({"ok": False, "type": "workflow_quota", "error": blocked},
                            status_code=403)
    unpublished = _wf_policy(file_name, schema, trigger="manual")
    if unpublished:
        return JSONResponse({"ok": False, "type": "governance", "error": unpublished},
                            status_code=403)
    # D3：一次运行计 1 次任务（不按步骤计），与对话任务共用每日额度
    allowed, why = _quota.try_consume_task()
    if not allowed:
        return JSONResponse({"ok": False, "type": "quota", "error": why}, status_code=429)

    run_id = await _start_workflow_run(
        schema, final_inputs, file_name=file_name, chat_sid=chat_sid,
        yaml_text="" if file_name else text, initiated_by=_request_identity(request))
    if payload.get("wait"):
        task = (_wf_live.get(run_id) or {}).get("task")
        if task is not None:
            await asyncio.shield(task)
        report = dict((_wf_live.get(run_id) or {}).get("report") or {})
        if payload.get("include_outputs"):
            rec = await asyncio.to_thread(_wf_store().load, run_id) or {}
            report["outputs"] = rec.get("outputs") or {}
        return report
    return {"ok": True, "run_id": run_id, "status": "running", "workflow": schema.name,
            "file": file_name, "input_warnings": input_warnings}


async def _launch_workflow(*, file_name: str, inputs: dict | None = None,
                           session_id: str = "", trigger: str = "manual",
                           trigger_key: str = "", retry_left: int = 0,
                           initiated_by: dict | None = None) -> dict:
    """按文件发起一次运行（v1.9.0：触发器与内部调用共用；REST 入口见 api_workflow_run）。

    与手动运行**同一套**判定：校验、入参、限额位次、每日额度 —— 触发器绝不能
    成为绕过任何一道检查的后门。返回 ``{"ok": True, "run_id", ...}`` 或
    ``{"ok": False, "status": HTTP 码, "type", "error"}``。
    """
    from automind.workflow import check_inputs

    schema, file_name, _text, err = await asyncio.to_thread(
        _load_workflow_for_run, {"file": file_name})
    if err is not None:
        try:
            body = json.loads(bytes(err.body).decode("utf-8"))
        except (ValueError, TypeError):
            body = {}
        first = ((body.get("errors") or [{}])[0] or {}).get("message") or body.get("error")
        return {"ok": False, "status": err.status_code, "type": "invalid",
                "error": str(first or "工作流无法加载"), "errors": body.get("errors") or []}
    final_inputs, input_errors, input_warnings = check_inputs(schema, inputs or {})
    if input_errors:
        return {"ok": False, "status": 422, "type": "inputs", "error": "；".join(input_errors)}
    blocked = await asyncio.to_thread(_workflow_slot_error,
                                      get_agent().tool_registry.list_names(), file_name)
    if blocked:
        return {"ok": False, "status": 403, "type": "workflow_quota", "error": blocked}
    unpublished = _wf_policy(file_name, schema, trigger=trigger)
    if unpublished:
        return {"ok": False, "status": 403, "type": "governance", "error": unpublished}
    allowed, why = _quota.try_consume_task()
    if not allowed:
        return {"ok": False, "status": 429, "type": "quota", "error": why}
    sid = session_id or _wf_session_for(file_name)
    run_id = await _start_workflow_run(
        schema, final_inputs, file_name=file_name, chat_sid=sid, trigger=trigger,
        trigger_key=trigger_key, retry_left=retry_left,
        initiated_by=initiated_by or {"user": f"trigger:{trigger_key or trigger}",
                                      "role": "system", "team": "triggers", "source": trigger})
    return {"ok": True, "run_id": run_id, "session_id": sid, "workflow": schema.name,
            "file": file_name, "input_warnings": input_warnings}


async def _start_workflow_run(schema: Any, inputs: dict, *, file_name: str, chat_sid: str,
                              resume: dict | None = None, yaml_text: str = "",
                              trigger: str = "manual", trigger_key: str = "",
                              retry_left: int = 0, attempt: int = 1,
                              initiated_by: dict | None = None) -> str:
    """登记一次运行并在后台启动，返回 run_id（R-101 / R-104 / R-105）。

    D4：运行用**独立的会话克隆**（键 ``wf:<run_id>``），与同一会话里的对话任务
    互不影响 —— 模型连接、Token 计数、执行状态都各自独享；会话身份与隔离工作
    目录仍按发起会话绑定，产物照常进该会话的工作副本。
    """
    from automind.workflow import WorkflowExecutor
    from automind.workflow.agent_step import make_agent_runner

    run_id = uuid.uuid4().hex[:12]
    base = get_agent()
    clone_key = f"wf:{run_id}"
    run_agent = _acquire_run_agent(base, clone_key)
    run_agent._workflow_busy = True
    # 工作流有自己的进度与审批通道（下面的 _emit / _approval_for）。克隆从全局
    # agent 继承来的事件出口与审批回调若恰好指向某个对话连接，模型用量等事件
    # 就会串到那个对话里去 —— 这里显式切断。
    run_agent.event_sink = None
    run_agent.approval_callback = None
    with suppress(Exception):
        run_agent.session_id = chat_sid
    store = _wf_store()
    record = await asyncio.to_thread(
        store.start, run_id=run_id, workflow=schema.name, file=file_name,
        source=schema.source, source_digest=schema.source_digest,
        inputs=_jsonable(inputs), session_id=chat_sid,
        resumed_from=str((resume or {}).get("from_run") or ""), trigger=trigger,
        trigger_key=trigger_key, on_restart=getattr(schema, "on_restart", "notify"),
        retry_left=retry_left, attempt=attempt)
    notify = getattr(schema, "notify", None)
    # v1.10.0：发起人（运行审计 R-304、并发配额按团队 R-306 都靠它）
    initiated_by = dict(initiated_by or {"user": "local", "role": "admin", "team": "default",
                                         "source": "local"})
    record["initiated_by"] = initiated_by
    if yaml_text:
        record["yaml"] = yaml_text
    if getattr(notify, "declared", False):
        record["notify"] = notify.as_dict()
    await asyncio.to_thread(store.save, record)
    step_index = {s.id: i for i, s in enumerate(schema.steps, 1)}
    for s in schema.steps:
        for c in s.substeps:
            step_index[c.id] = step_index[s.id]
    live: dict[str, Any] = {
        "run_id": run_id, "workflow": schema.name, "file": file_name,
        "session_id": chat_sid, "status": "running", "started": time.time(),
        "total": len(schema.steps), "current": "", "steps": {}, "report": None,
        "resumed_from": str((resume or {}).get("from_run") or ""),
        "trigger": trigger, "trigger_key": trigger_key, "attempt": attempt,
        "initiated_by": initiated_by, "queue_position": None,
        "task": None, "executor": None,
    }
    _wf_live[run_id] = live

    async def _emit(kind: str, data: dict) -> None:
        if kind == "step_start":
            live["current"] = data.get("id", "")
        elif kind == "step_end":
            live["steps"][data.get("id", "")] = data
        wtype = {"step_start": "wf_step_start", "step_end": "wf_step_done",
                 "iteration_end": "wf_iteration",
                 "agent_action": "wf_agent_action"}.get(kind)
        if wtype:
            await _broadcast_to_session(chat_sid, {
                "type": wtype, "run_id": run_id, "workflow": schema.name,
                "session_id": chat_sid, "step": _jsonable(data)})

    def _approval_for(step: Any, **more: Any):
        return make_approval_callback(
            None, "http", run_id, chat_sid, run_agent,
            extra={"source": "workflow", "workflow": schema.name,
                   "workflow_run_id": run_id, "step_id": step.id,
                   "step_index": step_index.get(step.id, 0), "step_type": step.type, **more})

    async def _human(step: Any, prompt: str, *, required: list | None = None,
                     level: int = 1, levels: int = 1) -> Any:
        # v1.10.0（R-303）：指定了审批人时，这一级只有他们的回答算数
        cb = _approval_for(step, required=list(required or []), level=level, levels=levels)
        who = f"，需要 {'、'.join(required)} 审批" if required else ""
        lvl = f"（第 {level}/{levels} 级）" if levels > 1 else ""
        return await cb("workflow_approval", {"prompt": prompt}, "sensitive",
                        f"工作流「{schema.name}」第 {step_index.get(step.id, 0)} 步"
                        f"「{step.id}」需要人工审批{lvl}{who}")

    async def _checkpoint(snapshot: dict) -> None:
        await asyncio.to_thread(store.checkpoint, run_id, snapshot)

    executor = WorkflowExecutor(
        schema, inputs=inputs, registry=run_agent.tool_registry, llm=run_agent.llm,
        approval=_human, on_event=_emit, run_id=run_id, resume=resume,
        on_checkpoint=_checkpoint, secrets=_wf_secrets(schema),
        agent_runner=make_agent_runner(run_agent, workflow=schema.name,
                                       approval_for_step=_approval_for, on_event=_emit))
    live["executor"] = executor
    if resume and resume.get("from_run"):
        await asyncio.to_thread(store.update, str(resume["from_run"]),
                                status="resumed", resumed_by=run_id)
    await _broadcast_to_session(chat_sid, {
        "type": "wf_run_start", "run_id": run_id, "workflow": schema.name,
        "file": file_name, "session_id": chat_sid, "total": len(schema.steps),
        "steps": [{"id": s.id, "type": s.type, "line": s.position.line,
                   # parallel 组的子步骤（v1.9.0）：界面在组下面逐个显示进度
                   **({"children": [{"id": c.id, "type": c.type, "line": c.position.line}
                                    for c in s.substeps]} if s.type == "parallel" else {})}
                  for s in schema.steps],
        "resumed_from": live["resumed_from"], "trigger": trigger,
        "reused_steps": sorted((resume or {}).get("steps", {}).keys())})
    live["task"] = asyncio.create_task(
        _execute_workflow_run(live, executor, run_agent, clone_key, schema))
    return run_id


async def _execute_workflow_run(live: dict, executor: Any, run_agent: Any,
                                clone_key: str, schema: Any) -> None:
    """后台执行一次工作流运行并收尾（报告、历史、出站事件、产物卡片）。"""
    from automind.core import session_ctx as _sctx
    from automind.core.metrics import METRICS

    run_id, chat_sid = live["run_id"], live["session_id"]
    t0 = time.perf_counter()
    run = None
    crash = ""
    ws_plan = None
    try:
        with suppress(Exception):
            run_agent.llm.reset()
        if getattr(run_agent, "resources", None) is not None:
            run_agent.resources.tokens.reset()
        release = await _wf_admit(live)
        try:
            ws_plan = run_agent._prepare_workspace(chat_sid)

            # v1.11.1：工作流里的浏览器步骤同样把画面推给右栏「浏览器」预览
            async def _wf_sink(ev: dict) -> None:
                if ev.get("type") == "browser_preview":
                    await _broadcast_to_session(chat_sid, {**ev, "session_id": chat_sid,
                                                           "run_id": run_id})

            with _sctx.bind_session(chat_sid, run_id=f"wf-{run_id}",
                                    workspace=ws_plan.path if ws_plan.isolated else None,
                                    sink=_wf_sink):
                run = await executor.run()
        finally:
            if release is not None:
                release()
    except asyncio.CancelledError:
        # 取消由我们自己的"停止"接口发起（或服务退出）。执行器已把当前步骤标
        # cancelled、整轮标 cancelled 并收好报告 —— 这里只负责把它记下来。
        run = getattr(executor, "_run", None)
        if run is None and live.get("queue_position") is not None:
            live["cancelled_in_queue"] = True           # 还在排队就被停了
        logger.info("workflow_run_cancelled", run_id=run_id)
    except Exception as e:
        run = getattr(executor, "_run", None)
        crash = f"{type(e).__name__}: {e}"
        logger.error("workflow_run_crashed", run_id=run_id, error=crash)
    finally:
        run_agent._workflow_busy = False

    try:
        await _finish_workflow_run(live, run, run_agent, schema, crash=crash,
                                   ws_plan=ws_plan, elapsed=time.perf_counter() - t0)
    except Exception as e:                              # pragma: no cover - 收尾兜底
        live["status"] = live.get("status") if live.get("status") != "running" else "failed"
        logger.error("workflow_finish_failed", run_id=run_id, error=str(e))
    finally:
        live["executor"] = None
        stale = _session_clones.pop(clone_key, None)
        if stale is not None:
            _close_clone_later(stale)
        METRICS.inc("automind_workflow_runs_total", status=str(live.get("status") or "unknown"))
        # 内存里只留最近 _WF_LIVE_KEEP 个已结束的运行（完整记录在磁盘上）
        done = [k for k, v in _wf_live.items() if v.get("status") != "running"]
        for k in done[:-_WF_LIVE_KEEP]:
            _wf_live.pop(k, None)


async def _wf_admit(live: dict):
    """并发配额与排队（v1.10.0，企业版，R-306）：按团队限制同时运行数，超出就排队。

    返回"释放名额"的回调（没有配额时为 None）。排队期间运行仍是 running 状态
    （可以停止），只是 ``queue_position`` 不为空；位次变化经 WebSocket 推给界面。
    """
    gov = _wf_gov()
    if gov is None or not hasattr(gov, "admit"):
        return None
    run_id, chat_sid = live["run_id"], live["session_id"]
    team = str((live.get("initiated_by") or {}).get("team") or "default")

    async def _on_queue(position: int | None) -> None:
        live["queue_position"] = position
        await _broadcast_to_session(chat_sid, {
            "type": "wf_run_queued", "run_id": run_id, "workflow": live["workflow"],
            "session_id": chat_sid, "position": position, "team": team})

    release = await gov.admit(run_id, team, _on_queue)
    if live.get("queue_position") is not None:
        await _on_queue(None)                           # 轮到了：清掉位次
    return release


async def _finish_workflow_run(live: dict, run: Any, run_agent: Any, schema: Any, *,
                               crash: str, ws_plan: Any, elapsed: float) -> None:
    from automind.workflow.runs import checkpoint_status

    run_id, chat_sid = live["run_id"], live["session_id"]
    if run is not None:
        report = run.as_dict()
        summary_line = run.summary_line()
    else:
        # 连第一步都没开始（准备工作目录失败、排队中被停止等）→ 退还额度，如实记录
        _quota.refund_task()
        queued_cancel = bool(live.get("cancelled_in_queue"))
        report = {"workflow": schema.name, "run_id": run_id,
                  "status": "cancelled" if queued_cancel else "failed",
                  "error": ("排队等待并发名额时被停止，一步都没有执行" if queued_cancel
                            else crash or "运行未能开始"), "steps": [], "summary": {
                      "total": len(schema.steps), "succeeded": 0, "failed": 0,
                      "skipped": len(schema.steps), "cancelled": 0, "ok": False,
                      "exit_code": 1}}
        summary_line = f"工作流「{schema.name}」未能开始：{report['error']}"
    if crash and not report.get("error"):
        report["error"] = crash
    usage = getattr(getattr(run_agent, "llm", None), "usage", None)
    tokens = {"prompt": int(getattr(usage, "prompt_tokens", 0) or 0),
              "completion": int(getattr(usage, "completion_tokens", 0) or 0)}
    tokens["total"] = tokens["prompt"] + tokens["completion"]
    report["tokens"] = tokens
    report["file"] = live.get("file", "")
    report["session_id"] = chat_sid
    status = str(report.get("status") or "failed")
    live["report"] = report
    live["finished"] = time.time()
    store = _wf_store()
    await asyncio.to_thread(store.finish, run_id, report)
    # 状态最后才切出 running：此前查询接口一直能拿到实时视图，不会读到"半截报告"
    live["status"] = status

    # R-103：写入任务历史（interaction="workflow"），历史视图里能看到报告
    ok = bool((report.get("summary") or {}).get("ok"))
    failed_lines = [f"✗ {s.get('id')}：{s.get('error')}" for s in report.get("steps") or []
                    if s.get("status") in ("failed", "aborted", "cancelled") and s.get("error")]
    record = {
        "session_id": run_id, "chat_session": chat_sid,
        "task": f"工作流：{schema.name}", "success": ok,
        "output": "\n".join([summary_line, *failed_lines[:10]]),
        "steps": (report.get("summary") or {}).get("total", 0),
        "backtracks": 0, "errors_corrected": 0,
        # v1.11.10：缓存命中/调用次数/上下文峰值随记录一起写入历史
        **_usage_record_fields(usage),
        "duration_ms": round(elapsed * 1000, 1), "plan": None,
        "interaction": "workflow", "status": status,
        "workflow_run": {k: v for k, v in report.items() if k != "outputs"},
    }
    _push_history(record)
    _accumulate_tokens(record)

    # 观测 + 出站事件（R-110）：没有全绿才发 —— workflow.partial / workflow.failed
    with suppress(Exception):
        _observability.record(chat_sid, {"type": "workflow_run", "session_id": run_id,
                                         "task": schema.name, "status": status})
    if status in ("partial", "aborted", "failed"):
        try:
            from automind.core import webhooks

            event = "workflow.partial" if status == "partial" else "workflow.failed"
            failed_step = next((s for s in report.get("steps") or []
                                if s.get("status") in ("failed", "aborted")), None)
            webhooks.emit(event, webhooks.build_payload(
                event, session_id=chat_sid, task=schema.name, status=status,
                elapsed=elapsed, tokens={"total": tokens["total"]},
                error=str((failed_step or {}).get("error") or report.get("error") or ""),
                extra={"run_id": run_id, "workflow": schema.name,
                       "file": live.get("file", ""),
                       "failed_step": (failed_step or {}).get("id", ""),
                       "summary": report.get("summary") or {},
                       "resumable": checkpoint_status(status) in ("partial", "interrupted")}))
        except Exception as e:
            logger.warning("workflow_webhook_failed", run_id=run_id, error=str(e))

    # R-105：开启会话隔离时，产物在会话副本里 —— 推一张产物卡片
    if ws_plan is not None and getattr(ws_plan, "isolated", False):
        try:
            from automind.core import workspace as _ws

            ch = _ws.changes(chat_sid, run_agent.config.project_root)
            if ch.get("ok"):
                await _broadcast_to_session(chat_sid, {
                    "type": "workspace_result", "session_id": chat_sid,
                    "path": ws_plan.path, "added": ch["added"][:200],
                    "modified": ch["modified"][:200], "deleted": ch["deleted"][:200],
                    "count": ch["count"], "workflow_run_id": run_id})
        except Exception as e:                          # pragma: no cover - 纯观测
            logger.warning("workflow_workspace_result_failed", error=str(e))

    await _broadcast_to_session(chat_sid, {
        "type": "wf_run_done", "run_id": run_id, "workflow": schema.name,
        "file": live.get("file", ""), "session_id": chat_sid, "status": status,
        "summary": report.get("summary") or {}, "summary_line": summary_line,
        "error": report.get("error") or "", "tokens": tokens,
        "duration_ms": round(elapsed * 1000, 1), "trigger": live.get("trigger", "manual"),
        "resumable": checkpoint_status(status) in ("partial", "interrupted"),
        "report": {k: v for k, v in report.items() if k != "outputs"}})

    # v1.9.0（专业版 workflow_triggers，R-209）：触发运行没有全绿 → 按触发器的 retry
    # 从断点自动续跑、按工作流的 notify 发通知。社区版没有这个特性，什么都不做。
    trig = _edition.get_feature("workflow_triggers")
    if trig is not None and hasattr(trig, "on_run_finished"):
        try:
            saved = await asyncio.to_thread(store.load, run_id) or {}
            await _maybe_await(trig.on_run_finished(saved, report))
        except Exception as e:                          # pragma: no cover - 扩展兜底
            logger.warning("workflow_trigger_finish_hook_failed", run_id=run_id, error=str(e))


async def _maybe_await(value: Any) -> Any:
    """扩展回调可同步可异步：是协程就等它。"""
    if asyncio.iscoroutine(value):
        return await value
    return value


def _live_view(live: dict) -> dict:
    """进行中运行的实时视图（GET 查询用）。"""
    return {
        "run_id": live["run_id"], "workflow": live["workflow"], "file": live.get("file", ""),
        "session_id": live.get("session_id", ""), "status": live.get("status", "running"),
        "current": live.get("current", ""), "total": live.get("total", 0),
        "steps": list(live.get("steps", {}).values()),
        "started_at": live.get("started"), "resumed_from": live.get("resumed_from", ""),
        "trigger": live.get("trigger", "manual"), "trigger_key": live.get("trigger_key", ""),
        "queue_position": live.get("queue_position"),
        "initiated_by": live.get("initiated_by"),
        "report": live.get("report"), "live": live.get("status") == "running",
    }


@app.get("/api/workflow/runs")
async def api_workflow_runs(request: Request, file: str = "", limit: int = 50,
                            trigger_key: str = ""):
    """最近的运行（界面"运行记录"与"可续跑"提示用）。"""
    rows = await asyncio.to_thread(_wf_store().list, limit=max(1, min(limit, 200)), file=file,
                                   trigger_key=trigger_key)
    identity = _request_identity(request)
    rows = [r for r in rows if _wf_can(identity, "view", str(r.get("file") or ""))[0]]
    for row in rows:
        live = _wf_live.get(str(row.get("run_id")))
        if live is not None and live.get("status") == "running":
            row["status"] = "running"
            row["live"] = True
            row["resumable"] = False
    return {"runs": rows}


async def _wf_run_file(run_id: str) -> str:
    live = _wf_live.get(run_id)
    if live is not None:
        return str(live.get("file") or "")
    rec = await asyncio.to_thread(_wf_store().load, run_id) or {}
    return str(rec.get("file") or "")


@app.get("/api/workflow/runs/{run_id}")
async def api_workflow_run_get(run_id: str, request: Request, include_outputs: bool = False):
    """查询一次运行：进行中返回实时进度，结束后返回完整报告（R-102）。"""
    denied = _wf_authorize(request, "view", await _wf_run_file(run_id))
    if denied is not None:
        return denied
    live = _wf_live.get(run_id)
    if live is not None and live.get("status") == "running":
        return _live_view(live)
    record = await asyncio.to_thread(_wf_store().load, run_id)
    if record is None:
        if live is not None:
            return _live_view(live)
        return JSONResponse({"error": "找不到这次运行"}, status_code=404)
    from automind.workflow.runs import summarize

    out = summarize(record)
    report = dict(record.get("report") or {})
    report.pop("outputs", None)
    out.update({"report": report or None, "records": record.get("records") or {},
                "inputs": record.get("inputs") or {}, "live": False})
    if include_outputs:
        out["outputs"] = record.get("outputs") or {}
    return out


@app.post("/api/workflow/runs/{run_id}/cancel")
async def api_workflow_run_cancel(run_id: str, request: Request):
    """取消一次进行中的运行：当前步骤标 cancelled、整轮 cancelled（R-102）。

    取消经 asyncio 一路传到工具层 —— 终端工具据此终止子进程；执行器只记录、
    不吞取消（见 workflow/executor.py 第 3 条硬边界）。
    """
    denied = _wf_authorize(request, "run", await _wf_run_file(run_id))
    if denied is not None:
        return denied
    live = _wf_live.get(run_id)
    task = (live or {}).get("task")
    if live is None or task is None or task.done() or live.get("status") != "running":
        return JSONResponse({"ok": False, "error": "这次运行不在进行中"}, status_code=409)
    task.cancel()
    logger.info("workflow_run_cancel_requested", run_id=run_id)
    return {"ok": True, "run_id": run_id, "status": "cancelling"}


@app.post("/api/workflow/runs/{run_id}/resume")
async def api_workflow_run_resume(run_id: str, request: Request, payload: dict | None = None):
    """断点续跑（R-104）：已成功的步骤直接沿用结果，从第一个未成功的步骤接着跑。

    文件自上次运行后被改过（摘要不同）则**拒绝**：旧步骤的结果是按旧版本产生的，
    拿它们去喂新版本的后续步骤，等于跑了一份谁都没批过的混合流程。
    """
    payload = payload or {}
    denied = _wf_authorize(request, "run", await _wf_run_file(run_id))
    if denied is not None:
        return denied
    result = await _resume_workflow(run_id, session_id=str(payload.get("session_id") or ""),
                                    initiated_by=_request_identity(request))
    if not result.get("ok"):
        status = int(result.pop("status", 409))
        return JSONResponse(result, status_code=status)
    return result


async def _resume_workflow(run_id: str, *, session_id: str = "", retry_left: int = 0,
                           as_retry: bool = False, initiated_by: dict | None = None) -> dict:
    """断点续跑的内部实现（REST 续跑、失败自动重试、重启续跑共用）。

    ``as_retry=True``：由触发器的 retry 发起 —— 新运行沿用原运行的触发来源，
    尝试次数 +1，``retry_left`` 是本次之后还能再试几次。
    """
    live = _wf_live.get(run_id)
    if live is not None and live.get("status") == "running":
        return {"ok": False, "status": 409, "error": "这次运行仍在进行中"}
    store = _wf_store()
    plan, why = await asyncio.to_thread(store.resume_plan, run_id)
    if plan is None:
        return {"ok": False, "status": 409, "error": why}
    record = await asyncio.to_thread(store.load, run_id) or {}
    file_name = str(record.get("file") or "")
    if file_name:
        schema, _, _, err = await asyncio.to_thread(_load_workflow_for_run, {"file": file_name})
    elif record.get("yaml"):
        schema, _, _, err = await asyncio.to_thread(
            _load_workflow_for_run, {"yaml": record["yaml"], "source": record.get("source")})
    else:
        return {"ok": False, "status": 409, "error": "这次运行没有保存工作流原文，无法续跑"}
    if err is not None:
        try:
            body = json.loads(bytes(err.body).decode("utf-8"))
        except (ValueError, TypeError):
            body = {}
        return {"ok": False, "status": err.status_code, **body}
    if schema.source_digest != record.get("source_digest"):
        return {"ok": False, "status": 409, "type": "digest_mismatch", "error": (
            "工作流文件自上次运行后已被修改（摘要不同）。沿用旧步骤的结果去跑新版本的"
            "后续步骤不安全，已拒绝续跑；请重新运行整个工作流")}
    blocked = await asyncio.to_thread(_workflow_slot_error,
                                      get_agent().tool_registry.list_names(), file_name)
    if blocked:
        return {"ok": False, "status": 403, "type": "workflow_quota", "error": blocked}
    unpublished = _wf_policy(file_name, schema, trigger=str(record.get("trigger") or "manual"))
    if unpublished:
        return {"ok": False, "status": 403, "type": "governance", "error": unpublished}
    allowed, why = _quota.try_consume_task()
    if not allowed:
        return {"ok": False, "status": 429, "type": "quota", "error": why}
    chat_sid = session_id or str(record.get("session_id") or "default")
    new_id = await _start_workflow_run(
        schema, dict(record.get("inputs") or {}), file_name=file_name, chat_sid=chat_sid,
        resume=plan, yaml_text=str(record.get("yaml") or ""),
        trigger=str(record.get("trigger") or "manual"),
        trigger_key=str(record.get("trigger_key") or ""),
        retry_left=retry_left if as_retry else 0,
        attempt=int(record.get("attempt") or 1) + 1,
        initiated_by=initiated_by or record.get("initiated_by"))
    return {"ok": True, "run_id": new_id, "resumed_from": run_id, "status": "running",
            "reused_steps": sorted(plan["steps"])}


@app.post("/api/workflow/runs/{run_id}/dismiss")
async def api_workflow_run_dismiss(run_id: str, request: Request):
    """把一次没跑完的运行标记为"忽略"（不再提示续跑）。"""
    denied = _wf_authorize(request, "run", await _wf_run_file(run_id))
    if denied is not None:
        return denied
    live = _wf_live.get(run_id)
    if live is not None and live.get("status") == "running":
        return JSONResponse({"ok": False, "error": "这次运行仍在进行中"}, status_code=409)
    rec = await asyncio.to_thread(_wf_store().update, run_id, status="dismissed")
    if rec is None:
        return JSONResponse({"ok": False, "error": "找不到这次运行"}, status_code=404)
    return {"ok": True, "run_id": run_id}


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
    """加载插件并将其 hooks 应用到当前 Agent（启用状态会持久化）。"""
    agent = get_agent()
    agent.plugin_manager.discover()   # 刚放进目录的插件也能直接启用
    hooks = agent.plugin_manager.load(name)
    if hooks is None:
        return JSONResponse({"error": f"插件加载失败或不存在：{name}"}, status_code=400)
    agent.apply_plugin_hooks()
    await asyncio.to_thread(_persist_enabled_plugins, agent)
    return {"ok": True, "loaded": agent.plugin_manager.loaded_names()}


@app.post("/api/plugins/{name}/unload")
async def api_plugin_unload(name: str):
    """卸载插件并刷新 Agent hooks（启用状态会持久化）。"""
    agent = get_agent()
    removed = agent.plugin_manager.unload(name)
    agent.apply_plugin_hooks()
    await asyncio.to_thread(_persist_enabled_plugins, agent)
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

    resume_info, resume_err = _resume_target(data, chat_sid)
    if resume_err:
        return JSONResponse({"error": f"无法从断点续跑：{resume_err}"}, status_code=409)
    if resume_info is not None:
        task = resume_info.get("task") or task
        data = {**data, "interaction": resume_info.get("interaction") or interaction,
                "overrides": {}}
    if not task and not images:
        return JSONResponse({"error": "任务内容为空"}, status_code=400)
    interaction, persist_mode, ov_receipt = _resolve_overrides(data, allow_plan=False)
    raw_task = task                 # 语义缓存以用户原文为键（不含注入前缀）
    prefer_tools: list[str] = []
    goal_ctx: dict = {}
    if resume_info is None:
        task = _apply_expert_override(task, data, ov_receipt)  # 专家注入（全部模式）
        task, prefer_tools = _apply_references(task, data, ov_receipt)  # v1.11.0
        task, goal_ctx["id"] = _apply_goal(task, data, ov_receipt)      # v1.11.0
        task, att_key = _apply_attachments(task, data, ov_receipt)
        raw_task += att_key
        task, mention_key = _apply_mentions(task, data, ov_receipt)  # v1.11.2 @ 引用
        raw_task += mention_key
        task = _apply_conversation_context(task, chat_sid)   # v1.7.6 会话延续

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
    _prev_interaction = (agent._interaction, agent._mode)
    if interaction:
        try:
            agent._interaction = InteractionMode(interaction)
            agent._mode = ExecutionMode(
                _INTERACTION_TO_EXECUTION.get(interaction, "plan_and_execute"))
            if persist_mode:
                _save_active(interaction=interaction)
        except ValueError:
            pass

    # 取本会话独立的 Agent —— 必须在切模型/路由**之前**，否则模式与模型仍然
    # 落在全局实例上，并发任务照样互相覆盖（企业版走会话池，其余走轻量克隆）
    _base = agent
    agent = _acquire_run_agent(agent, chat_sid)
    if not persist_mode:
        _base._interaction, _base._mode = _prev_interaction
    try:
        agent.session_id = chat_sid
    except Exception as e:
        logger.warning("session_id_assign_failed", error=str(e))
    # v1.11.0：本条引用的工具必下发；HTTP 路径没有计划审批通道
    agent.prefer_tools = prefer_tools
    agent.plan_review_cb = None

    # 按交互模式应用对应模型（per-mode 配置）
    agent = _apply_mode_model(agent, agent._interaction.value)

    # 模型智能路由（专业版 2 级 / 企业版 N 级）：按复杂度切换本次任务的模型
    agent = _apply_router(agent, raw_task, images)

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

    # 版本限额：每日任务次数（社区版 100 次/天；专业/企业不限）
    allowed, deny_reason = _quota.try_consume_task()
    if not allowed:
        return JSONResponse({"error": deny_reason, "quota": _quota.snapshot()},
                            status_code=429)

    # v1.11.6：服务端内部调用方（IM 机器人）登记的事件回调与审批通道 ——
    # 执行过程要回传到聊天窗口、危险操作要在聊天里批准
    run_hooks = _run_hooks.get(chat_sid)
    if run_hooks:
        agent.event_sink = run_hooks.get("event_sink")
        agent.approval_callback = run_hooks.get("approval")

    session_id = uuid.uuid4().hex[:12]
    _active_sessions[session_id] = {
        "task": task, "status": "running", "steps": 0,
        "created": str(time.time()), "provider": agent.config.llm.provider,
        "model": agent.config.llm.model, "interaction": agent._interaction.value,
    }
    _running_tasks["count"] += 1
    logger.info("task_start", session=session_id, interaction=agent._interaction.value,
                provider=agent.config.llm.provider, model=agent.config.llm.model,
                task=task[:120], running=_running_tasks["count"],
                pooled=_pool_enabled())

    try:
        # ── 对话模式：纯多轮对话 ──
        if agent._interaction == InteractionMode.CHAT and resume_info is None:
            t0 = time.perf_counter()
            hist = _get_session_history(chat_sid)
            # 语义缓存（专业版）：相似问题直接秒回，省一次 LLM 调用
            cached = _cache_lookup(raw_task, images)
            if cached is not None:
                hist.append({"role": "user", "content": raw_task})
                hist.append({"role": "assistant", "content": cached["reply"]})
                await _save_history_notify(chat_sid)
                _active_sessions[session_id]["status"] = "success"
                record = {
                    "session_id": session_id, "task": raw_task, "success": True,
                    "output": cached["reply"], "steps": 0, "backtracks": 0,
                    # 语义缓存秒回没发生 LLM 调用 → 用量字段全 0（结构保持一致）
                    "errors_corrected": 0, **_usage_record_fields(None),
                    "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "plan": None, "interaction": "chat",
                    "cached": True, "cache_score": cached["score"],
                }
                _push_history(record)
                return record
            # 知识库自动检索注入（片段挂进 hist，同一段不重复付费）
            task_with_kb = _apply_kb(task, hist)
            reply = await agent.chat(task_with_kb, images=images, history=hist)
            await _save_history_notify(chat_sid)
            usage = agent.llm.usage
            _cache_store(raw_task, images, reply, usage.total)
            _active_sessions[session_id]["status"] = "success"
            record = {
                "session_id": session_id, "task": task, "success": True,
                "output": reply, "steps": 0, "backtracks": 0,
                "errors_corrected": 0,
                **_usage_record_fields(usage),
                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                "plan": None, "interaction": "chat",
                "model": agent.config.llm.model,
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
                **_usage_record_fields(usage),
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
                **_usage_record_fields(usage),
                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                "plan": None, "interaction": "loop", "stop_reason": lp.get("stop_reason", ""),
            }
            _push_history(record)
            _accumulate_tokens(record)
            return record

        # ── 工作 / 编程模式：完整 Agent 流程 ──
        if resume_info is not None:
            result = await agent.resume_from_checkpoint(resume_info["id"])
        else:
            result = await agent.run(task)
        goal_ctx["output"] = result.output      # 目标进度行在输出末尾，别用截断版
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
            **_usage_record_fields(result.token_usage),
            "duration_ms": round(result.duration_ms, 1),
            "plan": _serialize_plan(result.plan) if result.plan else None,
            "interaction": agent._interaction.value,
        }
        _push_history(record)
        _accumulate_tokens(record)

        if not (run_hooks or {}).get("quiet"):
            # 机器人任务的结果回到聊天窗口，不广播进 Web 界面（否则会冒出一条来历不明的回复）
            await _broadcast({
                "type": "task_complete", "session_id": session_id,
                "success": result.success, "output": result.output,
                "steps": result.steps_executed, "backtracks": result.backtracks,
                "tokens": result.token_usage.total,
                "duration_ms": round(result.duration_ms, 1),
                "plan": record["plan"],
            })
            await _broadcast(_team_event(chat_sid, task, record))
        return record

    except _edition.FeatureNotAvailable as e:
        _active_sessions[session_id]["status"] = "error"
        _active_sessions[session_id]["error"] = str(e)
        _refund_if_never_started(agent)
        return JSONResponse({"error": str(e), "feature": e.feature,
                             "edition": _edition.get_edition()}, status_code=403)
    except Exception as e:
        import traceback
        logger.error("task_failed", session=session_id,
                     interaction=agent._interaction.value, error=str(e),
                     trace=traceback.format_exc()[-800:])
        _active_sessions[session_id]["status"] = "error"
        _active_sessions[session_id]["error"] = str(e)
        _refund_if_never_started(agent)
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        _running_tasks["count"] = max(0, _running_tasks["count"] - 1)
        if run_hooks:
            agent.event_sink = None
            agent.approval_callback = None
        _goal_finish(goal_ctx, session_id, raw_task)
        logger.info("task_end", session=session_id,
                    status=_active_sessions.get(session_id, {}).get("status", "?"),
                    running=_running_tasks["count"])


# ═══════════════════════════════════════════════════════════
# REST API — IM 机器人（v1.11.6）
# ═══════════════════════════════════════════════════════════


async def _bot_apply_workspace(chat_sid: str) -> dict:
    """聊天里回复 /apply：把该对话隔离副本的产物按相对路径覆盖回项目根。"""
    from automind.core import workspace as _wsm
    from automind.tools.file_editor import JOURNAL
    if chat_sid in _live_runs:
        return {"ok": False, "error": "该对话的任务仍在执行，结束后再应用"}
    return await asyncio.to_thread(
        lambda: _wsm.apply_to_project(chat_sid, _project_root(), journal=JOURNAL))


def _bots_manager():
    global _bots_mgr
    if _bots_mgr is None:
        from automind.bots import BotManager
        _bots_mgr = BotManager(read_cfg=_read_config, write_cfg=_write_config,
                               run_task=api_run, run_hooks=_run_hooks,
                               apply_workspace=_bot_apply_workspace)
    return _bots_mgr


_bots_mgr: Any = None


def _bot_error(e: Exception) -> JSONResponse:
    return JSONResponse({"error": str(e)}, status_code=int(getattr(e, "status", 400) or 400))


@app.get("/api/bots/channels")
async def api_bot_channels():
    """支持的渠道：表单字段 + 平台侧配置步骤（界面据此生成表单）。"""
    from automind.bots import CHANNELS
    return {"channels": [c.spec() for c in CHANNELS.values()]}


@app.get("/api/bots")
async def api_bots(request: Request):
    return {"bots": _bots_manager().list(), "base_url": str(request.base_url).rstrip("/")}


@app.post("/api/bots")
async def api_bot_create(data: dict):
    from automind.bots import ChannelError
    try:
        return {"ok": True, "bot": _bots_manager().create(data)}
    except ChannelError as e:
        return _bot_error(e)


@app.patch("/api/bots/{bot_id}")
async def api_bot_update(bot_id: str, data: dict):
    from automind.bots import ChannelError
    try:
        return {"ok": True, "bot": _bots_manager().update(bot_id, data)}
    except ChannelError as e:
        return _bot_error(e)


@app.delete("/api/bots/{bot_id}")
async def api_bot_delete(bot_id: str):
    if not _bots_manager().delete(bot_id):
        return JSONResponse({"error": "机器人不存在"}, status_code=404)
    return {"ok": True}


@app.post("/api/bots/{bot_id}/reconnect")
async def api_bot_reconnect(bot_id: str):
    """长连接立即重连（改了平台侧配置后不必等退避）。"""
    if not _bots_manager().reconnect(bot_id):
        return JSONResponse({"error": "机器人不存在"}, status_code=404)
    return {"ok": True}


@app.post("/api/bots/{bot_id}/test")
async def api_bot_test(bot_id: str, data: dict):
    """测试对话：模拟一条聊天消息（真的执行任务，但回复只进日志、不发到平台）。"""
    from automind.bots import ChannelError
    text = str(data.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "消息为空"}, status_code=400)
    try:
        return await _bots_manager().simulate(bot_id, text, chat_id=str(data.get("chat_id") or "console"))
    except ChannelError as e:
        return _bot_error(e)


@app.get("/api/bots/{bot_id}/log")
async def api_bot_log(bot_id: str, after: int = 0, wait: float = 0):
    """最近的收发记录；wait>0 时长轮询（有新记录立即返回）。"""
    mgr = _bots_manager()
    if mgr.get(bot_id) is None:
        return JSONResponse({"error": "机器人不存在"}, status_code=404)
    if wait > 0:
        return {"log": await mgr.wait_log(bot_id, after, timeout=min(30.0, wait))}
    return {"log": mgr.log(bot_id, after)}


@app.api_route("/api/bots/hook/{bot_id}/{token}", methods=["GET", "POST"])
async def api_bot_hook(bot_id: str, token: str, request: Request):
    """IM 平台回调入口（不需要平台访问令牌：凭 URL 里的机器人令牌 + 各平台签名）。"""
    from automind.bots import HttpReq
    body = await request.body()
    if len(body) > 256 * 1024:
        return JSONResponse({"error": "请求体过大"}, status_code=413)
    req = HttpReq(method=request.method, query=dict(request.query_params),
                  headers={k.lower(): v for k, v in request.headers.items()}, body=body)
    rep = await _bots_manager().handle_hook(bot_id, token, req)
    if isinstance(rep.body, (dict, list)):
        return JSONResponse(rep.body, status_code=rep.status)
    return PlainTextResponse(str(rep.body), status_code=rep.status,
                             media_type=rep.content_type or "text/plain")


def _refund_if_never_started(agent: Any) -> bool:
    """任务一次模型调用都没发生就失败 → 退还当日任务额度（v1.7.5）。

    额度是在任务**开始前**扣的（见 ``try_consume_task``），所以"没配 API Key"
    这类秒失败也会吃掉社区版 100 次/天里的一次 —— 而 ``refund_task()`` 此前
    **零调用者**，那句"任务未真正开始时退还额度"的文档承诺从未兑现。

    判据刻意用「累计用量为 0」而不是匹配异常文案：文案一改，退还就会静默失效，
    而失效的表现是"用户不明不白少了一次额度"—— 几乎不会被注意到。
    取不到用量时**不退还**（宁可少退，不可多退，否则等于变相解除限额）。
    """
    try:
        usage = getattr(getattr(agent, "llm", None), "usage", None)
        used = int(getattr(usage, "total", -1))
    except (TypeError, ValueError, AttributeError):
        return False
    if used != 0:
        return False
    try:
        _quota.refund_task()
    except Exception as e:                             # pragma: no cover - 防御
        logger.warning("quota_refund_failed", error=str(e))
        return False
    logger.info("quota_refunded", reason="任务未真正开始（无任何模型调用）")
    return True


@app.get("/api/chat/history")
async def api_chat_history(session_id: str = "default"):
    """返回某会话的对话历史（用于刷新后恢复显示，多用户隔离）。"""
    return {"messages": _get_session_history(session_id)}


@app.post("/api/chat/reset")
@app.delete("/api/chat/history")
async def api_chat_reset(session_id: str = "default"):
    """删除某会话的对话记录。"""
    _store.session_histories[session_id] = []
    from automind.server_store import SessionSaveError
    try:
        _save_session_history(session_id)
    except SessionSaveError as e:
        logger.error("chat_reset_persist_failed", sid=session_id, error=str(e))
        return JSONResponse(
            {"error": f"清空记录未能写入磁盘：{e}"}, status_code=500)
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
#: 会话标识 → 归属该会话的连接（同一用户的多个标签页）；审批弹窗据此补发
_ws_sessions: dict[str, list[WebSocket]] = {}
#: 会话标识 → 该会话**正在跑**的 Agent 实例（v1.7.2 中途插话）。
#: 插话要找的正是"正在生成的那个 Agent"：队列挂在它身上，执行循环从它取值。
#: 只增删于 _ws_run 的进出，键是会话而非连接 —— 同一会话的另一个标签页
#: 也能给自己正在跑的任务补话。
_live_runs: dict[str, Any] = {}
#: (会话, seq) → 前端自己给的回执标识（v1.7.2）。
#: seq 是**服务端**发号的，前端拿到它时已经需要先猜"这条回执对应我哪一次插入"
#: （连发两句相同内容就分不清）。让前端在请求里带一个 ref，服务端原样回显，
#: 定位就从"猜"变成"查表"。只在有任务在跑时使用，随运行结束一并清理。
_interject_refs: dict[tuple[str, int], str] = {}


async def _handle_interject(ws: WebSocket, client_id: str, data: dict) -> None:
    """WS action=interject：把用户中途补的一句话交给正在跑的任务。

    关键语义：**不取消正在跑的任务**。此前界面上唯一的"说话"方式是
    ``action=run``，而它的处理逻辑是"有旧任务就先 cancel" —— 于是用户在
    AI 写到一半时补一句，得到的实际效果是"把刚才那半截回答掐了重来"。

    三种去向，都要给回执：
      · 有任务在跑 → 入队，回 ``interjection_received``（随后执行循环
        在步骤边界取走，回 ``interjection_applied``）；
      · 没有任务在跑 → 当成一次新任务直接开跑，回 ``interjection_promoted``
        （用户的话不能掉在地上 —— 他觉得"我在补充"，系统看到的是"新的输入"，
        两边认知不一致时应当以"把它办掉"为准）；
      · 收不下（空/超长/超量）→ 回 ``interjection_rejected`` + 原因。
    """
    sid = data.get("session_id") or "default"
    text = str(data.get("text") or "").strip()
    # 前端自带的对账标识（可选）：原样回显在每一条回执里，让"哪条回执对应
    # 我哪一次插入"变成确定的查表，而不是靠文本匹配去猜
    ref = str(data.get("ref") or data.get("client_ref") or "")[:64]
    agent = _live_runs.get(sid)
    if agent is not None:
        try:
            item = agent.interject(text)
        except Exception as e:
            await ws.send_json({"type": "interjection_rejected", "text": text[:200],
                                "reason": str(e), "ref": ref})
            return
        if ref:
            _interject_refs[(sid, item.seq)] = ref
        await ws.send_json({
            "type": "interjection_received", "text": item.text, "seq": item.seq,
            "ref": ref,
            "pending": agent.pending_interjections(), "session_id": sid,
            "run_session_id": getattr(agent, "_run_session_id", ""),
            "mode": getattr(getattr(agent, "_interaction", None), "value", ""),
        })
        logger.info("interjection_received", session=sid, seq=item.seq,
                    chars=len(item.text))
        return

    if not text:
        await ws.send_json({"type": "interjection_rejected", "text": "",
                            "reason": "补充内容为空", "ref": ref})
        return
    # 没有正在跑的任务 → 直接把它当作一次新任务开跑
    await ws.send_json({"type": "interjection_promoted", "text": text, "ref": ref,
                        "reason": "当时没有正在执行的任务"})
    old = _ws_tasks.get(client_id)
    if old and not old.done():
        old.cancel()
    _ws_tasks[client_id] = asyncio.create_task(_ws_run(ws, client_id, {
        "task": text,
        "interaction": data.get("interaction", ""),
        "images": data.get("images") or [],
        "session_id": sid,
    }))


def _normalize_user_answers(raw: Any) -> list[dict] | None:
    """校验并归一化 WS 送来的回答结构；非法返回 None（v1.11.11）。

    前端可能来自多个标签页、也可能被脚本直接调用 —— 结构先钉死，
    免得把半截数据交给待决问题后，提问方拿到一份没法用的回答。
    """
    if not isinstance(raw, list) or not raw:
        return None
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        qid = item.get("id")
        if not isinstance(qid, str) or not qid.strip():
            return None
        selected = item.get("selected")
        if not isinstance(selected, list) or any(
                not isinstance(s, str) for s in selected):
            return None
        answer = {"id": qid, "selected": list(selected)}
        custom = item.get("custom")
        if custom is not None:
            if not isinstance(custom, str):
                return None
            if custom.strip():
                answer["custom"] = custom
        out.append(answer)
    return out


async def _handle_user_answer(ws: WebSocket, data: dict) -> None:
    """WS action=user_answer：回答模型提出的问题（v1.11.11）。

    多标签页语义：**不校验 client_id** —— 同一会话的任一窗口都可以作答，
    先到先得（问题本身是一次性 Future）。作答后要通知同会话的其它标签页
    关闭弹窗，否则它们会一直挂着一个已经失效的问题。
    """
    qid = str(data.get("question_id") or "")
    answers = _normalize_user_answers(data.get("answers"))
    if not qid or answers is None:
        # 结构不合法也回 stale（带格式说明）：既不能静默丢弃，也不能
        # 把它当成一次有效回答去 resolve（那会让提问方收到残缺答案）。
        await ws.send_json({
            "type": "user_question_stale", "question_id": qid,
            "message": ("回答格式不合法：answers 必须是非空数组，元素需含 "
                        "id（字符串）与 selected（字符串数组），custom 可选字符串。"),
        })
        return
    try:
        from automind.core import user_questions

        rec = user_questions.resolve(qid, answers)
    except Exception as e:              # 数据层异常不能把整条 WS 连接搞挂
        logger.warning("user_answer_resolve_failed", question_id=qid, error=str(e))
        rec = None
    if not isinstance(rec, dict):
        # 未知 / 已完成 / 已取消：resolve 约定返回 None，这里如实回执
        await ws.send_json({"type": "user_question_stale", "question_id": qid,
                            "message": "该问题已结束或不存在，本次回答不再生效"})
        return
    resolved = rec.get("answers")
    event = {
        "type": "user_question_resolved",
        "question_id": str(rec.get("question_id") or qid),
        # 用 resolve 归一化后的 answers（而不是回显请求），保证与提问方看到的一致
        "answers": resolved if isinstance(resolved, list) else answers,
        "session_id": str(rec.get("session_id") or _ws_session_of(ws) or ""),
    }
    try:
        await ws.send_json(event)
    except Exception:
        pass                                    # 发起页断了也要通知其它标签页
    await _broadcast_to_session(event["session_id"], event, exclude=ws)
    logger.info("user_question_resolved", question_id=event["question_id"],
                session=event["session_id"], answers=len(event["answers"]))


async def _handle_compact(ws: WebSocket, data: dict) -> None:
    """WS action=compact：手动压缩指定会话的上下文（v1.11.11）。

    Agent 只从"正在跑"或"本会话复用中的克隆"里取：取不到就不压缩 ——
    绝不临时重建一个空 Agent 假装压过了（那样用户会看到"省了 0 token"
    的成功提示，而真正在跑的那个上下文根本没动）。
    """
    sid = str(data.get("session_id") or _ws_session_of(ws) or "default")[:128]
    agent = _live_runs.get(sid) or _session_clones.get(sid)
    if agent is None:
        await ws.send_json({"type": "compact_result", "session_id": sid,
                            "ok": False,
                            "error": "该会话当前没有可压缩的上下文（任务未在运行）"})
        return
    result: Any = None
    force = getattr(agent, "force_compact", None)
    if callable(force):
        try:
            result = force()
            if inspect.isawaitable(result):
                result = await result
        except Exception as e:
            logger.warning("force_compact_failed", session=sid, error=str(e))
            result = {"ok": False, "error": str(e)[:300]}
    if not isinstance(result, dict):
        result = {"ok": False, "error": "该 Agent 不支持上下文压缩"}
    # 把结果摊平给前端：数值统一成 int，避免 None/字符串把界面算崩
    event = {
        "type": "compact_result", "session_id": sid,
        "ok": bool(result.get("ok", False)),
        "folded": int(result.get("folded", 0) or 0),
        "chars_reclaimed": int(result.get("chars_reclaimed", 0) or 0),
        "est_tokens_saved": int(result.get("est_tokens_saved", 0) or 0),
        "messages": int(result.get("messages", 0) or 0),
    }
    if result.get("scope"):
        event["scope"] = str(result["scope"])
    if result.get("error"):
        event["error"] = str(result["error"])[:300]
    await ws.send_json(event)
    logger.info("context_compacted_manually", session=sid, ok=event["ok"],
                folded=event["folded"], est_tokens_saved=event["est_tokens_saved"])


def _jsonable(obj: Any, _depth: int = 0) -> Any:
    """把工具参数转成可 JSON 序列化的形式（供审批弹窗回填编辑）。

    参数里可能混着 Path、枚举、自定义对象 —— 直接 send_json 会整条消息发不出去，
    审批请求发不到前端就等于任务卡死在那里。转不了的降级为字符串。
    """
    if _depth > 6:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v, _depth + 1) for v in obj]
    return str(obj)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # WebSocket 源校验 —— v1.4.6 起**默认开启**。
    #
    # 这里此前是"配置了 AUTOMIND_ALLOWED_ORIGINS 才校验，默认不校验"，
    # 而这条通道恰恰是任务执行主入口（{"action":"run"}）。关键在于
    # **WebSocket 不受同源策略约束**：浏览器对 ws:// 不做 CORS 预检，
    # 任何网页都能直接 new WebSocket("ws://127.0.0.1:8765/ws")。
    # 也就是说 v1.4.5 收紧 CORS 并**拦不住这条路** —— 用户只要访问一个
    # 恶意页面，那个页面就能驱动本机 Agent 跑命令、读写文件，
    # 而本地默认又不开鉴权。
    #
    # 策略与 CORS 保持一致：
    #   · 没有 Origin 头 —— 非浏览器客户端（CLI/SDK/curl），放行；
    #     浏览器发起 WS 时**一定**会带 Origin，伪造不了（受浏览器控制）。
    #   · Origin 为本机地址 —— 应用自身的界面，放行。
    #   · 显式配置 AUTOMIND_ALLOWED_ORIGINS —— 按配置放行。
    #   · 其余一律拒绝。
    origin = ws.headers.get("origin", "")
    if origin and not (_ws_origin_ok(origin) or origin in _WS_ALLOWED_ORIGINS):
        logger.warning("ws_origin_denied", origin=origin[:120],
                       client=ws.client.host if ws.client else "?")
        await ws.close(code=4403)
        return
    # 商用鉴权：配置令牌后，WS 需带 ?token=（静态令牌或 SSO 会话令牌均可）
    token = _auth_token()
    if token and not _token_ok(ws.query_params.get("token", ""), token):
        await ws.close(code=4401)
        return
    await ws.accept()
    client_id = uuid.uuid4().hex[:8]
    _ws_identity[client_id] = _identity_from_token(
        ws.query_params.get("token", ""),
        local=((ws.client.host if ws.client else "") or "") in _LOOPBACK)
    _ws_clients.setdefault("all", []).append(ws)
    # 先按默认会话登记：这样"连接刚建立、任务还没发"时也能收到本会话的审批弹窗
    _ws_bind_session(ws, "default")
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
            elif action == "bind":
                # v1.8.0：页面打开/切换会话时声明"我在看哪个会话"。此前连接只在
                # 发起任务时才绑定会话，于是后台运行的工作流（没有发起连接）
                # 的进度与审批弹窗送不到一个刚刷新过的页面上。
                _ws_bind_session(ws, str(data.get("session_id") or "default")[:128])
            elif action in ("interject", "inject", "supplement"):
                # 中途插话（v1.7.2）：三个名字都收，前端怎么写都不至于失效。
                # 它**不碰** _ws_tasks —— 插话不打断、不重启正在跑的任务。
                await _handle_interject(ws, client_id, data)
            elif action == "user_answer":
                # v1.11.11：回答模型提出的问题。多标签页任一可答（不校验
                # client_id），先到先得；作答后其它标签页的弹窗会被通知关闭。
                await _handle_user_answer(ws, data)
            elif action == "compact":
                # v1.11.11：手动压缩当前会话的上下文（只作用于已有 Agent）
                await _handle_compact(ws, data)
            elif action == "approval_response":
                # 审批结果可能来自**另一个标签页**（`_ws_approvals` 是进程级、
                # 按 approval_id 索引），所以这里不校验 client_id —— 用户开了两个
                # 窗口时，在哪个窗口点「批准」都应当生效。
                aid = str(data.get("approval_id") or "")
                fut = _ws_approvals.get(aid)
                who = _ws_identity.get(client_id)
                ok_who, why_not = _approver_matches(
                    (_pending_approvals.get(aid) or {}).get("required"), who)
                if fut and not fut.done() and not ok_who:
                    # v1.10.0：按角色审批 —— 不是指定审批人的回答不算数（也不当拒绝），
                    # 弹窗继续等真正的审批人
                    await ws.send_json({"type": "approval_forbidden", "approval_id": aid,
                                        "message": why_not})
                elif fut and not fut.done():
                    # 除批准/拒绝外还可携带 arguments（「修改后批准」）；
                    # 结构化结果由 ApprovalOutcome.normalize 统一解析
                    args = data.get("arguments")
                    fut.set_result({
                        "approved": bool(data.get("approved")),
                        "arguments": args if isinstance(args, dict) else None,
                        "comment": str(data.get("comment") or ""),
                        "approver": who,
                    })
                else:
                    # 迟到的回答（超时/取消后弹窗还开着）—— 如实回执，
                    # 免得用户以为自己那一票没生效却毫无反馈
                    try:
                        await ws.send_json({
                            "type": "approval_stale", "approval_id": aid,
                            "message": ("这次审批已经结束（超时或任务已中断），"
                                        "本次点击不再生效。"),
                        })
                    except Exception:
                        pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _fail_pending_approvals(client_id,
                                "发起审批的页面已关闭或连接中断，按拒绝处理")
        _ws_unbind(ws)
        _ws_identity.pop(client_id, None)
        t = _ws_tasks.pop(client_id, None)
        if t and not t.done():
            t.cancel()
        if ws in _ws_clients.get("all", []):
            _ws_clients["all"].remove(ws)


def _fail_pending_approvals(client_id: str, reason: str) -> int:
    """连接断开时立刻终结该连接上的待决审批，返回终结条数。

    此前断开只是"发不出去了"：回调要一直挂到 300 秒超时，任务在这段时间里
    既不前进也不结束，用户在别的窗口看到的就是"它卡住了"。
    现在就地按拒绝收尾（fail-closed），等待方**马上**拿到结果继续跑。
    """
    killed = 0
    for aid, rec in list(_pending_approvals.items()):
        if rec.get("client_id") != client_id:
            continue
        fut = rec.get("future")
        if fut is not None and not fut.done():
            try:
                fut.set_result({"approved": False, "arguments": None,
                                "comment": reason})
            except Exception:
                pass
        _pending_approvals.pop(aid, None)
        _ws_approvals.pop(aid, None)
        killed += 1
    if killed:
        logger.warning("approval_channel_closed", client=client_id,
                       pending=killed, reason=reason)
    return killed


# ── 出站事件（v1.7.3 第 8 项）──────────────────────────────

#: 事件类型 → webhook 事件名。只映射"客户系统真正关心"的几类：
#: 任务起止 + 审批三态。中间过程（思考、工具输出）不发 —— 出站事件是给
#: 集成方做自动化的，把每一条轨迹都推出去只会把对方打爆，也把客户数据
#: 送到没必要的地方。
_WEBHOOK_EVENT_MAP: dict[str, str] = {
    "task_start": "task_start",
    "chat_done": "task_complete",
    "task_complete": "task_complete",
    "task_error": "task_error",
    "task_cancelled": "task_cancelled",
    "approval_request": "approval_request",
    "approval_timeout": "approval_timeout",
    "approval_resolved": "approval_resolved",
}


def _emit_webhook(event_type: str, **fields: Any) -> None:
    """投递一条出站事件。

    **绝不抛异常、绝不阻塞**：这是主任务路径上唯一与外部 HTTP 打交道的地方，
    通知失败把任务弄挂是最不能接受的失败模式（用户宁可收不到通知）。
    默认未配置目标时是零成本空操作。
    """
    try:
        from automind.core import webhooks

        name = _WEBHOOK_EVENT_MAP.get(event_type)
        if not name:
            return
        webhooks.emit(name, webhooks.build_payload(name, **fields))
    except Exception as e:
        logger.warning("webhook_emit_failed", kind=event_type, reason=str(e))


def _webhook_for_event(event: dict) -> None:
    """把一条内部事件折算成出站事件（不需要外发的类型直接返回）。"""
    etype = event.get("type") or ""
    if etype not in _WEBHOOK_EVENT_MAP:
        return
    fields: dict[str, Any] = {
        "session_id": event.get("session_id") or "",
        "task": event.get("task") or "",
        "status": event.get("status") or "",
    }
    if etype in ("chat_done", "task_complete"):
        fields["status"] = "ok"
        if event.get("duration_ms") is not None:
            fields["elapsed"] = float(event["duration_ms"]) / 1000.0
        if event.get("tokens") is not None:
            fields["tokens"] = {"total": event.get("tokens") or 0}
    elif etype == "task_error":
        fields["status"] = "error"
        fields["error"] = str(event.get("error") or "")[:500]
    elif etype == "task_cancelled":
        fields["status"] = "cancelled"
    elif etype == "approval_request":
        fields.update({
            "tool": event.get("tool") or "", "tier": event.get("tier") or "",
            "reason": str(event.get("reason") or "")[:300],
            "arguments": event.get("arguments") or event.get("params") or {},
            "approval_id": event.get("approval_id") or "",
            "timeout_s": event.get("timeout_s") or 0,
            "on_timeout": event.get("action") or event.get("on_timeout") or "",
        })
        try:
            from automind.core import webhooks

            fields["callback_url"] = webhooks.approval_callback_url(
                str(fields["approval_id"]))
        except Exception:
            fields["callback_url"] = ""
    elif etype == "approval_timeout":
        fields.update({
            "tool": event.get("tool") or "", "tier": event.get("tier") or "",
            "approval_id": event.get("approval_id") or "",
            "status": str(event.get("action") or "timeout"),
            "timeout_s": event.get("timeout_s") or 0,
        })
    elif etype == "approval_resolved":
        fields.update({
            "tool": event.get("tool") or "", "tier": event.get("tier") or "",
            "approval_id": event.get("approval_id") or "",
            "status": "approved" if event.get("approved") else "denied",
        })
    _emit_webhook(etype, **fields)


def _record_and_notify(session_id: str, event: dict) -> None:
    """把一条任务事件并入观测/轨迹，并投递对应的出站事件。

    v1.7.3：把 server.py 里原先散落的 11 处 ``_observability.record(...)``
    收敛到这一个入口 —— 因为**事件漏斗只有一个**，出站通知才可能不漏：
    将来新增一条终态路径时，只要它照惯例记事件，通知就自动有了。
    """
    try:
        _observability.record(session_id, event)
    except Exception:
        pass
    _webhook_for_event(event)


@app.post("/api/approvals/{approval_id}")
async def api_approval_receipt(approval_id: str, request: Request):
    """外部系统的审批回执（v1.7.3 第 8 项）。

    为什么需要它：客户要求"在工单系统 / 群里批准"，而此前审批**只能**在 Web
    弹窗里点 —— 客户侧的自动化流程接不进来，只能让人守着浏览器。

    安全要求（三道，缺一不可）：

    1. **这是管理动作**：中间件已按 ``/api/approvals`` 前缀要求管理员权限，
       远程必须带 ``X-Admin-Token``（本机回环放行）—— 这个端点的能力等价于
       "批准任意高风险工具调用"，绝不能是公网上的开放审批后门；
    2. **迟到回执必须明确拒绝**（409 + ``approval_stale``），与 WebSocket 分支
       同语义 —— 静默丢弃会让对方的工单永远停在"已发送审批"；
    3. **拒绝时参数强制置空**：否则只读 ``modified`` 的调用点会把
       "拒绝 + 参数"误判成"改后批准"（那是把拒绝当成放行）。
    """
    from automind.core import webhooks

    aid = str(approval_id or "").strip()
    if not aid:
        return JSONResponse({"ok": False, "error": "缺少 approval_id"}, status_code=400)

    state = webhooks.approval_receipt_state(aid, _pending_approvals)
    if not state.get("resolvable"):
        return JSONResponse(
            {"ok": False, "type": "approval_stale", "approval_id": aid,
             "message": state.get("reason") or "这次审批已经结束，本次回执不再生效。"},
            status_code=409)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体必须是 JSON"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "error": "请求体必须是 JSON 对象"}, status_code=400)

    receipt = webhooks.build_approval_receipt(
        aid, data.get("approved"), str(data.get("comment") or ""),
        data.get("arguments") if isinstance(data.get("arguments"), dict) else None)
    # v1.10.0：回执方是谁。外部系统（持管理员令牌，可信通道）可以在请求体里说明
    # "这是谁在我们的工单里批的"：{"approver": {"user": "alice", "role": "finance"}}；
    # 不说明就按调用方自己的身份记
    claimed = data.get("approver")
    approver = ({"user": str(claimed.get("user") or ""), "role": str(claimed.get("role") or ""),
                 "team": str(claimed.get("team") or ""), "source": "receipt"}
                if isinstance(claimed, dict) else _request_identity(request))
    ok_who, why_not = _approver_matches((_pending_approvals.get(aid) or {}).get("required"),
                                        approver)
    if not ok_who:
        return JSONResponse({"ok": False, "type": "approver_mismatch", "approval_id": aid,
                             "error": why_not}, status_code=403)

    from automind.state.human_loop import ApprovalOutcome

    outcome = ApprovalOutcome.normalize(receipt)
    rec = _pending_approvals.get(aid) or {}
    fut = rec.get("future")
    if fut is None or fut.done():
        # 竞态兜底：状态检查与取 future 之间可能刚好被超时/断连收尾
        return JSONResponse({"ok": False, "type": "approval_stale", "approval_id": aid,
                             "message": "这次审批刚刚结束，本次回执不再生效。"},
                            status_code=409)
    fut.set_result({"approved": outcome.approved,
                    "arguments": outcome.arguments if outcome.approved else None,
                    "comment": outcome.comment or "外部系统回执", "approver": approver})
    logger.info("approval_receipt_external", approval_id=aid,
                approved=outcome.approved, modified=outcome.modified)
    return {"ok": True, "approval_id": aid, "approved": outcome.approved,
            "modified": outcome.modified}


def _external_approval_available() -> bool:
    """外部审批通道是否可用（v1.7.3）。

    两个条件缺一不可：① 配了出站 webhook（审批请求才会被送出去）；
    ② 配了管理员令牌或本机可访问（回执端点的鉴权前提）。
    判据集中在这里，避免"界面没送到就当场拒绝"与"外部还能批"两条规则
    各写一遍、然后在某个分支上互相矛盾。
    """
    try:
        from automind.core import webhooks

        return bool(webhooks.stats().get("enabled"))
    except Exception:                                   # pragma: no cover - 防御性
        return False


def _headless_approval_callback(chat_sid: str, session_id: str, agent: Any):
    """给"没有 WebSocket 的调用方"用的审批回调（工作流 / REST 触发）。

    复用 ``make_approval_callback`` 的同一条链路：同一张待决表
    （``_pending_approvals``）、同一套超时处置、同一个外部回执端点 ——
    否则"弹窗审批"和"外部回执审批"会演化成两套语义，而审批是安全控制，
    两套语义必然有一套是错的。
    """
    return make_approval_callback(None, "http", session_id, chat_sid, agent)


def make_approval_callback(
    ws: Any,
    client_id: str,
    session_id: str,
    chat_sid: str,
    agent: Any,
    *,
    release_slot: Any = None,
    reclaim_slot: Any = None,
    extra: dict | None = None,
):
    """构造"向前端要一次人工批准"的回调（``approval_cb``）。

    独立成模块级函数而不埋在 ``_ws_run`` 的闭包里，是因为这段逻辑是
    **整条审批链路唯一的阻塞点**：它既要发弹窗、又要等回答、还要处理
    超时/断连/送不到三种异常收尾。埋在闭包里就只能靠端到端跑整个任务
    才测得到，于是它长期处于"没人测"的状态 —— 而它的每一种失败形态
    在用户侧的表现都是"任务卡住不动"。

    v1.7.3：``ws`` 允许为 ``None``（工作流经 HTTP 触发时没有 WebSocket）——
    此时只广播弹窗、并把"外部系统回执"（``POST /api/approvals/{id}``）
    当成可用的回答通道。
    """
    approval_timeout = _approval_timeout_seconds(agent)

    async def _approval_cb(tool_name, args, tier, reason):
        approval_id = uuid.uuid4().hex[:10]
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        _ws_approvals[approval_id] = fut
        _pending_approvals[approval_id] = {
            "future": fut, "client_id": client_id, "session_id": session_id,
            "tool": tool_name, "tier": tier, "asked_at": time.time(),
            # v1.10.0（R-303）：这一级只接受哪些审批人（空 = 不限）
            "required": list((extra or {}).get("required") or []),
        }
        # 等待人工回答期间不再占用执行槽（名额还给别的任务）
        if release_slot is not None:
            release_slot()
        payload = {
            "type": "approval_request", "approval_id": approval_id,
            "session_id": session_id, "tool": tool_name, "tier": tier,
            "reason": reason,
            # 截断版仅供展示；editable 是「修改后批准」要回填的原始值，
            # 不能截断 —— 否则用户"没改的那些参数"会被截断值悄悄覆盖
            "params": {k: str(v)[:200] for k, (v) in (args or {}).items()},
            "editable": _jsonable(args or {}),
            # 前端据此显示倒计时：不给期限的话，弹窗看起来可以一直等，
            # 而实际上后端到点就按配置处置了
            "timeout_s": approval_timeout,
            "on_timeout": _approval_timeout_action(agent),
        }
        if extra:
            # v1.8.0（D1）：来自工作流的审批带上 workflow_run_id / step_id，
            # 界面据此显示"来自工作流 X 第 N 步"，而不是一个来历不明的工具名
            payload.update({k: v for k, v in extra.items() if k not in payload})
        delivered = 0
        if ws is not None:
            try:
                await ws.send_json(payload)
                delivered += 1
            except Exception as e:
                # 弹窗发不出去 = 用户根本没机会批准。这不是"等等看"，必须当场
                # 按拒绝收尾并把原因说清，否则整个任务会一直挂到超时。
                logger.warning("approval_request_undeliverable", tool=tool_name,
                               client=client_id, error=str(e))
                _pending_approvals.pop(approval_id, None)
                _ws_approvals.pop(approval_id, None)
                if reclaim_slot is not None:
                    reclaim_slot()
                return {"approved": False, "arguments": None,
                        "comment": f"审批请求无法送达界面（{type(e).__name__}），已按拒绝处理"}
        # 让同一会话的其它标签页也能看到并响应（开两个窗口时不必回到原窗口）
        try:
            delivered += await _broadcast_to_session(chat_sid, payload, exclude=ws)
        except Exception:                               # pragma: no cover - 防御性
            pass
        if delivered == 0 and not _external_approval_available():
            # 界面一个都没送到、外部回执通道也没开 → 没人能回答，当场按拒绝收尾。
            # （v1.7.3：工作流经 HTTP 触发时没有 ws，审批只能靠外部系统回执；
            #   那条路开了就继续等，没开就别让调用方白等一个超时。）
            logger.warning("approval_no_channel", tool=tool_name,
                           session=session_id, approval_id=approval_id)
            _pending_approvals.pop(approval_id, None)
            _ws_approvals.pop(approval_id, None)
            if reclaim_slot is not None:
                reclaim_slot()
            return {"approved": False, "arguments": None,
                    "comment": ("审批请求没有可送达的界面，且未配置外部审批通道"
                                "（webhook），已按拒绝处理")}
        # 审批事件同样并入观测轨迹：排障时要能看出"这一步在等人"。
        # v1.7.3：这里同时是**出站审批事件**的数据源（客户系统要在自己的
        # 工单/群里批准），所以把 reason/参数/时限一并记上 —— 只记 tool 与
        # tier 的话，收到通知的人根本不知道该不该批。
        try:
            _record_and_notify(chat_sid, {
                "type": "approval_request", "session_id": session_id,
                "tool": tool_name, "tier": tier, "approval_id": approval_id,
                "reason": reason, "arguments": _jsonable(args or {}),
                "timeout_s": approval_timeout,
                "action": _approval_timeout_action(agent)})
        except Exception:
            pass
        try:
            answered = await asyncio.wait_for(fut, timeout=approval_timeout)
        except TimeoutError:
            # 超时此前是**静默**返回 False：弹窗还挂在界面上，用户以为系统仍在
            # 等他点，实际上这一步早已按拒绝处理、任务也已经失败。必须明说。
            action = _approval_timeout_action(agent)
            approved = action == "approve"
            outcome = "自动批准" if approved else "自动拒绝"
            logger.warning("approval_timeout", tool=tool_name,
                           timeout_s=approval_timeout, session=session_id,
                           action=action)
            try:
                await ws.send_json({
                    "type": "approval_timeout", "approval_id": approval_id,
                    "session_id": session_id, "tool": tool_name,
                    "timeout_s": approval_timeout,
                    "action": action, "approved": approved,
                    "message": (f"审批等待超过 {approval_timeout:.0f} 秒未响应，"
                                f"已按「{outcome}」处理工具 {tool_name}。"
                                if approved else
                                f"审批等待超过 {approval_timeout:.0f} 秒未响应，"
                                f"已按「{outcome}」处理工具 {tool_name}（未执行）。"
                                "如需无人值守运行，请把审批模式改为「自动」或「全批准」，"
                                "或调大 execution.approval_timeout_seconds。"),
                })
            except Exception:
                pass          # 连接已断时发不出去很正常，日志已经记下了
            # 超时事件也并入轨迹与观测图（排障时要能看到"卡在审批上")
            record_ev = {"type": "approval_timeout", "session_id": session_id,
                         "tool": tool_name, "timeout_s": approval_timeout,
                         "action": action}
            try:
                _record_and_notify(chat_sid, record_ev)
            except Exception:
                pass
            # 返回值与"送不到"分支保持同一种形状（结构化 dict）——
            # 调用方虽然都用 ApprovalOutcome.normalize 归一化，
            # 但两条分支返回不同类型的值迟早会误导某处调用方。
            return {"approved": approved, "arguments": None,
                    "comment": (f"审批等待超过 {approval_timeout:.0f} 秒未响应，"
                                f"已按「{'自动批准' if approved else '自动拒绝'}」处理")}
        finally:
            _ws_approvals.pop(approval_id, None)
            _pending_approvals.pop(approval_id, None)
            if reclaim_slot is not None:
                reclaim_slot()
        # 走到这里说明**有人回答了**（界面点、或外部系统经 /api/approvals 回执）。
        # 记一条"审批已裁决"：指标要算批准率，出站事件要让客户系统知道结果 ——
        # 否则对方工单里的那一条会永远停在"等待审批"。
        try:
            _record_and_notify(chat_sid, {
                "type": "approval_resolved", "session_id": session_id,
                "tool": tool_name, "tier": tier, "approval_id": approval_id,
                "approved": bool((answered or {}).get("approved"))})
        except Exception:
            pass
        return answered

    return _approval_cb


async def _ws_run(ws: WebSocket, client_id: str, data: dict):
    """WebSocket 任务执行 — 对话流式输出，工作/编程可中断。"""
    from automind.core.types import ExecutionMode, InteractionMode, TokenUsage

    task = (data.get("task") or "").strip()
    images = data.get("images") or []
    chat_sid = data.get("session_id") or "default"  # 多用户会话隔离
    # v1.7.7 断点续跑：校验断点归属与有效性；任务原文取自断点，不信任前端
    resume_info, resume_err = _resume_target(data, chat_sid)
    if resume_err:
        await ws.send_json({"type": "task_error", "error": f"无法从断点续跑：{resume_err}",
                            "resume_failed": True})
        return
    if resume_info is not None:
        task = resume_info.get("task") or task
        data = {**data, "interaction": resume_info.get("interaction") or data.get("interaction"),
                "overrides": {}}
    if not task and not images:
        return
    # v1.7.7 输入框 overrides：本条消息的模式 / 专家 / 附件
    interaction, persist_mode, ov_receipt = _resolve_overrides(data)
    raw_task = task                 # 语义缓存/路由以用户原文为准
    prefer_tools: list[str] = []
    goal_ctx: dict = {}
    plan_review = bool((ov_receipt.get("plan") or {}).get("applied"))
    if resume_info is None:
        task = _apply_expert_override(task, data, ov_receipt)  # 专家注入（全部模式）
        task, prefer_tools = _apply_references(task, data, ov_receipt)  # v1.11.0 MCP / 技能
        task, goal_ctx["id"] = _apply_goal(task, data, ov_receipt)      # v1.11.0 长期目标
        task, att_key = _apply_attachments(task, data, ov_receipt)
        raw_task += att_key        # 带附件的消息不能命中"只看原文"的语义缓存
        task, mention_key = _apply_mentions(task, data, ov_receipt)  # v1.11.2 @ 引用
        raw_task += mention_key
        task = _apply_conversation_context(task, chat_sid)   # v1.7.6 会话延续
    # 连接与会话绑定：审批弹窗要能补发到这个会话的其它标签页
    _ws_bind_session(ws, chat_sid)

    agent = get_agent()
    _prev_interaction = (agent._interaction, agent._mode)
    if interaction:
        try:
            agent._interaction = InteractionMode(interaction)
            agent._mode = ExecutionMode(
                _INTERACTION_TO_EXECUTION.get(interaction, "plan_and_execute"))
            if persist_mode:
                _save_active(interaction=interaction)
        except ValueError:
            pass

    # 取本会话独立的 Agent（WS 路径此前直接用全局实例，两个标签页同时跑
    # 会互相覆盖交互模式、串上下文、清对方的 token 计数）
    _base = agent
    agent = _acquire_run_agent(agent, chat_sid)
    if not persist_mode:
        # 单条消息的模式覆盖只作用于本次运行：克隆已拿到该模式，全局模板复原，
        # 否则下一条不带覆盖的消息会莫名其妙地沿用这次的模式
        _base._interaction, _base._mode = _prev_interaction
    # 会话标识交给 Agent —— 决定轨迹按会话落盘、以及目录级隔离的工作副本归属
    try:
        agent.session_id = chat_sid
    except Exception as e:
        logger.warning("session_id_assign_failed", error=str(e))

    # 按交互模式应用对应模型（per-mode 配置）
    agent = _apply_mode_model(agent, agent._interaction.value)

    # 模型智能路由（专业版）：按任务复杂度切换模型
    agent = _apply_router(agent, raw_task, images)

    if agent.llm is None:
        await ws.send_json({"type": "task_error",
                            "error": "LLM 未初始化，请先为该模式配置可用模型的 API Key。"})
        return

    # 资源保护：并发任务上限
    if _running_tasks["count"] >= _MAX_CONCURRENT:
        await ws.send_json({"type": "task_error",
                            "error": f"当前并发任务已达上限（{_MAX_CONCURRENT}），请稍后再试。"})
        return

    # 版本限额：每日任务次数（社区版 100 次/天）
    allowed, deny_reason = _quota.try_consume_task()
    if not allowed:
        await ws.send_json({"type": "task_error", "error": deny_reason})
        return

    session_id = uuid.uuid4().hex[:12]
    # 本次运行的 id 挂在 Agent 上：插话回执要带上它，前端才能把"你补的这句话"
    # 与"哪一次运行"对上（用户可能连着跑了好几轮，回执串台比没有回执更糟）
    try:
        agent._run_session_id = session_id
    except Exception:                                  # pragma: no cover - 防御性
        pass
    # 登记"本会话正在跑谁"（v1.7.2 中途插话要找到这个实例）的时机说明：
    #
    #  · 不能在**提前返回**之前（LLM 未配置 / 并发上限 / 配额这三道门都会直接
    #    return，而注销挂在 finally 里）—— 否则那些路径会把一个"根本没开始跑"
    #    的 Agent 永久留在表里，此后用户每次插话都被收下并回执"已收到"，
    #    却永远没有执行循环来取，也不会报"没纳入"。那正是本功能最不能出现的
    #    一种失败：假装成功。
    #  · 也不能留在 try 之外（下面 `await ws.send_json(task_start)` 失败就没有
    #    finally 兜底了）—— 所以它放在 try 的**第一行**。
    _running_tasks["count"] += 1
    #: 本次任务是否已把并发槽让出去（审批等待期间让槽，只让一次）
    slot_state = {"released": False}

    def _release_slot_for_approval() -> bool:
        """审批等待期间释放并发执行槽 —— 审批不是"在执行"，不该占着名额。

        v1.6.3 及更早：审批弹窗挂多久，这个任务的并发槽就占多久（5 分钟不点，
        名额就空转 5 分钟），别的任务因此被"已达并发上限"挡在门外，而用户
        完全看不出两者有关。这里在**开始等待**时把槽还回去，用户真的回答了
        （或超时终结）再收回；由 ``slot_state`` 保证只让一次、且不会让成负数。
        """
        ex = getattr(agent.config, "execution", None)
        if not getattr(ex, "release_slot_on_approval_wait", True):
            return False
        if slot_state["released"]:
            return False
        if _running_tasks["count"] <= 0:
            return False
        _running_tasks["count"] -= 1
        slot_state["released"] = True
        _approval_waiting["count"] += 1
        logger.info("approval_slot_released", session=session_id,
                    waiting=_approval_waiting["count"])
        return True

    def _reclaim_slot_after_approval() -> None:
        if slot_state["released"]:
            slot_state["released"] = False
            _running_tasks["count"] += 1
            _approval_waiting["count"] = max(0, _approval_waiting["count"] - 1)
            logger.info("approval_slot_reclaimed", session=session_id)

    # 注入审批回调（ask 模式下工具调用前向前端请求批准）
    _approval_cb = make_approval_callback(
        ws, client_id, session_id, chat_sid, agent,
        release_slot=_release_slot_for_approval,
        reclaim_slot=_reclaim_slot_after_approval,
    )
    agent.approval_callback = _approval_cb
    # v1.11.0：本条引用的工具必下发；计划模式 = 计划生成后走同一条审批链路
    # （超时 / 断连 / 多标签页补发都现成），批准才执行
    agent.prefer_tools = prefer_tools
    agent.plan_review_cb = make_approval_callback(
        ws, client_id, session_id, chat_sid, agent,
        release_slot=_release_slot_for_approval,
        reclaim_slot=_reclaim_slot_after_approval,
        extra={"kind": "plan_review"},
    ) if plan_review else None

    # 注入执行过程事件回调（实时展示思考/工具调用/计划步骤）
    async def _event_sink(ev):
        # 先并入观测 DAG（纯内存，异常不影响推送），再发给前端
        try:
            _record_and_notify(chat_sid, ev)
        except Exception:
            pass
        # 插话生效事件补上 ref（v1.7.2）：事件是从执行循环深处发出来的，
        # 那里只认 seq；前端要的是"我发的那条请求"。在这里做一次翻译，
        # 免得把"查表"这件事推给每个消费方各写一遍。
        try:
            if ev.get("type") == "interjection_applied":
                for it in ev.get("items") or []:
                    it["ref"] = _interject_refs.pop((chat_sid, it.get("seq")), "")
        except Exception:                              # pragma: no cover - 纯装饰
            pass
        try:
            await ws.send_json({"session_id": session_id, **ev})
        except Exception:
            pass
    agent.event_sink = _event_sink

    _start_ev = {"type": "task_start", "session_id": session_id,
                 "interaction": agent._interaction.value,
                 # v1.7.7：overrides 回执 + 是否为断点续跑（前端据此给气泡挂 chip）
                 "overrides": ov_receipt, "ref": str(data.get("ref") or ""),
                 **({"resume": {k: resume_info.get(k) for k in
                                ("id", "kind", "done", "total", "remaining")}}
                    if resume_info is not None else {}),
                 # task 一并记上：出站事件（webhook）要告诉集成方"开始了什么"，
                 # 否则客户系统收到的只是一条"有个任务开始了"的空通知
                 "task": raw_task[:200]}
    _record_and_notify(chat_sid, _start_ev)
    await ws.send_json(_start_ev)
    t0 = time.perf_counter()
    try:
        # 登记"本会话正在跑谁"（v1.7.2 中途插话要找到这个实例）——放在 try 的
        # 第一行：从这一刻起本次运行的每一步都被 finally 兜住，插话打进队列后
        # 要么被执行循环取走（interjection_applied），要么在收尾时如实报
        # interjection_dropped，不存在"收下了却没人管"的中间态。
        _live_runs[chat_sid] = agent
        # ── 对话模式：流式 ──（续跑一律走下面的工作/编程完整流程）
        if agent._interaction == InteractionMode.CHAT and resume_info is None:
            hist = _get_session_history(chat_sid)
            # 语义缓存（专业版）：命中则整段回放，零 Token
            cached = _cache_lookup(raw_task, images)
            if cached is not None:
                await ws.send_json({"type": "chat_chunk", "session_id": session_id,
                                    "delta": cached["reply"]})
                hist.append({"role": "user", "content": raw_task})
                hist.append({"role": "assistant", "content": cached["reply"]})
                await _save_history_notify(chat_sid, ws)
                record = {
                    "session_id": session_id, "task": raw_task, "success": True,
                    "output": cached["reply"], "steps": 0, "backtracks": 0,
                    # 语义缓存秒回没发生 LLM 调用 → 用量字段全 0（结构保持一致）
                    "errors_corrected": 0, **_usage_record_fields(None),
                    "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "plan": None, "interaction": "chat",
                    "cached": True, "cache_score": cached["score"],
                }
                _push_history(record)
                # 终态事件带上身份与账目：出站通知与指标都要用
                # （只发 {"type": "chat_done"} 的话，客户系统收到的是
                # 一条没有会话、没有耗时、没有用量的空通知）
                _record_and_notify(chat_sid, {
                    "type": "chat_done", "session_id": session_id,
                    "task": raw_task[:200], "status": "ok",
                    "duration_ms": record["duration_ms"], "tokens": 0})
                await ws.send_json({"type": "chat_done", "session_id": session_id,
                                    "tokens": 0, "prompt_tokens": 0,
                                    "completion_tokens": 0, "cached": True,
                                    "duration_ms": record["duration_ms"]})
                return
            chunks: list[str] = []
            # 知识库自动检索注入（片段挂进 hist，同一段不重复付费）
            task_with_kb = _apply_kb(task, hist)
            async for delta in agent.chat_stream(task_with_kb, images=images,
                                                 history=hist):
                chunks.append(delta)
                await ws.send_json({"type": "chat_chunk", "session_id": session_id,
                                    "delta": delta})
            await _save_history_notify(chat_sid, ws)
            _cache_store(raw_task, images, "".join(chunks),
                         getattr(getattr(agent, "_last_stream_usage", None),
                                 "total", 0) or 0)
            usage = getattr(agent, "_last_stream_usage", None) or TokenUsage()
            record = {
                "session_id": session_id, "task": task, "success": True,
                "output": "".join(chunks), "steps": 0, "backtracks": 0,
                "errors_corrected": 0, **_usage_record_fields(usage),
                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                "plan": None, "interaction": "chat",
                # 中途插话账目：这轮收了几条、真正并进回答几条（可观测、可复盘）
                "interjections": agent.interjection_report(),
            }
            _push_history(record)
            _accumulate_tokens(record)
            _record_and_notify(chat_sid, {
                "type": "chat_done", "session_id": session_id,
                "task": raw_task[:200], "status": "ok",
                "duration_ms": record["duration_ms"], "tokens": usage.total})
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
                **_usage_record_fields(usage),
                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                "plan": None, "interaction": "multi",
                "multi_steps": ma.get("steps", []),
            }
            _push_history(record)
            _accumulate_tokens(record)
            _record_and_notify(chat_sid, {"type": "task_complete", **record})
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
                **_usage_record_fields(usage),
                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                "plan": None, "interaction": "loop",
                "stop_reason": lp.get("stop_reason", ""),
            }
            _push_history(record)
            _accumulate_tokens(record)
            _record_and_notify(chat_sid, {"type": "task_complete", **record})
            await ws.send_json({"type": "task_complete", **record})
            return

        # ── 工作 / 编程模式：完整流程（可中断）──
        # v1.7.7：断点续跑走真正的 resume（计划只跑剩余目标 / ReAct 带着已完成
        # 进度继续），不再把原任务文本重发一遍
        if resume_info is not None:
            result = await agent.resume_from_checkpoint(resume_info["id"])
        else:
            result = await agent.run(task)
        goal_ctx["output"] = result.output      # 目标进度行在输出末尾，别用截断版
        record = {
            "session_id": session_id, "task": task, "success": result.success,
            "output": result.output[:5000], "steps": result.steps_executed,
            "backtracks": result.backtracks, "errors_corrected": result.errors_corrected,
            **_usage_record_fields(result.token_usage),
            "duration_ms": round(result.duration_ms, 1),
            "plan": _serialize_plan(result.plan) if result.plan else None,
            "interaction": agent._interaction.value,
            # 中途插话账目：这轮收了几条、真正并进本轮几条（可观测、可复盘）
            "interjections": agent.interjection_report(),
        }
        _push_history(record)
        _accumulate_tokens(record)
        _record_and_notify(chat_sid, {"type": "task_complete", **record})
        await ws.send_json({"type": "task_complete", **record})
        # 团队协作：向所有在线成员广播活动（同事的 Agent 改了文件 → 通知你）
        await _broadcast(_team_event(chat_sid, task, record))

    except asyncio.CancelledError:
        try:
            _record_and_notify(chat_sid, {
                "type": "task_cancelled", "session_id": session_id,
                "task": raw_task[:200], "status": "cancelled"})
            await ws.send_json({"type": "task_cancelled", "session_id": session_id})
        except Exception:
            pass
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        # v1.7.5：一次模型调用都没发生就失败 → 退还当日额度（与 HTTP 路径同口径）
        _refund_if_never_started(agent)
        try:
            _record_and_notify(chat_sid, {
                "type": "task_error", "session_id": session_id,
                "task": raw_task[:200], "status": "error", "error": str(e)})
            await ws.send_json({"type": "task_error", "session_id": session_id,
                                "error": str(e)})
        except Exception:
            pass
    finally:
        agent.approval_callback = None
        agent.plan_review_cb = None
        agent.prefer_tools = []
        # v1.11.0 目标交付模式：这一轮记进目标，并告诉界面刷新目标进度
        _goal = _goal_finish(goal_ctx, session_id, raw_task)
        if _goal is not None:
            try:
                await ws.send_json({
                    "type": "goal_progress", "session_id": session_id,
                    "goal": {k: _goal.get(k) for k in (
                        "id", "title", "status", "progress", "run_count",
                        "success_count", "suggest_done", "tokens")}})
            except Exception:
                pass
        # ── 中途插话的收尾（v1.7.2）──
        # 用户在这轮跑的过程中补的话，若到最后还没被交给模型（回答先写完了、
        # 任务被取消、或到了续写次数上限），**必须如实回执**。
        # 静默丢弃是最坏的一种：用户看到自己那句话出现在屏幕上，就默认它生效了，
        # 而模型从头到尾没见过它 —— 之后所有基于它的期待都会落空且无从追查。
        try:
            leftover = agent.drain_interjections()
        except Exception as e:                         # pragma: no cover - 防御性
            leftover = []
            logger.warning("interjection_drain_failed", error=str(e))
        for _it in leftover:
            try:
                await ws.send_json({
                    "type": "interjection_dropped", "seq": _it.seq, "text": _it.text,
                    "session_id": session_id,
                    "ref": _interject_refs.pop((chat_sid, _it.seq), ""),
                    "reason": "本轮任务在你补充之前就结束了，这条没有交给模型",
                })
            except Exception:
                # 连接已断/任务被取消 —— 前端在终态事件里会把这批气泡标成
                # "未纳入本轮"，不靠这一条必达
                pass
        if leftover:
            logger.warning("interjection_dropped", count=len(leftover),
                           session=chat_sid, run=session_id)
        try:
            agent._interjections.dropped += len(leftover)
        except Exception:                              # pragma: no cover - 防御性
            pass
        # 清理本会话的 ref 表：运行结束 = 这些对账标识再也不会被用到，
        # 留着就是一条只增不减的内存
        for _key in [k for k in _interject_refs if k[0] == chat_sid]:
            _interject_refs.pop(_key, None)
        # 注销"本会话正在跑谁"：此后同一个会话再插话会被当作新任务开跑，
        # 而不是塞给一个已经结束的执行循环（那才是真的会被无声吞掉）
        if _live_runs.get(chat_sid) is agent:
            _live_runs.pop(chat_sid, None)
        agent.event_sink = None
        # 若仍处于"审批让槽"状态（任务在等待中被取消/异常），先把等待计数归位，
        # 再还槽 —— 否则 _approval_waiting 会永久偏差，诊断数据从此不可信。
        if slot_state["released"]:
            slot_state["released"] = False
            _approval_waiting["count"] = max(0, _approval_waiting["count"] - 1)
            _running_tasks["count"] += 1
        _running_tasks["count"] = max(0, _running_tasks["count"] - 1)


def _ws_session_of(ws: Any) -> str:
    """该连接当前归属的会话标识（未跑过任务时为空串）。

    连接一建立就登记（见 ``ws_endpoint``），`run` 时再按本次请求的
    ``session_id`` 更新 —— 审批弹窗要按会话找到"同一个用户的其它窗口"。
    """
    for sid, sockets in _ws_sessions.items():
        if ws in sockets:
            return sid
    return ""


def _ws_bind_session(ws: Any, session_id: str) -> None:
    """把连接绑定到某个会话（旧的绑定先摘掉）。"""
    for sid, sockets in list(_ws_sessions.items()):
        if ws in sockets and sid != session_id:
            sockets.remove(ws)
    _ws_sessions.setdefault(session_id, []).append(ws)


def _ws_unbind(ws: Any) -> None:
    for sockets in _ws_sessions.values():
        if ws in sockets:
            sockets.remove(ws)
    for sid in [s for s, v in _ws_sessions.items() if not v]:
        _ws_sessions.pop(sid, None)


async def _broadcast_to_session(session_key: str, data: dict, exclude: Any = None) -> int:
    """把消息推给**同一会话**的其它连接（用于审批请求的多窗口可达）。

    审批是阻塞式等待：弹窗只发到"发起这个任务的那个连接"时，用户只要把
    那个标签页最小化/切走，就从界面上看不到"它在等你点"。
    这里把同会话的其它连接也补一份，谁在场谁就能点。
    """
    sent = 0
    for ws in list(_ws_clients.get("all", [])):
        if ws is exclude:
            continue
        try:
            if _ws_session_of(ws) != session_key:
                continue
            await ws.send_json(data)
            sent += 1
        except Exception:
            pass
    return sent


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
# 应用内升级（v1.11.1）：上传商业扩展包 + 粘贴许可证 → 就地激活，免重启
# ═══════════════════════════════════════════════════════════

#: _attach_extensions 里第一条 403 降级路由在 app.router.routes 中的下标。
#: 热激活时新特性的真实路由要插到它**前面** —— 路由按注册顺序匹配，
#: 追加到末尾的话永远先命中降级路由，用户会看到"已激活却仍提示升级"。
_LOCKED_ROUTES_AT: dict[str, int] = {"index": -1}

#: 特性按这个顺序 attach（与启动时一致）
_ATTACHABLE_FEATURES: tuple[str, ...] = (
    "scheduler", "advanced_stats", "session_pool",
    "custom_templates", "audit_export",
    "sso_ldap", "rbac", "model_gateway",
    "experts_pro", "expert_approval", "observability",
    "workflow_triggers", "workflow_history", "workflow_governance",
    "workflow_from_trace", "workflow_templates",
)


def _hot_attach(added: list[str]) -> dict:
    """把运行中新激活的特性挂上路由、启动后台循环；返回是否仍需重启。"""
    routes = app.router.routes
    at = _LOCKED_ROUTES_AT["index"]
    if at < 0 or at > len(routes):
        return {"restart_required": bool(added), "attached": []}
    ctx = _build_server_ctx()
    before = len(routes)
    attached: list[str] = []
    failed: list[str] = []
    for name in _ATTACHABLE_FEATURES:
        if name not in added:
            continue
        feature = _edition.get_feature(name)
        if feature is None or not hasattr(feature, "attach"):
            continue
        try:
            feature.attach(ctx)
            attached.append(name)
        except Exception as e:
            failed.append(name)
            logger.warning("edition_hot_attach_failed", feature=name, error=str(e))
    new_routes = routes[before:]
    if new_routes:
        del routes[before:]
        routes[at:at] = new_routes
        _LOCKED_ROUTES_AT["index"] = at + len(new_routes)
    # 后台循环：启动时由 lifespan 拉起，热激活时这里补上
    for name in ("scheduler", "workflow_triggers"):
        if name in added:
            feat = _edition.get_feature(name)
            if feat is not None and hasattr(feat, "start"):
                try:
                    feat.start()
                except Exception as e:
                    failed.append(name)
                    logger.warning("edition_hot_start_failed", feature=name, error=str(e))
    return {"restart_required": bool(failed), "attached": attached, "failed": failed}


@app.get("/api/license")
async def api_license_status():
    """升级面板：当前版本 / 扩展包是否就绪 / 许可证状态 / 卡在哪一步。"""
    return _edition.upgrade_status()


@app.post("/api/license/activate")
async def api_license_activate(payload: dict):
    """粘贴许可证 → 校验 → 落盘 → 就地激活（新特性路由热挂载，免重启）。"""
    r = await asyncio.to_thread(_edition.activate_license, str(payload.get("license") or ""))
    if not r.get("ok"):
        return JSONResponse(r, status_code=400)
    r.update(_hot_attach(r.get("added") or []))
    if r.get("lost"):
        # 换成了更低档的许可证：已挂上的路由摘不掉，重启后才完全一致
        r["restart_required"] = True
    await _broadcast_edition_change()
    return r


@app.post("/api/license/package")
async def api_license_package(request: Request):
    """上传升级文件（请求体即文件内容）—— 免 pip。

    v1.11.7：除了 automind-pro 的 .whl，还接受发行方生成的**一键升级包** .ampkg
    （扩展包 + 许可证打在一个文件里），一次完成"装包 + 激活"。
    """
    data = await request.body()
    r = await asyncio.to_thread(_edition.install_upgrade, data)
    if not r.get("ok"):
        return JSONResponse(r, status_code=400)
    if r.get("added"):
        r.update(_hot_attach(r["added"]) | {"restart_required": r.get("restart_required", False)})
    if r.get("lost"):
        r["restart_required"] = True
    if r.get("added") or r.get("bundle"):
        await _broadcast_edition_change()
    return r


@app.get("/api/browser/status")
async def api_browser_status():
    """浏览器自动化环境自检（v1.11.1，右栏「浏览器」空状态里的"检测"按钮）。

    会真的拉起一次无头浏览器再关掉（约 1~3 秒），所以只在用户点击时调用。
    """
    try:
        from automind.tools.browser import browser_status
    except Exception as e:                    # 打包缺依赖时也要给一句人话
        return {"ready": False, "source": "", "sdk": False, "detail": f"浏览器模块不可用：{e}"}
    return await browser_status()


async def _broadcast_edition_change() -> None:
    """通知所有打开的标签页：版本变了，重新拉一次 /api/status。"""
    try:
        await _broadcast({"type": "edition_changed", "edition": _edition.get_edition()})
    except Exception as e:                    # pragma: no cover - 纯通知
        logger.warning("edition_broadcast_failed", error=str(e))


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
        # 契约 v1 追加键（对既有扩展向后兼容）：
        "register_token_validator": _register_token_validator,  # SSO 会话令牌
        "rebuild_agent": _rebuild_agent,                        # 网关切换即时生效
        # v1.9.0 追加（工作流无人值守，专业版 workflow_triggers / workflow_history）：
        "workflow_catalog": lambda: _workflow_catalog(
            _workflow_loader(get_agent().tool_registry.list_names())),
        "workflow_store": _wf_store,
        "start_workflow": _launch_workflow,
        "resume_workflow": _resume_workflow,
        "workflow_running": _wf_running,
        # v1.10.0 追加（工作流治理 / 轨迹转工作流 / 模板库）：
        "request_identity": _request_identity,
        "workflow_authorize": _wf_authorize,
        "workflow_can": _wf_can,
        "workflow_policy": _wf_policy,
        "workflow_loader": lambda: _workflow_loader(get_agent().tool_registry.list_names()),
        "workflow_dir": _workflow_dir,
        "save_workflow_file": _save_workflow_file,
        "data_dir": lambda: __import__("automind.core.paths", fromlist=["data_dir"]).data_dir(),
    }


def _attach_extensions() -> None:
    """激活商业扩展并注册其路由；随后注册社区版降级路由。

    路由匹配按注册顺序：专业版真实路由先注册故优先；降级路由仅在
    对应特性缺失时兜底命中，返回 403 + 升级提示。
    """
    _edition.load_extensions()
    ctx = _build_server_ctx()
    for name in _ATTACHABLE_FEATURES:
        feature = _edition.get_feature(name)
        if feature is not None and hasattr(feature, "attach"):
            try:
                feature.attach(ctx)
            except Exception as e:
                logger.warning("edition_attach_failed", feature=name, error=str(e))
    # 记下降级路由的起点：之后热激活的真实路由插到这里之前（见 _hot_attach）
    _LOCKED_ROUTES_AT["index"] = len(app.router.routes)

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
    # 自定义模板（专业版）
    for method, path in (("GET", "/api/templates/custom"),
                         ("POST", "/api/templates/custom"),
                         ("DELETE", "/api/templates/custom/{sid}")):
        app.add_api_route(path, _locked("custom_templates"), methods=[method])
    # 审计报告导出（专业版）
    app.add_api_route("/api/audit/export", _locked("audit_export"), methods=["GET"])
    # 工作流触发器 / 入站 webhook / 运行看板（专业版，v1.9.0）。社区版的自动化
    # 方式是"外部 cron / CI 调 POST /api/workflow/run"，这几个入口返回 403 + 升级提示
    app.add_api_route("/api/workflow/triggers", _locked("workflow_triggers"), methods=["GET"])
    for path in ("/api/workflow/triggers/{sid}/toggle", "/api/workflow/triggers/{sid}/token",
                 "/api/workflow/triggers/{sid}/run", "/api/workflow/hooks/{sid}"):
        app.add_api_route(path, _locked("workflow_triggers"), methods=["POST"])
    app.add_api_route("/api/workflow/dashboard", _locked("workflow_history"), methods=["GET"])
    # 工作流治理（企业版，v1.10.0）：发布审批 / 凭据 / 并发配额 / 运行审计导出
    for method, path in (("GET", "/api/workflow/governance"),
                         ("POST", "/api/workflow/governance/{sid}/submit"),
                         ("POST", "/api/workflow/governance/{sid}/approve"),
                         ("POST", "/api/workflow/governance/{sid}/reject"),
                         ("GET", "/api/workflow/secrets"), ("POST", "/api/workflow/secrets"),
                         ("DELETE", "/api/workflow/secrets/{sid}"),
                         ("GET", "/api/workflow/concurrency"), ("POST", "/api/workflow/concurrency"),
                         ("GET", "/api/workflow/runs/{sid}/audit")):
        app.add_api_route(path, _locked("workflow_governance"), methods=[method])
    # 从执行轨迹生成工作流 / 工作流模板库（专业版，v1.10.0）
    app.add_api_route("/api/workflow/from-trace/candidates", _locked("workflow_from_trace"),
                      methods=["GET"])
    app.add_api_route("/api/workflow/from-trace", _locked("workflow_from_trace"), methods=["POST"])
    for method, path in (("GET", "/api/workflow/templates"), ("POST", "/api/workflow/templates"),
                         ("DELETE", "/api/workflow/templates/{sid}"),
                         ("POST", "/api/workflow/templates/{sid}/use"),
                         ("GET", "/api/workflow/templates/export"),
                         ("POST", "/api/workflow/templates/import")):
        app.add_api_route(path, _locked("workflow_templates"), methods=[method])
    # 观测中心历史/看板（专业版）—— 社区版仅开放下方 /api/observe/dag
    for path in ("/api/observe/runs", "/api/observe/dashboard"):
        app.add_api_route(path, _locked("observability"), methods=["GET"])
    # SSO / LDAP（企业版）
    for method, path in (("POST", "/api/auth/login"), ("GET", "/api/auth/session"),
                         ("POST", "/api/auth/logout")):
        app.add_api_route(path, _locked("sso_ldap"), methods=[method])
    # 细粒度权限 RBAC（企业版）
    for method, path in (("GET", "/api/rbac"), ("POST", "/api/rbac/role"),
                         ("DELETE", "/api/rbac/role/{sid}"),
                         ("GET", "/api/rbac/check")):
        app.add_api_route(path, _locked("rbac"), methods=[method])
    # 私有模型网关（企业版）
    for method, path in (("GET", "/api/gateway"), ("POST", "/api/gateway"),
                         ("GET", "/api/gateway/check")):
        app.add_api_route(path, _locked("model_gateway"), methods=[method])
    # 专家进阶（专业版）
    for method, path in (("GET", "/api/experts/export"),
                         ("POST", "/api/experts/import"),
                         ("GET", "/api/experts/stats"),
                         ("POST", "/api/experts/{sid}/share")):
        app.add_api_route(path, _locked("experts_pro"), methods=[method])
    # 企业专家市场审批（企业版）
    for method, path in (("GET", "/api/experts/pending"),
                         ("POST", "/api/experts/{sid}/approve")):
        app.add_api_route(path, _locked("expert_approval"), methods=[method])
    # 知识库进阶（专业版）：多库管理 / 手动重嵌入 / 后端与定时设置
    for method, path in (("POST", "/api/kb/kbs"),
                         ("DELETE", "/api/kb/kbs/{sid}"),
                         ("POST", "/api/kb/reembed"),
                         ("POST", "/api/kb/settings")):
        app.add_api_route(path, _locked("rag_pro"), methods=[method])
    # 语义缓存（专业版）
    for method, path in (("GET", "/api/cache"), ("POST", "/api/cache/config"),
                         ("DELETE", "/api/cache")):
        app.add_api_route(path, _locked("semantic_cache"), methods=[method])
    # 模型智能路由（专业版）
    for method, path in (("GET", "/api/router"), ("POST", "/api/router"),
                         ("POST", "/api/router/preview")):
        app.add_api_route(path, _locked("model_router"), methods=[method])
    # 成本仪表盘（企业版）
    app.add_api_route("/api/costs", _locked("cost_dashboard"), methods=["GET"])
    # 知识库企业版：热度统计 / 检索审计 / 目录批量导入
    for method, path in (("GET", "/api/kb/stats"),
                         ("GET", "/api/kb/search-log"),
                         ("POST", "/api/kb/import-dir")):
        app.add_api_route(path, _locked("kb_enterprise"), methods=[method])

    logger.info("edition_ready", edition=_edition.get_edition(),
                features=[k for k, v in _edition.feature_flags().items() if v])


_attach_extensions()


# ═══════════════════════════════════════════════════════════
# OpenAI 兼容 API — IDE 集成（Continue.dev / Cline / 任意 OpenAI 客户端）
#   POST /v1/chat/completions（支持 SSE 流式） · GET /v1/models
#   鉴权与 /api/* 一致（AUTOMIND_AUTH_TOKEN / SSO 会话令牌）。
# ═══════════════════════════════════════════════════════════


def _openai_error(msg: str, status: int = 400,
                  etype: str = "invalid_request_error") -> JSONResponse:
    """OpenAI 错误格式（客户端据此展示可读错误）。"""
    return JSONResponse({"error": {"message": msg, "type": etype, "code": status}},
                        status_code=status)


@app.get("/v1/models")
async def v1_models():
    """OpenAI 兼容：模型列表（返回当前配置的模型）。"""
    agent = get_agent()
    mid = agent.config.llm.model if agent else "automind"
    return {"object": "list", "data": [
        {"id": mid, "object": "model",
         "created": int(_START_TIME), "owned_by": "automind"},
    ]}


@app.post("/v1/chat/completions")
async def v1_chat_completions(data: dict):
    """OpenAI 兼容：对话补全（IDE 面板经由此端点把 AutoMind 当模型用）。

    走当前配置的 LLM（含中转代理 / 企业网关收敛后的地址）；
    不执行工具（IDE 集成为纯对话语义），token 计入右栏用量统计。
    """
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return _openai_error("'messages' is required and must be a non-empty array")
    agent = get_agent()
    if agent.llm is None:
        return _openai_error(
            "LLM not initialized — open the AutoMind workbench and configure "
            "an API key first (右上角 🔑 API Keys)", 503, "server_error")

    # 消息清洗：只保留 role/content 文本（忽略客户端附加字段与多模态分片）
    msgs = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content", "")
        if isinstance(content, list):  # OpenAI 多模态分片 → 拼接文本部分
            content = "".join(p.get("text", "") for p in content
                              if isinstance(p, dict) and p.get("type") == "text")
        msgs.append({"role": m.get("role", "user"), "content": str(content)})

    model_name = agent.config.llm.model
    rid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    if bool(data.get("stream")):
        from fastapi.responses import StreamingResponse

        async def _sse():
            chunks: list[str] = []
            head = {"id": rid, "object": "chat.completion.chunk",
                    "created": created, "model": model_name,
                    "choices": [{"index": 0, "delta": {"role": "assistant"},
                                 "finish_reason": None}]}
            yield f"data: {json.dumps(head, ensure_ascii=False)}\n\n"
            try:
                async for delta in agent.llm.generate_stream(msgs):
                    if not isinstance(delta, str) or not delta:
                        continue
                    chunks.append(delta)
                    body = {"id": rid, "object": "chat.completion.chunk",
                            "created": created, "model": model_name,
                            "choices": [{"index": 0, "delta": {"content": delta},
                                         "finish_reason": None}]}
                    yield f"data: {json.dumps(body, ensure_ascii=False)}\n\n"
            except Exception as e:  # 流中断：以最后一个块传递错误说明
                err = {"id": rid, "object": "chat.completion.chunk",
                       "created": created, "model": model_name,
                       "choices": [{"index": 0,
                                    "delta": {"content": f"\n[AutoMind error: {e}]"},
                                    "finish_reason": None}]}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            tail = {"id": rid, "object": "chat.completion.chunk",
                    "created": created, "model": model_name,
                    "choices": [{"index": 0, "delta": {},
                                 "finish_reason": "stop"}]}
            yield f"data: {json.dumps(tail, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            # 流式用量估算计入统计（与对话模式一致的估算口径）
            try:
                reply = "".join(chunks)
                prompt_text = "".join(str(m.get("content", "")) for m in msgs)
                p_tokens = agent.llm.token_count(prompt_text)
                c_tokens = agent.llm.token_count(reply)
                # v1.11.10：IDE 补全也是一次真实 LLM 调用、也占一份上下文窗口。
                # 流式没有 usage 回包，只能按估算值计 —— 缓存命中不可见，记 0。
                _accumulate_tokens({
                    "prompt_tokens": p_tokens,
                    "completion_tokens": c_tokens,
                    "tokens": p_tokens + c_tokens,
                    "input_tokens": p_tokens,
                    "llm_calls": 1,
                    "peak_prompt_tokens": p_tokens,
                })
            except Exception:
                pass

        return StreamingResponse(_sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    try:
        resp = await agent.llm.generate(msgs)
    except Exception as e:
        return _openai_error(f"upstream LLM error: {e}", 502, "server_error")
    # 非流式响应自带 usage：缓存命中与总输入口径由 LLMResponse 归一化后给出
    _accumulate_tokens(_usage_record_fields(resp))
    return {
        "id": rid, "object": "chat.completion", "created": created,
        "model": model_name,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": resp.text},
                     "finish_reason": resp.finish_reason or "stop"}],
        "usage": {"prompt_tokens": resp.prompt_tokens,
                  "completion_tokens": resp.completion_tokens,
                  "total_tokens": resp.total_tokens},
    }


@app.get("/api/integrations/continue")
async def api_integration_continue(request: Request):
    """生成 Continue.dev（VS Code / JetBrains）即贴即用的接入配置。

    这个端点会把**明文访问令牌**写进返回的 YAML 里（Continue.dev 需要它）。
    因此按"泄密端点"对待（v1.4.5）：
      · 未配置令牌时，只接受本机请求 —— 否则局域网内任何人都能拿它探明
        服务地址、模型名与"当前未开启鉴权"这一事实；
      · 响应标注 no-store，不进任何缓存/代理；
      · 令牌只对本机或已鉴权的调用者给出。
    """
    if not _auth_token() and not _is_local_request(request):
        return JSONResponse(
            {"error": "该接口仅限本机访问；如需远程使用请先配置 auth_token。"},
            status_code=403)
    base = str(request.base_url).rstrip("/")
    agent = get_agent()
    model = agent.config.llm.model if agent else "automind"
    token = _auth_token()
    key_line = f"    apiKey: {token}" if token else "    apiKey: none  # 未开启鉴权可任意填"
    yaml_cfg = (
        "models:\n"
        f"  - name: AutoMind ({model})\n"
        "    provider: openai\n"
        f"    model: {model}\n"
        f"    apiBase: {base}/v1\n"
        f"{key_line}\n"
        "    roles:\n      - chat\n      - edit\n      - apply\n"
    )
    return JSONResponse(
        {"base_url": f"{base}/v1", "model": model,
         "auth_required": bool(token), "yaml": yaml_cfg},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@app.get("/manual")
async def manual_page():
    """内置使用手册（static/manual.html，界面右上角 📖 入口）。"""
    html_path = STATIC_DIR / "manual.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return JSONResponse({"error": "手册文件缺失"}, status_code=404)


def _missing_dist_assets(html: str) -> list[str]:
    """返回 dist 首页引用但磁盘上不存在的构建产物（正常为空列表）。"""
    missing = []
    for ref in _dist_asset_refs(html):
        # "/static/dist/assets/x.js" → STATIC_DIR / "dist/assets/x.js"
        rel = ref[len("/static/"):]
        if not (STATIC_DIR / rel).exists():
            missing.append(ref)
    return missing


def _classic_index() -> HTMLResponse:
    """经典原生 JS 界面（无构建步骤、无 ES 模块）—— 全链路的最后兜底。"""
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        html = html_path.read_text(encoding="utf-8")
        # 版本化静态资源 URL → 版本升级即自动 cache-bust（server_web.py）
        return HTMLResponse(_versioned_html(html, __version__))
    return HTMLResponse(_fallback_html())


@app.get("/")
async def index():
    # v1.0 React 前端（Vite 构建产物，资源带内容哈希无需手动 cache-bust）；
    # 未构建 / 产物不完整时回退到经典原生 JS 界面，功能一致。
    dist_index = STATIC_DIR / "dist" / "index.html"
    if dist_index.exists():
        try:
            html = dist_index.read_text(encoding="utf-8")
        except OSError as e:
            logger.error(f"读取 React 前端产物失败（{e}）→ 回退经典界面")
        else:
            # 完整性守卫：index.html 与 assets 来自不同次构建时，引用的哈希
            # 文件会 404 让用户看到"界面未能加载"。此处提前发现并回退到
            # 必定可用的经典界面，用户始终有可用界面而不是错误页。
            missing = _missing_dist_assets(html)
            if not missing:
                return HTMLResponse(html)
            logger.error(f"React 前端产物不完整，缺失 {missing} → 回退经典界面")
    return _classic_index()


@app.get("/legacy")
async def legacy_index():
    """经典原生 JS 界面的固定入口。

    供前端启动兜底使用：内核过旧（不支持 ES 模块）或 React 产物加载失败时，
    错误页提供"打开兼容版界面"一键切换到这里 —— 无构建产物依赖，功能一致。
    """
    return _classic_index()


def _fallback_html() -> str:
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>AutoMind</title></head><body style="font-family:sans-serif;max-width:800px;margin:50px auto">
<h1>AutoMind Agent</h1><p>前端文件未找到。请确保 static/index.html 存在。</p>
<p>启动: <code>python -m automind.server --port 8765</code></p></body></html>"""


def _bootstrap_auth(host: str) -> str:
    """按绑定地址决定鉴权策略；返回要展示给用户的提示行。

    v1.4.5：此前 AUTOMIND_AUTH_TOKEN 默认空 = 完全不鉴权，而 --host 0.0.0.0
    又是一句话的事 —— 一旦这么起，局域网内任何人都能直接驱动这台机器上的 Agent
    （跑命令、读写文件）。默认值不该让"图省事"直接等于"裸奔"。

    现在：
      · 绑本机（默认）—— 不强制令牌，只提示一句，保持零摩擦；
      · 绑非本机且未配令牌 —— **自动生成并持久化一个随机令牌**，醒目打印出来。
        宁可让用户多复制一次令牌，也不要静默地把机器暴露出去。
    """
    loopback = host in ("127.0.0.1", "::1", "localhost")
    if _auth_token():
        return "  🔐 鉴权: 已启用（请求需携带访问令牌）"
    if loopback:
        return "  🔓 鉴权: 未启用（仅监听本机，外部无法访问）"

    import secrets
    token = secrets.token_urlsafe(24)
    global _AUTH_TOKEN
    _AUTH_TOKEN = token
    try:
        cfg = _read_config()
        cfg["auth_token"] = token
        _write_config(cfg)
        saved = "已保存到配置文件，下次启动继续有效"
    except Exception as e:
        saved = f"保存配置失败（{e}），本次运行有效"
    return ("  ⚠️  检测到绑定了非本机地址且未设置访问令牌。\n"
            "  为避免局域网内任何人都能直接操作这台机器，已自动生成访问令牌：\n"
            f"\n      {token}\n\n"
            f"  （{saved}；也可用 AUTOMIND_AUTH_TOKEN 环境变量自行指定）\n"
            "  浏览器访问时在地址后加 ?token=<令牌>，API 调用用 Authorization: Bearer <令牌>")


def _probe_port(host: str, port: int) -> dict | None:
    """端口空闲返回 None；被占用则返回占用者信息。

    只做一次真实 bind —— `connect` 探测在 Windows 上会漏判（防火墙静默丢包），
    而"能不能绑上"才是启动会不会失败的唯一判据。
    """
    import errno
    import socket

    probe_host = host or "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        try:
            sk.bind((probe_host, port))
            return None
        except OSError as e:
            # 只认"地址已被占用"。绑不上还有别的原因（IPv6 地址、本机没有该 IP、
            # 特权端口……），那些一律交回 uvicorn 报它自己的错，别误报成端口冲突
            occupied = (e.errno in (errno.EADDRINUSE, errno.EACCES)
                        or getattr(e, "winerror", 0) in (10013, 10048))
            if not occupied:
                return None

    info: dict[str, Any] = {"host": probe_host, "port": port,
                            "version": "", "frozen": False}
    # 占用者是不是另一个 AutoMind？知道了才能把话说准
    reach = "127.0.0.1" if probe_host in ("0.0.0.0", "::", "") else probe_host
    try:
        import urllib.request
        # 本机地址必须绕开系统代理，否则代理会把 127.0.0.1 也接管掉
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{reach}:{port}/api/health", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        if isinstance(data, dict) and data.get("status") == "ok":
            info["version"] = str(data.get("version") or "")
            info["frozen"] = bool((data.get("paths") or {}).get("frozen"))
    except Exception:
        pass
    return info


def _port_conflict_message(info: dict) -> str:
    """端口冲突的中文说明 —— 重点是回答"那我浏览器里看到的到底是谁"。"""
    port, reach = info["port"], info["host"]
    if reach in ("0.0.0.0", "::", ""):
        reach = "127.0.0.1"
    url = f"http://{reach}:{port}"
    lines = [f"\n✗ 端口 {port} 已被占用，本次启动已中止。\n"]
    if info["version"]:
        kind = "桌面版安装包" if info["frozen"] else "另一个 AutoMind 实例"
        lines.append(f"  占用者：AutoMind v{info['version']}（{kind}）")
        if info["version"] != __version__:
            # 这正是"改完代码重启，浏览器里版本却没变"的真凶：新进程压根没起来，
            # 浏览器打开的仍是那个一直在跑的旧实例
            lines.append(
                f"  ⚠ 现在用浏览器打开 {url} 看到的是它 —— v{info['version']}，"
                f"而不是你刚启动的 v{__version__}。")
    else:
        lines.append("  占用者：未知程序（该端口上的服务不响应 AutoMind 健康检查）")
    lines.append("\n  解决办法（任选其一）：")
    if info["version"] and info["frozen"]:
        lines.append("    · 退出已在运行的 AutoMind 桌面版（关闭其窗口，"
                     "或在任务管理器中结束 AutoMind.exe），再重新启动；")
    else:
        lines.append("    · 关闭占用该端口的程序后重新启动；")
    lines.append(f"    · 换一个端口：python -m automind.server --port {port + 1}")
    return "\n".join(lines)


def main():
    import argparse

    # 中文 Windows 控制台默认 GBK：横幅里的 ╔ ║ 一写就 UnicodeEncodeError，
    # 进程直接崩在第一行。launch.bat 的 chcp 65001 只救了那一个入口，
    # 照 README 直接敲 `python -m automind.server` 的用户仍会撞上。
    from automind.core.console import enable_utf8_console
    enable_utf8_console()

    parser = argparse.ArgumentParser(description="AutoMind Web Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    # 端口预检必须在横幅之前。此前的顺序是"先打横幅再交给 uvicorn"：端口被占
    # 时 uvicorn 只在日志里留一行英文 bind 错误就**静默退出（返回码 0）**，
    # 而屏幕上最后停留的是"打开浏览器访问 http://127.0.0.1:8765"。用户照做，
    # 打开的其实是那个一直在跑的旧实例 —— 于是得出"代码改了、版本没更新"。
    conflict = _probe_port(args.host, args.port)
    if conflict:
        print(_port_conflict_message(conflict), file=sys.stderr, flush=True)
        sys.exit(1)

    auth_note = _bootstrap_auth(args.host)
    # v1.11.9：启动时依赖快检 —— 不阻断启动（缺库不应让用户连界面都打不开），
    # 只在横幅下打一行可操作的提示。检测只定位模块、不执行包代码，秒级完成。
    try:
        from automind.core.dep_preflight import missing_items
        _gaps = missing_items()
        _dep_note = ("" if not _gaps else
                     f"  ⚠ 依赖自检：{len(_gaps)} 项未就绪"
                     f"（运行 automind doctor 查看详情、--fix 一键补齐）\n")
    except Exception:                         # pragma: no cover - 预检自身绝不拦启动
        _dep_note = ""
    print(f"""
╔══════════════════════════════════════════════════╗
║         AutoMind Web UI v{__version__:<24}║
║                                                  ║
║  打开浏览器访问: http://{args.host}:{args.port}              ║
║  API 文档:      http://{args.host}:{args.port}/docs         ║
║                                                  ║
║  按 Ctrl+C 停止服务器                             ║
╚══════════════════════════════════════════════════╝
{auth_note}
{_dep_note}""", flush=True)   # 不 flush 的话横幅会被 uvicorn 的日志插到后面去

    import uvicorn
    started = time.time()
    uvicorn.run("automind.server:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")
    # uvicorn 绑定失败时不抛异常、不设返回码，只是立刻返回。兜住这种情况，
    # 别让脚本/任务调度以为服务起来了。
    if time.time() - started < 2.0:
        print(f"\n✗ 服务器启动后立即退出（未能监听 {args.host}:{args.port}）。"
              f"\n  请查看上方日志；如提示地址已被使用，换端口："
              f"python -m automind.server --port {args.port + 1}",
              file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
