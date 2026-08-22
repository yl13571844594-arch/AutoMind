"""ReAct 执行器 — Observe → Think → Act → Observe 循环。"""

from __future__ import annotations

from typing import Any

from automind.core.logging import get_logger
from automind.core.types import (
    LLMResponse,
    ToolCall,
    ToolResult,
)
from automind.tools.base import ToolRegistry
from automind.tools.function_calling import FunctionCallHandler

logger = get_logger("automind.react")


class ReActExecutor:
    """ReAct 模式执行器。

    ReAct 循环:
        1. OBSERVE: 读取当前上下文和工具执行结果
        2. THINK: LLM 推理下一步做什么 (生成思考 + 可能的工具调用)
        3. ACT: 执行工具调用
        4. 重复直到 LLM 输出最终答案或达到迭代上限

    配置:
        max_iterations: 最大思考-行动循环数
        stop_on_no_tools: 当 LLM 不再请求工具时停止
    """

    SYSTEM_PROMPT = (
        "You are an AI agent that can use tools to accomplish tasks. "
        "For each step, think about what you need to do, then use the "
        "appropriate tool. After getting tool results, evaluate if you're "
        "done or need to take more actions.\n\n"
        "Workflow:\n"
        "1. Analyze the current state\n"
        "2. Decide what tool to use (if any)\n"
        "3. Interpret the tool result\n"
        "4. Repeat until the task is complete\n"
        "5. Provide a final summary when done"
    )

    def __init__(
        self,
        llm: Any,
        tool_registry: ToolRegistry,
        max_iterations: int = 50,
        stop_on_no_tools: bool = True,
        permissions: Any = None,
        approval_cb: Any = None,
        auto_validate: bool = True,
        tool_budget: int | None = None,
    ) -> None:
        self.llm = llm
        self.tool_registry = tool_registry
        self.fn_handler = FunctionCallHandler(tool_registry)
        self.max_iterations = max_iterations
        self.stop_on_no_tools = stop_on_no_tools
        self.permissions = permissions
        self.approval_cb = approval_cb  # async (tool_name, args, tier, reason) -> bool
        # TDD 内环：每次代码写入/编辑后自动做语法验证，结果注入观察反馈
        self.auto_validate = auto_validate
        self.thoughts: list[str] = []
        self.actions: list[tuple[ToolCall, ToolResult]] = []
        self.validations: list[dict] = []  # 自动验证记录
        # 工具名 → {streak: 连续失败次数, last: 最后一次原因}
        self._tool_failures: dict[str, dict] = {}
        # 单轮下发的工具上限（None = 用类默认值；0/负数 = 不限，全量下发）
        self.tool_budget = self.TOOL_BUDGET if tool_budget is None else tool_budget
        #: 本轮实际下发 schema 的工具名（随任务展开动态增补）
        self._active_tools: set[str] = set()
        #: 本次 run 的消息列表 —— **提升为实例属性**，预算吃紧时才压得到它
        self.messages: list[dict[str, Any]] = []
        #: run() 是否正在进行中（上层据此判断"现在压缩压得到实处吗"）
        self.running = False
        #: 供上层观测：工具下发与压缩的账
        self.token_savings: dict[str, Any] = {
            "tools_total": 0, "tools_sent": 0, "tools_expanded": [],
            "compactions": 0, "chars_reclaimed": 0,
        }

    _CODE_TOOLS = ("file_write", "file_edit", "file_multi_edit")

    def _auto_validate_result(self, tc: ToolCall, result: ToolResult) -> ToolResult:
        """TDD 内环：代码修改成功后立即做语法校验，结论写回观察结果。

        模型在下一轮 OBSERVE 中即可看到 "syntax_check: ..."，
        有错立即修复 —— 形成 编辑 → 验证 → 修复 的自动闭环。
        覆盖 file_write / file_edit / file_multi_edit 产出的全部 .py/.json 文件；
        优先使用工具输出中已解析的绝对路径（参数里的相对路径可能相对项目根）。
        """
        if not (self.auto_validate and result.success and tc.name in self._CODE_TOOLS):
            return result
        checked: list[tuple[str, str]] = []
        for path in self._touched_paths(tc, result):
            note = self._check_syntax(path)
            if note is None:
                continue
            ok = note.startswith("OK")
            self.validations.append({"tool": tc.name, "path": path, "ok": ok,
                                     **({} if ok else {"error": note})})
            checked.append((path, note))
        if checked:
            # 单文件保持简洁格式（syntax_check: OK），多文件带路径前缀
            summary = (checked[0][1] if len(checked) == 1
                       else "; ".join(f"{p}: {n}" for p, n in checked))
            try:
                if isinstance(result.output, dict):
                    result.output["auto_validation"] = f"syntax_check: {summary}"
            except Exception:
                pass
        return result

    @staticmethod
    def _touched_paths(tc: ToolCall, result: ToolResult) -> list[str]:
        """收集本次调用实际写入的文件路径（优先工具输出的解析后路径）。"""
        out = result.output if isinstance(result.output, dict) else {}
        if tc.name == "file_multi_edit":
            paths = []
            for r in out.get("results", []):
                o = r.get("output") if isinstance(r, dict) else None
                if isinstance(o, dict) and o.get("path") and r.get("success"):
                    paths.append(str(o["path"]))
            return paths
        path = out.get("path") or (tc.arguments or {}).get("path", "")
        return [str(path)] if path else []

    @staticmethod
    def _check_syntax(path: str) -> str | None:
        """校验单个文件；返回 "OK" / 错误说明，非目标类型或读不到返回 None。

        覆盖 Python / JSON / YAML / TOML —— Agent 产出最多的四类结构化文件。
        """
        from pathlib import Path as _P
        try:
            if path.endswith(".py"):
                import ast as _ast
                _ast.parse(_P(path).read_text(encoding="utf-8"))
                return "OK"
            if path.endswith(".json"):
                import json as _json
                _json.loads(_P(path).read_text(encoding="utf-8"))
                return "OK"
            if path.endswith((".yaml", ".yml")):
                import yaml as _yaml
                _yaml.safe_load(_P(path).read_text(encoding="utf-8"))
                return "OK"
            if path.endswith(".toml"):
                import tomllib as _toml
                _toml.loads(_P(path).read_text(encoding="utf-8"))
                return "OK"
        except SyntaxError as e:
            return (f"FAILED — {e.msg} (line {e.lineno}). "
                    f"Fix this syntax error before proceeding.")
        except ValueError as e:  # json.JSONDecodeError / tomllib.TOMLDecodeError
            kind = "JSON" if path.endswith(".json") else "TOML"
            return f"FAILED — invalid {kind}: {e}. Fix this before proceeding."
        except ImportError:
            return None  # 校验器依赖缺失（如无 pyyaml）→ 跳过不阻塞
        except Exception as e:
            # yaml.YAMLError 等解析错误也应反馈给模型
            if type(e).__module__.startswith("yaml"):
                return f"FAILED — invalid YAML: {e}. Fix this before proceeding."
            return None  # 文件读不到等情况不干扰主流程
        return None


    # ═══════════════════════════════════════════════════════════
    # 工具下发预算 —— ReAct 每一轮都要把工具 schema 重新发一遍
    # ═══════════════════════════════════════════════════════════
    #
    # 31 个内置工具的 OpenAI schema 加起来约 2.5 万字符 ≈ 6k~8k token。
    # ReAct 一步一次调用，**每步都要重付一遍**：跑 20 步就是十几万 token
    # 只花在"告诉模型它有哪些工具"上，而其中绝大多数与当前任务毫无关系
    # （写一个 Python 脚本用不上 email_tool / ppt_tool / ocr_tool）。
    #
    # 做法：按任务文本挑一批相关的**完整下发**，其余只在系统提示里留一行
    # "名字 — 一句话"的目录。模型想用目录里的工具时，在思考里提到它的名字，
    # 下一轮就会补发完整 schema（见 _expand_tools_from_text）。
    # 能力没有减少，只是不再每轮都把整本说明书重念一遍。

    #: 单轮最多下发的工具数（0/负数 = 不限）
    TOOL_BUDGET = 14

    #: 任何任务都可能用到的基础能力 —— 永远在场，不参与打分
    CORE_TOOLS = ("terminal", "file_read", "file_write", "file_edit", "file_search")

    #: 预算还有余额时的补位顺序（按通用程度，越靠前越常用）
    FILLER_ORDER = (
        "python_sandbox", "file_multi_edit", "web_search", "web_fetch",
        "git_tool", "http_request", "browser", "db_query", "csv_tool",
        "excel_tool", "process_tool", "code_generate", "archive",
    )

    #: 工具 → 触发词。任务多为中文，只靠英文工具名匹配不上，必须给中文别名。
    TOOL_HINTS: dict[str, tuple[str, ...]] = {
        "python_sandbox": ("python", "脚本", "计算", "运行代码", "sandbox"),
        "browser": ("浏览器", "网页", "点击", "登录", "自动化", "browser", "selenium"),
        "web_fetch": ("网页", "抓取", "url", "链接", "网址", "fetch"),
        "web_search": ("搜索", "查一下", "查查", "search", "资料", "调研"),
        "http_request": ("接口", "api", "http", "请求", "调用服务"),
        "code_generate": ("生成代码", "写代码", "脚手架", "codegen"),
        "archive": ("压缩", "解压", "zip", "tar", "打包"),
        "excel_tool": ("excel", "xlsx", "xls", "表格", "工作簿", "电子表格"),
        "word_tool": ("word", "docx", "文档", "公文", "报告"),
        "pdf_tool": ("pdf", "扫描件"),
        "ppt_tool": ("ppt", "pptx", "幻灯", "演示", "汇报材料"),
        "email_tool": ("邮件", "邮箱", "email", "smtp", "发信"),
        "calendar": ("日历", "日程", "会议", "提醒", "calendar"),
        "db_query": ("数据库", "sql", "查表", "mysql", "sqlite", "postgres"),
        "csv_tool": ("csv", "逗号分隔", "数据表"),
        "screenshot_tool": ("截图", "屏幕", "screenshot"),
        "ocr_tool": ("ocr", "识别文字", "图片文字", "扫描"),
        "image_tool": ("图片", "图像", "缩放", "裁剪", "水印", "image"),
        "chart_tool": ("图表", "画图", "折线", "柱状", "饼图", "chart", "可视化"),
        "audio_tool": ("音频", "录音", "转写", "mp3", "wav"),
        "video_tool": ("视频", "剪辑", "mp4", "转码"),
        "git_tool": ("git", "提交", "分支", "commit", "仓库", "版本控制"),
        "process_tool": ("进程", "服务", "端口", "杀掉", "process"),
        "clipboard_tool": ("剪贴板", "复制到", "clipboard"),
        "notify": ("通知", "提醒我", "弹窗", "notify"),
        "im_integration": ("钉钉", "企业微信", "飞书", "slack", "群机器人", "推送到群"),
        "file_multi_edit": ("批量修改", "多个文件", "重构"),
    }

    def _select_tools(self, task: str, context: str) -> None:
        """按任务相关度挑出本轮要完整下发 schema 的工具。"""
        names = self.tool_registry.list_names()
        self.token_savings["tools_total"] = len(names)
        budget = self.tool_budget
        if not budget or budget <= 0 or len(names) <= budget:
            self._active_tools = set(names)
            self.token_savings["tools_sent"] = len(names)
            return

        text = f"{task}\n{context}".lower()
        picked = {n for n in self.CORE_TOOLS if n in names}
        scored: list[tuple[int, str]] = []
        for n in names:
            if n in picked:
                continue
            score = 0
            if n in text:                       # 任务里直接点名了某个工具
                score += 10
            for kw in self.TOOL_HINTS.get(n, ()):
                if kw in text:
                    score += 3
            # 工具名本身的词也算线索（file_search → "search"）
            for part in n.split("_"):
                if len(part) > 3 and part in text:
                    score += 2
            if score:
                scored.append((score, n))
        scored.sort(key=lambda x: (-x[0], x[1]))
        for _, n in scored:
            if len(picked) >= budget:
                break
            picked.add(n)
        # 还有余额就补满 —— 与其空着，不如多给模型几个选择。
        # 顺序按"通用程度"而非字母序：按字母序补会先塞进 archive / audio_tool /
        # calendar 这类冷门工具，把 python_sandbox、git_tool 这些真正常用的挤掉。
        for n in list(self.FILLER_ORDER) + names:
            if len(picked) >= budget:
                break
            if n in names:
                picked.add(n)
        self._active_tools = picked
        self.token_savings["tools_sent"] = len(picked)
        logger.info("react_tool_budget", total=len(names), sent=len(picked))

    def _schemas(self) -> list[dict[str, Any]]:
        """当前活跃工具的 schema（顺序稳定，便于提示缓存命中）。"""
        return [t.to_openai_schema() for t in self.tool_registry.list_all()
                if t.name in self._active_tools]

    def _dormant_catalog(self) -> str:
        """未下发工具的一行式目录 —— 让模型知道"还有这些，可以要"。"""
        rows = []
        for t in self.tool_registry.list_all():
            if t.name in self._active_tools:
                continue
            desc = (t.description or "").strip().splitlines()
            rows.append(f"- {t.name}: {(desc[0] if desc else '')[:70]}")
        if not rows:
            return ""
        return (
            "Additional tools exist but their full schemas are not loaded, to keep "
            "the context small. If you need one, write its exact name in your "
            "reasoning and it will be available on the next step:\n" + "\n".join(rows)
        )

    def _expand_tools_from_text(self, text: str) -> list[str]:
        """模型在思考里点名了某个未下发的工具 → 下一轮补发它的完整 schema。"""
        if not text:
            return []
        added = [n for n in self.tool_registry.list_names()
                 if n not in self._active_tools and n in text]
        if added:
            self._active_tools.update(added)
            self.token_savings["tools_expanded"].extend(added)
            logger.info("react_tools_expanded", tools=added)
        return added

    # ═══════════════════════════════════════════════════════════
    # 上下文压缩 —— 压的必须是 ReAct 自己的消息列表
    # ═══════════════════════════════════════════════════════════

    #: 折叠旧观察时，每条最多保留的字符数
    OBS_KEEP_CHARS = 240
    #: 折叠标记 —— 靠它认出"这条已经折过了"，避免反复压缩连说明一起再折一遍
    FOLD_MARK = "…[早前的观察结果已折叠"
    #: 最近这么多条消息不动（模型正靠它们判断"下一步做什么"）
    COMPACT_KEEP_RECENT = 8

    def compact(self, keep_recent: int | None = None) -> dict[str, Any]:
        """就地压缩本轮 ReAct 的消息列表，返回压缩账目。

        预算告警时**必须压这里**：ReAct 每轮重新发送的就是 self.messages，
        而工具观察结果（文件内容、命令输出、网页正文）往往一条就好几千字符，
        是上下文里最肥的一块。此前上层压的是 ContextManager —— 那份记录
        从头到尾就没进过 ReAct 的请求体，等于**花了摘要的钱，一个 token 都没省**。

        只折叠旧观察的正文、不删消息：assistant(tool_calls) 与 tool 结果必须
        成对出现，随手删几条会让下一次请求直接被 API 判为非法。
        """
        keep = self.COMPACT_KEEP_RECENT if keep_recent is None else keep_recent
        msgs = self.messages
        cutoff = max(0, len(msgs) - keep)
        reclaimed = 0
        folded = 0
        for m in msgs[:cutoff]:
            if m.get("role") != "tool":
                continue
            content = m.get("content")
            if not isinstance(content, str) or len(content) <= self.OBS_KEEP_CHARS:
                continue
            if self.FOLD_MARK in content:
                continue                       # 已经折过了，别再折一次
            head = content[: self.OBS_KEEP_CHARS]
            dropped = len(content) - len(head)
            reclaimed += dropped
            folded += 1
            m["content"] = (
                f"{head}\n{self.FOLD_MARK}，省略 {dropped} 字符。"
                f"如仍需要完整内容，请重新调用相应工具。]"
            )
        if folded:
            self.token_savings["compactions"] += 1
            self.token_savings["chars_reclaimed"] += reclaimed
            logger.info("react_context_compacted", folded=folded,
                        chars_reclaimed=reclaimed, messages=len(msgs))
        return {"folded": folded, "chars_reclaimed": reclaimed,
                "messages": len(msgs)}

    async def run(
        self,
        task: str,
        context: str = "",
        on_thought: Any = None,
        on_action: Any = None,
    ) -> str:
        """执行 ReAct 循环。

        Args:
            task: 用户任务。
            context: 额外上下文。
            on_thought: 思考回调 (可选)。
            on_action: 动作回调 (可选)。

        Returns:
            最终答案文本。
        """
        self.running = True
        try:
            return await self._run(task, context, on_thought, on_action)
        finally:
            self.running = False

    async def _run(
        self,
        task: str,
        context: str = "",
        on_thought: Any = None,
        on_action: Any = None,
    ) -> str:
        # 只挑与本任务相关的工具完整下发，其余留一份一行式目录（见 _select_tools）
        self._select_tools(task, context)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
        ]
        catalog = self._dormant_catalog()
        catalog_at = -1          # 目录消息在 messages 里的下标（-1 = 没有目录）
        if catalog:
            catalog_at = len(messages)
            messages.append({"role": "system", "content": catalog})
        if context:
            messages.append({"role": "system", "content": f"Environment:\n{context}"})
        messages.append({"role": "user", "content": task})
        # 暴露给上层：预算吃紧时 compact() 压的就是这个列表
        self.messages = messages

        active_sig = frozenset(self._active_tools)
        tool_schemas = self._schemas()

        for iteration in range(self.max_iterations):
            # 活跃工具集变了（模型点名要了某个休眠工具）→ 重建 schema 与目录
            if frozenset(self._active_tools) != active_sig:
                active_sig = frozenset(self._active_tools)
                tool_schemas = self._schemas()
                if catalog_at >= 0:
                    # 目录里少了刚补发的那几个（它们现在有完整 schema 了）
                    messages[catalog_at]["content"] = self._dormant_catalog()
            # THINK
            response = await self.llm.generate(messages, tools=tool_schemas)

            if response.text:
                self.thoughts.append(response.text)
                # 模型在思考里点名了休眠工具 → 下一轮补发它的完整 schema
                self._expand_tools_from_text(response.text)
                if on_thought:
                    await on_thought(response.text)

            # 没有工具调用 → 任务完成
            if not response.tool_calls:
                if self.stop_on_no_tools:
                    return response.text
                # 否则添加 assistant 消息并继续
                messages.append({"role": "assistant", "content": response.text})
                continue

            # ACT — 先做权限门控，再执行
            messages.append(self._assistant_message(response))
            results = []
            for tc in response.tool_calls:
                allowed, deny_reason = await self._gate(tc)
                if not allowed:
                    results.append(ToolResult(
                        tool_name=tc.name, success=False,
                        error=f"操作被拒绝/未批准：{deny_reason}"))
                    continue
                # B-03 修复：工具不存在 / 参数不合法等异常若逸出会整体崩溃 ReAct 循环，
                # 这里兜底为失败结果，让 LLM 据此继续决策而非丢失全部上下文。
                # 熔断：同一工具连续失败 N 次后不再重试。
                # 此前模型会对着同一个坏工具反复调用直到迭代上限耗尽 ——
                # 既烧 token 又拖时间，而失败原因从头到尾没人看见。
                tripped = self._breaker_reason(tc.name)
                if tripped:
                    results.append(ToolResult(
                        tool_name=tc.name, success=False, error=tripped))
                    continue
                try:
                    args = tc.arguments if isinstance(tc.arguments, dict) else {}
                    result = await self.tool_registry.dispatch(tc.name, **args)
                    # TDD 内环：代码修改后立即语法验证并注入观察
                    result = self._auto_validate_result(tc, result)
                    self._record_tool_outcome(tc.name, result.success,
                                              result.error or "")
                    results.append(result)
                except Exception as e:
                    reason = f"{type(e).__name__}: {e}"
                    self._record_tool_outcome(tc.name, False, reason)
                    results.append(ToolResult(
                        tool_name=tc.name, success=False,
                        error=f"工具执行异常：{reason}"))

            for tc, result in zip(response.tool_calls, results):
                self.actions.append((tc, result))
                if on_action:
                    await on_action(tc, result)

            # OBSERVE
            tool_messages = self.fn_handler.tool_results_to_messages(
                response.tool_calls, results
            )
            messages.extend(tool_messages)

        # 达到迭代上限：返回最后一次有意义的思考，避免空泛提示
        if self.thoughts:
            return (self.thoughts[-1] +
                    "\n\n（提示：已达到最大迭代步数，以上为当前进展。）")
        return "已达到最大迭代步数，任务可能尚未完成。"

    #: 同一工具连续失败达到此次数即熔断，本轮任务内不再调用
    FAILURE_THRESHOLD = 3

    def _record_tool_outcome(self, name: str, success: bool, error: str) -> None:
        """记录一次工具调用结果；连续失败到阈值即熔断并留痕。"""
        st = self._tool_failures.setdefault(name, {"streak": 0, "last": ""})
        if success:
            st["streak"] = 0
            st["last"] = ""
            return
        st["streak"] += 1
        st["last"] = (error or "")[:300]
        logger.warning("tool_call_failed", tool=name,
                       streak=st["streak"], error=st["last"])
        if st["streak"] == self.FAILURE_THRESHOLD:
            logger.error("tool_circuit_open", tool=name,
                         threshold=self.FAILURE_THRESHOLD, last_error=st["last"])

    def _breaker_reason(self, name: str) -> str:
        """熔断已触发时返回给模型看的说明，否则返回空串。"""
        st = self._tool_failures.get(name)
        if not st or st["streak"] < self.FAILURE_THRESHOLD:
            return ""
        return (f"工具「{name}」已连续失败 {st['streak']} 次，已停止调用以免空转。"
                f"最后一次的失败原因：{st['last']}。"
                f"请改用其它方式完成该步骤，或先修复该工具的配置。")

    def tool_failure_report(self) -> dict[str, dict]:
        """本轮各工具的失败情况（供上层推送给前端标红）。"""
        return {k: dict(v) for k, v in self._tool_failures.items() if v["streak"]}

    async def _gate(self, tc: ToolCall) -> tuple[bool, str]:
        """工具调用前的权限门控。返回 (是否允许, 原因)。"""
        if self.permissions is None:
            return True, ""
        try:
            tool = self.tool_registry.get(tc.name)
            tier = tool.permission_tier
        except Exception:
            from automind.core.types import PermissionTier
            tier = PermissionTier.SENSITIVE
        decision, reason = self.permissions.check(tc.name, tier, tc.arguments)
        if decision.value == "allow":
            return True, reason
        if decision.value == "deny":
            return False, reason
        # ask_user —— 走到这里说明**权限策略明确要求人工确认**
        # （"询问"模式下的非只读操作，或"自动"模式下的高危操作）。
        #
        # 安全修复（v1.4.5）：这两条分支此前都 `return True`：
        #   · 没有审批通道就直接放行 —— 注释写的是"自主运行不阻塞"，但真正表达
        #     "我要自主运行"的方式是把 approval_mode 设成 auto/approve_all，
        #     那样 permissions.check() 根本不会返回 ask_user，压根到不了这里。
        #     靠"回调恰好没接"来放行，等于让配置疏漏静默变成放权。
        #   · 回调抛异常就直接放行 —— 而最常见的异常正是前端断开。
        # 审批是安全控制，两种情形一律 fail-closed：问不到人 = 没批准。
        if self.approval_cb is None:
            return False, (f"{reason}；当前没有可用的审批通道，已按拒绝处理"
                           "（若需无人值守运行，请将审批模式设为「自动」或「全批准」）")
        try:
            from automind.state.human_loop import ApprovalOutcome
            outcome = ApprovalOutcome.normalize(
                await self.approval_cb(tc.name, tc.arguments, tier.value, reason))
            if outcome.approved and outcome.modified:
                # 「修改后批准」：就地替换本次调用的参数。必须改 tc.arguments
                # 本身 —— 后续真正执行工具时读的是它。
                logger.info("approval_modified", tool=tc.name,
                            keys=sorted(outcome.arguments or {}))
                tc.arguments = dict(outcome.arguments or {})
                return True, f"{reason}；用户修改参数后批准"
            return outcome.approved, (
                reason if outcome.approved
                else f"用户拒绝：{outcome.comment or reason}")
        except Exception as e:
            logger.warning("react_approval_failed", tool=tc.name,
                           error=str(e), decision="denied")
            return False, f"审批通道异常（{type(e).__name__}），已按拒绝处理"

    def get_trace(self) -> str:
        """获取执行跟踪。"""
        lines = ["ReAct Execution Trace:", "=" * 50]
        for i, thought in enumerate(self.thoughts):
            lines.append(f"\n[Think {i + 1}]")
            lines.append(thought[:500])
            if i < len(self.actions):
                tc, result = self.actions[i]
                status = "OK" if result.success else "FAIL"
                lines.append(f"[Action {i + 1}] {status}: {tc.name}(...)")
                output = str(result.output)[:200] if result.success else str(result.error)[:200]
                lines.append(f"  → {output}")
        return "\n".join(lines)

    @staticmethod
    def _assistant_message(response: LLMResponse) -> dict[str, Any]:
        """构建包含工具调用的 assistant 消息。"""
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": response.text or None,
        }
        if response.tool_calls:
            import json
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        # OpenAI 兼容接口要求 arguments 为合法 JSON 字符串
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in response.tool_calls
            ]
        return msg
