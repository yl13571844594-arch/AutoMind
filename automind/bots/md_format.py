"""IM 机器人 Markdown 适配层（v1.11.14）。

同一个 Agent 输出要发到飞书 / 钉钉 / 企业微信（自建应用 + 智能机器人）/ 微信公众号 /
通用 Webhook，而各家 Markdown 的**语法支持并不一致**：飞书卡片表格渲染很差，
企业微信自建应用不认围栏代码块、内容上限只有 2048 字节，公众号只能发纯文本。
把原始 Markdown 直接丢给平台的结果就是「飞书里好好的、企微里全变星号」「表格糊成一行」。

这里用一组**纯函数**把通用 Markdown 转成各平台最合适的形态（只做标记转换，不丢内容）：

* :func:`adapt_for_channel` —— 按渠道降级语法（表格转字段列表、代码块转引用等）；
* :func:`plain_text` —— 公众号 / 纯文本渠道：去标记但保留结构与可读性；
* :func:`split_message` —— 按平台字节上限分片，优先在块边界断开、不切围栏代码块；
* :func:`render_title` —— 各平台标题规范（飞书 header plain_text、钉钉限长、企微正文标题…）。

约定：所有函数**绝不抛异常**。参数异常 / 未识别的语法一律安全降级（原样返回或去标记），
宁可少一点美化，也不能把消息内容弄丢。

各平台规则（详见 ``docs/IM_BOTS.md``「各平台 Markdown 渲染规则与差异」）::

    渠道        标题                    表格             代码块            上限
    feishu      header plain_text       转字段列表       保留围栏代码块    20000B
    dingtalk    title ≤30 字            转字段列表       保留围栏代码块    15000B
    wecom       "## 标题"（正文首行）   转项目列表       转 "> " 引用      2000B
    wecom_bot   "## 标题"（正文首行）   转项目列表       保留围栏代码块    20000B
    wechat_mp   "【标题】"              转 "• 字段" 文本  去围栏、保留代码  1900B
    webhook     原样                    原样             原样              60000B
"""

from __future__ import annotations

import re
from collections.abc import Callable

__all__ = [
    "CHANNEL_LIMITS",
    "adapt_for_channel",
    "normalize_channel",
    "plain_text",
    "render_title",
    "split_message",
]

#: 各渠道单条消息的字节上限。实际发送时以 ``Channel.limit_bytes`` 为准，
#: 这里只作为 :func:`split_message` 的兜底（防止调用方传入超过平台硬上限的值）。
CHANNEL_LIMITS: dict[str, int] = {
    "feishu": 20000,
    "dingtalk": 15000,
    "wecom": 2000,
    "wecom_bot": 20000,
    "wechat_mp": 1900,
    "webhook": 60000,
}

#: 走 markdown 渲染的渠道（会自动做语法降级）
_MARKDOWN_CHANNELS = frozenset({"feishu", "dingtalk", "wecom", "wecom_bot"})
#: 只能发纯文本的渠道
_PLAIN_CHANNELS = frozenset({"wechat_mp"})
#: 常见别名（大小写不敏感），避免调用方写错就直接放弃适配
_ALIASES = {
    "lark": "feishu",
    "dingding": "dingtalk",
    "ding": "dingtalk",
    "wecom_app": "wecom",
    "wecom_robot": "wecom_bot",
    "wechat": "wechat_mp",
    "wxmp": "wechat_mp",
    "mp": "wechat_mp",
    "generic": "webhook",
    "callback": "webhook",
}

#: 续片标记预留的字节数（"（续 999/999）" 约 20 字节，留足余量）
_MARKER_RESERVE = 24

# ── 正则 ──────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([^\s`]*)\s*$")
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s+")
_HEADING_LINE_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^ {0,3}(?:[-*+]|\d+[.)])\s+")
_UNORDERED_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^ {0,3}>\s?")
_QUOTE_LINE_RE = re.compile(r"^ {0,3}>\s?(.*)$")
_HR_RE = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
_TASK_RE = re.compile(r"^\[([ xX])\]\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$")
_IMG_RE = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HTML_IMG_RE = re.compile(
    r"<img\s+[^>]*?src\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))[^>]*>", re.IGNORECASE)
