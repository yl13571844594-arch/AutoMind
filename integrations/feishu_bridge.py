#!/usr/bin/env python3
"""飞书机器人 ⇄ AutoMind 桥接（长连接模式，免公网）。

作用：在飞书里给机器人发消息 → 调用本机 AutoMind 的 ``POST /api/run`` →
把结果发回飞书会话。整个链路的详细说明见 ``docs/FEISHU.md``。

为什么用长连接（WebSocket）而不是 HTTP 回调：长连接由本进程**主动外连**飞书，
因此不需要公网 IP、域名、HTTPS 证书，也不需要 ICP 备案 —— 这对跑在自己电脑/内网
上的 AutoMind 是唯一现实的选择。代价是进程必须一直活着。

飞书侧前置（缺一个都会表现为"机器人没反应"，见 docs/FEISHU.md §3.2）：
    1. 自建应用已添加「机器人」能力；
    2. 事件与回调 → 订阅方式 → 「使用长连接接收事件」；
    3. 已添加事件 ``im.message.receive_v1``；
    4. 权限已开（im:message / im:message:send_as_bot / 收消息相关）；
    5. **已创建版本并发布且管理员审核通过**。

运行：
    pip install lark-oapi requests

    set FEISHU_APP_ID=cli_xxxxxxxxxxxx
    set FEISHU_APP_SECRET=<app secret>
    set AUTOMIND_AUTH_TOKEN=<与 AutoMind 服务端一致>
    set FEISHU_ALLOWLIST=ou_你的open_id,oc_允许的群chat_id
    python integrations/feishu_bridge.py

安全取向（与 automind 仓库自身的风格一致，一律 fail-closed）：
    · 白名单为空时**拒绝启动**（除非显式 FEISHU_ALLOW_ALL=1）——
      "能跟机器人说话"等价于"能在这台机器上执行工具"，这是本路线最大的风险面；
    · 群聊里必须 @机器人才响应，避免群里任何人随口一句就跑任务；
    · 桥接进程只做出站连接，不监听任何公网端口，也不要给它做端口转发。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
        P2ImMessageReceiveV1,
    )
except ImportError:  # pragma: no cover - 缺依赖时给人话，而不是一堆 traceback
    print("缺少依赖：pip install lark-oapi", file=sys.stderr)
    raise SystemExit(2) from None

try:
    import requests
except ImportError:  # pragma: no cover
    print("缺少依赖：pip install requests", file=sys.stderr)
    raise SystemExit(2) from None

# Windows 控制台默认是 GBK，中文日志会直接抛 UnicodeEncodeError 把进程弄挂。
# 这跟"桥接挂了"长得一模一样，但原因完全无关 —— 提前收口掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")       # type: ignore[union-attr]
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()

#: AutoMind 服务地址。默认回环 —— 回环请求在本机被视作可信（server.py 的 _admin_ok），
#: 因此不需要 X-Admin-Token。**不要**把它指向别的机器。
AUTOMIND_BASE = os.environ.get("AUTOMIND_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
AUTOMIND_TOKEN = os.environ.get("AUTOMIND_AUTH_TOKEN", "").strip()

#: 允许驱动 Agent 的飞书 open_id / chat_id（逗号分隔）。空 = 拒绝启动（见模块 docstring）。
ALLOWLIST = {x.strip() for x in os.environ.get("FEISHU_ALLOWLIST", "").split(",") if x.strip()}
ALLOW_ALL = os.environ.get("FEISHU_ALLOW_ALL", "").strip().lower() in ("1", "true", "yes", "on")

#: 默认交互模式。刻意默认 chat（纯对话、不调工具）——
#: 因为 HTTP /api/run 路径没有审批通道，工作模式下的高危工具会被 fail-closed 拒绝，
#: 详见 docs/FEISHU.md §3.4。确认审批链路可用后再改成 work / coding。
DEFAULT_MODE = os.environ.get("FEISHU_DEFAULT_MODE", "chat").strip().lower()

#: 长任务先回一句"收到"，避免用户以为机器人死了。chat 模式很快，默认不发。
SEND_ACK = os.environ.get("FEISHU_ACK", "0").strip().lower() in ("1", "true", "yes", "on")

#: 单次 /api/run 的读取超时（秒）。工作/编程模式可能跑很久。
RUN_TIMEOUT = float(os.environ.get("FEISHU_RUN_TIMEOUT", "900"))
#: 并发执行的任务数上限（与 AutoMind 的 AUTOMIND_MAX_CONCURRENT 无关，这里只是别把线程打满）
MAX_WORKERS = int(os.environ.get("FEISHU_WORKERS", "2"))

#: 飞书单条文本消息的保守长度上限。超长内容分段发送。
CHUNK = 3000
MAX_CHUNKS = 5

#: 指令前缀 → 交互模式。取值见 automind/core/types.py 的 InteractionMode。
MODE_COMMANDS = {
    "/chat": "chat",
    "/work": "work",
    "/coding": "coding",
    "/loop": "loop",
    "/multi": "multi",
}

HELP_TEXT = (
    "AutoMind 机器人可用指令：\n"
    "  /chat <内容>    纯对话（默认，不调用工具）\n"
    "  /work <内容>    工作模式（完整 Agent 流程，会调用工具）\n"
    "  /coding <内容>  编程模式\n"
    "  /loop <内容>    循环工程模式\n"
    "  /multi <内容>   多智能体协同\n"
    "  /help           显示本说明\n"
    "直接发消息 = 当前默认模式。"
)


# ═══════════════════════════════════════════════════════════════
# 去重（飞书可能重投同一条事件）
# ═══════════════════════════════════════════════════════════════

_seen: OrderedDict[str, float] = OrderedDict()
_seen_lock = threading.Lock()
_SEEN_MAX = 2048


def already_handled(event_id: str) -> bool:
    """该事件是否处理过。True 表示应跳过。有界 LRU，不会无限增长。"""
    if not event_id:
        return False
    with _seen_lock:
        if event_id in _seen:
            return True
        _seen[event_id] = time.time()
        while len(_seen) > _SEEN_MAX:
            _seen.popitem(last=False)
    return False


# ═══════════════════════════════════════════════════════════════
# 飞书发送
# ═══════════════════════════════════════════════════════════════

_client: Any = None
_client_lock = threading.Lock()


def _get_client() -> Any:
    """惰性构造飞书 API 客户端。

    惰性而非模块级：没配 APP_ID 时也要能正常跑完参数校验并打印那句人话提示，
    而不是在 import 阶段就抛一个与真实原因无关的异常。
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = (lark.Client.builder()
                           .app_id(APP_ID).app_secret(APP_SECRET).build())
    return _client


