"""协同工具 —— 本地通知 / 日历 / 即时通讯集成。

  · notify —— 本机桌面通知（Windows toast / macOS osascript / Linux notify-send）。
    只在本机弹窗，不外发，故为 SAFE。
  · calendar —— ICS 文件读写（跨平台）+ Windows 下可选的 Outlook COM。
  · im_integration —— 通过群机器人 Webhook 发消息（钉钉/企业微信/飞书/Slack）。
    **外发不可撤回**，定档 DANGEROUS，逐条审批；且刻意不做"遍历群列表群发"。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from automind.core.logging import get_logger
from automind.core.types import PermissionTier, ToolResult
from automind.tools._toolkit import BlockedTarget, bad, check_url, err, need, ok
from automind.tools.base import AbstractTool

logger = get_logger("automind.tools.collab")


# ── notify ──────────────────────────────────────────────────

class NotifyTool(AbstractTool):
    """在本机弹出桌面通知。"""

    name = "notify"
    description = (
        "Show a desktop notification on the local machine (Windows toast / macOS "
        "Notification Center / Linux notify-send). Useful to tell the user a long "
        "task finished. Local only — nothing is sent over the network."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Notification title."},
            "message": {"type": "string", "description": "Body text."},
            "urgency": {"type": "string", "enum": ["low", "normal", "critical"]},
        },
        "required": ["title"],
    }
    permission_tier = PermissionTier.SAFE
    risk_score = 5

    async def execute(self, **kwargs: Any) -> ToolResult:
        title = str(kwargs.get("title", "")).strip() or "AutoMind"
        body = str(kwargs.get("message") or "")
        urgency = str(kwargs.get("urgency") or "normal")
        system = platform.system()
        try:
            if system == "Windows":
                return self._windows(title, body)
            if system == "Darwin":
                return self._macos(title, body)
            return self._linux(title, body, urgency)
        except Exception as e:
            return err(self.name, e)

    def _windows(self, title: str, body: str) -> ToolResult:
        # 走 PowerShell 的 WinRT toast；参数经 -EncodedCommand 传入，
        # 避免标题/正文里的引号被当成 PowerShell 语法（命令注入）。
        import base64
        ps = (
            '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,'
            ' ContentType = WindowsRuntime] | Out-Null;'
            '$t = [Windows.UI.Notifications.ToastNotificationManager]::'
            'GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);'
            '$n = $t.GetElementsByTagName("text");'
            f'$n.Item(0).AppendChild($t.CreateTextNode({self._ps_str(title)})) | Out-Null;'
            f'$n.Item(1).AppendChild($t.CreateTextNode({self._ps_str(body)})) | Out-Null;'
            '$toast = [Windows.UI.Notifications.ToastNotification]::new($t);'
            '[Windows.UI.Notifications.ToastNotificationManager]::'
            'CreateToastNotifier("AutoMind").Show($toast);'
        )
        enc = base64.b64encode(ps.encode("utf-16-le")).decode()
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-EncodedCommand", enc],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return bad(self.name, f"通知发送失败：{(r.stderr or '').strip()[:200]}")
        return ok(self.name, shown=True, platform="windows", title=title)

    @staticmethod
    def _ps_str(s: str) -> str:
        """PowerShell 单引号字符串字面量：内部单引号翻倍转义。"""
        return "'" + str(s).replace("'", "''") + "'"

    def _macos(self, title: str, body: str) -> ToolResult:
        # osascript 同理：用 argv 传参而非拼字符串
        script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
        r = subprocess.run(["osascript", "-e", script, title, body],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return bad(self.name, f"通知发送失败：{(r.stderr or '').strip()[:200]}")
        return ok(self.name, shown=True, platform="macos", title=title)

    def _linux(self, title: str, body: str, urgency: str) -> ToolResult:
        if not shutil.which("notify-send"):
            return bad(self.name,
                       "未找到 notify-send。请安装：Debian/Ubuntu `apt install libnotify-bin`，"
                       "Fedora `dnf install libnotify`。")
        r = subprocess.run(["notify-send", "-u", urgency, "-a", "AutoMind", title, body],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return bad(self.name, f"通知发送失败：{(r.stderr or '').strip()[:200]}")
        return ok(self.name, shown=True, platform="linux", title=title)


# ── calendar ────────────────────────────────────────────────

class CalendarTool(AbstractTool):
    """本地日历：ICS 文件读写；Windows 下可读写 Outlook。"""

    name = "calendar"
    description = (
        "Manage local calendar events. Actions: list (read events from an .ics file), "
        "add (append an event to an .ics file), create (new .ics calendar), "
        "outlook_list / outlook_add (Windows Outlook via COM, requires pywin32 and "
        "a running Outlook)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "add", "create", "outlook_list", "outlook_add"],
            },
            "path": {"type": "string", "description": "Path to the .ics file."},
            "summary": {"type": "string", "description": "Event title."},
            "start": {"type": "string", "description": "Start time, ISO 8601 (e.g. 2026-08-10T14:00:00)."},
            "end": {"type": "string", "description": "End time, ISO 8601. Defaults to start + duration."},
            "duration_minutes": {"type": "number", "description": "Used when end is omitted (default 60)."},
            "location": {"type": "string"},
            "description": {"type": "string"},
            "days_ahead": {"type": "number", "description": "Look-ahead window for list actions (default 30)."},
        },
        "required": ["action"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 30

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).lower()
        try:
            if action in ("outlook_list", "outlook_add"):
                return self._outlook(action, kwargs)
            if action in ("list", "add", "create"):
                return self._ics(action, kwargs)
            return bad(self.name, f"不支持的 action：{action}")
        except Exception as e:
            return err(self.name, e)

    # ── ICS ───────────────────────────────────────────────

    @staticmethod
    def _parse_dt(raw: Any, default: datetime | None = None) -> datetime:
        if not raw:
            if default is not None:
                return default
            raise ValueError("缺少时间参数（start）")
        s = str(raw).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(s)

    def _ics(self, action: str, kw: dict) -> ToolResult:
        need("icalendar")
        from icalendar import Calendar, Event  # noqa: PLC0415 - 懒加载

        path = Path(str(kw.get("path") or "")).expanduser()
        if not str(path):
            return bad(self.name, "需要提供 .ics 文件路径（path）")

        if action == "list":
            if not path.is_file():
                return bad(self.name, f"日历文件不存在：{path}")
            cal = Calendar.from_ical(path.read_bytes())
            horizon = datetime.now().astimezone() + timedelta(
                days=int(kw.get("days_ahead") or 30))
            events = []
            for comp in cal.walk("VEVENT"):
                dt = comp.get("DTSTART")
                start = dt.dt if dt else None
                # 全天事件是 date 而非 datetime，统一成可比较的形式
                if start is not None and not isinstance(start, datetime):
                    start = datetime.combine(start, datetime.min.time())
                if isinstance(start, datetime) and start.tzinfo is None:
                    start = start.astimezone()
                if isinstance(start, datetime) and start > horizon:
                    continue
                events.append({
                    "summary": str(comp.get("SUMMARY") or ""),
                    "start": start.isoformat() if start else None,
                    "location": str(comp.get("LOCATION") or ""),
                    "description": str(comp.get("DESCRIPTION") or "")[:500],
                })
            events.sort(key=lambda e: e["start"] or "")
            return ok(self.name, path=str(path), events=events, count=len(events))

        # add / create
        summary = str(kw.get("summary") or "").strip()
        if not summary:
            return bad(self.name, "需要提供事件标题（summary）")
        start = self._parse_dt(kw.get("start"))
        end = (self._parse_dt(kw["end"]) if kw.get("end")
               else start + timedelta(minutes=int(kw.get("duration_minutes") or 60)))

        cal = (Calendar.from_ical(path.read_bytes())
               if action == "add" and path.is_file() else Calendar())
        cal.setdefault("prodid", "-//AutoMind//Calendar//CN")
        cal.setdefault("version", "2.0")
        ev = Event()
        ev.add("summary", summary)
        ev.add("dtstart", start)
        ev.add("dtend", end)
        ev.add("dtstamp", datetime.now().astimezone())
        ev.add("uid", f"{datetime.now().timestamp()}@automind")
        if kw.get("location"):
            ev.add("location", str(kw["location"]))
        if kw.get("description"):
            ev.add("description", str(kw["description"]))
        cal.add_component(ev)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(cal.to_ical())
        return ok(self.name, path=str(path), summary=summary,
                  start=start.isoformat(), end=end.isoformat(),
                  message=f"已添加日程「{summary}」到 {path.name}")

    # ── Outlook COM（仅 Windows）──────────────────────────

    def _outlook(self, action: str, kw: dict) -> ToolResult:
        if platform.system() != "Windows":
            return bad(self.name, "Outlook COM 集成仅在 Windows 上可用；"
                                  "其它平台请使用 list/add 操作 .ics 文件。")
        need("win32com")
        import pythoncom  # noqa: PLC0415
        import win32com.client  # noqa: PLC0415

        # 工具在线程池里跑，COM 必须在本线程显式初始化，否则 Dispatch 会失败
        pythoncom.CoInitialize()
        try:
            outlook = win32com.client.Dispatch("Outlook.Application")
            ns = outlook.GetNamespace("MAPI")
            cal = ns.GetDefaultFolder(9)        # 9 = olFolderCalendar

            if action == "outlook_list":
                days = int(kw.get("days_ahead") or 30)
                items = cal.Items
                items.IncludeRecurrences = True
                items.Sort("[Start]")
                now = datetime.now()
                end = now + timedelta(days=days)
                # Restrict 用本地化日期格式易出错，这里用固定格式并容错回退
                flt = (f"[Start] >= '{now.strftime('%m/%d/%Y %H:%M')}' AND "
                       f"[Start] <= '{end.strftime('%m/%d/%Y %H:%M')}'")
                try:
                    items = items.Restrict(flt)
                except Exception:
                    pass
                out = []
                for it in items:
                    if len(out) >= 200:
                        break
                    try:
                        out.append({"summary": str(it.Subject),
                                    "start": str(it.Start),
                                    "end": str(it.End),
                                    "location": str(it.Location or "")})
                    except Exception:
                        continue
                return ok(self.name, source="outlook", events=out, count=len(out))

            summary = str(kw.get("summary") or "").strip()
            if not summary:
                return bad(self.name, "需要提供事件标题（summary）")
            start = self._parse_dt(kw.get("start"))
            appt = outlook.CreateItem(1)        # 1 = olAppointmentItem
            appt.Subject = summary
            appt.Start = start.strftime("%Y-%m-%d %H:%M")
            appt.Duration = int(kw.get("duration_minutes") or 60)
            if kw.get("location"):
                appt.Location = str(kw["location"])
            if kw.get("description"):
                appt.Body = str(kw["description"])
            appt.Save()
            return ok(self.name, source="outlook", summary=summary,
                      start=start.isoformat(),
                      message=f"已在 Outlook 中创建日程「{summary}」")
        finally:
            pythoncom.CoUninitialize()


# ── im_integration ──────────────────────────────────────────

class ImIntegrationTool(AbstractTool):
    """通过群机器人 Webhook 发送消息。"""

    name = "im_integration"
    description = (
        "Send a message to a team chat via an incoming-webhook bot "
        "(DingTalk / WeCom / Feishu / Slack). The webhook URL comes from "
        "AUTOMIND_IM_WEBHOOK (or the `webhook` argument). Sending is irreversible "
        "and requires human approval. Broadcasting to many channels is not supported."
    )
    parameters = {
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "enum": ["dingtalk", "wecom", "feishu", "slack", "generic"],
                "description": "Chat platform. Defaults to AUTOMIND_IM_PROVIDER or generic.",
            },
            "text": {"type": "string", "description": "Message text."},
            "title": {"type": "string", "description": "Optional title (markdown-capable platforms)."},
            "webhook": {"type": "string", "description": "Override the configured webhook URL."},
            "markdown": {"type": "boolean", "description": "Send as markdown where supported."},
        },
        "required": ["text"],
    }
    permission_tier = PermissionTier.DANGEROUS
    risk_score = 70

    async def execute(self, **kwargs: Any) -> ToolResult:
        text = str(kwargs.get("text") or "").strip()
        if not text:
            return bad(self.name, "text 不能为空")
        provider = str(kwargs.get("provider")
                       or os.environ.get("AUTOMIND_IM_PROVIDER") or "generic").lower()
        webhook = str(kwargs.get("webhook") or os.environ.get("AUTOMIND_IM_WEBHOOK") or "")
        if not webhook:
            return bad(self.name,
                       "未配置群机器人 Webhook。请设置环境变量 AUTOMIND_IM_WEBHOOK"
                       "（并可用 AUTOMIND_IM_PROVIDER 指定平台），或在参数中传 webhook。",
                       needs_config=True)
        try:
            # Webhook 也要过 SSRF 校验：它可能来自配置文件或模型
            check_url(webhook)
        except BlockedTarget as e:
            return bad(self.name, str(e), blocked=True)

        title = str(kwargs.get("title") or "AutoMind 通知")
        md = bool(kwargs.get("markdown"))
        payload = self._payload(provider, title, text, md)
        try:
            need("httpx")
            import httpx  # noqa: PLC0415
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(webhook, json=payload)
            if r.status_code >= 400:
                return bad(self.name,
                           f"发送失败，HTTP {r.status_code}：{(r.text or '')[:200]}")
            # 各家都用 HTTP 200 + 业务错误码，需再看一层响应体
            try:
                body = r.json()
            except ValueError:
                body = {}
            code = body.get("errcode", body.get("code", body.get("StatusCode", 0)))
            if code not in (0, None, "0"):
                return bad(self.name,
                           f"平台返回业务错误：{body.get('errmsg') or body.get('msg') or body}")
            logger.info("im_message_sent", provider=provider, chars=len(text))
            return ok(self.name, sent=True, provider=provider,
                      chars=len(text), message="消息已发送")
        except Exception as e:
            return err(self.name, e)

    @staticmethod
    def _payload(provider: str, title: str, text: str, md: bool) -> dict:
        """按各平台的消息体格式组装。"""
        if provider == "dingtalk":
            return ({"msgtype": "markdown",
                     "markdown": {"title": title, "text": f"### {title}\n\n{text}"}}
                    if md else {"msgtype": "text", "text": {"content": text}})
        if provider == "wecom":
            return ({"msgtype": "markdown", "markdown": {"content": f"### {title}\n\n{text}"}}
                    if md else {"msgtype": "text", "text": {"content": text}})
        if provider == "feishu":
            return ({"msg_type": "interactive",
                     "card": {"header": {"title": {"tag": "plain_text", "content": title}},
                              "elements": [{"tag": "div",
                                            "text": {"tag": "lark_md", "content": text}}]}}
                    if md else {"msg_type": "text", "content": {"text": text}})
        if provider == "slack":
            return {"text": f"*{title}*\n{text}" if md else text}
        return {"title": title, "text": text}


__all__ = ["CalendarTool", "ImIntegrationTool", "NotifyTool"]