_HTML_A_RE = re.compile(
    r"<a\s+[^>]*?href\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))[^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL)
_AUTOLINK_RE = re.compile(r"<((?:https?|mailto|ftp):[^<>\s]+)>")
_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>|<!--.*?-->", re.DOTALL)
_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")


def normalize_channel(channel: object) -> str:
    """渠道名归一化：大小写 / 别名 / 脏值都不抛异常（未知返回原样小写串）。"""
    try:
        key = str(channel or "").strip().lower()
    except Exception:                              # pragma: no cover - 防御性
        return ""
    return _ALIASES.get(key, key)


def _as_text(value: object) -> str:
    """把任意值安全转成 str（``None`` → ``""``；转换失败返回空串，绝不抛异常）。"""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:                              # pragma: no cover - 防御性
        return ""


# ═══════════════════════════════════════════════════════════════
# 通用清洗
# ═══════════════════════════════════════════════════════════════


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _strip_html(text: str) -> str:
    """去掉 HTML 标签，但**保留链接 / 图片地址**（不因转换丢 URL）。"""

    def img(m: re.Match[str]) -> str:
        url = m.group(1) or m.group(2) or m.group(3) or ""
        return f"[图片]({url})" if url else ""

    def anchor(m: re.Match[str]) -> str:
        url = m.group(1) or m.group(2) or m.group(3) or ""
        inner = m.group(4).strip()
        if not url:
            return inner
        return f"[{inner}]({url})" if inner else f"[链接]({url})"

    text = _HTML_IMG_RE.sub(img, text)
    text = _HTML_A_RE.sub(anchor, text)
    text = _BR_RE.sub("\n", text)
    text = _AUTOLINK_RE.sub(r"\1", text)         # <https://x> → https://x（地址不丢）
    return _TAG_RE.sub("", text)


def _tidy(text: str) -> str:
    """压缩多余空行（**围栏代码块内部原样保留**，不改变代码里的空行）。"""
    out: list[str] = []
    fence_char = ""
    blank = 0
    for line in text.split("\n"):
        fm = _FENCE_RE.match(line)
        if fence_char:
            out.append(line)
            if fm and fm.group(1)[0] == fence_char and not fm.group(2):
                fence_char = ""
            continue
        if fm:
            fence_char = fm.group(1)[0]
            out.append(line)
            blank = 0
            continue
        if not line.strip():
            blank += 1
            if blank <= 1:
                out.append("")
            continue
        blank = 0
        out.append(line)
    return "\n".join(out).strip("\n")


def _map_outside_fences(text: str, fn: Callable[[str], str]) -> str:
    """只对**围栏代码块之外**的文本做转换（代码块内容必须原样保留）。"""
    lines = text.split("\n")
    out: list[str] = []
    buf: list[str] = []
    fence_char = ""

    def flush() -> None:
        if buf:
            out.extend(fn("\n".join(buf)).split("\n"))
            buf.clear()

    for line in lines:
        fm = _FENCE_RE.match(line)
        if fence_char:
            out.append(line)
            if fm and fm.group(1)[0] == fence_char and not fm.group(2):
                fence_char = ""
            continue
        if fm:
            flush()
            fence_char = fm.group(1)[0]
            out.append(line)
            continue
        buf.append(line)
    flush()
    return "\n".join(out)


# ── 表格 ──────────────────────────────────────────────────────


def _split_cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    head, sep = lines[index], lines[index + 1]
    if "|" not in head or "|" not in sep or _FENCE_RE.match(head):
        return False
    return bool(_TABLE_SEP_RE.match(sep))


def _table_row(headers: list[str], cells: list[str], *,
               bullet: str, bold_header: bool) -> str:
    parts: list[str] = []
    width = max(len(headers), len(cells))
    for i in range(width):
        cell = cells[i] if i < len(cells) else ""
        if not cell:
            continue
        head = headers[i] if i < len(headers) else ""
        if head and len(headers) > 1:
            parts.append(f"**{head}**：{cell}" if bold_header else f"{head}：{cell}")
        else:
            parts.append(cell)
    if not parts:
        return ""
    return f"{bullet} " + " · ".join(parts)


def _rewrite_tables(text: str, *, bullet: str, bold_header: bool,
                    plain: bool = False) -> str:
    """把管道表格转成「逐行字段列表」（平台不支持表格时的通用降级）。

    ``plain=True`` 时同时把单元格内的行内标记去掉（供 :func:`plain_text` 用）。
    """
    lines = text.split("\n")
    out: list[str] = []
    fence_char = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        fm = _FENCE_RE.match(line)
        if fence_char:
            out.append(line)
            if fm and fm.group(1)[0] == fence_char and not fm.group(2):
                fence_char = ""
            i += 1
            continue
        if fm:
            fence_char = fm.group(1)[0]
            out.append(line)
            i += 1
            continue
        if _is_table_start(lines, i):
            headers = _split_cells(lines[i])
            if plain:
                headers = [_inline(h) for h in headers]
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i] \
                    and not _FENCE_RE.match(lines[i]):
                cells = _split_cells(lines[i])
                if plain:
                    cells = [_inline(c) for c in cells]
                row = _table_row(headers, cells, bullet=bullet, bold_header=bold_header)
                if row:
                    out.append(row)
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