def send_text(chat_id: str, text: str) -> None:
    """往会话发一条文本消息（超长自动分段）。

    刻意不用「回复某条消息」的 reply 接口：它在消息已被撤回/过期时会失败，
    而控制类机器人最不该做的就是因为"回不到那条消息"而整个结果发不出去。
    发到会话本身更稳。
    """
    chunks = [text[i:i + CHUNK] for i in range(0, max(len(text), 1), CHUNK)][:MAX_CHUNKS]
    if len(text) > CHUNK * MAX_CHUNKS:
        chunks.append("…（输出过长，已截断，完整内容见 AutoMind 界面）")
    for piece in chunks:
        body = (CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": piece}, ensure_ascii=False))
                .build())
        req = (CreateMessageRequest.builder()
               .receive_id_type("chat_id")
               .request_body(body)
               .build())
        try:
            resp = _get_client().im.v1.message.create(req)
        except Exception as e:  # 网络异常不该把工作线程弄死
            print(f"[feishu] 发送异常：{type(e).__name__}: {e}", file=sys.stderr)
            return
        if not getattr(resp, "success", lambda: False)():
            print(f"[feishu] 发送失败 code={getattr(resp, 'code', '?')} "
                  f"msg={getattr(resp, 'msg', '?')}", file=sys.stderr)
            return


