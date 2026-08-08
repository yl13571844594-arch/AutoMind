"""邮件工具 —— SMTP 发送 / IMAP 收取。用标准库实现，不引入额外依赖。

**外发是不可撤回的动作**，因此 send 定档 DANGEROUS：在「询问」模式下每封都要
用户点批准，且 v1.4.5 起审批通道异常一律按拒绝处理（问不到人就不发）。

**刻意不提供群发能力**。收件人总数硬上限 ``MAX_RECIPIENTS``，超出直接拒绝并
提示改用正规邮件服务 —— 一个能被模型驱动的无限群发接口就是垃圾邮件基础设施，
这不是版本差异能解决的问题，所以专业版也不解锁它。

凭据只从配置/环境变量读取（``AUTOMIND_SMTP_*`` / ``AUTOMIND_IMAP_*``），
不接受模型在参数里传密码，也不会把密码写进任何返回值或日志。
"""

from __future__ import annotations

import email.utils
import imaplib
import os
import smtplib
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from automind.core.logging import get_logger
from automind.core.types import PermissionTier, ToolResult
from automind.tools._toolkit import bad, err, ok
from automind.tools.base import AbstractTool

logger = get_logger("automind.tools.email")

#: 单次发送的收件人上限（to + cc + bcc 合计）。见模块文档：不做群发。
MAX_RECIPIENTS = 20
#: 单个附件与总附件大小上限
MAX_ATTACH_BYTES = 20 * 1024 * 1024


def _cfg(prefix: str) -> dict[str, str]:
    """从环境变量读取一组邮件配置（不落日志）。"""
    return {
        "host": os.environ.get(f"AUTOMIND_{prefix}_HOST", ""),
        "port": os.environ.get(f"AUTOMIND_{prefix}_PORT", ""),
        "user": os.environ.get(f"AUTOMIND_{prefix}_USER", ""),
        "password": os.environ.get(f"AUTOMIND_{prefix}_PASSWORD", ""),
        "ssl": os.environ.get(f"AUTOMIND_{prefix}_SSL", "1"),
    }


def _decode(raw: Any) -> str:
    """解码 MIME 编码过的头部（=?utf-8?B?...?=）。"""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(str(raw))))
    except Exception:
        return str(raw)