# ── 代码围栏 ──────────────────────────────────────────────────


def _fence_close(line: str, char: str) -> bool:
    m = _FENCE_RE.match(line)
    if not m or m.group(2):
        return False
    return m.group(1)[0] == char


def _fences_to_quotes(text: str) -> str:
    """企业微信自建应用不认 ``` 围栏：把代码块转成 ``> `` 引用（保留代码内容）。"""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _FENCE_RE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        char = m.group(1)[0]
        lang = m.group(2)
        body: list[str] = []
        i += 1
        while i < len(lines):
            if _fence_close(lines[i], char):
                i += 1
                break
            body.append(lines[i])
            i += 1
        out.append(f"> 代码块（{lang}）：" if lang else "> 代码块：")
        out.extend(f"> {ln}" if ln else ">" for ln in body)
    return "\n".join(out)


def _images_to_links(text: str) -> str:
    """平台不认图片语法时退化成链接 —— 地址保留，不会丢内容。"""

    def repl(m: re.Match[str]) -> str:
        alt = m.group(1).strip()
        url = m.group(2)
        return f"[图片：{alt}]({url})" if alt else f"[图片]({url})"

    return _IMG_RE.sub(repl, text)


# ═══════════════════════════════════════════════════════════════
# 渠道适配
# ═══════════════════════════════════════════════════════════════


def _adapt_markdown(text: str, channel: str) -> str:
    text = _normalize_newlines(text)
    text = _rewrite_tables(text, bullet="-", bold_header=True)
    text = _map_outside_fences(text, _strip_html)
    if channel == "wecom":
        text = _map_outside_fences(text, _images_to_links)
        text = _fences_to_quotes(text)             # 自建应用不认围栏：转引用（代码内容原样）
    elif channel == "wecom_bot":
        text = _map_outside_fences(text, _images_to_links)
    return _tidy(text)


