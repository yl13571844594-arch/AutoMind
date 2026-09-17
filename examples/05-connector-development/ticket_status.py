"""示例连接器：查询客户内部工单系统（可直接复制到 ~/.automind/connectors/ 使用）。

## 这个文件演示什么

把一段"客户内部 HTTP API 调用"变成模型可以自己调用的工具 —— **不改
AutoMind 的任何源码**。它同时演示了四件每个真实连接器都会遇到的事：

1. ``name`` / ``description`` / ``parameters`` 怎么写，模型才用得对；
2. ``permission_tier`` 怎么选（这条决定它会不会弹出人工审批）；
3. 返回值约定（``ToolResult`` + ``output`` 里放什么，模型才能接着往下做）；
4. 配置与凭证从环境变量读，缺了怎么报一个**用户能照做**的错。

## 怎么用（三步）

::

    # 1) 复制到连接器目录（Windows 下就是 %USERPROFILE%\\.automind\\connectors\\）
    mkdir -p ~/.automind/connectors
    cp ticket_status.py ~/.automind/connectors/

    # 2) 告诉它工单系统在哪（凭证永远别写进代码）
    setx TICKET_API_BASE "https://tickets.internal.example.com/api/v1"
    setx TICKET_API_TOKEN "改成你们系统的令牌"

    # 3) 重启 AutoMind，或在界面上点一次「重载工具」

之后就可以直接对模型说"帮我看下工单 TK-1024 的状态"，它会在需要时调用
``ticket_status``。**不需要修改 automind/agent.py，也不需要写 MCP server。**

只想先在本机试通、不想连真系统：把 ``TICKET_API_BASE`` 指向任何返回 JSON
的地址即可（例如 ``python -m http.server`` 加一个 json 文件），或者直接跑
同目录下的 ``selfcheck.py``。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool

# ── 配置：一律从环境变量读 ───────────────────────────────────
#
# 为什么不写死在代码里、也不读 AutoMind 的配置文件：
# 连接器是**独立分发**的（客户拿走一个 .py 文件就能用），它不该要求
# 调用方先改 automind 的配置结构；而凭证写在源码里迟早会被提交进仓库。
# 想改成读别的来源（Vault、Windows 凭据管理器）就改 _config()。

_ENV_BASE = "TICKET_API_BASE"
_ENV_TOKEN = "TICKET_API_TOKEN"
_ENV_TIMEOUT = "TICKET_API_TIMEOUT"


class _NotConfigured(RuntimeError):
    """缺配置 —— 消息里直接给出"该设哪个环境变量、怎么设"。

    这是连接器最常见的失败：代码没问题、网络没问题，只是没人告诉它地址。
    报一句 ``KeyError: 'TICKET_API_BASE'`` 对用户毫无帮助；这里把**下一步
    动作**写进错误文本，模型会把它原样转述给用户，用户照着做即可。
    """


def _config() -> tuple[str, str, float]:
    """读运行期配置；缺关键项时抛 _NotConfigured。

    路径里的 ``TICKET_API_TIMEOUT`` 有默认值：超时不该是必填项，
    但**必须有**（否则模型可能被一个挂住的内部接口拖到任务超时）。
    """
    base = (os.environ.get(_ENV_BASE) or "").strip().rstrip("/")
    token = (os.environ.get(_ENV_TOKEN) or "").strip()
    if not base:
        raise _NotConfigured(
            f"连接器还没配好：请先设置环境变量 {_ENV_BASE}，"
            f"例如 {_ENV_BASE}=https://tickets.internal.example.com/api/v1 。"
            f"设完重启 AutoMind（或在界面上重载工具）即可生效。")
    if not base.startswith(("http://", "https://")):
        raise _NotConfigured(
            f"{_ENV_BASE} 必须带上协议头（http:// 或 https://），当前是 {base!r}。")
    try:
        timeout = float(os.environ.get(_ENV_TIMEOUT) or 15)
    except ValueError:
        timeout = 15.0
    return base, token, max(1.0, min(timeout, 120.0))


def _request(path: str, params: dict[str, str], body: dict[str, Any] | None = None) -> Any:
    """发一次请求并解析 JSON；把网络层的异常翻成一句人话。

    ``urlopen`` 是**阻塞**调用，所以调用方把它放进线程里跑（见 ``execute``）——
    直接在协程里调用会把整个进程的事件循环卡住（其它会话、心跳、进度推送
    全都一起冻住），这不是"慢一点"，是"整个服务停摆"。
    """
    base, token, timeout = _config()
    url = f"{base}{path}"
    if params:
        url = f"{url}?{urlencode({k: v for k, v in params.items() if v not in (None, '')})}"
    headers = {"Accept": "application/json", "User-Agent": "AutoMind-Connector/1.0"}
    if token:
        # 认证方式随各家系统不同：Bearer / X-Api-Key / 查询串签名……改成你们的那种。
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"

    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        # 4xx/5xx 是**业务上可解释**的（工单不存在、令牌过期），要单独报，
        # 别和"连不上"混成一句，否则用户不知道该改令牌还是该找网管。
        detail = e.read().decode("utf-8", errors="replace")[:300] if e.fp else ""
        raise RuntimeError(
            f"工单系统返回 HTTP {e.code}（{url}）。"
            f"{'服务端说明：' + detail if detail else ''}"
            f"{'（401/403 通常是令牌不对或过期）' if e.code in (401, 403) else ''}") from e
    except URLError as e:
        raise RuntimeError(
            f"连不上工单系统（{url}）：{e.reason}。"
            f"请确认 {_ENV_BASE} 是否正确、本机能否访问该地址。") from e

    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError as e:
        # 返回的不是 JSON（多半是被网关/登录页拦了）—— 把开头一段带出去，
        # 这比"解析失败"有用得多：用户一眼就能看出自己拿到的是个 HTML 登录页。
        raise RuntimeError(
            f"工单系统返回的不是 JSON（{url}）：{raw[:200]!r}") from e


class TicketStatusTool(AbstractTool):
    """查询/列出工单。"""

    # `name` 是模型调用时用的唯一标识，也是它唯一能看到的线索。
    # 两点经验：
    #   · 用**动词_名词**的小写+下划线（与内置 32 个工具同一风格）；
    #   · 名字里带上用户会说出口的词（"工单"→ 英文 ticket）。
    #     AutoMind 的 ReAct 循环按"工具名是否出现在任务文本里"挑选要下发
    #     哪些工具的完整 schema（默认一轮只发 14 个），名字取得贴切，
    #     模型第一次就会拿到它；取得玄乎（比如 crm_do）就只能靠模型
    #     在推理里点名才会被补发。
    name = "ticket_status"

    # `description` 是给**模型**看的说明书，不是给人看的简介。写清三件事：
    # 它做什么、什么场景该用、有什么副作用。中文/英文都行，跟着你的用户走。
    description = (
        "查询客户内部工单系统：按工单号取详情，或用关键词/状态列出工单，"
        "也可以给某个工单追加一条备注。"
        "用户提到\"工单\"\"问题单\"\"TK-数字\"时用它。"
        "action=comment 会真的写入工单系统，属于对外发声，请先向用户确认内容。"
    )

    # `parameters` 是 JSON Schema：模型的**参数名与类型**完全由它决定。
    #   · 只有写进 properties 的字段模型才敢传，写进 required 的才会必填；
    #   · description 会被模型逐字读到 —— 枚举值务必在 description 里列全
    #     （"action 只能是 get/list/comment 之一"），否则它会自己发明一个；
    #   · 类型别写太花：string/number/boolean/object/array 就够，
    #     联合类型、anyOf 这类各家模型的函数调用支持度参差不齐。
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "操作类型，只能是 get（按号取详情）、"
                               "list（按条件列出）、comment（追加备注）之一。默认 get。",
            },
            "ticket_id": {
                "type": "string",
                "description": "工单号，例如 TK-1024。action=get/comment 时必填。",
            },
            "query": {"type": "string", "description": "关键词，action=list 时可用。"},
            "status": {
                "type": "string",
                "description": "按状态过滤（open / pending / closed），action=list 时可用。",
            },
            "limit": {"type": "number", "description": "action=list 返回条数上限，默认 20。"},
            "comment": {"type": "string", "description": "备注正文，action=comment 时必填。"},
        },
        "required": ["action"],
    }

    # `permission_tier` 决定**要不要人工审批**（AutoMind 的权限引擎按档位决策）：
    #   SAFE      只读、可重复执行，通常直接放行（本工具的 get/list 就是这类）
    #   SENSITIVE 会改变本机或外部状态，多半要用户点确认（写文件、发请求）
    #   DANGEROUS 不可逆/对外发声（删数据、发邮件、给客户回消息）
    #
    # 档位是**整个工具**一个，不能按 action 分开声明。所以有写操作的连接器
    # 只能整体报高：本工具会改工单，故取 SENSITIVE。
    # 千万**别**为了"少弹窗"把写操作报成 SAFE —— 那等于让模型可以不经确认
    # 去改客户的系统，而且界面上看不出任何异常。
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 40                       # 0-100，仅用于风险排序/展示

    async def execute(self, **kwargs: Any) -> ToolResult:
        """执行入口 —— **必须 async**，且**永远返回 ToolResult，不抛异常**。

        两条约定都是硬性的：

        * ``async``：注册表用 ``await tool.execute(**kwargs)`` 调用它；
        * 返回而不抛：抛出去虽然也会被注册表兜住转成失败结果，但**当前位置**
          能给出更准确的说明（比如"你少了 ticket_id"），而兜底那层只能看到
          一个异常文本。错误信息越靠近出错的地方越准确。
        """
        action = str(kwargs.get("action") or "get").strip().lower()
        try:
            if action == "get":
                return await self._get(kwargs)
            if action == "list":
                return await self._list(kwargs)
            if action == "comment":
                return await self._comment(kwargs)
            return self._fail(
                f"action 只支持 get/list/comment，收到的是 {action!r}。",
                hint="请改用这三个值之一重试。")
        except _NotConfigured as e:
            # 配置缺失是**环境问题**，不是模型调用错了 —— 直接把可照做的
            # 指引给它，模型会转述给用户。
            return self._fail(str(e), not_configured=True)
        except Exception as e:                            # noqa: BLE001 - 见下
            # 连接器里"兜住一切"是**有意**的：任何异常逸出都只会退化成一句
            # 与上下文无关的堆栈。这里统一翻成模型能读懂的失败结果，并把
            # 异常类型保留下来，便于用户在日志里对上号。
            return self._fail(f"{type(e).__name__}: {e}")

    # ── 三个动作 ─────────────────────────────────────────────

    async def _get(self, kwargs: dict[str, Any]) -> ToolResult:
        ticket_id = str(kwargs.get("ticket_id") or "").strip()
        if not ticket_id:
            return self._fail("action=get 需要 ticket_id（例如 TK-1024）。")
        data = await asyncio.to_thread(_request, f"/tickets/{ticket_id}", {})
        return ToolResult(
            tool_name=self.name, success=True,
            # output 放**结构化**结果：模型据此继续推理（比如再问一句"要催吗"），
            # 上层也能直接拿去做展示，不必再去解析一段自然语言。
            # 键名用英文短词，值保持原始类型（别统统 str()）。
            output={
                "ticket_id": data.get("id", ticket_id),
                "title": data.get("title", ""),
                "state": data.get("status", "unknown"),
                "assignee": data.get("assignee", ""),
                "updated_at": data.get("updated_at", ""),
                "url": data.get("url", ""),
            },
            metadata={"source": "ticket_api", "action": "get"},
        )

    async def _list(self, kwargs: dict[str, Any]) -> ToolResult:
        try:
            limit = int(kwargs.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        params = {
            "q": str(kwargs.get("query") or ""),
            "status": str(kwargs.get("status") or ""),
            "limit": str(max(1, min(limit, 100))),
        }
        data = await asyncio.to_thread(_request, "/tickets", params)
        items = data.get("items", data) if isinstance(data, dict) else data
        rows = [
            {"ticket_id": it.get("id", ""), "title": it.get("title", ""),
             "state": it.get("status", ""), "assignee": it.get("assignee", "")}
            for it in (items or []) if isinstance(it, dict)
        ]
        return ToolResult(
            tool_name=self.name, success=True,
            output={"count": len(rows), "tickets": rows,
                    "filter": {k: v for k, v in params.items() if v}},
            metadata={"source": "ticket_api", "action": "list"},
        )

    async def _comment(self, kwargs: dict[str, Any]) -> ToolResult:
        ticket_id = str(kwargs.get("ticket_id") or "").strip()
        comment = str(kwargs.get("comment") or "").strip()
        if not ticket_id or not comment:
            return self._fail("action=comment 需要同时提供 ticket_id 与 comment。")
        data = await asyncio.to_thread(
            _request, f"/tickets/{ticket_id}/comments", {}, {"body": comment})
        return ToolResult(
            tool_name=self.name, success=True,
            output={"ticket_id": ticket_id, "comment_id": data.get("id", ""),
                    "posted": True, "url": data.get("url", "")},
            metadata={"source": "ticket_api", "action": "comment"},
        )

    # ── 失败结果 ─────────────────────────────────────────────

    def _fail(self, message: str, **output: Any) -> ToolResult:
        """统一的失败结果：``success=False`` + 一句能据以改正的 error。

        ⚠️ 常见坑：忘了 ``success=False``（默认是 False，但很多人会写成
        ``success=True`` 顺手带一句错误文本）。那样模型会以为调用成功了、
        拿着空 output 继续往下做，整条链路会跑出一个"看起来完成了"的错答案 ——
        比直接失败难查十倍。
        """
        return ToolResult(tool_name=self.name, success=False, error=message,
                          output=output or None)


# ── 想用 httpx + 内网地址？两处改法 ───────────────────────────
#
# 1) 换成异步客户端：把 ``_request`` 换成 ``httpx.AsyncClient`` 并把
#    ``asyncio.to_thread(...)`` 去掉（少了线程开销，也不再需要 _request 里
#    那套阻塞说明）。需要 ``pip install httpx``，记得用
#    ``automind.tools._toolkit.need("httpx")`` 取依赖，缺库时它会给出
#    可照抄的安装命令。
#
# 2) 内网地址的放行：AutoMind 的 ``_toolkit.check_url`` **默认拒绝私网与
#    回环地址**（防 SSRF：模型可能被网页内容诱导去请求本机或云元数据），
#    用 ``check_url(url, allow_private=True)`` 显式放行，换取"仍然拦住
#    169.254.169.254 这类云凭据地址"。
#
# 完整写法见 docs/CONNECTORS.md 的「需要网络/凭证时怎么办」一节。
