"""branch 条件 —— 受限比较的解析与求值（v1.9.0 扩展算子，R-206）。

为什么单独一个模块
----------------
条件写法要被**两处**用同一套规则解释：加载期的校验（schema）与执行期的判定
（executor）。此前两边各写一份"找算子"的代码，只有三个算子时还对得上；
算子一多（``>=`` 与 ``>`` 谁先、``in`` 会不会误中 ``main`` 里的 in），两份实现
迟早出现"校验说是 A、执行按 B 判"——那是 branch 最危险的一类错：
**走错分支，而报告是绿的**。所以解析只有这一份。

仍然是白名单、仍然**不求值**
--------------------------
算子固定九个，两侧只能是模板占位符或字面量；没有 and/or、没有函数、没有算术。
理由与 template.py 一致：一旦支持表达式，YAML 就成了代码，评审也就失效了。

=========  =====================================  ==============================
算子        含义                                    两侧
=========  =====================================  ==============================
==  !=     渲染后的**文本**相等 / 不等             任意
contains   左侧文本包含右侧文本                     任意
contains_any 左侧文本包含右侧列表中**任一**项     右侧写法同 in（v1.11.5，关键词路由）
in      左侧文本是右侧列表中的一项              右侧：渲染成列表的模板，或
                                                   字面量列表 ``[a, b, "c d"]``
>  <       数值比较（两侧都必须能解析成数字，       任意；解析不了 → 该步骤失败，
>= <=      否则**判失败**而不是判假）               不静默走 else
exists     左侧变量存在且不为空值（null）           只有左侧，且必须是单个占位符
=========  =====================================  ==============================
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from automind.workflow.exceptions import suggest

#: 全部算子（报错文案与界面提示按这个顺序列）
CONDITION_OPS: tuple[str, ...] = ("==", "!=", ">", "<", ">=", "<=", "contains",
                                  "contains_any", "in", "exists")
NUMERIC_OPS: frozenset[str] = frozenset({">", "<", ">=", "<="})
#: 右侧是列表的算子（方括号字面量列表，或渲染成列表的模板）
LIST_OPS: frozenset[str] = frozenset({"in", "contains_any"})
#: 符号算子按"长的优先"匹配：同一位置上 `>=` 先于 `>`
_SYMBOL_OPS: tuple[str, ...] = ("==", "!=", ">=", "<=", ">", "<")
_WORD_OPS = re.compile(r"\s(contains_any|contains|in)\s")
_EXISTS = re.compile(r"^(?P<left>.*\S)\s+exists$", re.DOTALL)
_PLACEHOLDER = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
#: 看起来像"比较算子"但不在白名单里的写法 —— 给出定向提示用
_LOOKS_LIKE_OP = re.compile(r"^(?:[=!<>~]{1,3}|=~|!~|[A-Za-z_]+)$")
_NOT_OPS = ("matches", "startswith", "endswith", "like", "is", "not", "and", "or")


@dataclass(frozen=True)
class Condition:
    """一条解析好的条件。``right`` 对 exists 为空串。"""

    op: str
    left: str
    right: str


def _mask(text: str) -> str:
    """把占位符与引号串里的字符换成同长度的填充，避免其中的 `>`、`in` 被当成算子。"""
    def fill(match: re.Match[str]) -> str:
        return "\x00" * (match.end() - match.start())
    return _QUOTED.sub(fill, _PLACEHOLDER.sub(fill, text))


def split_condition(condition: str) -> Condition | None:
    """把 ``左 <算子> 右`` 拆开；找不到白名单算子返回 None。

    取**最靠左**的算子（同一位置长的优先）：``{{ x }} == a>b`` 里是 ``==``，
    右值是字面量 ``a>b``。占位符与引号串内部不参与匹配。
    """
    text = str(condition or "").strip()
    if not text:
        return None
    masked = _mask(text)
    best: tuple[int, int, str] | None = None          # (位置, -长度, 算子)
    for op in _SYMBOL_OPS:
        idx = masked.find(op)
        if idx > 0:
            cand = (idx, -len(op), op)
            best = cand if best is None or cand < best else best
    for word in _WORD_OPS.finditer(masked):
        idx = word.start(1)
        cand = (idx, -len(word.group(1)), word.group(1))
        best = cand if best is None or cand < best else best
    if best is None:
        # 一元的 exists 放在最后判：`{{ x }} == exists` 是在和字面量 "exists" 比较
        match = _EXISTS.match(text)
        if match and "\x00" not in masked[match.end("left"):]:
            return Condition("exists", match.group("left").strip(), "")
        return None
    idx, neg_len, op = best
    return Condition(op, text[:idx].strip(), text[idx - neg_len:].strip())


def parse_literal_list(text: str) -> list[str] | None:
    """``[a, b, "c d"]`` → ``["a", "b", "c d"]``；不是方括号写法返回 None。"""
    raw = text.strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        return [_scalar_text(v) for v in parsed]
    inner = raw[1:-1].strip()
    if not inner:
        return []
    return [part.strip().strip("\"'") for part in inner.split(",")]


def _scalar_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _is_template(text: str) -> bool:
    return "{{" in text


def _is_single_placeholder(text: str) -> bool:
    raw = text.strip()
    match = _PLACEHOLDER.fullmatch(raw)
    return bool(match) and raw.count("{{") == 1


def as_number(text: str) -> float | None:
    """数值比较用：能无歧义解析成数字就给数字，否则 None（调用方判失败）。"""
    raw = str(text).strip().strip("\"'")
    if not raw or raw.lower() in ("nan", "inf", "-inf", "infinity", "-infinity"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def check_condition(condition: str) -> list[tuple[str, list[str]]]:
    """加载期检查；返回 ``[(问题说明, 建议), ...]``，空列表 = 没问题。"""
    text = str(condition or "").strip()
    cond = split_condition(text)
    if cond is None:
        return [_unknown_operator(text)]
    problems: list[tuple[str, list[str]]] = []
    if cond.op == "exists":
        if not _is_single_placeholder(cond.left):
            problems.append((f"exists 的左侧必须是**单个**变量占位符，如 "
                             f"`{{{{ steps.fetch.output.id }}}} exists`；实际是 {cond.left!r}", []))
        return problems
    if not cond.left or not cond.right:
        problems.append((f"条件 {condition!r} 的比较算子 {cond.op!r} 两侧不能为空。"
                         "写法：`{{ steps.x.output.status }} == ok`", []))
        return problems
    if cond.op in _SYMBOL_OPS and cond.right[:1] in "=<>!~":
        glued = cond.op + re.match(r"[=<>!~]+", cond.right).group(0)
        problems.append((f"不支持的比较算子 {glued!r}。仅支持：{'、'.join(CONDITION_OPS)}",
                         suggest(glued, list(_SYMBOL_OPS))))
        return problems
    if cond.op == "contains_any":
        literal = parse_literal_list(cond.right)
        if not _is_template(cond.right) and literal is None:
            problems.append((f"contains_any 的右侧应当是方括号写的关键词列表（如 [退款, 退货]），"
                             f"或渲染成列表的模板；实际是 {cond.right!r}。"
                             "只有一个关键词请用 contains", []))
        elif literal is not None and not [w for w in literal if w]:
            problems.append(("contains_any 的关键词列表不能为空（空列表永远不成立）", []))
    if cond.op == "in":
        if cond.left.endswith((" not", "\tnot")) or cond.left == "not":
            problems.append(("不支持 `not in`：请改用 `in` 并把 then / else 两个去向对调", []))
        elif not _is_template(cond.right) and parse_literal_list(cond.right) is None:
            problems.append((f"in 的右侧应当是渲染成列表的模板，或方括号写的字面量列表"
                             f"（如 [high, urgent]）；实际是 {cond.right!r}。"
                             "只想判断“包含某段文字”请用 contains", []))
    if cond.op in NUMERIC_OPS:
        for side, value in (("左", cond.left), ("右", cond.right)):
            if not _is_template(value) and as_number(value) is None:
                problems.append((f"数值比较 {cond.op!r} 的{side}侧应当是数字或模板，"
                                 f"实际是 {value!r}（文本请用 == / != / contains）", []))
    return problems


def _unknown_operator(text: str) -> tuple[str, list[str]]:
    """没找到白名单算子：尽量指出"你写的那个是什么"。"""
    hint = ""
    suggestions: list[str] = []
    masked = _mask(text)
    if re.search(r"(?<![=!<>])=(?!=)", masked):
        hint = "（检测到单个 '='，比较相等请写 '=='）"
        suggestions = ["=="]
    elif " equals " in f" {masked} ":
        hint = "（比较相等请写 '=='，本实现不做表达式求值）"
        suggestions = ["=="]
    else:
        tokens = [t for t in masked.split() if "\x00" not in t]
        for token in tokens:
            if _LOOKS_LIKE_OP.match(token) and (
                    not token.isalpha() or token.lower() in _NOT_OPS):
                hint = f"（{token!r} 不是支持的算子）"
                # 单词型的写法（matches / startswith / like）多半是想做"包含"判断
                suggestions = (["contains"] if token.isalpha()
                               else suggest(token, list(_SYMBOL_OPS)))
                break
    return (f"无法解析的 branch 条件：{text!r}{hint}。仅支持这些受限比较："
            f"{'、'.join(CONDITION_OPS)}；两侧可以是模板 `{{{{ ... }}}}` 或字面量。"
            "不支持 and/or/函数调用等表达式", suggestions)


__all__ = [
    "CONDITION_OPS",
    "NUMERIC_OPS",
    "Condition",
    "as_number",
    "check_condition",
    "parse_literal_list",
    "split_condition",
]
