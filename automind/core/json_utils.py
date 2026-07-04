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
    """稳健解析 LLM 工具调用的 arguments 字段，始终返回 dict（失败返回 {}）。"""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        data = repair_json(raw)
    return data if isinstance(data, dict) else {}