# ═══════════════════════════════════════════════════════════════
# AutoMind 调用
# ═══════════════════════════════════════════════════════════════

def run_agent(task: str, interaction: str, chat_id: str) -> str:
    """调 ``POST /api/run`` 并返回可读文本。

    ``session_id`` 用飞书会话 id：同一个群/单聊天然是同一段多轮上下文
    （api_run 会用它取 _get_session_history）。
    """
    payload = {
        "task": task,
        "interaction": interaction,
        "session_id": f"feishu:{chat_id}",
    }
    headers = {"Content-Type": "application/json"}
    if AUTOMIND_TOKEN:
        headers["Authorization"] = f"Bearer {AUTOMIND_TOKEN}"

    try:
        resp = requests.post(f"{AUTOMIND_BASE}/api/run", json=payload,
                             headers=headers, timeout=(10, RUN_TIMEOUT))
    except requests.Timeout:
        return f"⏱ 任务超过 {RUN_TIMEOUT:.0f} 秒仍未返回。任务可能仍在后台执行，稍后用相同会话继续。"
    except requests.RequestException as e:
        return f"❌ 连不上 AutoMind（{AUTOMIND_BASE}）：{type(e).__name__}: {e}"

    try:
        data = resp.json()
    except ValueError:
        return f"❌ AutoMind 返回了非 JSON 内容（HTTP {resp.status_code}）：{resp.text[:300]}"

    if resp.status_code != 200 or "error" in data:
        detail = data.get("error") or data.get("detail") or resp.text[:300]
        if resp.status_code == 401:
            detail += "\n（提示：AUTOMIND_AUTH_TOKEN 与 AutoMind 服务端不一致）"
        return f"❌ 执行失败（HTTP {resp.status_code}）：{detail}"

    output = str(data.get("output") or "").strip() or "（任务完成，但没有文本输出）"
    if not data.get("success", True):
        output = "⚠️ 任务未完全成功：\n" + output
    meta = (f"\n\n———\n步骤 {data.get('steps', '?')} · "
            f"tokens {data.get('tokens', '?')} · "
            f"{data.get('duration_ms', '?')}ms · {interaction}")
    return output + meta


# ═══════════════════════════════════════════════════════════════
# 消息处理
# ═══════════════════════════════════════════════════════════════

_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS,
                           thread_name_prefix="automind-bridge")


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """防御性取属性：SDK 各版本字段可能缺省，缺了就当没有，而不是抛异常。"""
    try:
        value = getattr(obj, name, default)
    except Exception:
        return default
    return default if value is None else value


def _parse_text(content: str) -> str:
    try:
        data = json.loads(content or "{}")
    except ValueError:
        return ""
    return str(data.get("text") or "") if isinstance(data, dict) else ""


def _strip_mentions(text: str, mentions: Any) -> str:
    """去掉 @机器人 留下的占位符（形如 ``@_user_1``）。"""
    for mention in mentions or []:
        key = str(_attr(mention, "key", "") or "")
        if key:
            text = text.replace(key, "")
    return text.strip()


def _split_command(text: str) -> tuple[str, str]:
    """拆「模式前缀 + 正文」；无前缀则用默认模式。"""
    piece = text.strip()
    head = piece.split(maxsplit=1)[0].lower() if piece else ""
    if head in MODE_COMMANDS:
        return MODE_COMMANDS[head], piece[len(head):].strip()
    return DEFAULT_MODE, piece


def _allowed(chat_id: str, open_id: str) -> bool:
    if ALLOW_ALL:
        return True
    return bool(ALLOWLIST & {chat_id, open_id})


