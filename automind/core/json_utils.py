"""稳健的 JSON 提取 — 容忍 LLM 输出中的代码围栏与多余文本。"""

from __future__ import annotations

import json
import re
from typing import Any


def extract_json(text: str) -> Any | None:
    """从 LLM 文本响应中尽力提取一个 JSON 对象/数组。

    依次尝试：
        1. 直接解析。
        2. 去除 ```json ... ``` / ``` ... ``` 代码围栏后解析。
        3. 截取第一个平衡的 {...} 或 [...] 片段后解析。

    Returns:
        解析出的对象；全部失败返回 None。
    """
    if not text:
        return None
    text = text.strip()

    # 1) 直接解析
    try:
        return json.loads(text)
    except Exception:
        pass

    # 2) 去除代码围栏
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1).strip()
        try:
            return json.loads(candidate)
        except Exception:
            text = candidate  # 继续用围栏内内容做平衡截取

    # 3) 截取第一个平衡的 JSON 片段
    for opener, closer in (("{", "}"), ("[", "]")):
        snippet = _balanced_slice(text, opener, closer)
        if snippet:
            try:
                return json.loads(snippet)
            except Exception:
                continue
    return None


def _balanced_slice(text: str, opener: str, closer: str) -> str | None:
    """返回从第一个 opener 起、括号配对平衡的子串（忽略字符串内的括号）。"""
    start = text.find(opener)
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def repair_json(text: str) -> Any | None:
    """尽力修复被截断 / 略有瑕疵的 JSON（如工具调用参数被 max_tokens 截断）。

    策略：先常规提取；失败则按字符扫描，补齐未闭合的字符串与括号后再解析。
    """
    if not text:
        return None
    direct = extract_json(text)
    if direct is not None:
        return direct

    s = text.strip()
    # 去除可能的代码围栏
    if s.startswith("```"):
        s = s.strip("`")
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
    start = next((i for i, c in enumerate(s) if c in "{["), -1)
    if start == -1:
        return None
    s = s[start:]

    stack: list[str] = []
    in_str = False
    escape = False
    for ch in s:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()

    repaired = s
    if in_str:
        repaired += '"'           # 闭合未结束的字符串
    # 移除尾随逗号后再补齐括号
    repaired = repaired.rstrip().rstrip(",")
    for closer in reversed(stack):
        repaired += closer
    try:
        return json.loads(repaired)
    except Exception:
        return None


def parse_tool_arguments(raw: str | None) -> dict:
    """稳健解析 LLM 工具调用的 arguments 字段，始终返回 dict（失败返回 {}）。

    会尽力**修补**截断的 JSON。工具调用路径请用
    :func:`parse_tool_arguments_checked` —— 截断的参数不该被补齐后执行。
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        data = repair_json(raw)
    return data if isinstance(data, dict) else {}


def parse_tool_arguments_checked(raw: str | None) -> tuple[dict, str]:
    """同 :func:`parse_tool_arguments`，另返回"参数本身有问题"的说明（无问题为空串）。

    v1.7.6：此前解析失败一律返回 ``{}``，工具于是**不带任何参数**被执行 ——
    模型收到的是"缺少必需参数 path"，被引向错误的修法（补参数），而真正的
    原因是它的参数 JSON 被 ``max_tokens`` 截断了（大段 file_write 最常见）。
    更糟的是 ``repair_json`` 能把截断的 JSON **补齐成合法对象**：半截文件内容
    会被当成完整内容写进磁盘，任务还显示成功。

    因此：能直接解析（含去围栏/截取平衡片段）→ 正常；需要"补齐括号/引号"
    才能解析 → 视为**截断**，返回说明，由调用方拒绝执行；完全解析不了同理。
    """
    if not raw or not str(raw).strip():
        return {}, ""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data, ""
    except Exception:
        data = extract_json(raw)
        if isinstance(data, dict):
            return data, ""
    size = len(str(raw))
    if repair_json(raw) is not None:
        return {}, (f"工具参数 JSON 不完整（共 {size} 字符，结尾被截断，多半是超出了"
                    f"单次输出长度上限）。本次调用**未执行**。")
    return {}, (f"工具参数不是合法的 JSON 对象（共 {size} 字符）。本次调用**未执行**。")
