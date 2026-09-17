"""断言实现 —— 每条断言都要能回答"期望什么 / 实际是什么"。

评测报告的可用性取决于失败信息：一句"断言失败"没有价值，用户需要的是
"``contains: 'hello'`` 失败：输出里没有 'hello'；实际输出前 200 字是 …"。

因此 :class:`CheckResult` 固定携带三样东西：``expected``（期望）、
``actual``（实际）、``detail``（结构化补充，如实际调用了哪些工具）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from automind.eval.suite import ASSERTION_TYPES, Assertion

#: 失败信息里回显"实际内容"的最大长度（够定位问题，又不至于把报告撑爆）
_PREVIEW = 240

#: 断言类型 → 中文名（报告表格用）
TYPE_LABELS = {
    "contains": "包含子串",
    "not_contains": "不包含子串",
    "regex": "匹配正则",
    "file_exists": "文件存在",
    "tool_called": "调用过工具",
    "max_seconds": "耗时上限",
    "max_tokens": "Token 上限",
}


@dataclass
class EvalOutcome:
    """一次任务执行的全部可观测结果（断言只读这份数据）。"""

    output: str = ""
    success: bool = True
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""
    timed_out: bool = False
    #: 产物落盘路径集合（相对工作区，正斜杠）—— 由执行器遍历工作区得到。
    #: 断言**不**自己扫盘：执行器可能是假的/远端的，扫盘会让"文件存在"这条
    #: 断言在多进程执行器下直接失效。
    artifacts: list[str] = field(default_factory=list)


@dataclass
class CheckResult:
    """一条断言的判定结果。"""

    type: str
    expected: str
    actual: str
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "expected": self.expected,
                "actual": self.actual, "passed": self.passed, "detail": self.detail}


def _preview(text: str) -> str:
    text = (text or "").replace("\r\n", "\n")
    if len(text) <= _PREVIEW:
        return text
    return text[:_PREVIEW] + f"…（共 {len(text)} 字符）"


def _value_of(a: Assertion) -> Any:
    return a.params.get("value")


def _fail(a: Assertion, expected: str, actual: str, **detail: Any) -> CheckResult:
    return CheckResult(a.type, expected, actual, False, detail)


def _ok(a: Assertion, expected: str, actual: str, **detail: Any) -> CheckResult:
    return CheckResult(a.type, expected, actual, True, detail)


# ═══════════════════════════════════════════════════════════════
# 文本类
# ═══════════════════════════════════════════════════════════════


def _check_contains(a: Assertion, out: EvalOutcome) -> CheckResult:
    needle = str(_value_of(a))
    exp = f"输出包含 {needle!r}"
    if needle and needle in (out.output or ""):
        return _ok(a, exp, "已包含")
    return _fail(a, exp, f"未包含；实际输出：{_preview(out.output)}")


def _check_not_contains(a: Assertion, out: EvalOutcome) -> CheckResult:
    needle = str(_value_of(a))
    exp = f"输出不含 {needle!r}"
    if not needle or needle not in (out.output or ""):
        return _ok(a, exp, "确实不含")
    idx = (out.output or "").find(needle)
    return _fail(a, exp, f"出现了；上下文：{_preview((out.output or '')[max(0, idx - 60):])}")


def _check_regex(a: Assertion, out: EvalOutcome) -> CheckResult:
    pattern = str(_value_of(a))
    exp = f"输出匹配正则 /{pattern}/"
    try:
        m = re.search(pattern, out.output or "")
    except re.error as e:
        # 正则写错是套件的问题，必须报出来（否则它永远"失败"，看起来像模型问题）
        return _fail(a, exp, f"正则本身非法：{e}")
    if m:
        return _ok(a, exp, f"命中 {m.group(0)[:80]!r}")
    return _fail(a, exp, f"未命中；实际输出：{_preview(out.output)}")


# ═══════════════════════════════════════════════════════════════
# 文件类
# ═══════════════════════════════════════════════════════════════


def _safe_rel(path: str) -> tuple[Path | None, CheckResult | None]:
    """把断言里的路径限制在任务工作区内（绝对路径/``..`` 直接判失败）。"""
    p = Path(path)
    if p.is_absolute() or ".." in p.parts:
        return None, CheckResult(
            "file_exists", f"工作区内的相对路径 {path!r}", "路径越界，已拒绝判定",
            False, {"reason": "escape"})
    return p, None


def _check_file_exists(a: Assertion, out: EvalOutcome, root: Path) -> CheckResult:
    raw = a.params.get("path", _value_of(a))
    rel = str(raw or "")
    min_bytes = int(a.params.get("min_bytes") or 0)
    exp = f"存在文件 {rel!r}" + (f"（至少 {min_bytes} 字节）" if min_bytes else "")
    rel_path, bad = _safe_rel(rel)
    if bad is not None:
        return _fail(a, exp, bad.actual, reason="escape")
    target = root / rel_path
    if not target.is_file():
        # 顺带回显工作区里实际有哪些文件 —— "到底写哪去了"是最常见的困惑
        return _fail(a, exp, "文件不存在", artifacts=sorted(out.artifacts)[:20])
    size = target.stat().st_size
    if min_bytes and size < min_bytes:
        return _fail(a, exp, f"文件存在但只有 {size} 字节", size=size)
    return _ok(a, exp, f"存在，{size} 字节", size=size)


# ═══════════════════════════════════════════════════════════════
# 行为类
# ═══════════════════════════════════════════════════════════════


def _match_args(actual: dict[str, Any], want: dict[str, Any]) -> bool:
    return all(k in actual and actual[k] == v for k, v in want.items())


def _check_tool_called(a: Assertion, out: EvalOutcome) -> CheckResult:
    name = str(a.params.get("name", _value_of(a) or ""))
    want_args = a.params.get("args") or a.params.get("arguments") or {}
    if not isinstance(want_args, dict):
        want_args = {}
    exp = f"调用过工具 {name!r}" + (f"（参数含 {want_args}）" if want_args else "")
    called = [tc.get("name", "") for tc in out.tool_calls]
    if name not in called:
        return _fail(a, exp, f"没有调用；实际调用序列：{called or '（一次都没有）'}")
    if not want_args:
        return _ok(a, exp, f"调用了 {called.count(name)} 次")
    for tc in out.tool_calls:
        if tc.get("name") == name and _match_args(tc.get("arguments") or {}, want_args):
            return _ok(a, exp, f"命中，参数={tc.get('arguments')}")
    same = [tc.get("arguments") for tc in out.tool_calls if tc.get("name") == name]
    return _fail(a, exp, f"调用了 {name}，但参数不匹配；实际参数：{same}")


# ═══════════════════════════════════════════════════════════════
# 成本类
# ═══════════════════════════════════════════════════════════════


def _check_max_seconds(a: Assertion, out: EvalOutcome) -> CheckResult:
    limit = float(_value_of(a) or 0)
    exp = f"耗时 ≤ {limit:g}s"
    if out.timed_out:
        return _fail(a, exp, f"任务超时（执行器在 {out.seconds:.1f}s 处中止）")
    if out.seconds <= limit:
        return _ok(a, exp, f"实际 {out.seconds:.2f}s")
    return _fail(a, exp, f"实际 {out.seconds:.2f}s，超出 {out.seconds - limit:.2f}s")


def _check_max_tokens(a: Assertion, out: EvalOutcome) -> CheckResult:
    limit = float(_value_of(a) or 0)
    exp = f"Token ≤ {limit:g}"
    if not out.total_tokens:
        # 拿不到用量时**判失败**：成本约束无法验证 ≠ 满足约束。
        # 静默通过会让"成本超支"永远不被发现，这正是评测最不该犯的错。
        return _fail(a, exp, "拿不到 token 用量（执行器未上报），无法确认成本上限",
                     prompt_tokens=out.prompt_tokens, completion_tokens=out.completion_tokens)
    if out.total_tokens <= limit:
        return _ok(a, exp, f"实际 {out.total_tokens}（prompt {out.prompt_tokens} + "
                           f"completion {out.completion_tokens}）")
    return _fail(a, exp, f"实际 {out.total_tokens}，超出 {out.total_tokens - int(limit)}",
                 prompt_tokens=out.prompt_tokens, completion_tokens=out.completion_tokens)


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════


def check_assertion(a: Assertion, out: EvalOutcome, workspace: str | Path) -> CheckResult:
    """判定一条断言。未知类型判失败并给出明确说明（绝不静默通过）。"""
    root = Path(workspace)
    if a.type == "contains":
        return _check_contains(a, out)
    if a.type == "not_contains":
        return _check_not_contains(a, out)
    if a.type == "regex":
        return _check_regex(a, out)
    if a.type == "file_exists":
        return _check_file_exists(a, out, root)
    if a.type == "tool_called":
        return _check_tool_called(a, out)
    if a.type == "max_seconds":
        return _check_max_seconds(a, out)
    if a.type == "max_tokens":
        return _check_max_tokens(a, out)
    return CheckResult(a.type, a.text, f"未知断言类型（支持：{sorted(ASSERTION_TYPES)}）",
                       False)


def check_all(assertions: list[Assertion], out: EvalOutcome,
              workspace: str | Path) -> list[CheckResult]:
    """逐条判定；单条断言内部异常不应让整场评测崩掉，而是记成失败。

    兜底分支自己也要足够稳：断言对象可能坏到连 ``type`` / ``text`` 都读不出来
    （自定义断言类写错）。那种情况下抛出去，整场评测只会得到一个 traceback、
    报告里空空如也 —— 比任何断言失败都难排查。
    """
    results: list[CheckResult] = []
    for a in assertions:
        try:
            results.append(check_assertion(a, out, workspace))
        except Exception as e:                   # 断言实现的 bug 也要可见
            try:
                kind, expected = a.type, a.text
            except Exception:
                kind, expected = "<?>", "<?>（断言对象本身不可读）"
            results.append(CheckResult(kind, expected,
                                       f"断言执行异常：{type(e).__name__}: {e}", False))
    return results