def render_title(channel: str, title: str) -> str:
    """按平台规范渲染标题。

    * 飞书：卡片 header 用 ``plain_text``（去标记、限 60 字）；
    * 钉钉：markdown title 限 30 字（只取第一行，避免多行标题被平台截断）；
    * 企微 / 智能机器人：正文首行 ``## 标题``；
    * 公众号：``【标题】``；
    * Webhook / 未知渠道：原样返回（下游自己渲染）。
    """
    raw = _as_text(title)
    try:
        key = normalize_channel(channel)
        text = _normalize_newlines(raw).strip()
        while True:                              # 多级标题前缀只去一层语义（## 标题 → 标题）
            stripped = re.sub(r"^ {0,3}#{1,6}\s*", "", text).strip()
            if stripped == text:
                break
            text = stripped
        if not text:
            return ""
        if key == "feishu":
            return _clip(_title_plain(text.split("\n", 1)[0]), 60)
        if key == "dingtalk":
            return _clip(_title_plain(text.split("\n", 1)[0]), 30)
        first = _title_plain(text.split("\n", 1)[0])
        if key in ("wecom", "wecom_bot"):
            return "## " + first
        if key == "wechat_mp":
            return f"【{first}】"
        return raw.strip()
    except Exception:                              # pragma: no cover - 防御性
        return raw


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _title_plain(text: str) -> str:
    """标题里只保留文字：去链接 URL、代码标记、加粗/斜体/删除线符号、HTML 标签。"""
    text = _strip_html(text)
    text = _IMG_RE.sub(lambda m: m.group(1) or "图片", text)
    text = _LINK_RE.sub(lambda m: m.group(1) if m.group(1) else m.group(2), text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("~~", "")
    text = re.sub(r"\*([^*\n]+)\*", r"\1", text)
    return text.strip()


def adapt_for_channel(channel: str, md: str, *, title: str = "") -> str:
    """把通用 Markdown 转成该渠道可渲染的最优形态（绝不抛异常）。

    ``title`` 只对**把标题放正文**的渠道生效（企微 / 智能机器人 / 公众号）：
    正文首行会补上对应格式的标题；飞书 / 钉钉 / Webhook 的标题走平台独立字段，
    由 :func:`render_title` 处理，这里不重复拼进正文。
    """
    raw = _as_text(md)
    try:
        key = normalize_channel(channel)
        if not key or key == "webhook":
            return raw                            # 下游自己渲染：保持原始 Markdown
        if key in _PLAIN_CHANNELS:
            body = plain_text(raw)
            head = render_title(key, title) if title else ""
            if not head:
                return body
            return f"{head}\n{body}" if body.strip() else head
        if key in _MARKDOWN_CHANNELS:
            body = _adapt_markdown(raw, key)
            head = render_title(key, title) if title and key in ("wecom", "wecom_bot") else ""
            if not head:
                return body
            return f"{head}\n\n{body}" if body.strip() else head
        return raw                                # 未知渠道：宁可原样，也不动内容
    except Exception:                              # pragma: no cover - 防御性
        return raw


# ═══════════════════════════════════════════════════════════════
# 纯文本（公众号）
# ═══════════════════════════════════════════════════════════════


def plain_text(md: str) -> str:
    """去掉 Markdown 标记，但**保留结构与可读性**（公众号只能发纯文本）。

    * 标题 → ``【标题】``；列表 → ``• ``（有序列表保留编号）；引用 → ``│ ``；
    * 表格 → ``• 表头：值 · 表头：值``（逐行）；
    * 代码块去掉围栏、内容原样保留；链接 → ``文字（url）``；图片 → ``图片：说明（url）``；
    * HTML 标签去掉（``<a href>`` 的地址会保留成链接文本）。
    """
    raw = _as_text(md)
    try:
        text = _map_outside_fences(_normalize_newlines(raw), _strip_html)
        if not text.strip():
            return ""
        text = _rewrite_tables(text, bullet="•", bold_header=False, plain=True)
        out: list[str] = []
        fence_char = ""
        for line in text.split("\n"):
            fm = _FENCE_RE.match(line)
            if fence_char:
                if fm and fm.group(1)[0] == fence_char and not fm.group(2):
                    fence_char = ""
                else:
                    out.append(line)              # 代码内容原样保留
                continue
            if fm:
                fence_char = fm.group(1)[0]       # 围栏行本身丢掉
                continue
            out.append(_plain_line(line))
        return _tidy("\n".join(out))
    except Exception:                              # pragma: no cover - 防御性
        return raw


def _plain_line(line: str) -> str:
    if not line.strip():
        return ""
    m = _HR_RE.match(line)
    if m:
        return "────────"
    m = _HEADING_LINE_RE.match(line)
    if m:
        return f"【{_inline(m.group(2))}】"
    m = _QUOTE_LINE_RE.match(line)
    if m:
        return f"│ {_inline(m.group(1))}".rstrip()
    m = _UNORDERED_RE.match(line)
    if m:
        task = _TASK_RE.match(m.group(2))
        if task:                                 # 任务列表：- [ ] / - [x] → ☐ / ☑
            box = "☑" if task.group(1).lower() == "x" else "☐"
            return f"{m.group(1)}{box} {_inline(task.group(2))}"
        return f"{m.group(1)}• {_inline(m.group(2))}"
    m = _ORDERED_RE.match(line)
    if m:
        return f"{m.group(1)}{m.group(2)}. {_inline(m.group(3))}"
    return _inline(line)


def _inline(text: str) -> str:
    """去掉一行里的行内标记（链接保留 URL；行内代码内容原样）。"""
    codes: list[str] = []

    def keep_code(m: re.Match[str]) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    text = _INLINE_CODE_RE.sub(keep_code, text)
    text = _IMG_RE.sub(lambda m: (f"图片：{m.group(1).strip()}" if m.group(1).strip()
                                  else "图片") + f"（{m.group(2)}）", text)
    text = _LINK_RE.sub(
        lambda m: m.group(2) if not m.group(1) or m.group(1) == m.group(2)
        else f"{m.group(1)}（{m.group(2)}）", text)
    text = _AUTOLINK_RE.sub(r"\1", text)
    text = re.sub(r"\*\*([^*\n]+?)\*\*", r"\1", text)
    text = re.sub(r"__([^_\n]+?)__", r"\1", text)
    text = re.sub(r"~~([^~\n]+?)~~", r"\1", text)
    text = re.sub(r"\*([^*\n]+?)\*", r"\1", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)
    text = _ESCAPE_RE.sub(r"\1", text)
    return text


# ═══════════════════════════════════════════════════════════════
# 安全分片
# ═══════════════════════════════════════════════════════════════


def _marker(index: int, total: int) -> str:
    return f"（续 {index + 1}/{total}）"


def _hard_split(text: str, limit: int) -> list[str]:
    """按 UTF-8 字节硬切（单个字符超过上限时也保证前进，不会死循环）。"""
    if limit <= 0:
        return [text]
    out: list[str] = []
    cur = ""
    size = 0
    for ch in text:
        ch_size = len(ch.encode("utf-8"))
        if cur and size + ch_size > limit:
            out.append(cur)
            cur, size = ch, ch_size
        elif not cur and ch_size > limit:
            out.append(ch)                        # 上限比一个字符还小：只能逐字发
        else:
            cur += ch
            size += ch_size
    if cur:
        out.append(cur)
    return out or [""]


def _segment(text: str) -> list[tuple[str, bool]]:
    """切成「块」：段落 / 标题 / 列表项 / 引用 / 表格 / 围栏代码块。

    返回 ``(块文本, 块前是否有空行)``；围栏代码块整块保留，作为不可再分的块
    （真正超限时由 :func:`_split_fence` 补围栏后再切）。
    """
    lines = text.split("\n")
    blocks: list[str] = []
    spaced: list[bool] = []
    cur: list[str] = []
    cur_spaced = False
    after_blank = False

    def emit() -> None:
        nonlocal cur
        if cur:
            blocks.append("\n".join(cur))
            spaced.append(cur_spaced)
            cur = []

    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence:
            emit()
            char = fence.group(1)[0]
            block = [line]
            i += 1
            while i < len(lines):
                block.append(lines[i])
                if _fence_close(lines[i], char):
                    i += 1
                    break
                i += 1
            blocks.append("\n".join(block))
            spaced.append(after_blank)
            after_blank = False
            continue
        if not line.strip():
            emit()
            after_blank = True
            i += 1
            continue
        if _is_table_start(lines, i):
            emit()
            block = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i] \
                    and not _FENCE_RE.match(lines[i]):
                block.append(lines[i])
                i += 1
            blocks.append("\n".join(block))
            spaced.append(after_blank)
            after_blank = False
            continue
        if _HEADING_RE.match(line) or _LIST_RE.match(line) \
                or _QUOTE_RE.match(line) or _HR_RE.match(line):
            emit()
            cur = [line]
            cur_spaced = after_blank
            after_blank = False
        else:
            if not cur:
                cur_spaced = after_blank
                after_blank = False
            cur.append(line)
        i += 1
    emit()
    return list(zip(blocks, spaced, strict=True))


