"""评测套件格式（YAML）—— 定义、解析与校验。

格式设计的一条硬规则：**写错的断言必须当场报错**。

理由：评测最容易骗人的形态是"静默失效"——把 ``contains`` 写成了 ``contain``，
解析器如果忽略未知键，这条断言就永远通过，报告里是一片绿色，而它什么都没检查。
因此本模块对未知键、缺失必填项、类型错误一律抛 :class:`SuiteError` 并指出行位置
（YAML 的 ``line`` 由 PyYAML 的 compose 阶段给出）。

一个套件长这样::

    name: smoke
    description: 冒烟评测（需要配置任一提供商的 API Key）
    mode: coding                 # 套件默认交互模式，任务可各自覆盖
    cases:
      - id: write_hello
        prompt: 在当前目录创建 hello.txt，内容为 hello
        mode: coding
        expect:
          - tool_called: file_write
          - file_exists: hello.txt
            min_bytes: 5
          - contains: hello
          - not_contains: traceback
          - regex: "hello"
          - max_seconds: 120
          - max_tokens: 60000

断言支持列表/字典/标量三种写法，彼此等价::

    expect:
      contains: [done, 完成]        # 列表 = 多条断言
      tool_called: file_write      # 标量
      file_exists:                 # 字典 = 带参数
        path: hello.txt
        min_bytes: 5
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 已知的断言类型 → 是否需要参数。
#: 不在表里的键一律报错（拼错 ``contians`` 比"少写一条断言"危险得多）。
ASSERTION_TYPES: dict[str, bool] = {
    "contains": True,          # 子串必须出现
    "not_contains": True,      # 子串必须不出现
    "regex": True,             # 正则必须匹配
    "file_exists": True,       # 相对 project_root 的文件必须存在（可带 min_bytes）
    "tool_called": True,       # 必须调用过某工具（可带参数子集 args）
    "max_seconds": True,       # 单任务耗时上限
    "max_tokens": True,        # 单任务 Token 上限（成本控制）
}

#: 套件/任务级别允许出现的键（未知键报错，理由同上：typo 会静默改变行为）
_CASE_KEYS = {"id", "prompt", "mode", "expect", "setup", "description",
              "timeout_seconds", "expect_fail"}
_SUITE_KEYS = {"name", "description", "mode", "cases", "setup", "timeout_seconds"}

MODES = ("chat", "work", "coding", "multi", "loop")


class SuiteError(ValueError):
    """套件格式错误 —— 带上出错位置，便于直接改文件。"""

    def __init__(self, message: str, where: str = "") -> None:
        super().__init__(f"{message}（{where}）" if where else message)
        self.where = where


@dataclass
class Assertion:
    """一条断言：类型 + 参数 + 原始写法（报告里要原样回显，便于对着 YAML 改）。"""

    type: str
    params: dict[str, Any] = field(default_factory=dict)
    raw: str = ""

    @property
    def text(self) -> str:
        """人类可读的"期望什么"（报告用）。"""
        if self.raw:
            return self.raw
        if not self.params:
            return self.type
        inner = ", ".join(f"{k}={v!r}" for k, v in self.params.items())
        return f"{self.type}({inner})"


@dataclass
class EvalCase:
    """一个评测任务。"""

    id: str
    prompt: str
    mode: str = "coding"
    assertions: list[Assertion] = field(default_factory=list)
    setup: dict[str, str] = field(default_factory=dict)
    description: str = ""
    timeout_seconds: float = 0.0
    #: 反向断言：**期望这条任务有断言失败**。
    #: 存在的理由：评测框架最危险的失效形态是"断言永远通过"（写错的断言、
    #: 接错的执行器），它会让报告全绿而什么都没检查。带一条 ``expect_fail``
    #: 的任务等于给框架本身做体检 —— 如果它竟然通过了，说明判定链路断了。
    expect_fail: bool = False


@dataclass
class EvalSuite:
    """一个套件（一批任务）。"""

    name: str
    cases: list[EvalCase]
    description: str = ""
    mode: str = "coding"
    setup: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 0.0
    path: str = ""

    def total_assertions(self) -> int:
        return sum(len(c.assertions) for c in self.cases)


# ═══════════════════════════════════════════════════════════════
# 断言解析
# ═══════════════════════════════════════════════════════════════


def _parse_one(kind: Any, value: Any, where: str) -> list[Assertion]:
    """把 ``kind: value`` 展开成一条或多条断言（列表则展开）。"""
    key = str(kind).strip()
    if key not in ASSERTION_TYPES:
        raise SuiteError(
            f"未知的断言类型 '{key}'。支持的写法："
            f"{', '.join(sorted(ASSERTION_TYPES))}", where)
    if isinstance(value, list):
        out: list[Assertion] = []
        for item in value:
            out.extend(_parse_one(key, item, where))
        return out
    if isinstance(value, dict):
        params = dict(value)
        params.pop("type", None)          # 允许 {type: contains, value: x} 式写法
        if "value" in params and len(params) == 1:
            params = {"value": params["value"]}
        return [Assertion(type=key, params=params,
                          raw=f"{key}: {_inline(params)}")]
    if value is None:
        raise SuiteError(f"断言 '{key}' 缺少值", where)
    if key in ("max_seconds", "max_tokens"):
        try:
            num = float(value)
        except (TypeError, ValueError) as e:
            raise SuiteError(f"断言 '{key}' 需要数字，收到 {value!r}", where) from e
        return [Assertion(type=key, params={"value": num}, raw=f"{key}: {num:g}")]
    return [Assertion(type=key, params={"value": value}, raw=f"{key}: {value!r}")]


def _inline(params: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in params.items())


def parse_expect(raw: Any, where: str) -> list[Assertion]:
    """解析 ``expect`` —— 支持列表、字典、单条标量。"""
    if raw is None:
        return []
    if isinstance(raw, dict):
        out: list[Assertion] = []
        for k, v in raw.items():
            out.extend(_parse_one(k, v, where))
        return out
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, dict):
                # 至少要有一种形态：{contains: x} / {type: contains, value: x}
                if "type" in item and "value" in item:
                    out.extend(_parse_one(item["type"], item["value"], where))
                    continue
                for k, v in item.items():
                    out.extend(_parse_one(k, v, where))
                continue
            raise SuiteError(f"expect 列表里的元素必须是映射，收到 {item!r}", where)
        return out
    raise SuiteError("expect 必须是映射或列表", where)


def parse_setup(raw: Any, where: str) -> dict[str, str]:
    """解析 ``setup``：``{相对路径: 文件内容}``，任务开始前预置文件。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SuiteError("setup 必须是映射：{相对路径: 内容}", where)
    out: dict[str, str] = {}
    for k, v in raw.items():
        out[str(k)] = "" if v is None else str(v)
    return out