class EmailTool(AbstractTool):
    """收发邮件。send 需人工批准；不提供群发。"""

    name = "email_tool"
    description = (
        "Send and read email. Actions: send (compose and send one message), "
        "list (recent messages in an IMAP folder), read (fetch one message by uid), "
        "folders (list IMAP folders), check_config (verify credentials are configured). "
        "Sending requires human approval and is capped at "
        f"{MAX_RECIPIENTS} recipients — bulk/mass mailing is deliberately not supported. "
        "Credentials come from AUTOMIND_SMTP_* / AUTOMIND_IMAP_* environment variables; "
        "never pass passwords as arguments."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send", "list", "read", "folders", "check_config"],
            },
            "to": {"type": "array", "items": {"type": "string"}, "description": "Recipient addresses."},
            "cc": {"type": "array", "items": {"type": "string"}},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "Plain-text body."},
            "html": {"type": "string", "description": "Optional HTML alternative body."},
            "attachments": {
                "type": "array", "items": {"type": "string"},
                "description": "File paths to attach.",
            },
            "folder": {"type": "string", "description": "IMAP folder (default INBOX)."},
            "limit": {"type": "number", "description": "How many messages to list (default 20, max 100)."},
            "uid": {"type": "string", "description": "Message uid for the read action."},
            "unseen_only": {"type": "boolean", "description": "List only unread messages."},
        },
        "required": ["action"],
    }
    # 外发不可撤回 → DANGEROUS，「询问」模式下逐封审批
    permission_tier = PermissionTier.DANGEROUS
    risk_score = 75

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).lower()
        try:
            if action == "check_config":
                return self._check_config()
            if action == "send":
                return self._send(kwargs)
            if action in ("list", "read", "folders"):
                return self._imap(action, kwargs)
            return bad(self.name, f"不支持的 action：{action}")
        except Exception as e:
            return err(self.name, e)

    # ── 配置 ──────────────────────────────────────────────

    def _check_config(self) -> ToolResult:
        s, i = _cfg("SMTP"), _cfg("IMAP")
        return ok(
            self.name,
            smtp_configured=bool(s["host"] and s["user"] and s["password"]),
            imap_configured=bool(i["host"] and i["user"] and i["password"]),
            smtp_host=s["host"] or None, imap_host=i["host"] or None,
            smtp_user=s["user"] or None,
            hint=("凭据通过环境变量配置：AUTOMIND_SMTP_HOST / _PORT / _USER / _PASSWORD，"
                  "收件同理用 AUTOMIND_IMAP_*。出于安全，密码不接受作为参数传入。"))

    # ── 发送 ──────────────────────────────────────────────

    def _send(self, kw: dict) -> ToolResult:
        c = _cfg("SMTP")
        if not (c["host"] and c["user"] and c["password"]):
            return bad(self.name,
                       "SMTP 未配置。请设置环境变量 AUTOMIND_SMTP_HOST / AUTOMIND_SMTP_USER "
                       "/ AUTOMIND_SMTP_PASSWORD（以及可选的 AUTOMIND_SMTP_PORT）后重试。")

        to = [a.strip() for a in (kw.get("to") or []) if str(a).strip()]
        cc = [a.strip() for a in (kw.get("cc") or []) if str(a).strip()]
        if not to:
            return bad(self.name, "缺少收件人（to）")
        total = len(to) + len(cc)
        if total > MAX_RECIPIENTS:
            return bad(self.name,
                       f"收件人共 {total} 个，超过上限 {MAX_RECIPIENTS}。本工具不提供群发能力，"
                       "批量邮件请使用正规的邮件服务商（并遵守其反垃圾政策与当地法规）。")
        bad_addr = [a for a in to + cc if "@" not in a or a.startswith("@") or a.endswith("@")]
        if bad_addr:
            return bad(self.name, f"收件人地址格式不正确：{', '.join(bad_addr)}")

        msg = EmailMessage()
        msg["From"] = c["user"]
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = str(kw.get("subject") or "(无主题)")
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid()
        msg.set_content(str(kw.get("body") or ""))
        if kw.get("html"):
            msg.add_alternative(str(kw["html"]), subtype="html")

        attached, total_bytes = [], 0
        for raw in (kw.get("attachments") or []):
            p = Path(str(raw)).expanduser()
            if not p.is_file():
                return bad(self.name, f"附件不存在：{p}")
            data = p.read_bytes()
            total_bytes += len(data)
            if total_bytes > MAX_ATTACH_BYTES:
                return bad(self.name,
                           f"附件总大小超过 {MAX_ATTACH_BYTES // 1024 // 1024}MB 上限")
            msg.add_attachment(data, maintype="application",
                               subtype="octet-stream", filename=p.name)
            attached.append(p.name)

        port = int(c["port"] or (465 if c["ssl"] not in ("0", "false") else 587))
        use_ssl = c["ssl"] not in ("0", "false")
        try:
            if use_ssl and port == 465:
                srv: Any = smtplib.SMTP_SSL(c["host"], port, timeout=30)
            else:
                srv = smtplib.SMTP(c["host"], port, timeout=30)
                if use_ssl:
                    srv.starttls()
            with srv:
                srv.login(c["user"], c["password"])
                srv.send_message(msg, to_addrs=to + cc)
        except smtplib.SMTPAuthenticationError:
            # 不回显任何凭据内容
            return bad(self.name, "SMTP 认证失败：账号或密码/授权码不正确。"
                                  "多数邮箱需使用「授权码」而非登录密码。")
        except (smtplib.SMTPException, OSError) as e:
            return bad(self.name, f"发送失败：{type(e).__name__}: {e}")

        logger.info("email_sent", to_count=len(to), cc_count=len(cc),
                    attachments=len(attached))
        return ok(self.name, sent=True, to=to, cc=cc,
                  subject=msg["Subject"], attachments=attached,
                  message=f"已发送给 {len(to) + len(cc)} 个收件人")

    # ── 收取 ──────────────────────────────────────────────

    def _imap(self, action: str, kw: dict) -> ToolResult:
        c = _cfg("IMAP")
        if not (c["host"] and c["user"] and c["password"]):
            return bad(self.name,
                       "IMAP 未配置。请设置 AUTOMIND_IMAP_HOST / AUTOMIND_IMAP_USER "
                       "/ AUTOMIND_IMAP_PASSWORD 后重试。")
        port = int(c["port"] or 993)
        try:
            m = imaplib.IMAP4_SSL(c["host"], port)
        except OSError as e:
            return bad(self.name, f"无法连接 IMAP 服务器：{e}")
        try:
            m.login(c["user"], c["password"])
            if action == "folders":
                _, data = m.list()
                names = [_decode(x.decode(errors="replace").split(' "/" ')[-1].strip('"'))
                         for x in data or []]
                return ok(self.name, folders=names, count=len(names))

            folder = str(kw.get("folder") or "INBOX")
            m.select(folder, readonly=True)      # readonly：绝不改动邮箱状态
            if action == "read":
                return self._read_one(m, kw)
            return self._list(m, kw, folder)
        except imaplib.IMAP4.error as e:
            return bad(self.name, f"IMAP 操作失败：{e}")
        finally:
            try:
                m.logout()
            except Exception:
                pass

    def _list(self, m: Any, kw: dict, folder: str) -> ToolResult:
        limit = max(1, min(int(kw.get("limit") or 20), 100))
        crit = "(UNSEEN)" if kw.get("unseen_only") else "ALL"
        _, data = m.uid("search", None, crit)
        uids = (data[0].split() if data and data[0] else [])[-limit:]
        items = []
        for uid in reversed(uids):
            # 只取头部，不拉正文 —— 列表场景没必要把整封信拖下来
            _, d = m.uid("fetch", uid,
                         "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            raw = b""
            for part in d or []:
                if isinstance(part, tuple) and len(part) > 1:
                    raw = part[1]
            import email as _email
            hdr = _email.message_from_bytes(raw)
            items.append({
                "uid": uid.decode(),
                "from": _decode(hdr.get("From")),
                "subject": _decode(hdr.get("Subject")),
                "date": _decode(hdr.get("Date")),
            })
        return ok(self.name, folder=folder, messages=items, count=len(items),
                  filter=("未读" if kw.get("unseen_only") else "全部"))

    def _read_one(self, m: Any, kw: dict) -> ToolResult:
        uid = str(kw.get("uid") or "")
        if not uid:
            return bad(self.name, "read 需要提供 uid（可先用 list 获取）")
        # BODY.PEEK 而非 BODY：不把邮件标记成已读，避免工具悄悄改变用户邮箱状态
        _, d = m.uid("fetch", uid, "(BODY.PEEK[])")
        raw = b""
        for part in d or []:
            if isinstance(part, tuple) and len(part) > 1:
                raw = part[1]
        if not raw:
            return bad(self.name, f"未找到 uid={uid} 的邮件")
        import email as _email
        msg = _email.message_from_bytes(raw)
        body, attachments = "", []
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "")
            if part.get_content_type() == "text/plain" and "attachment" not in disp:
                try:
                    body += part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    pass
            elif "attachment" in disp:
                attachments.append(_decode(part.get_filename()))
        return ok(self.name, uid=uid, **{
            "from": _decode(msg.get("From")),
            "to": _decode(msg.get("To")),
            "subject": _decode(msg.get("Subject")),
            "date": _decode(msg.get("Date")),
            "body": body[:20000],
            "truncated": len(body) > 20000,
            "attachments": attachments,
        })