def _split_lines(lines: list[str], limit: int) -> list[str]:
    out: list[str] = []
    cur = ""
    for line in lines:
        if cur:
            candidate = cur + "\n" + line
            if len(candidate.encode("utf-8")) <= limit:
                cur = candidate
                continue
            out.append(cur)
            cur = ""
        if len(line.encode("utf-8")) <= limit:
            cur = line
            continue
        pieces = _hard_split(line, limit)
        out.extend(pieces[:-1])
        cur = pieces[-1]
    if cur or not out:
        out.append(cur)
    return out


def _split_fence(lines: list[str], fence_match: re.Match[str], limit: int) -> list[str]:
    """切超长代码块：每一片都补上开/闭围栏，保证平台渲染时围栏是闭合的。"""
    opening = lines[0]
    char = fence_match.group(1)[0]
    closing = char * 3
    has_close = len(lines) >= 2 and _fence_close(lines[-1], char)
    body = lines[1:-1] if has_close else lines[1:]
    overhead = (len(opening.encode("utf-8")) + len(closing.encode("utf-8")) + 2)
    if overhead >= limit:
        return _split_lines(lines, limit)         # 上限小到装不下围栏：安全降级
    room = max(1, limit - overhead - 1)
    expanded: list[str] = []
    for line in body:
        if len(line.encode("utf-8")) > room:
            expanded.extend(_hard_split(line, room))
        else:
            expanded.append(line)
    parts: list[str] = []
    cur: list[str] = []
    size = overhead
    for line in expanded:
        add = len(line.encode("utf-8")) + (1 if cur else 0)
        if cur and size + add > limit:
            parts.append("\n".join([opening, *cur, closing]))
            cur, size, add = [], overhead, len(line.encode("utf-8"))
        cur.append(line)
        size += add
    if cur or not parts:
        parts.append("\n".join([opening, *cur, closing]))
    return parts