# ═══════════════════════════════════════════════════════════════
# 套件解析
# ═══════════════════════════════════════════════════════════════


def _line_of(node: Any) -> int:
    mark = getattr(node, "start_mark", None)
    return int(getattr(mark, "line", -1)) + 1 if mark else -1


def _load_yaml(text: str, path: str) -> tuple[Any, Any]:
    """加载 YAML，返回 ``(纯数据, 原始节点树)`` —— 节点树用于报行号。"""
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SuiteError(f"YAML 解析失败：{e}", path) from e
    try:
        node = yaml.compose(text)
    except yaml.YAMLError:
        node = None
    return data, node


def _case_nodes(node: Any) -> list[Any]:
    """取出 ``cases:`` 列表里每个元素的**键节点**（用于报行号）。

    取键节点而不是映射节点：映射的 ``start_mark`` 指向缩进位置，而用户要改的
    是 ``- id: xxx`` 那一行。差一两行会让"请直接改文件"的提示变成一次找茬。
    """
    if node is None or not hasattr(node, "value"):
        return []
    for k, v in node.value:
        if getattr(k, "value", None) == "cases" and getattr(v, "value", None):
            return [item for item in v.value]
    return []


def _case_key_node(item: Any) -> Any:
    """从一个 cases 元素（映射节点）里取 ``id`` 的键节点，取不到就退回自身。"""
    if item is None or not hasattr(item, "value"):
        return item
    for k, _v in item.value:
        if getattr(k, "value", None) == "id":
            return k
    return item


