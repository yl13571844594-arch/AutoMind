"""工作流文件加载 —— 解析 YAML/JSON，并把"哪一行错了"带出来。

为什么要自己走一遍 YAML 节点树（而不是 `yaml.safe_load`）
-------------------------------------------------------
`yaml.safe_load` 交给我们的是一堆普通 `str`/`int`/`dict`，**行号信息全丢了**。
而对工作流来说，"哪一行错了"就是可用性的全部：一份 200 行的流程文件里
只说"未知的步骤类型"，写文件的人得自己一行行找。

所以这里用 `yaml.compose` 拿到节点树，自己转成 Python 对象，同时：
  · 记下**每个容器**对象的行/列（`positions` 表，按 `id()` 索引）。
    标量没法挂属性，按 id 索引是唯一不侵入数据的做法 —— 且整份文档在
    schema 构建完之前一直存活，id 不会失效。
  · 顺手检出**重复键**。PyYAML 遇到重复键是"后者胜、前者静默消失"，
    于是 `id: fetch` 写两遍时，前一个步骤连同它的参数一起人间蒸发，
    而文件看起来还是对的。这类静默覆盖正是本模块要消灭的东西。

未知字段的策略
-------------
默认 **报错**（`unknown_fields="error"`）。理由见 schema.py：写错一个字段名
会让实际行为与文件不一致，而"跑的就是批的那版"是这套机制的全部意义。
需要放行（比如给工作流文件加私有注释字段）时显式打开 `"warning"`，
此时会在 `schema.warnings` 里如实列出。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from automind.workflow.exceptions import (
    Issue,
    Position,
    UnknownSchemaVersionError,
    WorkflowLoadError,
)
from automind.workflow.schema import (
    SUPPORTED_VERSIONS,
    UNKNOWN_FIELD_ERROR,
    UNKNOWN_FIELD_WARNING,
    WorkflowSchema,
    collect_issues,
)

#: 工作流文件的扩展名 —— CLI 据此决定用 YAML 还是 JSON 解析。
#: 两种格式共用同一套 schema：JSON 是 YAML 的子集，运维环境里
#: 生成 JSON 往往比生成 YAML 更省事（`json.dumps` 一定合法）。
YAML_SUFFIXES = (".yaml", ".yml")
JSON_SUFFIXES = (".json",)


# ═══════════════════════════════════════════════════════════════
# YAML → (Python 对象, 位置表)
# ═══════════════════════════════════════════════════════════════


class _DuplicateKeyError(Exception):
    """YAML 里出现重复键。内部异常，由 build 转成带行号的 Issue 列表。"""

    def __init__(self, key: str, position: Position, first: Position) -> None:
        super().__init__(key)
        self.key = key
        self.position = position
        self.first = first


def build_document(text: str, *, source: str = "") -> tuple[Any, dict[int, Position], list[Issue]]:
    """解析成 `(document, positions, issues)`。

    issues 里目前只会有重复键与结构异常；其余校验交给 `schema.collect_issues`。
    分开的理由：解析层的问题（YAML 语法、重复键）连"这是个什么结构"都还没确定，
    拿不到字段路径，报错形态自然不同。
    """
    issues: list[Issue] = []
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        position = Position(line=mark.line + 1, column=mark.column + 1) if mark else Position()
        problem = getattr(exc, "problem", None) or str(exc)
        issues.append(Issue(
            message=f"YAML 语法错误：{problem}",
            position=position, level="error", file=source))
        return None, {}, issues

    if root is None:
        issues.append(Issue(message="文件是空的：工作流至少要声明 version / name / steps",
                            position=Position(1, 1), level="error", file=source))
        return None, {}, issues

    positions: dict[int, Position] = {}
    try:
        document = _convert(root, positions)
    except _DuplicateKeyError as exc:
        issues.append(Issue(
            message=(f"重复的键 '{exc.key}'（本处第 {exc.position.line} 行，"
                     f"首次出现在第 {exc.first.line} 行）。YAML 对重复键的处理是"
                     "后者覆盖前者、前者静默消失 —— 一个写了两遍的步骤会连同它的"
                     "参数一起不见了，而文件看起来还是对的，因此这里直接报错"),
            position=exc.position, level="error", file=source))
        return None, positions, issues
    return document, positions, issues


def _convert(node: yaml.Node, positions: dict[int, Position]) -> Any:
    """把 YAML 节点树转成普通 Python 对象，同时记录容器位置、检出重复键。"""
    position = Position(line=node.start_mark.line + 1, column=node.start_mark.column + 1)
    if isinstance(node, yaml.MappingNode):
        out: dict[Any, Any] = {}
        first_seen: dict[Any, Position] = {}
        for key_node, value_node in node.value:
            key = _scalar(key_node)
            if key in out:
                raise _DuplicateKeyError(str(key), Position(
                    line=key_node.start_mark.line + 1,
                    column=key_node.start_mark.column + 1), first_seen[key])
            first_seen[key] = Position(line=key_node.start_mark.line + 1,
                                       column=key_node.start_mark.column + 1)
            out[key] = _convert(value_node, positions)
        positions[id(out)] = position
        return out
    if isinstance(node, yaml.SequenceNode):
        items = [_convert(item, positions) for item in node.value]
        positions[id(items)] = position
        return items
    return _scalar(node)


def _scalar(node: yaml.Node) -> Any:
    """标量节点 → Python 值。

    **不能用 `yaml.safe_load(node.value)`**：那会在**同一个字符串**上再起一个
    PyYAML Loader，而主 Loader 的 Composer 还停在这份输入的中间 ——
    第二次解析会把它的事件流搅乱，于是文件里只要有一个含 `{{ ... }}` 的标量
    （本项目的模板语法，几乎每份工作流都有），就会在第二次解析时炸出
    "expected '<document start>', but found '<scalar>'"。这个坑实测踩过。

    所以这里自己按 YAML 1.1 的核心规则解析标量：布尔、空值、整数、浮点，
    其余一律当字符串。与 `yaml.safe_load` 的差别只在两处，且都是有意为之：
      · `<<` 合并键不再展开（本格式用不到，且展开会让"哪个字段从哪来"变得难查）；
      · `1_000` / `0o17` 这类 YAML 1.1 冷门写法保持字符串（工作流里不会这么写数字）。
    字符串**不做 strip**：提示词里的缩进与换行是内容的一部分。
    """
    text = node.value
    if text is None:
        return None
    if node.style:                      # 带引号（单/双/块）→ 明示是字符串，不再猜
        return text
    lowered = text.strip().lower()
    if lowered in _YAML_BOOL_TRUE:
        return True
    if lowered in _YAML_BOOL_FALSE:
        return False
    if lowered in _YAML_NULL:
        return None
    if _INT_RE.match(text.strip()):
        try:
            return int(text.strip(), 10)
        except ValueError:                          # pragma: no cover - 正则已保证
            return text
    if _FLOAT_RE.match(text.strip()):
        try:
            return float(text.strip())
        except ValueError:                          # pragma: no cover - 正则已保证
            return text
    return text


#: YAML 1.1 的真值集合。刻意**不含** `y`/`n`/`on`/`off` 之外的单字母变体 ——
#: 这里跟随 PyYAML 的 SafeLoader，避免"我们认的 true 和别人不一样"。
_YAML_BOOL_TRUE = frozenset({"true", "yes", "on"})
_YAML_BOOL_FALSE = frozenset({"false", "no", "off"})
_YAML_NULL = frozenset({"", "~", "null"})
_INT_RE = re.compile(r"^[-+]?[0-9]+$")
_FLOAT_RE = re.compile(r"^[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?$")


# ═══════════════════════════════════════════════════════════════
# 对外入口
# ═══════════════════════════════════════════════════════════════


class WorkflowLoader:
    """工作流加载器。

    使用示例::

        loader = WorkflowLoader(tool_names=registry.list_names())
        schema = loader.load("change_request.yaml")     # 失败抛 WorkflowLoadError
        schema, errors, warnings = loader.try_load(...)  # 失败收集问题，不抛
    """

    def __init__(self, *, tool_names: list[str] | None = None,
                 unknown_fields: str = UNKNOWN_FIELD_ERROR) -> None:
        if unknown_fields not in (UNKNOWN_FIELD_ERROR, UNKNOWN_FIELD_WARNING):
            raise ValueError(f"unknown_fields 只能是 {UNKNOWN_FIELD_ERROR!r} 或 "
                             f"{UNKNOWN_FIELD_WARNING!r}，实际是 {unknown_fields!r}")
        self.tool_names = list(tool_names) if tool_names else None
        self.unknown_fields = unknown_fields

    # ── 文件入口 ──────────────────────────────────────

    def load(self, path: str | Path) -> WorkflowSchema:
        """读取并校验；有任何错误就抛 `WorkflowLoadError`（附全部问题）。"""
        schema, errors, _ = self.try_load(path)
        if schema is None:
            raise WorkflowLoadError(
                f"工作流文件校验失败：{path}",
                issues=errors, file=str(path),
                position=errors[0].position if errors else Position())
        return schema

    def try_load(self, path: str | Path) -> tuple[WorkflowSchema | None, list[Issue], list[Issue]]:
        """读取并校验；返回 `(schema | None, errors, warnings)`，**不抛异常**。

        为什么要有"不抛"的入口：CI 与前端批量校验时，需要的是"列出所有文件
        的所有问题"，而不是第一个文件就中断。
        """
        file_path = Path(path)
        source = str(file_path)
        if not file_path.exists():
            return None, [Issue(message=f"文件不存在：{source}", level="error", file=source)], []
        if file_path.is_dir():
            return None, [Issue(message=f"这是一个目录，不是一个工作流文件：{source}",
                                level="error", file=source)], []
        try:
            raw = file_path.read_bytes()
        except OSError as exc:
            return None, [Issue(message=f"无法读取文件：{exc}", level="error", file=source)], []

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            # 工作流文件一律要求 UTF-8：GBK 保存的中文提示词在别的机器上
            # 会变成乱码，而"乱码被当成提示词发给 LLM"极难排查。
            return None, [Issue(
                message=f"文件不是 UTF-8 编码（{exc}）。请另存为 UTF-8",
                level="error", file=source)], []

        digest = hashlib.sha256(raw).hexdigest()
        schema, errors, warnings = self.try_parse(
            text, source=source, source_digest=digest,
            is_json=file_path.suffix.lower() in JSON_SUFFIXES)
        return schema, errors, warnings

    # ── 文本入口（前端粘贴、测试、服务端收 YAML 时用）──────

    def parse(self, text: str, *, source: str = "") -> WorkflowSchema:
        """解析并校验一段文本；有错就抛。"""
        schema, errors, _ = self.try_parse(text, source=source)
        if schema is None:
            raise WorkflowLoadError(
                "工作流校验失败" + (f"：{source}" if source else ""),
                issues=errors, file=source,
                position=errors[0].position if errors else Position())
        return schema

    def try_parse(self, text: str, *, source: str = "", source_digest: str = "",
                  is_json: bool = False) -> tuple[WorkflowSchema | None, list[Issue], list[Issue]]:
        """解析并校验一段文本；返回 `(schema | None, errors, warnings)`。

        `is_json` 只影响**解析器**的选择，校验完全共用一套 —— 两种格式
        必须产生完全一样的结论，否则"用 JSON 写就能绕过校验"会立刻成为一个洞。
        """
        digest = source_digest or hashlib.sha256(text.encode("utf-8")).hexdigest()

        if is_json:
            try:
                document = json.loads(text)
            except json.JSONDecodeError as exc:
                return None, [Issue(
                    message=f"JSON 语法错误：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）",
                    position=Position(line=exc.lineno, column=exc.colno),
                    level="error", file=source)], []
            positions: dict[int, Position] = {}
        else:
            document, positions, parse_issues = build_document(text, source=source)
            if parse_issues:
                return None, parse_issues, []

        schema, errors, warnings = collect_issues(
            document, positions=positions, source=source, source_digest=digest,
            tool_names=self.tool_names, unknown_fields=self.unknown_fields)

        for issue in errors + warnings:
            issue.file = source
        if schema is not None and errors:
            # 理论上 collect_issues 有错时返回 None；这里兜一层，
            # 保证"有 error 就一定没有 schema"这个不变量不被将来的改动破坏。
            schema = None
        return schema, errors, warnings


def load_workflow(path: str | Path, *, tool_names: list[str] | None = None,
                  unknown_fields: str = UNKNOWN_FIELD_ERROR) -> WorkflowSchema:
    """便捷函数：加载一份工作流，失败抛 `WorkflowLoadError`。"""
    return WorkflowLoader(tool_names=tool_names, unknown_fields=unknown_fields).load(path)


def validate_file(path: str | Path, *, tool_names: list[str] | None = None,
                  unknown_fields: str = UNKNOWN_FIELD_ERROR
                  ) -> tuple[WorkflowSchema | None, list[Issue], list[Issue]]:
    """便捷函数：只校验不抛异常（CI / 批量校验用）。"""
    return WorkflowLoader(tool_names=tool_names,
                          unknown_fields=unknown_fields).try_load(path)


def load_version_checked(text: str, *, source: str = "") -> WorkflowSchema:
    """解析并**先把版本号挑出来**，版本不认识时抛 `UnknownSchemaVersionError`。

    为什么需要这个入口：调用方（服务端 / CI）对"版本不认识"和"文件写错了"
    要给出完全不同的指引 —— 前者是"请升级 AutoMind / 这份流程属于更新的 schema"，
    后者是"请改工作流"。若只有一种异常类型，这两句提示就分不出来。

    校验口径与 `parse` 完全一致：这里只是**先单独确认一次版本**，
    再走同一条校验路径，不存在"版本先过了、后面又按另一套语义解释"的可能。
    """
    source = source or ""
    is_json = source.lower().endswith(JSON_SUFFIXES)
    version, doc_issues, position = _peek_version(text, is_json=is_json, source=source)
    if doc_issues:
        # 连文档结构都没解析出来（YAML 语法错等）→ 直接报加载失败
        raise WorkflowLoadError("工作流校验失败", issues=doc_issues, file=source,
                                position=position or Position())
    if version is None or version not in SUPPORTED_VERSIONS:
        supported = "、".join(str(v) for v in SUPPORTED_VERSIONS)
        detail = ("缺少 version 字段" if version is None
                  else f"不支持的 schema 版本：{version}")
        raise UnknownSchemaVersionError(
            f"{detail}。本实现仅支持 {supported}；遇到不认识的版本必须明确报错，"
            "绝不按相近版本执行",
            issues=[Issue(message=f"{detail}（本实现仅支持 {supported}）",
                          path="version", position=position or Position(), file=source)],
            position=position, path="version", file=source)

    loader = WorkflowLoader()
    schema, errors, _ = loader.try_parse(text, source=source, is_json=is_json)
    if schema is None:
        raise WorkflowLoadError("工作流校验失败", issues=errors, file=source,
                                position=errors[0].position if errors else Position())
    return schema


def _peek_version(text: str, *, is_json: bool,
                  source: str) -> tuple[int | None, list[Issue], Position]:
    """只把 `version` 挑出来看一眼；返回 `(版本 | None, 解析层问题, 位置)`。

    `None` 同时表示"没写 version"与"写了但不是整数"——调用方给的都是
    "必须显式声明版本"这一类指引，不需要在此细分（细分由 collect_issues 出完整报告）。
    """
    if is_json:
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, [Issue(
                message=f"JSON 语法错误：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）",
                position=Position(line=exc.lineno, column=exc.colno),
                level="error", file=source)], Position(line=exc.lineno, column=exc.colno)
        if not isinstance(document, dict):
            return None, [], Position()
        raw = document.get("version")
        return (raw if isinstance(raw, int) and not isinstance(raw, bool) else None), [], Position()

    document, positions, parse_issues = build_document(text, source=source)
    if parse_issues:
        return None, parse_issues, parse_issues[0].position
    if not isinstance(document, dict):
        return None, [], Position()
    raw = document.get("version")
    ok = isinstance(raw, int) and not isinstance(raw, bool)
    return (raw if ok else None), [], positions.get(id(raw), Position())


__all__ = [
    "JSON_SUFFIXES",
    "YAML_SUFFIXES",
    "WorkflowLoader",
    "build_document",
    "load_version_checked",
    "load_workflow",
    "validate_file",
]