def handle_message(data: P2ImMessageReceiveV1) -> None:
    """长连接回调。**必须尽快返回** —— 它跑在 SDK 的接收线程上，阻塞会拖慢所有事件。"""
    event = _attr(data, "event")
    message = _attr(event, "message")
    if message is None:
        return

    event_id = str(_attr(_attr(data, "header"), "event_id", "") or "")
    if already_handled(event_id):
        return

    chat_id = str(_attr(message, "chat_id", "") or "")
    chat_type = str(_attr(message, "chat_type", "") or "")
    msg_type = str(_attr(message, "message_type", "") or "")
    mentions = _attr(message, "mentions", []) or []
    sender = _attr(_attr(_attr(event, "sender"), "sender_id"), "open_id", "")
    open_id = str(sender or "")

    # 群聊：必须 @机器人才响应。不判断的话，群里任何人闲聊都会触发任务。
    if chat_type != "p2p" and not mentions:
        return

    if not _allowed(chat_id, open_id):
        print(f"[feishu] 已拒绝未授权会话 chat={chat_id[:12]}… open={open_id[:12]}…",
              file=sys.stderr)
        send_text(chat_id, "⛔ 你不在允许名单内，无法驱动这台机器上的 AutoMind。")
        return

    if msg_type != "text":
        send_text(chat_id, f"暂只支持文本消息（收到的是 {msg_type}）。")
        return

    text = _strip_mentions(_parse_text(_attr(message, "content", "")), mentions)
    if not text:
        return

    if text.split(maxsplit=1)[0].lower() in ("/help", "help", "帮助"):
        send_text(chat_id, HELP_TEXT)
        return

    interaction, task = _split_command(text)
    if not task:
        send_text(chat_id, HELP_TEXT)
        return

    if SEND_ACK and interaction != "chat":
        send_text(chat_id, f"🤖 已收到，正在以「{interaction}」模式执行…")

    _pool.submit(_work, chat_id, task, interaction)


def _work(chat_id: str, task: str, interaction: str) -> None:
    """工作线程：跑任务并把结果发回去。异常一律转成给用户的说明。"""
    try:
        result = run_agent(task, interaction, chat_id)
    except Exception as e:  # 兜底：绝不让一条异常静默吞掉用户的请求
        result = f"❌ 桥接内部错误：{type(e).__name__}: {e}"
    send_text(chat_id, result)


# ═══════════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════════

def main() -> int:
    if not APP_ID or not APP_SECRET:
        print("请设置 FEISHU_APP_ID / FEISHU_APP_SECRET", file=sys.stderr)
        return 2
    if not ALLOWLIST and not ALLOW_ALL:
        print("拒绝启动：FEISHU_ALLOWLIST 为空。\n"
              "  能跟机器人说话 = 能在这台机器上执行工具，所以白名单不是可选项。\n"
              "  填法：FEISHU_ALLOWLIST=ou_xxx,oc_yyy（open_id / chat_id，逗号分隔）\n"
              "  确实要放开（自担风险）：FEISHU_ALLOW_ALL=1", file=sys.stderr)
        return 2
    if ALLOW_ALL:
        print("⚠️  FEISHU_ALLOW_ALL=1：任何能给机器人发消息的人都能驱动这台机器。",
              file=sys.stderr)

    # 事件分发：长连接模式下 encrypt_key / verification_token 传空串即可
    # （长连接走 SDK 内部可信分发，不经过公网回调的验签/解密路径）。
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(handle_message)
               .build())

    print(f"AutoMind 桥接已启动：{AUTOMIND_BASE} · 默认模式 {DEFAULT_MODE} · "
          f"白名单 {len(ALLOWLIST) or 'ALL'} 项", flush=True)
    ws_client = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler,
                               log_level=lark.LogLevel.INFO)
    ws_client.start()          # 阻塞，断线由 SDK 自动重连
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