def load_suite(path: str | Path) -> EvalSuite:
    """读取并校验一个套件文件；任何格式问题都抛 :class:`SuiteError`。"""
    p = Path(path)
    if not p.is_file():
        raise SuiteError(f"套件文件不存在：{p}")
    text = p.read_text(encoding="utf-8")
    data, node = _load_yaml(text, str(p))
    if not isinstance(data, dict):
        raise SuiteError("套件顶层必须是映射（name/cases/...）", str(p))

    unknown = set(data) - _SUITE_KEYS
    if unknown:
        raise SuiteError(
            f"套件里有未知字段 {sorted(unknown)}；支持：{sorted(_SUITE_KEYS)}", str(p))

    cases_raw = data.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise SuiteError("套件必须包含非空的 cases 列表", str(p))

    suite_mode = str(data.get("mode") or "coding")
    if suite_mode not in MODES:
        raise SuiteError(f"未知 mode '{suite_mode}'，应为 {MODES}", str(p))
    suite_timeout = float(data.get("timeout_seconds") or 0.0)
    suite_setup = parse_setup(data.get("setup"), str(p))

    nodes = _case_nodes(node)
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for i, raw in enumerate(cases_raw):
        where = f"{p}: cases[{i}]"
        if not isinstance(raw, dict):
            raise SuiteError(f"任务必须是映射，收到 {raw!r}", where)
        ln = _line_of(_case_key_node(nodes[i])) if i < len(nodes) else -1
        if ln > 0:
            where = f"{p}:{ln}"
        bad = set(raw) - _CASE_KEYS
        if bad:
            raise SuiteError(
                f"任务里有未知字段 {sorted(bad)}；支持：{sorted(_CASE_KEYS)}", where)
        cid = str(raw.get("id") or "").strip()
        if not cid:
            raise SuiteError("任务缺少 id", where)
        if cid in seen:
            raise SuiteError(f"任务 id 重复：'{cid}'（报告与 --include 都靠它定位）", where)
        seen.add(cid)
        prompt = str(raw.get("prompt") or "").strip()
        if not prompt:
            raise SuiteError(f"任务 '{cid}' 缺少 prompt", where)
        mode = str(raw.get("mode") or suite_mode)
        if mode not in MODES:
            raise SuiteError(f"未知 mode '{mode}'，应为 {MODES}", where)
        cases.append(EvalCase(
            id=cid, prompt=prompt, mode=mode,
            assertions=parse_expect(raw.get("expect"), where),
            setup={**suite_setup, **parse_setup(raw.get("setup"), where)},
            description=str(raw.get("description") or ""),
            timeout_seconds=float(raw.get("timeout_seconds") or suite_timeout or 0.0),
            expect_fail=bool(raw.get("expect_fail") or False),
        ))

    return EvalSuite(
        name=str(data.get("name") or p.stem),
        cases=cases,
        description=str(data.get("description") or ""),
        mode=suite_mode,
        setup=suite_setup,
        timeout_seconds=suite_timeout,
        path=str(p),
    )


def find_suites(root: str | Path | None = None) -> list[Path]:
    """列出内置套件目录下的 ``*.yml``（供 ``GET /api/eval/suites`` 使用）。"""
    base = Path(root) if root else Path(__file__).parent / "suites"
    if not base.is_dir():
        return []
    return sorted(base.glob("*.yml")) + sorted(base.glob("*.yaml"))
