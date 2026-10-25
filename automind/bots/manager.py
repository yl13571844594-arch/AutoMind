"""机器人管理与执行（v1.11.6）：IM 消息 → 任务 → 过程与结果回传到同一个聊天窗口。

流程::

    平台回调 ─► handle_hook ─► 渠道验签 / 解析 ─► 立即应答平台（几秒内必须应答）
                                     │
                                     └─► 后台 process：指令（/stop、同意……）或任务
                                              │
                         run_task（= /api/run）◄─┘  事件流 ─► ProgressRelay ─► 节流后发回聊天
                                              │
                                   审批请求 ─► 发到聊天，回复「同意 xxxx」放行

设计取舍：

* **一个聊天 = 一个会话**：同一个群 / 单聊里的消息共享上下文（会话 id 由机器人 id + 聊天 id
  派生），``/new`` 开新会话；
* **同一聊天同时只跑一个任务**：任务进行中再来消息会提示"可发 /stop 停止"，
  而回复「同意 / 拒绝」这类审批指令不受影响；
* **审批在聊天里完成**：危险操作的批准请求发到聊天，限时等待，超时按拒绝处理（fail-closed）；
* **过程节流**：计划、审批、失败立即发；普通工具调用按间隔合并成一条，避免刷屏和触发平台限流；
* **白名单**：配置了 ``allow_users`` 时只有名单里的人能下达任务。
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from automind.bots.channels import (
    CHANNELS,
    ChannelError,
    HttpReply,
    HttpReq,
    Incoming,
    get_channel,
)
from automind.bots.md_format import split_message
from automind.core.logging import get_logger

logger = get_logger("automind.bots")

MODES = {"chat": "对话", "work": "工作", "coding": "编程", "multi": "多智能体", "loop": "循环工程"}
_MODE_ALIASES = {"对话": "chat", "聊天": "chat", "工作": "work", "编程": "coding", "代码": "coding",
                 "coding": "coding", "code": "coding", "chat": "chat", "work": "work",
                 "多智能体": "multi", "multi": "multi", "循环": "loop", "loop": "loop"}
PROGRESS_LEVELS = ("brief", "detailed", "off")
#: 设置里可改的字段（其余如 id / hook_token / created 由服务端维护）
EDITABLE = ("name", "channel", "enabled", "mode", "progress", "progress_interval",
            "approval_timeout", "allow_users", "credentials", "public_base_url", "transport")
_LOG_MAX = 80

Sender = Callable[[Incoming, str, str], Awaitable[None]]


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _md_list(lines: list[str]) -> str:
    """把过程行整理成 Markdown 列表项（各平台语法降级交给 md_format 适配层）。"""
    out: list[str] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        out.append(text if text.startswith(("- ", "• ", "> ")) else f"- {text}")
    return "\n".join(out)


class _ChatState:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.started = 0.0
        self.text = ""
        self.generation = 0
        self.mode = ""
        self.approvals: dict[str, dict[str, Any]] = {}


class ProgressRelay:
    """把 Agent 事件流翻译成聊天里的几条过程消息（节流 + 合并）。"""

    def __init__(self, send: Callable[[str, str], Awaitable[None]], level: str,
                 interval: float) -> None:
        self.send = send
        self.level = level if level in PROGRESS_LEVELS else "brief"
        self.interval = max(3.0, float(interval or 10))
        self.lines: list[str] = []
        self.goals: dict[str, tuple[int, str]] = {}
        self.tool_counts: dict[str, int] = {}
        self.tool_fail = 0
        self.steps_done = 0
        self.steps_total = 0
        self._last = time.monotonic()
        self._stop = asyncio.Event()
        self._ticker: asyncio.Task | None = None
        self.workspace: dict[str, Any] | None = None

    def start(self) -> None:
        if self.level != "off":
            self._ticker = asyncio.create_task(self._tick())

    async def close(self) -> None:
        self._stop.set()
        if self._ticker is not None:
            self._ticker.cancel()
            try:
                await self._ticker
            except (asyncio.CancelledError, Exception):
                pass

    async def _tick(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass
            if not self._stop.is_set():
                await self.flush()

    async def flush(self, title: str = "⏳ 执行中") -> None:
        if self.level == "off":
            self.lines.clear()
            return
        lines = list(self.lines)
        self.lines.clear()
        if self.level == "brief" and self.tool_counts:
            tools = "、".join(f"{k}×{v}" for k, v in sorted(self.tool_counts.items(), key=lambda x: -x[1])[:6])
            lines.append(f"🛠 已调用工具：{tools}" + (f"（失败 {self.tool_fail} 次）" if self.tool_fail else ""))
            self.tool_counts.clear()
            self.tool_fail = 0
        if not lines:
            return
        if self.steps_total:
            title += f"（{self.steps_done}/{self.steps_total}）"
        self._last = time.monotonic()
        await self.send(_md_list(lines), title)

    async def on_event(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        try:
            if t == "plan_created":
                steps = ev.get("steps") or []
                self.steps_total = len(steps)
                rows = []
                for i, s in enumerate(steps, 1):
                    desc = str(s.get("description") or "")[:80]
                    self.goals[str(s.get("goal_id") or i)] = (i, desc)
                    rows.append(f"{i}. {desc}")
                if self.level != "off":
                    await self.send("\n".join(rows[:20]) + (f"\n…共 {len(rows)} 步" if len(rows) > 20 else ""),
                                    f"📋 已生成计划（{len(rows)} 步）")
            elif t == "plan_step_start" and self.level == "detailed":
                i, desc = self.goals.get(str(ev.get("goal_id")), (0, ""))
                if i:
                    self.lines.append(f"▶ 第 {i} 步：{desc}")
            elif t == "plan_step_end":
                i, desc = self.goals.get(str(ev.get("goal_id")), (0, ""))
                if ev.get("success"):
                    self.steps_done += 1
                    if i and self.level == "detailed":
                        self.lines.append(f"✓ 第 {i} 步完成")
                elif i:
                    self.lines.append(f"✗ 第 {i} 步失败：{str(ev.get('error') or '')[:120]}")
            elif t == "step_action":
                tool = str(ev.get("tool") or "")
                ok = bool(ev.get("success"))
                if self.level == "detailed":
                    args = ev.get("args") or {}
                    brief = str(args.get("command") or args.get("path") or args.get("query")
                                or args.get("url") or "")[:80]
                    self.lines.append(f"{'🛠' if ok else '⚠'} {tool}" + (f"：{brief}" if brief else "")
                                      + ("" if ok else " ✗"))
                else:
                    self.tool_counts[tool] = self.tool_counts.get(tool, 0) + 1
                    self.tool_fail += 0 if ok else 1
            elif t == "tool_error":
                net = "（网络中断，正在等待恢复后重试）" if ev.get("network") else ""
                self.lines.append(f"⚠ 工具失败：{ev.get('tool')}{net}：{str(ev.get('error') or '')[:150]}")
                if ev.get("circuit_open") or ev.get("network"):
                    await self.flush()
            elif t == "workspace_result":
                self.workspace = ev
            elif t == "loop_iter":
                self.lines.append(f"🔁 第 {ev.get('iter')}/{ev.get('max')} 轮")
            elif t == "ma_plan":
                roles = [f"- **{p.get('role')}**：{str(p.get('subtask') or '')[:40]}"
                         for p in ev.get("plan") or []]
                if roles:
                    await self.send("\n".join(roles), "👥 多智能体分工")
            if len(self.lines) >= 12:
                await self.flush()
        except Exception as e:                       # 过程回传失败不能影响任务本身
            logger.warning("bot_relay_failed", event=t, error=f"{type(e).__name__}: {e}")


class BotManager:
    def __init__(self, *, read_cfg: Callable[[], dict], write_cfg: Callable[[dict], None],
                 run_task: Callable[[dict], Awaitable[Any]], run_hooks: dict[str, dict],
                 apply_workspace: Callable[[str], Awaitable[dict]] | None = None) -> None:
        self._read = read_cfg
        self._write = write_cfg
        self._run_task = run_task
        self._run_hooks = run_hooks
        self._apply_workspace = apply_workspace
        self._chats: dict[str, _ChatState] = {}
        self._logs: dict[str, deque] = {}
        self._waiters: dict[str, list[asyncio.Future]] = {}
        from automind.bots.longconn import LongConnRunner
        #: v1.11.7 长连接（飞书 / 钉钉 Stream / 企业微信智能机器人）：本机主动连平台，不需要公网
        self.longconn = LongConnRunner(self._on_stream_message)

    # ── 长连接 ───────────────────────────────────────────────

    def start(self) -> None:
        """服务启动时调用：按配置建立全部长连接。"""
        self.longconn.sync(self._all())

    async def stop(self) -> None:
        await self.longconn.stop()

    def sync_connections(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return                      # 不在事件循环里（如单元测试直接调用）：启动时再统一建立
        self.longconn.sync(self._all())

    def reconnect(self, bot_id: str) -> bool:
        bot = self.get(bot_id)
        if bot is None:
            return False
        self.longconn.restart(bot)
        return True

    async def _on_stream_message(self, bot: dict[str, Any], msg: Incoming) -> None:
        # 以最新配置为准（连接建立后改过授权名单 / 模式也立即生效）
        current = self.get(bot["id"]) or bot
        if not current.get("enabled", True):
            return
        await self.process(current, msg, self._channel_sender(current))

    # ── 配置 ─────────────────────────────────────────────────

    def _all(self) -> list[dict[str, Any]]:
        bots = self._read().get("bots")
        return bots if isinstance(bots, list) else []

    def _save(self, bots: list[dict[str, Any]]) -> None:
        cfg = self._read()
        cfg["bots"] = bots
        self._write(cfg)

    def get(self, bot_id: str) -> dict[str, Any] | None:
        return next((b for b in self._all() if b.get("id") == bot_id), None)

    @staticmethod
    def masked(bot: dict[str, Any]) -> dict[str, Any]:
        ch = CHANNELS.get(str(bot.get("channel")))
        secret_keys = {f[0] for f in ch.fields if f[2]} if ch else set()
        creds = {k: (("••••" + str(v)[-4:]) if k in secret_keys and v else v)
                 for k, v in (bot.get("credentials") or {}).items()}
        transport = bot.get("transport") or (ch.transports[0] if ch else "webhook")
        out = {**bot, "credentials": creds, "transport": transport,
               "missing": ch.missing(bot.get("credentials") or {}, transport) if ch else []}
        if transport == "stream":
            from automind.bots.longconn import status
            out["conn"] = status(str(bot.get("id")))
        return out

    def list(self) -> list[dict[str, Any]]:
        out = []
        for b in self._all():
            m = self.masked(b)
            running = [k for k, st in self._chats.items()
                       if k.startswith(b["id"] + ":") and st.task and not st.task.done()]
            m["running"] = len(running)
            out.append(m)
        return out

    def _normalize(self, data: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        bot = dict(base)
        for k in EDITABLE:
            if k in data:
                bot[k] = data[k]
        if bot.get("channel") not in CHANNELS:
            raise ChannelError(f"不支持的渠道：{bot.get('channel')}")
        bot["name"] = str(bot.get("name") or CHANNELS[bot["channel"]].label + "机器人")[:40]
        bot["enabled"] = bool(bot.get("enabled", True))
        mode = str(bot.get("mode") or "work")
        bot["mode"] = mode if mode in MODES else "work"
        bot["progress"] = bot.get("progress") if bot.get("progress") in PROGRESS_LEVELS else "brief"
        bot["progress_interval"] = min(120, max(3, int(bot.get("progress_interval") or 10)))
        bot["approval_timeout"] = min(1800, max(30, int(bot.get("approval_timeout") or 300)))
        users = bot.get("allow_users") or []
        if isinstance(users, str):
            users = [u.strip() for u in users.replace("，", ",").replace("\n", ",").split(",")]
        bot["allow_users"] = [str(u).strip() for u in users if str(u).strip()][:200]
        bot["public_base_url"] = str(bot.get("public_base_url") or "").strip().rstrip("/")
        transports = CHANNELS[bot["channel"]].transports
        bot["transport"] = bot.get("transport") if bot.get("transport") in transports else transports[0]
        # 敏感字段回传的是打码值（••••xxxx）时保留原值，避免"打开设置点保存"把密钥冲掉
        old = base.get("credentials") or {}
        new = bot.get("credentials") or {}
        creds = {}
        for f in CHANNELS[bot["channel"]].fields:
            v = new.get(f[0], old.get(f[0], ""))
            if isinstance(v, str) and v.startswith("••••"):
                v = old.get(f[0], "")
            creds[f[0]] = str(v or "").strip()
        bot["credentials"] = creds
        return bot

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        bots = self._all()
        if len(bots) >= 20:
            raise ChannelError("机器人数量已达上限（20）")
        bot = self._normalize(data, {"id": uuid.uuid4().hex[:10],
                                     "hook_token": secrets.token_urlsafe(18),
                                     "created": time.time()})
        bots.append(bot)
        self._save(bots)
        logger.info("bot_created", bot=bot["id"], channel=bot["channel"])
        self.sync_connections()
        return self.masked(bot)

    def update(self, bot_id: str, data: dict[str, Any]) -> dict[str, Any]:
        bots = self._all()
        for i, b in enumerate(bots):
            if b.get("id") == bot_id:
                nb = self._normalize(data, b)
                if data.get("rotate_token"):
                    nb["hook_token"] = secrets.token_urlsafe(18)
                bots[i] = nb
                self._save(bots)
                self.sync_connections()
                return self.masked(nb)
        raise ChannelError("机器人不存在", status=404)

    def delete(self, bot_id: str) -> bool:
        bots = self._all()
        keep = [b for b in bots if b.get("id") != bot_id]
        if len(keep) == len(bots):
            return False
        self._save(keep)
        for key, st in list(self._chats.items()):
            if key.startswith(bot_id + ":") and st.task and not st.task.done():
                st.task.cancel()
        self._logs.pop(bot_id, None)
        self.sync_connections()
        return True

    # ── 日志（设置页里的"最近消息"）──────────────────────────

    def _log(self, bot_id: str, direction: str, chat: str, text: str, title: str = "",
             user: str = "") -> None:
        q = self._logs.setdefault(bot_id, deque(maxlen=_LOG_MAX))
        q.append({"seq": (q[-1]["seq"] + 1) if q else 1, "at": _now(), "ts": time.time(),
                  "dir": direction, "chat": chat, "user": user, "title": title, "text": text[:4000]})
        for fut in self._waiters.pop(bot_id, []):
            if not fut.done():
                fut.set_result(True)

    def log(self, bot_id: str, after: int = 0) -> list[dict[str, Any]]:
        return [e for e in self._logs.get(bot_id, []) if e["seq"] > after]

    # ── 入口 ─────────────────────────────────────────────────

    async def handle_hook(self, bot_id: str, token: str, req: HttpReq) -> HttpReply:
        bot = self.get(bot_id)
        if bot is None or not secrets.compare_digest(str(bot.get("hook_token") or ""), token or ""):
            return HttpReply(status=404, body={"error": "not found"})
        ch = get_channel(bot["channel"])
        try:
            reply, msg = await ch.handle(bot, req)
        except ChannelError as e:
            logger.warning("bot_hook_rejected", bot=bot_id, channel=bot["channel"], error=str(e))
            self._log(bot_id, "err", "", f"回调被拒：{e}")
            return HttpReply(status=e.status, body={"error": str(e)})
        except Exception as e:
            logger.warning("bot_hook_failed", bot=bot_id, error=f"{type(e).__name__}: {e}")
            self._log(bot_id, "err", "", f"回调处理失败：{type(e).__name__}: {e}")
            return HttpReply(status=400, body={"error": f"{type(e).__name__}: {e}"})
        if msg is None or not msg.text:
            return reply
        if not bot.get("enabled", True):
            return reply
        sender = self._channel_sender(bot)
        if msg.reply.get("wait"):
            # 通用 Webhook 的同步模式：等这条消息处理完，把全部回复放进响应
            replies: list[dict[str, Any]] = []

            async def capture(m: Incoming, text: str, title: str) -> None:
                replies.append({"title": title, "text": text})
                await sender(m, text, title)
            task = await self.process(bot, msg, capture)
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=600)
                except TimeoutError:
                    replies.append({"title": "⏳", "text": "任务仍在执行，结果稍后推送"})
            return HttpReply(body={"ok": True, "replies": replies})
        await self.process(bot, msg, sender)
        return reply

    def _channel_sender(self, bot: dict[str, Any]) -> Sender:
        ch = get_channel(bot["channel"])

        async def send(msg: Incoming, text: str, title: str = "") -> None:
            # 标题在部分渠道会占正文额度（企微 / 公众号把标题放正文首行），先预留再分片
            reserved = len(title.encode("utf-8")) + 40 if title else 0
            limit = max(500, ch.limit_bytes - reserved)
            chunks = split_message(text, limit, channel=bot["channel"], marker=False)
            total = len(chunks)
            for i, part in enumerate(chunks[:6]):
                if i == 0:
                    part_title = title
                else:
                    part_title = (f"{title}（续 {i + 1}/{total}）" if title
                                  else f"（续 {i + 1}/{total}）")
                try:
                    await ch.send(bot, msg, part, part_title)
                except Exception as e:
                    logger.warning("bot_send_failed", bot=bot["id"], channel=bot["channel"],
                                   error=f"{type(e).__name__}: {e}")
                    self._log(bot["id"], "err", msg.chat_id, f"发送失败：{e}")
                    return
            if total > 6:                        # 与旧版一致：最多连发 6 条，其余记日志
                logger.warning("bot_message_truncated", bot=bot["id"], channel=bot["channel"],
                               chunks=total, sent=6)
        return send

    async def simulate(self, bot_id: str, text: str, chat_id: str = "console",
                       user_id: str = "console") -> dict[str, Any]:
        """设置页「测试对话」：模拟一条来自聊天的消息，回复只进日志、不发到平台。"""
        bot = self.get(bot_id)
        if bot is None:
            raise ChannelError("机器人不存在", status=404)

        async def console(m: Incoming, t: str, title: str = "") -> None:
            return None
        msg = Incoming(chat_id=chat_id, user_id=user_id, user_name="测试", text=text.strip())
        await self.process(bot, msg, console, check_allow=False)
        return {"ok": True}

    async def wait_log(self, bot_id: str, after: int, timeout: float = 25.0) -> list[dict[str, Any]]:
        """长轮询：有新日志立刻返回，否则最多等 timeout 秒。"""
        got = self.log(bot_id, after)
        if got:
            return got
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._waiters.setdefault(bot_id, []).append(fut)
        try:
            await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            pass
        return self.log(bot_id, after)

    # ── 处理一条消息 ─────────────────────────────────────────

    def _key(self, bot: dict[str, Any], msg: Incoming) -> str:
        return f"{bot['id']}:{msg.chat_id}"

    def _session(self, bot: dict[str, Any], msg: Incoming, st: _ChatState) -> str:
        h = hashlib.sha1(f"{bot['id']}|{msg.chat_id}".encode()).hexdigest()[:10]
        return f"bot-{bot['id']}-{h}" + (f"-{st.generation}" if st.generation else "")

    async def process(self, bot: dict[str, Any], msg: Incoming, sender: Sender, *,
                      check_allow: bool = True) -> asyncio.Task | None:
        """处理一条消息；是任务则返回后台任务（调用方可选择等待）。"""
        key = self._key(bot, msg)
        st = self._chats.setdefault(key, _ChatState())
        self._log(bot["id"], "in", msg.chat_id, msg.text, user=msg.user_name or msg.user_id)

        async def reply(text: str, title: str = "") -> None:
            self._log(bot["id"], "out", msg.chat_id, text, title)
            await sender(msg, text, title)

        allow = bot.get("allow_users") or []
        if check_allow and allow and msg.user_id not in allow and msg.user_name not in allow:
            await reply(f"你（{msg.user_id}）不在这个机器人的授权名单里，无法下达任务。", "🔒 未授权")
            return None
        text = msg.text.strip()
        cmd, _, arg = text.partition(" ")
        cmd_l = cmd.lower().lstrip("/／")
        if cmd_l in ("help", "帮助", "?", "？"):
            await reply(self._help(bot), "🤖 使用说明")
            return None
        if cmd_l in ("同意", "批准", "approve", "yes", "拒绝", "reject", "no", "deny"):
            await self._answer_approval(st, cmd_l in ("同意", "批准", "approve", "yes"), arg.strip(), reply)
            return None
        if cmd_l in ("stop", "停止", "取消", "cancel"):
            if st.task and not st.task.done():
                st.task.cancel()
                await reply("已停止当前任务。", "⏹ 已停止")
            else:
                await reply("当前没有正在执行的任务。")
            return None
        if cmd_l in ("status", "状态"):
            if st.task and not st.task.done():
                await reply(f"- **正在执行**：{st.text[:80]}\n"
                            f"- **已用时**：{time.time() - st.started:.0f} 秒", "⏳ 执行中")
            else:
                await reply(f"空闲。当前模式：{MODES.get(st.mode or bot['mode'], bot['mode'])}")
            return None
        if cmd_l in ("new", "reset", "新对话", "新会话"):
            st.generation += 1
            await reply("已开始新的对话，之前的上下文不再带入。", "🆕 新对话")
            return None
        if cmd_l in ("mode", "模式"):
            m = _MODE_ALIASES.get(arg.strip().lower())
            if not m:
                await reply("用法：/mode 对话 | 工作 | 编程 | 多智能体 | 循环")
            else:
                st.mode = m
                await reply(f"本聊天之后的任务用「{MODES[m]}」模式执行。")
            return None
        if cmd_l in ("apply", "应用", "应用到项目"):
            await self._apply(bot, msg, st, reply)
            return None
        if st.task and not st.task.done():
            await reply(f"上一个任务还在执行（{st.text[:40]}…）。发送 /stop 可停止，或等它完成后再发。", "⏳ 请稍候")
            return None
        st.text = text
        st.started = time.time()
        st.task = asyncio.create_task(self._run(bot, msg, st, reply))
        return st.task

    def _help(self, bot: dict[str, Any]) -> str:
        return ("直接发消息即可下达任务，我会把执行过程和结果发回这里。\n\n"
                f"- **当前模式**：{MODES.get(bot['mode'], bot['mode'])}\n"
                "- **指令**：\n"
                "  - /stop 停止当前任务\n"
                "  - /status 查看状态\n"
                "  - /new 开始新对话\n"
                "  - /mode 对话|工作|编程 切换模式\n"
                "  - /apply 把隔离副本的产物应用到项目\n"
                "  - 同意 / 拒绝 [编号] 回复操作审批")

    async def _answer_approval(self, st: _ChatState, ok: bool, aid: str, reply) -> None:
        pending = {k: v for k, v in st.approvals.items() if not v["future"].done()}
        if not pending:
            await reply("当前没有等待审批的操作。")
            return
        if aid and aid in pending:
            target = pending[aid]
        elif len(pending) == 1:
            target = next(iter(pending.values()))
        else:
            await reply("有多个待审批操作，请带上编号回复，如：同意 " + next(iter(pending)))
            return
        target["future"].set_result(ok)
        await reply(("✅ 已批准" if ok else "⛔ 已拒绝") + f"：{target['tool']}")

    async def _apply(self, bot, msg, st, reply) -> None:
        if self._apply_workspace is None:
            await reply("当前版本不支持从聊天应用产物，请在 Web 界面里点「应用到项目」。")
            return
        if st.task and not st.task.done():
            await reply("任务仍在执行，结束后再应用。")
            return
        try:
            r = await self._apply_workspace(self._session(bot, msg, st))
        except Exception as e:
            await reply(f"应用失败：{e}", "⚠ 应用失败")
            return
        if not r.get("ok"):
            await reply(str(r.get("error") or "这个对话没有隔离副本产物"), "ℹ 无可应用产物")
            return
        files = r.get("applied") or []
        lines = [f"已按相对路径应用 {len(files)} 个文件到 {r.get('project')}"]
        lines += [f"- {f}" for f in files[:20]]
        if r.get("backup_dir"):
            lines.append(f"被覆盖的原文件已备份到 {r['backup_dir']}")
        await reply("\n".join(lines), "⤵ 已应用到项目")

    async def _run(self, bot: dict[str, Any], msg: Incoming, st: _ChatState, reply) -> None:
        sid = self._session(bot, msg, st)
        mode = st.mode or bot.get("mode") or "work"
        relay = ProgressRelay(reply, str(bot.get("progress") or "brief"),
                              float(bot.get("progress_interval") or 10))

        async def approval(tool_name: str, args: dict, tier: Any, reason: str) -> dict[str, Any]:
            aid = secrets.token_hex(2)
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            st.approvals[aid] = {"future": fut, "tool": tool_name}
            shown = "\n".join(f"- {k}: {str(v)[:160]}" for k, v in list((args or {}).items())[:6])
            timeout = float(bot.get("approval_timeout") or 300)
            await relay.flush()
            await reply(f"**操作**：{tool_name}（{getattr(tier, 'value', tier)}）\n"
                        f"**原因**：{reason}\n\n{shown}\n\n"
                        f"回复「同意 {aid}」放行，「拒绝 {aid}」拦下"
                        f"（{int(timeout)} 秒内不回复按拒绝处理）",
                        "⚠ 需要你批准")
            try:
                ok = await asyncio.wait_for(fut, timeout=timeout)
            except TimeoutError:
                ok = False
                await reply(f"审批 {aid} 超时，已按拒绝处理。")
            finally:
                st.approvals.pop(aid, None)
            return {"approved": bool(ok), "arguments": None,
                    "comment": "聊天中批准" if ok else "聊天中拒绝或超时"}

        self._run_hooks[sid] = {"event_sink": relay.on_event, "approval": approval, "quiet": True}
        relay.start()
        t0 = time.time()
        await reply(f"**任务**：{msg.text[:200]}\n\n"
                    f"**模式**：{MODES.get(mode, mode)} · 发送 /stop 可停止", "🤖 收到，开始执行")
        try:
            res = await self._run_task({"task": msg.text, "session_id": sid,
                                        "overrides": {"mode": mode}})
            body = res
            if hasattr(res, "status_code"):
                import json as _json
                try:
                    body = _json.loads(bytes(res.body).decode("utf-8"))
                except Exception:
                    body = {"error": f"HTTP {res.status_code}"}
                if res.status_code >= 400:
                    raise RuntimeError(str(body.get("error") or body))
            await relay.close()
            await relay.flush()
            ok = bool(body.get("success", True))
            out = str(body.get("output") or "（没有输出）").strip()
            ws = relay.workspace
            if ws and ws.get("count"):
                out += (f"\n\n- 📁 **隔离副本产物**：新增 {len(ws.get('added') or [])}"
                        f" · 修改 {len(ws.get('modified') or [])}"
                        "\n- 回复 /apply 可按相对路径覆盖回项目根（覆盖前自动备份）")
            meta = [f"⏱ 用时 {time.time() - t0:.0f} 秒"]
            if body.get("steps"):
                meta.append(f"🧭 步数 {body['steps']}")
            if body.get("tokens"):
                meta.append(f"🔤 {body['tokens']} tokens")
            footer = "\n".join(f"- {m}" for m in meta)
            await reply(f"{out}\n\n{footer}", "✅ 任务完成" if ok else "⚠ 部分完成")
        except asyncio.CancelledError:
            await relay.close()
            await reply("任务已停止。", "⏹ 已停止")
            raise
        except Exception as e:
            await relay.close()
            await relay.flush()
            logger.warning("bot_task_failed", bot=bot["id"], error=f"{type(e).__name__}: {e}")
            await reply(f"{e}", "❌ 执行失败")
        finally:
            self._run_hooks.pop(sid, None)
            for a in st.approvals.values():
                if not a["future"].done():
                    a["future"].set_result(False)
            st.approvals.clear()