def _split_block(block: str, limit: int) -> list[str]:
    lines = block.split("\n")
    if len(lines) > 1 and _FENCE_RE.match(lines[0]):
        return _split_fence(lines, _FENCE_RE.match(lines[0]), limit)
    return _split_lines(lines, limit)


def _pack(segments: list[tuple[str, bool]], limit: int, reserve: int) -> list[str]:
    chunks: list[str] = []
    cur = ""
    for block, spaced in segments:
        sep = "" if not cur else ("\n\n" if spaced else "\n")
        cap = limit if not chunks else limit - reserve
        if len(f"{cur}{sep}{block}".encode()) <= cap:
            cur = f"{cur}{sep}{block}"
            continue
        if cur:
            chunks.append(cur)
            cur = ""
            cap = limit if not chunks else limit - reserve   # 续片要扣掉标记占用的字节
            if len(block.encode("utf-8")) <= cap:
                cur = block
                continue
        # 单块超限：切出来的每一片（含第一片）都预留续片标记的字节，
        # 否则后面再补「（续 N/M）」会把这片顶出平台上限。
        split_cap = max(1, cap if chunks else limit - reserve)
        parts = _split_block(block, split_cap)
        chunks.extend(parts[:-1])
        cur = parts[-1]
    if cur or not chunks:
        chunks.append(cur)
    return [c for c in chunks if c.strip()] or [""]


def split_message(md: str, limit_bytes: int, *, channel: str = "",
                  marker: bool = True) -> list[str]:
    """按字节上限安全分片，返回可直接发送的若干段（绝不抛异常）。

    * 优先在**块边界**（空行 / 段落 / 标题 / 列表项）断开；不会切在 `` ``` `` 围栏内部；
    * 单块超限时先按行切，单行仍超限再按 UTF-8 字符硬切；
    * 围栏代码块被迫切开时，每一片都会补上开/闭围栏（渲染不会漏出代码）；
    * ``marker=True``（默认）时续片末尾补 ``（续 N/M）``；``channel`` 已知时先按该渠道
      适配再分片，保证切的是「最终渲染文本」。

    空串返回 ``[""]``，``limit_bytes <= 0`` 时原样返回 ``[md]``。
    """
    raw = _as_text(md)
    try:
        key = normalize_channel(channel)
        try:
            limit = int(limit_bytes)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return [raw]
        hard_cap = CHANNEL_LIMITS.get(key) or 0
        if hard_cap:
            limit = min(limit, hard_cap)
        text = raw
        if key in _MARKDOWN_CHANNELS:
            text = adapt_for_channel(key, text)
        if not text:
            return [""]
        text = _normalize_newlines(text)
        if len(text.encode("utf-8")) <= limit:
            return [text]
        use_marker = marker and limit > _MARKER_RESERVE + 8
        reserve = _MARKER_RESERVE if use_marker else 0
        chunks = _pack(_segment(text), limit, reserve)
        if use_marker and len(chunks) > 1:
            total = len(chunks)
            for idx in range(1, total):
                mark = _marker(idx, total)
                candidate = f"{chunks[idx].rstrip()}\n{mark}"
                if len(candidate.encode("utf-8")) <= limit:
                    chunks[idx] = candidate
        return chunks
    except Exception:                              # pragma: no cover - 防御性
        return _hard_split(raw, limit_bytes if isinstance(limit_bytes, int) and limit_bytes > 0
                           else 1) or [raw]
