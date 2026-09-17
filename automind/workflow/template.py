"""模板渲染 —— 严格、可枚举、**不求值**。

为什么要有这个模块（而不是直接上 Jinja2）
----------------------------------------
工作流文件是**客户能读到、能评审**的交付物。一旦模板支持表达式求值，
这份文件就不再是"流程描述"，而是一段**代码**：

* 评审方看不懂 `{{ ''.__class__.__mro__ }}` 这类东西，评审就退化成"信任"；
* 模板渲染发生在**执行器进程里**，它能读到环境变量、能拿到工具输出；
  一个能做属性链求值的引擎就是一条现成的代码执行通道；
* 更现实的问题：客户环境里未必装了 Jinja2，而 pyyaml 是核心依赖
  （见 pyproject.toml），工作流不该再多拖一个模板引擎的版本风险。

因此这里只实现**四种变量引用 + 受限比较**，并且**只做取值，不做求值**：

    {{ inputs.ticket_id }}          声明的入参
    {{ steps.fetch.output }}        某步骤的输出（可继续 .foo / [0] 往下取）
    {{ steps.fetch.error }}         某步骤的失败原因
    {{ env.HOME }}                  进程环境变量（必须存在）

严格模式（默认且唯一）
--------------------
引用不存在的变量**直接抛 TemplateError**，绝不渲染成空字符串。

这条不是洁癖：在自动化里，"渲染成空串"会一路往下走 —— URL 变成
`https://itsm/api/tickets/`、SQL 变成 `WHERE id = `、变更单正文少了一段，
于是在**远端系统上留下一个看起来正常、实际串味的操作**。等到人发现时，
错的不是模板，是已经改掉的生产数据。宁可当场停。

报错时一定会附上"当前可用变量清单"：给人看是"你还能写哪些"，
给模型看是"照抄这个清单"，两边都比一句 undefined 有用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from automind.workflow.exceptions import Position, TemplateError, suggest

# ═══════════════════════════════════════════════════════════════
# 语法
# ═══════════════════════════════════════════════════════════════

#: `{{ ... }}`。非贪婪，且不允许跨 `}}` —— 嵌套花括号会被判为非法表达式。
PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)

#: 变量路径的合法形状：``root.key`` / ``root.key[0]`` / ``root["key with space"]``
_PATH = re.compile(
    r"""^\s*
        (?P<root>[A-Za-z_][A-Za-z0-9_]*)
        (?P<rest>(?:\s*\.\s*[A-Za-z_][A-Za-z0-9_-]*
                 |\s*\[\s*(?:\d+|'[^']*'|"[^"]*")\s*\])*)
        \s*$""",
    re.VERBOSE,
)

#: 路径分段的两种写法：`.key` 与 `[0]` / `['key']` / `["key"]`。
#: 用三重引号包住是为了让里面的单双引号都能原样写 —— 这个正则要匹配引号本身，
#: 用普通单/双引号串起来会一路反斜杠，反而看不出它在匹配什么。
_PART = re.compile(r"""\.\s*([A-Za-z_][A-Za-z0-9_-]*)|\[\s*(\d+|'[^']*'|"[^"]*")\s*\]""")

#: 每个模板里占位符的条数上限。挡住"一个模板套几万个变量"的退化情形
#: （比如误把整份日志塞进 prompt），它会让渲染本身变成拒绝服务。
MAX_PLACEHOLDERS = 200

#: 环境变量的白名单形状。为什么限制得这么死：`env.*` 是本实现唯一能读到
#: **执行器进程**信息的入口，放开了就等于让工作流文件随意读宿主环境。
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: 转义写法：`\{{` 渲染成字面量 `{{`。为什么需要它：工作流里给 LLM 的提示词
#: 经常要示范"模板长什么样"，没有转义就只能靠拆字符串绕开。
ESCAPED_OPEN = "\\{{"


@dataclass(frozen=True)
class Token:
    """一个 `{{ ... }}` 占位符。"""

    raw: str                                   # 花括号内的原文（已 strip）
    path: tuple[str, ...] = ()                 # 解析出的取值路径，如 ("steps","a","output")
    #: 解析失败时的原始文本（用于报错）
    invalid: str = ""

    @property
    def is_valid(self) -> bool:
        return not self.invalid

    def render_path(self) -> str:
        return ".".join(self.path) if self.path else self.raw


@dataclass
class TemplateProblem:
    """加载期发现的模板问题（由 schema 汇总成 Issue）。"""

    message: str
    path: str = ""
    #: unknown_step / forward_reference / unknown_input / bad_env / syntax / not_a_variable
    kind: str = "syntax"
    suggestions: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# 解析
# ═══════════════════════════════════════════════════════════════


def find_placeholders(text: str) -> list[Token]:
    """把文本里的占位符全部找出来（不渲染）。

    先处理转义：`\\{{` 是字面量，不参与后续匹配。做法是把转义对替换成一个
    不可能出现在正文里的哨兵，解析完再换回 —— 比手写一个扫描器简单得多，
    也更容易看出"哪些位置被保护了"。
    """
    if not text:
        return []
    protected, sentinel = _protect(text)
    tokens: list[Token] = []
    for match in PLACEHOLDER.finditer(protected):
        raw = match.group(1).strip()
        path = _parse_path(raw)
        tokens.append(Token(raw=raw, path=path or (), invalid="" if path else raw))
    if len(tokens) > MAX_PLACEHOLDERS:
        raise TemplateError(
            f"单个模板里的占位符过多（{len(tokens)} 个，上限 {MAX_PLACEHOLDERS}）",
            expression=_clip(text))
    return tokens


def _protect(text: str) -> tuple[str, str]:
    """把 `\\{{` 换成哨兵，避免被当成占位符。

    哨兵用 \\x00（YAML/JSON 文本里不可能出现的控制字符），且必须在
    `find_placeholders` 之后由 `render` 还原 —— 两处共用这一个函数，
    保证"解析看到的文本"和"渲染用的文本"是同一份。
    """
    sentinel = "\x00"
    return text.replace(ESCAPED_OPEN, sentinel), sentinel


def _parse_path(raw: str) -> tuple[str, ...] | None:
    """把 `steps.a.output[0].name` 解析成路径元组；不是纯变量引用就返回 None。

    返回 None 的典型输入：``1 + 1``、``os.system('x')``、``a or b``。
    这些**不是"暂不支持"，是"刻意不支持"**（见模块开头）。
    """
    match = _PATH.match(raw)
    if not match:
        return None
    parts: list[str] = [match.group("root")]
    for part in _PART.finditer(match.group("rest") or ""):
        dotted, bracketed = part.group(1), part.group(2)
        if dotted is not None:
            parts.append(dotted)
        elif bracketed is not None:
            parts.append(bracketed.strip("'\""))
    return tuple(parts)


def _clip(text: str, limit: int = 120) -> str:
    one_line = " ".join(str(text).split())
    return one_line if len(one_line) <= limit else one_line[:limit] + "…"


# ═══════════════════════════════════════════════════════════════
# 加载期校验
# ═══════════════════════════════════════════════════════════════


def validate_template(text: str, *, inputs: set[str], step_ids: set[str],
                      order: dict[str, int] | None = None,
                      current_index: int | None = None) -> list[TemplateProblem]:
    """静态检查一个模板串；返回全部问题（空列表 = 没问题）。

    能查的都在这儿查掉：变量名拼错、引用不存在的步骤、`env` 名字写错、
    写出了表达式。**必须在加载期查**——等执行到第 5 步才发现第 1 步的模板
    引用错了，前 4 步的副作用（可能已经改了生产系统）就白做了。

    传入 `order`（步骤 id → 声明下标）与 `current_index` 时，还会区分出
    "引用了排在自己**后面**的步骤"（`forward_reference`）—— 它结构合法，
    只是当前执行路径上多半还没跑，因此由调用方降级成 warning 而不是拒绝加载。
    """
    problems: list[TemplateProblem] = []
    try:
        tokens = find_placeholders(text)
    except TemplateError as exc:                       # 占位符数量超限
        return [TemplateProblem(message=exc.message, kind="syntax")]

    for token in tokens:
        if not token.is_valid:
            problems.append(TemplateProblem(
                message=(f"无法解析的模板占位符：{{{{ {token.raw} }}}}。"
                         "本实现只做变量取值，**不支持表达式求值**"
                         "（没有算术、比较、函数调用、and/or）；"
                         "需要计算请放到上游步骤里用工具完成"),
                path=token.raw, kind="not_a_variable"))
            continue

        root = token.path[0]
        if root == "inputs":
            if len(token.path) < 2:
                problems.append(TemplateProblem(
                    message="inputs 引用缺少入参名，写法应为 {{ inputs.名字 }}",
                    path=token.render_path(), kind="syntax"))
                continue
            name = token.path[1]
            if name not in inputs:
                problems.append(TemplateProblem(
                    message=f"引用了未声明的入参 '{name}'。请在 inputs 段声明它"
                            "（含 type / required / default）",
                    path=token.render_path(), kind="unknown_input",
                    suggestions=suggest(name, sorted(inputs))))
        elif root == "steps":
            if len(token.path) < 3:
                problems.append(TemplateProblem(
                    message=("steps 引用不完整，写法应为 "
                             "{{ steps.<步骤id>.output }} 或 {{ steps.<步骤id>.error }}"),
                    path=token.render_path(), kind="syntax"))
                continue
            step_id, field_name = token.path[1], token.path[2]
            if field_name not in ("output", "error"):
                problems.append(TemplateProblem(
                    message=(f"steps.{step_id}.{field_name} 不是可引用的字段："
                             "一个步骤只暴露 output（成功时的结果）与 error（失败原因）"),
                    path=token.render_path(), kind="syntax",
                    suggestions=suggest(field_name, ["output", "error"])))
                continue
            if step_id not in step_ids:
                problems.append(TemplateProblem(
                    message=f"引用了不存在的步骤 '{step_id}'",
                    path=token.render_path(), kind="unknown_step",
                    suggestions=suggest(step_id, sorted(step_ids))))
            elif (order is not None and current_index is not None
                  and order.get(step_id, -1) > current_index):
                problems.append(TemplateProblem(
                    message=(f"引用了排在后面的步骤 '{step_id}' 的输出。"
                             "工作流严格按文件顺序执行，这一步跑到时它还没执行，"
                             "取不到值"),
                    path=token.render_path(), kind="forward_reference"))
        elif root == "env":
            if len(token.path) < 2:
                problems.append(TemplateProblem(
                    message="env 引用缺少变量名，写法应为 {{ env.NAME }}",
                    path=token.render_path(), kind="syntax"))
                continue
            name = token.path[1]
            if not ENV_NAME.match(name):
                problems.append(TemplateProblem(
                    message=(f"环境变量名 '{name}' 不合法：只允许大写字母、数字与下划线"
                             "（如 {{ env.ITSM_TOKEN }}）。环境变量是工作流能读到的"
                             "宿主信息，故刻意收紧格式"),
                    path=token.render_path(), kind="bad_env"))
        else:
            # 根写错最常见的形态是"把变量名当根用"：`{{ ticket_id }}` 而不是
            # `{{ inputs.ticket_id }}`。光报"未知根变量"没用，得指出它**本该**
            # 挂在哪个根下面 —— 命中了就直说，没命中才给通用候选。
            anchored = _guess_root(root, inputs=inputs, step_ids=step_ids)
            hint = f"，它看起来是 {'、'.join(anchored)} 下的变量" if anchored else ""
            problems.append(TemplateProblem(
                message=(f"未知的模板根变量 '{root}'{hint}。可用的根变量只有三个："
                         "inputs（入参）、steps（步骤结果）、env（环境变量）"),
                path=token.render_path(), kind="syntax",
                suggestions=anchored or ["inputs", "steps", "env"]))
    return problems


def _guess_root(root: str, *, inputs: set[str], step_ids: set[str]) -> list[str]:
    """猜一个"写错的根"本该属于哪个根变量。

    只在能确定时才给：名字命中某个已声明的入参或某个已存在的步骤 id
    （含"少写半截"的情形，如 `ticket` → `ticket_id`），说明作者想写的其实就是
    那个变量、只是漏了根前缀。猜不出来就返回空列表 —— 不许瞎猜，
    给错建议比不给建议更糟（人会照着改到别的地方去）。
    """
    hits: list[str] = []
    if inputs and suggest(root, sorted(inputs), limit=1):
        hits.append("inputs")
    if step_ids and suggest(root, sorted(step_ids), limit=1):
        hits.append("steps")
    if ENV_NAME.match(root):                 # 全大写含下划线 → 大概率想读环境变量
        hits.append("env")
    return hits


# ═══════════════════════════════════════════════════════════════
# 渲染
# ═══════════════════════════════════════════════════════════════


def render(text: str, context: dict[str, Any], *,
           position: Position | None = None, path_name: str = "",
           strict: bool = True) -> Any:
    """渲染一个模板串。

    两种返回形态，取决于模板的**整体形状**：

    * 整串就是一个占位符（``"{{ inputs.count }}"``）→ 返回**原值**（int 还是 int、
      dict 还是 dict）。这条很重要：`args: {timeout: "{{ inputs.s }}"}` 里
      渲染成字符串 "30" 会让工具拿到字符串类型，行为与预期不符；
    * 其余情况 → 返回**字符串**（多占位符或带前后缀时只能是字符串）。

    `strict=True`（默认）时任何取不到的值都抛错；本实现**不提供**严格模式
    之外的开关，`strict` 参数只用于内部测试与显式降级的场景，
    传 False 时未定义变量渲染成空串 —— 那正是本模块开头说明要避免的行为，
    生产路径一律走默认值。
    """
    if not isinstance(text, str) or not text:
        return text

    protected, sentinel = _protect(text)

    # 整串单占位符 → 保留原始类型。
    # 判据是"这一个 match 恰好覆盖整串"，而不是 `PLACEHOLDER.fullmatch(protected)`：
    # 后者在含多个占位符时也能匹配到从第一个 `{{` 到**最后一个** `}}` 的整段，
    # 于是 `"{{ a }}/x/{{ b }}"` 会被误判成单个占位符。实测踩过这个坑。
    for match in PLACEHOLDER.finditer(protected):
        if match.start() != 0 or match.end() != len(protected):
            break
        raw = match.group(1).strip()
        parsed = _parse_path(raw)
        if parsed is None:
            raise TemplateError(
                f"无法解析的模板占位符：{{{{ {raw} }}}}。本实现只做变量取值，"
                "不支持表达式求值", position=position, path=path_name, expression=_clip(raw))
        return _lookup(parsed, context, position=position, path_name=path_name,
                       expression=raw, available=_available(context), strict=strict)

    def _sub(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        parsed = _parse_path(raw)
        if parsed is None:
            raise TemplateError(
                f"无法解析的模板占位符：{{{{ {raw} }}}}。本实现只做变量取值，"
                "不支持表达式求值（没有算术、函数调用、and/or）",
                position=position, path=path_name, expression=_clip(raw))
        value = _lookup(parsed, context, position=position, path_name=path_name,
                        expression=raw, available=_available(context), strict=strict)
        return _to_text(value)

    result = PLACEHOLDER.sub(_sub, protected)
    return result.replace(sentinel, "{{")          # 还原转义的字面量


def render_structure(value: Any, context: dict[str, Any], *,
                     position: Position | None = None, path_name: str = "",
                     strict: bool = True) -> Any:
    """递归渲染任意结构（dict / list / str），保留原有类型。

    工具的 `args` 是嵌套的（`json_body: {...}` 里还有模板），所以渲染必须
    递归 —— 只处理顶层字符串会让最深处的那个 URL 悄悄保持原样（带着 `{{ }}`
    发出去），而对方系统多半会"收下一个字面量花括号"并在日志里留下一串噪音。
    """
    if isinstance(value, str):
        return render(value, context, position=position, path_name=path_name, strict=strict)
    if isinstance(value, dict):
        return {k: render_structure(v, context, position=position,
                                    path_name=f"{path_name}.{k}" if path_name else str(k),
                                    strict=strict)
                for k, v in value.items()}
    if isinstance(value, list):
        return [render_structure(v, context, position=position,
                                 path_name=f"{path_name}[{i}]" if path_name else f"[{i}]",
                                 strict=strict)
                for i, v in enumerate(value)]
    return value


def _available(context: dict[str, Any]) -> list[str]:
    """当前可用的变量清单（报错时附上）。

    只列到"根 + 一级键"，不把整棵子树铺开：报错信息本身也要能读。
    """
    out: list[str] = []
    for root, value in context.items():
        if isinstance(value, dict) and value:
            for key in list(value)[:40]:
                out.append(f"{root}.{key}")
        else:
            out.append(root)
    return sorted(out)


def _lookup(path_1: tuple[str, ...], context: dict[str, Any], *,
            position: Position | None = None, path_name: str = "", expression: str = "",
            available: list[str], strict: bool = True) -> Any:
    """按路径取值。取不到就抛 TemplateError（严格模式）。

    形参名 `path_1` 是刻意的：这个模块里 `path` 同时被用来表示"变量路径元组"
    （模板语法的解析结果）与"字段路径字符串"（YAML 里哪个字段），两者同名
    会在调用点撞出 `got multiple values for argument 'path'` —— 实测踩过一次。
    这里把"变量路径"叫 `path_1`、"字段路径"统一叫 `path_name`，不再混用。
    """
    current: Any = context
    walked: list[str] = []
    for part in path_1:
        nxt, ok, detail = _step(current, part)
        walked.append(part)
        if not ok:
            if not strict:
                return ""
            written = ".".join(path_1)
            raise TemplateError(
                f"模板引用了不存在的变量：{{{{ {written} }}}} —— 在 {'.'.join(walked)} 处{detail}",
                position=position, path=path_name or written,
                expression=expression or written, available=available,
                suggestions=suggest(part, _keys(current)))
        current = nxt
    return current


def _step(current: Any, part: str) -> tuple[Any, bool, str]:
    """在 current 上取下 part，返回 `(值, 是否成功, 失败说明)`。"""
    if isinstance(current, dict):
        try:
            # 刻意用 `[]` 而不是先 `in` 再取：`__contains__` 不会触发 dict 的
            # `__missing__`，于是"任意键都取得到"的占位对象（dry-run 用的
            # _DryRunPlaceholder）会被判成缺键。用下标才会走到 `__missing__`。
            return current[part], True, ""
        except KeyError:
            return None, False, (f"取不到键 '{part}'"
                                 + (f"（该层可用键：{'、'.join(_keys(current))}）"
                                    if current else "（该层是空映射）"))
    if isinstance(current, (list, tuple)):
        if part.isdigit():
            idx = int(part)
            if 0 <= idx < len(current):
                return current[idx], True, ""
            return None, False, f"下标越界：{part}（该列表长度为 {len(current)}）"
        # 对列表取非数字键是常见笔误（把列表当映射用了），单独说清
        return None, False, (f"'{part}' 不是列表下标（此处是一个长度为 {len(current)} 的列表，"
                             "应写 [0] 这样的数字下标）")
    return None, False, (f"'{part}' 不能在这里继续取值"
                         f"（此处是 {type(current).__name__}，需要是映射或列表）")


def _keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(k) for k in value]
    if isinstance(value, (list, tuple)):
        return [str(i) for i in range(len(value))]
    return []


def _to_text(value: Any) -> str:
    """拼进字符串时的转换。

    只对"明显是值"的类型做 JSON 化，其余走 str()。不用 repr()：
    repr 会给字符串加引号，拼进 URL 或提示词里就是脏数据。

    特例：覆写了 `__str__` 的 dict 子类走 `str()` 而不是 JSON ——
    dry-run 的占位对象（`_DryRunPlaceholder`）就是这种，它要渲染成
    一句人话；走 JSON 会变成毫无信息量的 `{}`。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        if type(value).__str__ is not dict.__str__ and not isinstance(value, list):
            return str(value)
        import json

        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):                # pragma: no cover - 防御性
            return str(value)
    return str(value)


def describe(text: str) -> str:
    """把模板里的变量引用列出来（dry-run 计划里"这一步依赖什么"）。"""
    try:
        tokens = find_placeholders(text)
    except TemplateError:
        return ""
    return "、".join(f"{{{{ {t.raw} }}}}" for t in tokens if t.is_valid)


__all__ = [
    "ENV_NAME",
    "MAX_PLACEHOLDERS",
    "PLACEHOLDER",
    "TemplateProblem",
    "Token",
    "describe",
    "find_placeholders",
    "render",
    "render_structure",
    "validate_template",
]
