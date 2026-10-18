"""ReAct 执行器 — Observe → Think → Act → Observe 循环。"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any

from automind.core.exceptions import BudgetExhaustedError, LLMContextTooLargeError
from automind.core.llm import classify_llm_error, is_transient_llm_error
from automind.core.logging import get_logger
from automind.core.types import (
    LLMResponse,
    ToolCall,
    ToolResult,
)
from automind.planning.react_progress import SIDE_EFFECT_TOOLS, ReactProgressGuard
from automind.tools.base import ToolRegistry
from automind.tools.function_calling import FunctionCallHandler
from automind.tools.output_budget import limits_from_config

logger = get_logger("automind.react")


def build_initial_messages(system_prompt: str, tool_catalog: str, context: str,
                           memory_note: str, task: str) -> list[dict[str, Any]]:
    """拼出 ReAct 的初始消息 —— 顺序按"稳定前缀优先"排（v1.11.10）。

    DeepSeek / GLM / OpenAI 的自动提示缓存都只认**逐字节前缀**：越早的内容
    发生变化，后面越长的部分都跟着作废。所以这里的顺序刻意固定为：

        系统提示（静态） → 工具目录（任务内基本不变）
        → 环境/项目索引（同一会话内稳定） → 记忆片段（每个任务都可能变）
        → 用户任务

    把"每次任务都可能不一样"的记忆放在稳定内容之后，多个任务之间就还能
    复用前面那一段前缀；放在环境消息里则会把环境和项目索引一起带崩。
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    if tool_catalog:
        messages.append({"role": "system", "content": tool_catalog})
    if context:
        messages.append({"role": "system", "content": f"Environment:\n{context}"})
    if memory_note:
        messages.append({"role": "system", "content": memory_note})
    messages.append({"role": "user", "content": task})
    return messages


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
        # v1.7.5：两条约束直接写进系统提示。
        # 实测里最大的两类工具失败就是"名字凭记忆拼写"和"路径写错范围" ——
        # 而系统提示此前对这两件事一个字都没说（工具 schema 里也只写了
        # "Path to the file"）。把它们说清楚，比每次失败后再纠正便宜得多。
        "Rules:\n"
        "* Use the EXACT tool names from the tool list. Never invent synonyms "
        "or translations (e.g. it is `file_read`, not `read_file`; it is "
        "`terminal`, not `shell` or `execute_command`).\n"
        "* All file paths are resolved against the project root. Prefer paths "
        "RELATIVE to the project root (e.g. `src/app.py`). Absolute paths "
        "inside the project also work; paths outside it are rejected.\n"
        "* If a tool call fails, read the error: it names the allowed values. "
        "Fix the arguments and retry once — do not repeat the identical call.\n\n"
        "Workflow:\n"
        "1. Analyze the current state\n"
        "2. Decide what tool to use (if any)\n"
        "3. Interpret the tool result\n"
        "4. Repeat until the task is complete\n"
        "5. Provide a final summary when done\n\n"
        # v1.11.11：新增协作工具的用途必须写进系统提示 —— 否则它们只出现在
        # 工具目录里，模型不知道"什么时候该用"，能力等于不存在。
        "Collaboration tools:\n"
        "* todo_write: for multi-step tasks, keep a short, user-visible todo list. "
        "Always send the COMPLETE list (empty array clears it); one item in_progress at a time.\n"
        "* ask_user_question: when a decision genuinely belongs to the user (target, "
        "format, destructive choice, ambiguity inspection cannot resolve), ask with options "
        "and wait. Do not guess on the user's behalf. A timeout is NOT approval.\n"
        "* present: after producing final deliverable files, declare the 1-2 most "
        "important with present so the user can open them directly."
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
        output_limits: dict[str, int] | None = None,
        obs_keep_chars: int | None = None,
        repeat_threshold: int = 2,
        result_cache: bool = True,
        no_progress_limit: int = 12,
        tool_timeout: float = 300.0,
        tool_timeout_max: float = 1800.0,
        interjection_source: Any = None,
        prefer_tools: list[str] | None = None,
    ) -> None:
        self.llm = llm
        self.tool_registry = tool_registry
        #: 工具结果进上下文前的体积限额（v1.6.4：当前轮的大输出也必须夹住）
        self.output_limits = output_limits or limits_from_config()
        self.fn_handler = FunctionCallHandler(tool_registry, self.output_limits)
        self.max_iterations = max_iterations
        #: v1.11.7：仍在推进时步数用完可续加的次数（0 = 不续加，到上限即收尾）
        self.max_iteration_extensions = self.MAX_ITERATION_EXTENSIONS
        self.iteration_extensions_used = 0
        self.stop_on_no_tools = stop_on_no_tools
        self.permissions = permissions
        self.approval_cb = approval_cb  # async (tool_name, args, tier, reason) -> bool
        # ── v1.7.0 单步工具超时 ──────────────────────────
        # ReAct 此前**没有**任何单步上限：工具挂住（等网络/等子进程/等系统锁）
        # 整个任务就停在那里。模型可在参数里用 timeout 申请更长，但不超过上限。
        try:
            self.tool_timeout = max(0.0, float(tool_timeout or 0))
        except (TypeError, ValueError):
            self.tool_timeout = 300.0
        try:
            self.tool_timeout_max = max(0.0, float(tool_timeout_max or 0))
        except (TypeError, ValueError):
            self.tool_timeout_max = 1800.0
        if self.tool_timeout_max and self.tool_timeout:
            self.tool_timeout_max = max(self.tool_timeout, self.tool_timeout_max)
        #: 本轮因超时被中止的工具调用数（供报告与前端显示）
        self.tool_timeouts = 0
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
        #: 用户在输入框显式引用的工具（MCP 连接器 / 技能）：不参与打分，必下发
        self.prefer_tools: list[str] = list(prefer_tools or [])
        #: 本次 run 的消息列表 —— **提升为实例属性**，预算吃紧时才压得到它
        self.messages: list[dict[str, Any]] = []
        #: run() 是否正在进行中（上层据此判断"现在压缩压得到实处吗"）
        self.running = False
        #: 供上层观测：工具下发 / 压缩 / 截断 / 去重的账
        self.token_savings: dict[str, Any] = {
            "tools_total": 0, "tools_sent": 0, "tools_expanded": [],
            "compactions": 0, "chars_reclaimed": 0,
            "truncated_results": 0, "chars_dropped": 0,
            "duplicate_results_shortcircuited": 0, "chars_saved_dedupe": 0,
        }
        #: 结束原因：no_more_tools（模型认为完成）| max_iterations | error
        #:          | no_progress（反复做同一件事，提前收尾）
        #:          | task_budget（整轮任务总时长预算到点）
        #:          | llm_unavailable（模型长时间连不上，v1.7.6）
        self.stop_reason = ""
        #: 墙钟截止时刻的顺延量（秒；v1.11.0 预算窗口续开时由 Agent 推入）
        self.deadline_shift = 0.0
        #: 已完成的迭代数（增量进度，上限耗尽时用于交代"跑到第几步"）
        self.iterations_used = 0
        #: 中途异常的说明（非空表示被中断而非正常收尾）
        self.interrupted_reason = ""
        #: 旧观察折叠时每条保留的字符数（None = 类默认；可配）
        self.obs_keep_chars = (self.OBS_KEEP_CHARS if obs_keep_chars is None
                               else int(obs_keep_chars))
        # ── v1.7.0 无进展治理 ─────────────────────────────
        # 连续 threshold 次完全相同的动作 → 先提示后拦截（见 react_progress）
        self.progress = ReactProgressGuard(repeat_threshold, cache_enabled=result_cache)
        #: 拦截到该次数即提前收尾（0 = 不提前收尾，只拦到迭代上限）
        self.no_progress_limit = max(0, int(no_progress_limit))
        #: 只读结果复用是否开启（透传给 progress，便于报告里如实标注）
        self.result_cache = bool(result_cache)
        #: 因"没有审批通道"而被拒的动作数（配置问题的可观测信号：
        #  「询问」模式下这个数字持续增长，说明审批回调没接上）
        self.skipped_approvals = 0
        # ── v1.7.2 用户中途插话 ───────────────────────────
        #: 取插话的回调 ``() -> list[Interjection]``；None = 本执行器不支持插话。
        #: 每次迭代边界都会调用它 —— 用户补的那句话要在**下一次思考之前**
        #: 进入消息列表，否则模型这一轮已经想完了，补充只能等下一轮。
        self.interjection_source = interjection_source
        #: 本轮真正并入的插话（供部分交付清单与报告如实交代）
        self.interjections_merged: list[dict[str, Any]] = []

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
        # v1.11.11 协作/补充工具：命中任务文本时才完整下发，避免每步白付 schema
        "todo_write": ("待办", "清单", "步骤", "计划一下", "多步", "todo", "checklist"),
        "ask_user_question": ("询问", "确认一下", "让我选", "哪个", "是否", "选择", "选项",
                              "偏好", "确认", "ask user"),
        "present": ("交付", "产物", "最终文件", "报告", "导出", "生成的文件", "deliverable"),
        "pwsh": ("powershell", "pwsh", "$env", "get-childitem", "windows"),
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
        # v1.11.0：用户引用的工具不看关键词打分 —— "我点名要用这个连接器"
        # 却因为任务描述里没出现它的关键词而没下发，是最让人困惑的一种失效
        picked |= {n for n in self.prefer_tools if n in names}
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
            if n not in self.TOOL_HINTS:
                # v1.11.2：MCP / 插件 / 连接器工具没有内置中文别名，此前只能靠
                # 英文名碰运气 ——"在 GitHub 上建个 issue"选不中 mcp__github__create_issue。
                # 补两条线索：点名了服务器 / 插件名，以及工具描述与任务的词面重合。
                score += self._extra_tool_score(n, text)
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

    def _extra_tool_score(self, name: str, text: str) -> int:
        """非内置工具的相关度：命名空间（MCP 服务器 / 插件名）+ 描述词面重合。"""
        import re
        score = 0
        if name.startswith("mcp__"):
            server = name.split("__")[1] if name.count("__") >= 2 else ""
            if len(server) >= 2 and server.lower() in text:
                score += 6
        try:
            desc = (self.tool_registry.get(name).description or "").lower()
        except Exception:
            return score
        hits = 0
        for w in set(re.findall(r"[a-z][a-z0-9]{3,}", desc)):
            if w not in self._STOP_WORDS and w in text:
                hits += 1
        for run in re.findall(r"[一-鿿]{2,}", desc):
            for i in range(len(run) - 1):
                if run[i:i + 2] in text:
                    hits += 1
                    break
        return score + min(hits, 4)

    #: 描述里太常见、没有区分度的英文词
    _STOP_WORDS = frozenset({
        "tool", "tools", "with", "from", "that", "this", "file", "files", "data", "into",
        "will", "your", "when", "used", "uses", "using", "return", "returns", "given",
        "list", "name", "path", "value", "string", "true", "false", "optional", "default",
    })

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
        from automind.tools.base import _norm, _short_name
        flat = _norm(text)
        added = []
        for n in self.tool_registry.list_names():
            if n in self._active_tools:
                continue
            if n in text:
                added.append(n)
                continue
            # v1.11.2：命名空间工具模型几乎不写全名 —— "github.create_issue" /
            # "create_issue" 也算点名（本名足够长才认，避免 "read" 这种误伤）
            short = _short_name(n)
            if short and len(short) >= 6 and (short in text or (
                    n.startswith("mcp__") and _norm("".join(n.split("__")[1:])) in flat)):
                added.append(n)
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
    #: 其中留给**尾部**的字符数（v1.7.5：改成头+尾保留）
    #: 工具输出里最有价值的信息常在结尾 —— 测试失败汇总、编译器报错行、
    #: 非零退出的原因、大文件读取的最后几行。只留头部会把这些全丢掉。
    FOLD_TAIL_CHARS = 80
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

        v1.6.4：每条保留的字符数由 ``obs_keep_chars`` 配置（默认 240），并把
        「压缩率 / 折叠字符数 / 估算 token 节省」一并算进返回账目，供观测中心
        展示 —— 让"长任务越跑越省"这件事可以被看见、也可以被调参。
        """
        keep = self.COMPACT_KEEP_RECENT if keep_recent is None else keep_recent
        keep_chars = max(40, int(self.obs_keep_chars))
        msgs = self.messages
        cutoff = max(0, len(msgs) - keep)
        reclaimed = 0
        folded = 0
        before_chars = sum(len(m.get("content") or "") for m in msgs
                           if isinstance(m.get("content"), str))
        for m in msgs[:cutoff]:
            if m.get("role") != "tool":
                continue
            content = m.get("content")
            if not isinstance(content, str) or len(content) <= keep_chars:
                continue
            if self.FOLD_MARK in content:
                continue                       # 已经折过了，别再折一次
            # v1.7.5：改成**头 + 尾**保留，与"当前轮裁剪"的口径一致。
            # 此前只留头部 —— 而工具输出里最有价值的信息经常在结尾：
            # pytest 的 FAILED 汇总、编译器的错误行、命令的非零退出原因、
            # 大文件读取的末尾几行。只留头部等于每次折叠都把这些扔掉，
            # 模型于是"记得自己读过，却不记得读出了什么"，只能重读一遍。
            head_chars = keep_chars - self.FOLD_TAIL_CHARS
            head = content[: max(0, head_chars)]
            tail = content[-self.FOLD_TAIL_CHARS:] if self.FOLD_TAIL_CHARS else ""
            kept = len(head) + len(tail)
            dropped = len(content) - kept
            reclaimed += dropped
            folded += 1
            m["content"] = (
                f"{head}\n{self.FOLD_MARK}，中间省略 {dropped} 字符"
                f"（已保留开头与结尾）。"
                f"如仍需要完整内容，请重新调用相应工具。]\n{tail}"
            )
        after_chars = sum(len(m.get("content") or "") for m in msgs
                          if isinstance(m.get("content"), str))
        stat: dict[str, Any] = {
            "folded": folded, "chars_reclaimed": reclaimed,
            "messages": len(msgs),
            # 可观测的压缩账：压缩率与估算 token 节省（1 token ≈ 3.5 字符）
            "chars_before": before_chars, "chars_after": after_chars,
            "ratio": round(reclaimed / before_chars, 4) if before_chars else 0.0,
            "est_tokens_saved": int(reclaimed / 3.5),
            "keep_chars": keep_chars,
        }
        if folded:
            self.token_savings["compactions"] += 1
            self.token_savings["chars_reclaimed"] += reclaimed
            logger.info("react_context_compacted", folded=folded,
                        chars_reclaimed=reclaimed, messages=len(msgs),
                        ratio=stat["ratio"])
        return stat

    def token_report(self) -> dict[str, Any]:
        """本轮 ReAct 的省 token 总账（下发预算 + 折叠 + 截断 + 去重 + 复用）。

        观测中心把它作为 ``context_compacted`` / ``tool_output_truncated``
        事件的汇总推送 —— 此前只有"预算 80% 了"这一个信号，看不到
        "到底省下来多少、还能不能再省"。
        """
        fn = getattr(self, "fn_handler", None)
        trunc = fn.savings_report() if fn is not None else {}
        guard = getattr(self, "progress", None)
        return {
            **dict(self.token_savings),
            "truncated_results": trunc.get("truncated_results", 0),
            "chars_dropped": trunc.get("dropped_chars", 0),
            "est_tokens_saved_truncation": trunc.get("est_tokens_saved", 0),
            # v1.7.0：跨轮去重（同参结果不再重发）与只读复用（不重复执行）
            "duplicate_results_shortcircuited": trunc.get("deduped_results", 0),
            "results_reused": getattr(guard, "cache_hits", 0) if guard else 0,
            "repeat_actions_blocked": getattr(guard, "blocked", 0) if guard else 0,
            "repeat_actions_guided": getattr(guard, "guided", 0) if guard else 0,
            "tool_timeouts": getattr(self, "tool_timeouts", 0),
            "iterations_used": self.iterations_used,
            "max_iterations": self.max_iterations,
            "stop_reason": self.stop_reason,
            # v1.7.2：本次合并进来的用户中途补充（条数与原文），
            # 交付清单里要说清楚"你的补充到底进没进去"
            "interjections_merged": len(getattr(self, "interjections_merged", []) or []),
        }

    async def run(
        self,
        task: str,
        context: str = "",
        on_thought: Any = None,
        on_action: Any = None,
        on_no_progress: Any = None,
        deadline: float | None = None,
        on_timeout: Any = None,
        on_interjection: Any = None,
        on_reconnect: Any = None,
        memory_note: str = "",
    ) -> str:
        """执行 ReAct 循环。

        Args:
            task: 用户任务。
            context: 额外上下文（环境 / 项目索引；同一会话内尽量稳定）。
            on_thought: 思考回调 (可选)。
            on_action: 动作回调 (可选)。
            on_no_progress: 无进展事件回调 ``(dict) -> None`` (可选)。
                模型开始"原地打转"时推给上层 —— 用户在界面上就能看到
                "它卡在重复读同一个文件"，而不是干等到迭代上限。
            deadline: 整轮任务的绝对截止时刻（``time.monotonic()`` 基准）。
                None = 不限。到点不再开新步骤，直接交部分交付清单。
            on_timeout: 单步工具超时回调 ``(dict) -> None`` (可选)。
            on_interjection: 用户中途补充被并入时的回调 ``(dict) -> None``
                (可选) —— 补的那句话**真的交给模型了**才会触发，用户据此
                知道"我插的话生效了"，而不是插完只能猜。
            on_reconnect: 模型连接中断、正在等待重连时的回调
                ``(dict) -> None`` (可选，v1.7.6)。
            memory_note: 每个任务都可能变化的记忆片段（v1.11.10）。单独作为
                一条 system 消息放在环境之后，避免它把稳定的前缀缓存一起作废。

        Returns:
            最终答案文本。**上限耗尽 / 无进展提前收尾 / 超时 / 被中断时**
            返回的是"部分交付清单"（已完成动作、已产出产物、卡在哪、
            建议下一步），而不是一句"已达最大迭代步数" 就把进度全丢掉。
        """
        self.running = True
        self.stop_reason = ""
        self.interrupted_reason = ""
        self.iterations_used = 0
        self.tool_timeouts = 0
        self.interjections_merged = []
        #: v1.11.11 上下文溢出后的"压缩 + 重试"是否已用掉（每轮任务最多一次）
        self.context_retry_used = False
        #: 连续"整轮没有任何成功工具调用"的轮数（见 NO_SUCCESS_LIMIT）
        self._rounds_without_success = 0
        #: 连续"空回复/被截断回复"的轮数（见 EMPTY_RESPONSE_LIMIT）
        self._empty_response_streak = 0
        # 无进展记账与只读缓存都按"本轮任务"重置 —— 上一轮 task 的重复判定
        # 与结果缓存对新任务是噪声（文件可能已经变了）
        try:
            self.progress.reset()
        except AttributeError:      # 老实例（测试里 __new__ 造的）没有该属性
            pass
        try:
            return await self._run(task, context, on_thought, on_action,
                                   on_no_progress, deadline, on_timeout,
                                   on_interjection, on_reconnect,
                                   memory_note=memory_note)
        except asyncio.CancelledError:
            # 被取消（用户停止 / 会话关闭 / 超时）—— 同样要留下结构化进度，
            # 否则用户只看到"任务没了"，不知道已经做成了什么。
            self.stop_reason = "cancelled"
            self.interrupted_reason = "任务被取消"
            raise
        except Exception as e:
            self.stop_reason = "error"
            self.interrupted_reason = f"{type(e).__name__}: {e}"
            raise
        finally:
            self.running = False

    #: 清单里最多列出的动作条数（太长没人看，也太占上下文）
    MANIFEST_MAX_ACTIONS = 40

    def _manifest_action(self, tc: ToolCall, result: ToolResult) -> dict[str, Any]:
        """把一个动作整理成清单条目（含产物路径与失败原因）。"""
        item: dict[str, Any] = {
            "tool": tc.name,
            "ok": bool(result.success),
            "args": {k: str(v)[:120] for k, v in (tc.arguments or {}).items()},
        }
        out = result.output if result.success else result.error
        if isinstance(out, dict):
            for key in ("path", "output_file", "file", "saved_to", "url"):
                if out.get(key):
                    item["artifact"] = str(out[key])[:300]
                    break
            else:
                item["output"] = str(out)[:160]
        else:
            item["output"] = str(out)[:160]
        if not result.success:
            item["error"] = str(result.error or "")[:200]
        return item

    def partial_report(self, task: str = "") -> dict[str, Any]:
        """结构化「部分交付清单」—— ReAct 路径的对等 ExecutionReport。

        Plan-Execute 路径失败时有 ``ExecutionReport``（每步成功/失败/重试），
        ReAct 此前什么都没有：迭代上限耗尽时只回一段最后的思考加一句
        "已达最大迭代步数"，中途被取消时更是什么都不留。用户既不知道
        "做成了什么"，也不知道"卡在哪"，只能重跑一遍再烧一次 token。
        """
        done = [self._manifest_action(tc, r) for tc, r in self.actions]
        ok = [a for a in done if a["ok"]]
        failed = [a for a in done if not a["ok"]]
        artifacts: list[str] = []
        for a in ok:
            p = a.get("artifact")
            if p and p not in artifacts:
                artifacts.append(p)
        # 未闭合的失败：同一工具最后一次仍失败，说明这步没解决
        open_failures: dict[str, str] = {}
        for a in failed:
            open_failures[a["tool"]] = a.get("error", "")
        progress = self._progress_report()
        return {
            "kind": "react",
            "task": (task or "")[:300],
            "stop_reason": self.stop_reason or "unknown",
            "interrupted_reason": self.interrupted_reason,
            "iterations_used": self.iterations_used,
            "max_iterations": self.max_iterations,
            "completed_actions": len(ok),
            "failed_actions": len(failed),
            "artifacts": artifacts[:50],
            "open_failures": open_failures,
            "actions": done[-self.MANIFEST_MAX_ACTIONS:],
            "last_thought": (self.thoughts[-1][:800] if self.thoughts else ""),
            "no_progress": progress,
            "tool_timeouts": getattr(self, "tool_timeouts", 0),
            "tokens": self.token_report(),
            # v1.7.2：本次并入的用户中途补充条数 —— 交付清单要能回答
            # "我插的那句话到底进去了没有"（插了没进去却不说，是最坏的一种）
            "interjections_merged": len(getattr(self, "interjections_merged", []) or []),
        }

    def _progress_report(self) -> dict[str, Any]:
        """无进展账目（老实例/未启用时返回空结构，保证清单字段稳定）。"""
        guard = getattr(self, "progress", None)
        if guard is None:
            return {"repeats": 0, "guided": 0, "blocked": 0,
                    "cache_hits": 0, "blocked_tools": {}, "tool_calls": {}}
        return guard.report()

    async def _absorb_interjections(self, messages: list[dict[str, Any]],
                                    on_interjection: Any = None) -> int:
        """把用户中途补充并入消息列表，返回并入条数。

        用户补的那句话必须以 ``user`` 消息出现：模型对"用户说了什么"和
        "系统提示说了什么"的权重完全不同，塞进 system 提示里它多半会当成
        背景噪音。渲染措辞见 :func:`automind.core.interject.render_for_model`。

        取话失败（回调抛异常）不能连累任务执行 —— 但也不静默：记一条 warning，
        用户插的话最多"没赶上这一轮"，而任务照常跑完。
        """
        source = getattr(self, "interjection_source", None)
        if source is None:
            return 0
        try:
            items = list(source() or [])
        except Exception as e:                        # pragma: no cover - 防御性
            logger.warning("interjection_drain_failed", error=str(e))
            return 0
        if not items:
            return 0

        from automind.core.interject import mark_applied, render_for_model

        messages.append({"role": "user", "content": render_for_model(items)})
        mark_applied(items, "react_step")
        merged = getattr(self, "interjections_merged", None)
        if merged is None:                            # 老实例（测试 __new__ 造的）
            merged = self.interjections_merged = []
        merged.extend({"seq": i.seq, "text": i.text, "at": "react_step"} for i in items)
        logger.info("interjection_merged", where="react_step",
                    seqs=[i.seq for i in items])
        if on_interjection:
            try:
                await on_interjection({"type": "interjection_applied",
                                       "at": "react_step",
                                       "items": [i.to_event() for i in items]})
            except Exception as e:                    # pragma: no cover - 纯观测
                logger.warning("interjection_emit_failed", error=str(e))
        return len(items)

    @staticmethod
    def render_manifest(rep: dict[str, Any]) -> str:
        """把部分交付清单渲染成给人/给模型看的中文文本。"""
        stop = rep.get("stop_reason", "")
        stop_note = {
            "max_iterations": "；即已达到最大迭代步数上限，任务可能尚未完成。",
            "no_progress": "；即在原地重复同一步动作（无进展），已提前收尾以免继续烧预算。",
            "empty_response": "；即连续多轮没有产生任何可用内容（多为输出被 max_tokens "
                              "截断，或模型只输出了推理而没有正文）。",
            "task_budget": "；即本轮任务的总时长预算或 token 预算已到点"
                           "（具体是哪一种见上方括号里的原因），"
                           "已停止开启新步骤（已完成的工作全部保留）。",
            "llm_unavailable": "；即模型服务长时间连不上（已多次自动重连），"
                               "已完成的工作全部保留，网络恢复后说「继续」即可接着做。",
            "cancelled": "；即被用户/会话中断。",
            "error": "；即中途抛错被中断。",
        }.get(stop, "")
        lines = [
            "",
            "——— 部分交付清单（本次未跑完，以下为**已经真实发生**的动作）———",
            f"· 停止原因：{stop}"
            + (f"（{rep['interrupted_reason']}）" if rep.get("interrupted_reason") else "")
            + stop_note,
            f"· 迭代进度：{rep.get('iterations_used', 0)}/{rep.get('max_iterations', 0)} 步",
            f"· 成功动作：{rep.get('completed_actions', 0)} 个；"
            f"失败动作：{rep.get('failed_actions', 0)} 个",
        ]
        np = rep.get("no_progress") or {}
        to = rep.get("tool_timeouts", 0)
        if to:
            lines.append(f"· 单步超时：{to} 次工具调用超过等待上限被中止"
                         "（输出已丢弃，避免把过期结果当成新信息）")
        if rep.get("interjections_merged"):
            lines.append(f"· 你在执行途中的补充：{rep['interjections_merged']} 条"
                         "已并入本轮（模型看到的是 user 消息，不是系统提示）")
        if np.get("repeats"):
            dup = np.get("cache_hits", 0)
            lines.append(
                f"· 无进展信号：重复动作 {np['repeats']} 次"
                + (f"（其中拦截 {np['blocked']} 次未执行）" if np.get("blocked") else "")
                + (f"；只读结果已复用 {dup} 次，避免了重复读同一份内容"
                   if dup else ""))
            worst = sorted((np.get("blocked_tools") or {}).items(),
                           key=lambda kv: -kv[1])[:3]
            if worst:
                lines.append("    反复被拦的动作："
                             + "；".join(f"{k} ×{v}" for k, v in worst))
        arts = rep.get("artifacts") or []
        if arts:
            lines.append("· 已产出的文件/产物：")
            lines += [f"    - {a}" for a in arts[:20]]
        else:
            lines.append("· 已产出的文件/产物：无（尚未落盘任何产物）")
        openf = rep.get("open_failures") or {}
        if openf:
            lines.append("· 仍未解决的失败（卡在这里）：")
            lines += [f"    - {k}: {str(v)[:160]}" for k, v in list(openf.items())[:8]]
        acts = rep.get("actions") or []
        if acts:
            lines.append("· 动作明细（时间顺序，最后若干条）：")
            for a in acts[-12:]:
                mark = "✓" if a.get("ok") else "✗"
                extra = a.get("artifact") or a.get("output") or a.get("error") or ""
                lines.append(f"    {mark} {a.get('tool')}  {str(extra)[:140]}")
        lines.append(
            "· 下一步建议：可直接说「继续」，或指出要接着做的那一条；"
            "若某个动作反复失败，请先修该失败的根因（见上方失败原因）再继续。")
        return "\n".join(lines)

    async def _run(
        self,
        task: str,
        context: str = "",
        on_thought: Any = None,
        on_action: Any = None,
        on_no_progress: Any = None,
        deadline: float | None = None,
        on_timeout: Any = None,
        on_interjection: Any = None,
        on_reconnect: Any = None,
        memory_note: str = "",
    ) -> str:
        # 只挑与本任务相关的工具完整下发，其余留一份一行式目录（见 _select_tools）
        self._select_tools(task, context)
        catalog = self._dormant_catalog()
        # v1.11.10：顺序固定为"稳定前缀在前、易变内容在后"（见 build_initial_messages）
        messages: list[dict[str, Any]] = build_initial_messages(
            self.SYSTEM_PROMPT, catalog, context, memory_note, task)
        # 暴露给上层：预算吃紧时 compact() 压的就是这个列表
        self.messages = messages

        active_sig = frozenset(self._active_tools)
        tool_schemas = self._schemas()
        trunc_before = self.fn_handler.savings_report()["truncated_results"]

        limit = self.max_iterations
        self.iteration_extensions_used = 0
        for iteration in itertools.count():
            if iteration >= limit:
                # v1.11.7：步数用完但任务仍在推进（最近几轮有工具调用成功）→ 续开一个步数窗口，
                # 而不是把做到一半的长任务交成"部分完成"。没在推进就照常收尾。
                if not self._should_extend_iterations():
                    break
                limit += self.max_iterations
                self.iteration_extensions_used += 1
                logger.info("react_iterations_extended", window=self.iteration_extensions_used,
                            new_limit=limit)
                messages.append({"role": "user", "content": (
                    f"[系统提示] 已执行 {iteration} 步，任务仍在推进，已为你续加 {self.max_iterations} 步"
                    f"（第 {self.iteration_extensions_used}/{self.max_iteration_extensions} 次续加）。"
                    "请先回顾还没完成的交付项，优先完成它们，不要重复已经做过的步骤；"
                    "全部完成后直接给出最终总结。")})
            self.iterations_used = iteration + 1
            # 用户中途补充（v1.7.2）：在下一次思考**之前**并进消息列表。
            # 放在这里而不是流式回调里，是因为 ReAct 的"步与步之间"才是唯一
            # 安全的注入点 —— 模型正在生成的这一次调用没法中途改写；而放在
            # 循环开头，意味着用户补的话最多等一步就能被看见。
            await self._absorb_interjections(messages, on_interjection)
            # v1.7.0 整轮任务预算：到点**不再开新步骤**。
            # 判据放在这里而不是"硬掐断"：掐断会丢掉已经做完的工作，
            # 而这里能把成果完整地交出去（部分交付清单）。
            if deadline is not None and time.monotonic() >= deadline + self.deadline_shift:
                self.stop_reason = "task_budget"
                logger.warning("react_task_budget_exhausted",
                               iterations=self.iterations_used,
                               max_iterations=self.max_iterations)
                break
            # 活跃工具集变了（模型点名要了某个休眠工具）→ 重建 schema
            if frozenset(self._active_tools) != active_sig:
                prev_sig = active_sig
                active_sig = frozenset(self._active_tools)
                tool_schemas = self._schemas()
                added = sorted(active_sig - prev_sig)
                if added and catalog:
                    # v1.11.10：**只追加、不改写**。
                    # 旧实现会改写第 2 条 system 消息（工具目录），而 DeepSeek /
                    # GLM / OpenAI 的提示缓存按逐字节前缀命中 —— 开头改一个字，
                    # 后面几千 token 的缓存全部作废，且这一步往往发生在任务
                    # 中段（缓存已经攒得很厚的时候）。追加一条增量说明即可，
                    # 模型知道"这些工具现在能直接调了"，前缀保持不变。
                    messages.append({"role": "system", "content": (
                        "[工具目录更新] 以下工具已加载完整 schema，现在可以直接调用："
                        + "、".join(added)
                        + "。上方目录中它们的一行式简表可以忽略。")})
            # THINK
            # v1.7.5：预算耗尽**不再让任务裸崩**。此前这里没有任何 try，
            # ResourceManager 抛出的 RuntimeError 会一路上浮成 HTTP 500 /
            # task_error，而**已经做完的工作全部丢失**（下面的
            # partial_report() 根本轮不到执行）。实测默认预算下，一个每步
            # 2.7k token 的长任务会在第 22~31 步被掐死，输出为空。
            # 现在改成"到点不再开新步骤"，复用既有的部分交付路径。
            try:
                response = await self._generate_resilient(
                    messages, tool_schemas, on_reconnect, deadline)
            except BudgetExhaustedError as e:
                self.stop_reason = "task_budget"
                self.interrupted_reason = str(e)
                logger.warning("react_token_budget_exhausted",
                               iterations=self.iterations_used,
                               error=str(e))
                break
            except Exception as e:
                # v1.7.6：模型长时间连不上（provider 层 + 任务层重连都用尽）。
                # 已经做过实事 → 交部分交付清单（成果不丢，用户可"接着做"）；
                # 一步都没做 → 照常抛出，让用户看到明确的错误与排障提示。
                if not (is_transient_llm_error(e) and self.actions):
                    raise
                self.stop_reason = "llm_unavailable"
                self.interrupted_reason = str(e)[:300]
                logger.warning("react_llm_unavailable",
                               iterations=self.iterations_used, error=str(e)[:200])
                break

            if response.text:
                self.thoughts.append(response.text)
                # 模型在思考里点名了休眠工具 → 下一轮补发它的完整 schema
                self._expand_tools_from_text(response.text)
                if on_thought:
                    await on_thought(response.text)

            # 没有工具调用 → 通常意味着任务完成
            if not response.tool_calls:
                text = (response.text or "").strip()
                # v1.7.5：空回复 / 被 max_tokens 截断**不是完成**。
                #
                # 判据此前只有"没有 tool_calls"，于是下面这条路径会直接 return ——
                # 哪怕 text 是空串。对推理模型尤其致命：deepseek-flash 这类模型
                # 的 reasoning 与 content 共用 max_tokens，实测最坏一轮 reasoning
                # 吃掉 69.6% 的预算；一旦预算被 reasoning 用光，content 就是空、
                # finish_reason 是 "length"，而任务会被记成**成功但输出为空**。
                truncated = str(getattr(response, "finish_reason", "") or "") == "length"
                if text and not truncated:
                    if self.stop_on_no_tools:
                        self.stop_reason = "no_more_tools"
                        await self._report_truncations(trunc_before)
                        return response.text
                    messages.append({"role": "assistant", "content": response.text})
                    continue
                if self.stop_on_no_tools:
                    self._empty_response_streak += 1
                    if self._empty_response_streak >= self.EMPTY_RESPONSE_LIMIT:
                        self.stop_reason = "empty_response"
                        self.interrupted_reason = (
                            f"连续 {self._empty_response_streak} 次收到空回复"
                            + ("（finish_reason=length，输出被 max_tokens 截断）"
                               if truncated else "")
                            + "，已停止以免空转")
                        logger.warning("react_empty_response",
                                       streak=self._empty_response_streak,
                                       truncated=truncated,
                                       iterations=self.iterations_used)
                        break
                    messages.append({"role": "user", "content": (
                        "上一轮没有产生任何内容"
                        + ("（输出被 max_tokens 截断，请直接给结论，不要长篇推理）"
                           if truncated else "（既没有正文也没有工具调用）")
                        + "。请立刻给出**简短**的结论或下一步动作。")})
                    continue
                messages.append({"role": "assistant", "content": response.text})
                continue

            # ACT — 先做权限门控，再执行
            messages.append(self._assistant_message(response))
            results = []
            for tc in response.tool_calls:
                # v1.7.5：**先归一化工具名，再做任何判断**。
                # 放在这里而不是 _gate 里，是因为归一化不该依赖"有没有配权限引擎"：
                # 未配权限引擎时 _gate 会提前返回，名字就保持原样流到熔断记账与
                # 事件推送里 —— 于是 `read_file` 与 `file_read` 变成两条独立的
                # 失败记录，各自计数，熔断永远攒不到阈值。
                self._normalize_tool_call(tc)
                # v1.7.6：参数 JSON 残缺/被截断 → 不执行，直接告诉模型真正的原因
                # 与修法。放在门控之前：残缺参数既不该弹审批，也不该被执行。
                arg_err = getattr(tc, "arguments_error", "")
                if arg_err:
                    note = self._broken_args_note(tc.name, arg_err)
                    self._record_tool_outcome(tc.name, False, note)
                    results.append(ToolResult(
                        tool_name=tc.name, success=False, error=note,
                        metadata={"arguments_error": True}))
                    continue
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
                if tripped and await self._network_half_open(tc.name):
                    tripped = ""            # v1.11.6：网络已恢复 → 半开放行，不再整轮封死
                if tripped:
                    results.append(ToolResult(
                        tool_name=tc.name, success=False, error=tripped))
                    continue
                # v1.7.0 无进展治理（见 planning/react_progress.py）：
                #   · 完全相同的动作连续出现 → 先提示、后拦截（本地判定，零 token）
                #   · 只读工具的同参调用 → 直接复用上次结果，不执行也不重发内容
                verdict, advice = self._progress_check(tc)
                if verdict == "block":
                    results.append(ToolResult(
                        tool_name=tc.name, success=False, error=advice))
                    if on_no_progress:
                        await self._emit_no_progress(on_no_progress, tc, advice)
                    continue
                reuse = self._cached_result(tc)
                if reuse is not None:
                    results.append(self._as_reuse(tc, reuse, advice))
                    continue
                try:
                    args = tc.arguments if isinstance(tc.arguments, dict) else {}
                    # v1.7.0 单步超时：工具挂住不再等于任务挂住。
                    timeout = self._effective_timeout(args)
                    if timeout:
                        result = await asyncio.wait_for(
                            self.tool_registry.dispatch(tc.name, **args),
                            timeout=timeout)
                    else:
                        result = await self.tool_registry.dispatch(tc.name, **args)
                    # TDD 内环：代码修改后立即语法验证并注入观察
                    result = self._auto_validate_result(tc, result)
                    self._remember_result(tc, result)
                    self._record_tool_outcome(tc.name, result.success,
                                              result.error or "",
                                              network=self._is_network_failure(result))
                    results.append(self._with_advice(result, advice))
                except TimeoutError:
                    # 超时不是"工具失败"，但**必须**走同一条熔断与反馈链路：
                    # 否则模型会看不出区别地再试一遍，又白等一个 timeout。
                    note = self._tool_timeout_note(tc.name, timeout)
                    self.tool_timeouts += 1
                    self._record_tool_outcome(tc.name, False, note)
                    if on_timeout:
                        await self._emit_tool_timeout(on_timeout, tc, timeout, note)
                    results.append(ToolResult(
                        tool_name=tc.name, success=False, error=note,
                        metadata={"timeout": True, "timeout_s": timeout}))
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

            # 无进展提前收尾：反复做同一件事时，继续跑到迭代上限只是继续付钱。
            # 判据是"被拦截的次数"而非"重复次数"——被放行的重复仍可能走在
            # 正确的收敛路径上（例如分批处理），只有真被拦住才算烧空转。
            if self._no_progress_exhausted():
                self.stop_reason = "no_progress"
                break

            # v1.7.5：连续多轮**一个工具都没成功**也提前收尾。
            # 上面那条看的是"动作重不重复"，熔断看的是"同一个工具失败几次"，
            # 而两者都能被"每步换个名字/参数"绕开。这条看的是结果。
            self._note_round_outcome(results)
            if self._no_success_exhausted():
                self.stop_reason = "no_progress"
                self.interrupted_reason = (
                    f"连续 {self._rounds_without_success} 轮没有任何工具调用成功"
                    f"（多为权限被拒、参数不合法或工具名不存在），已提前收尾")
                logger.warning("react_no_successful_tool_call",
                               rounds=self._rounds_without_success,
                               iterations=self.iterations_used)
                break

        if self.stop_reason not in ("no_progress", "task_budget", "empty_response",
                                    "llm_unavailable"):
            # 达到迭代上限：**给出部分交付清单**，而不是把已完成的工作丢掉只留一句提示
            self.stop_reason = "max_iterations"
        await self._report_truncations(trunc_before)
        rep = self.partial_report(task)
        head = rep["last_thought"] or "（本轮没有产生可用的思考内容）"
        return head + "\n" + self.render_manifest(rep)

    @staticmethod
    def _broken_args_note(name: str, arg_err: str) -> str:
        """参数残缺时喂回模型的说明 —— 要讲清"为什么"和"下一步怎么改"。"""
        return (f"{arg_err}\n建议：调用「{name}」时把大段内容拆小 —— "
                f"先写入文件的前一部分，再用追加/编辑方式分几次补全；"
                f"或只传必要参数、避免在参数里内嵌超长文本。不要原样重试同一次调用。")

    # ── v1.7.6 任务层重连 ──────────────────────────────────

    #: provider 层自己的退避重试（约半分钟）用尽后，任务层再按这些间隔等待
    #: 重连。合计约 100 秒 —— 覆盖"代理切节点 / Wi-Fi 重连 / 中转重启"这类
    #: 常见的分钟级中断，又不会让一个真的配错了的地址把任务挂上十几分钟。
    LLM_RECONNECT_DELAYS: tuple[float, ...] = (10.0, 30.0, 60.0)

    async def _generate_resilient(self, messages: list[dict[str, Any]],
                                  tool_schemas: Any, on_reconnect: Any,
                                  deadline: float | None) -> Any:
        """``llm.generate`` + 任务层重连。

        用户报告"任务跑一段时间就 Connection error."：长任务动辄几十次模型
        调用，只要其中**一次**碰上中转抖动，此前整轮就作废。这里在 provider
        层重试之外再兜一层：等一等、推一条"正在重连"事件给界面，再原样重发
        同一组消息 —— 消息列表没动，模型看到的上下文与断线前完全一致。
        """
        delays = tuple(getattr(self, "llm_reconnect_delays", self.LLM_RECONNECT_DELAYS))
        attempt = 0
        offline_waits = 0
        while True:
            try:
                resp = await self.llm.generate(messages, tools=tool_schemas)
                if attempt:
                    await self._emit_reconnect(on_reconnect, {
                        "type": "llm_reconnect", "state": "recovered",
                        "attempt": attempt})
                    logger.info("react_llm_reconnected", attempts=attempt)
                return resp
            except (BudgetExhaustedError, asyncio.CancelledError):
                raise
            except LLMContextTooLargeError as e:
                # v1.11.11：上下文溢出不再直接让任务失败 —— 先折叠一次旧观察，
                # 再原样重发。只重试一次：压缩救不了的单条超大消息（一次塞进
                # 几十万字符的工具结果）继续重试也只是再烧一次调用。
                if getattr(self, "context_retry_used", False):
                    raise
                self.context_retry_used = True
                stat = self.compact()
                logger.warning("react_context_overflow_retry",
                               folded=stat.get("folded", 0),
                               chars_reclaimed=stat.get("chars_reclaimed", 0),
                               error=str(e)[:200])
                await self._emit_reconnect(on_reconnect, {
                    "type": "context_retry", "folded": stat.get("folded", 0),
                    "chars_reclaimed": stat.get("chars_reclaimed", 0),
                    "est_tokens_saved": stat.get("est_tokens_saved", 0),
                })
                continue
            except Exception as e:
                if not is_transient_llm_error(e):
                    raise
                if attempt >= len(delays):
                    # v1.11.7：重连间隔用完时先看看是不是"本机断网了"。断网（Wi-Fi 掉线、
                    # 云电脑网络重置）常常持续几分钟 —— 此前 100 秒后整轮任务作废，做了一半的
                    # 长任务全部白跑。断网就等网络回来（有上限、尊重整轮时长预算），回来后再重试。
                    if offline_waits >= self.LLM_OFFLINE_WAITS or not await self._wait_offline(
                            on_reconnect, deadline, e):
                        raise
                    offline_waits += 1
                    attempt = len(delays) - 1           # 恢复后再给一次完整的最后一档重试
                    continue
                wait = float(delays[attempt])
                # 整轮时长预算快到了：别为了重连把预算睡过去
                if deadline is not None and \
                        time.monotonic() + wait >= deadline + getattr(self, "deadline_shift", 0.0):
                    raise
                attempt += 1
                logger.warning("react_llm_reconnect_wait", attempt=attempt,
                               wait_s=wait, error=str(e)[:200])
                await self._emit_reconnect(on_reconnect, {
                    "type": "llm_reconnect", "state": "waiting",
                    "attempt": attempt, "max_attempts": len(delays),
                    "wait_s": wait, "kind": classify_llm_error(e),
                    "error": str(e)[:240]})
                await asyncio.sleep(wait)

    #: 断网时最多等几次网络恢复（每次最长 LLM_OFFLINE_MAX_WAIT 秒）
    LLM_OFFLINE_WAITS = 2
    LLM_OFFLINE_MAX_WAIT = 600.0

    async def _wait_offline(self, on_reconnect: Any, deadline: float | None, err: Exception) -> bool:
        """本机断网 → 等网络恢复；网络本来就通（是服务端的问题）或等不到 → False。"""
        try:
            from automind.core.netresilience import network_up, wait_for_network
            ok, detail = await network_up(fresh=True)
        except Exception:                             # pragma: no cover - 探测失败按"不是断网"
            return False
        if ok:
            return False
        budget = self.LLM_OFFLINE_MAX_WAIT
        if deadline is not None:
            budget = min(budget, deadline + getattr(self, "deadline_shift", 0.0) - time.monotonic())
        if budget <= 5:
            return False
        logger.warning("react_llm_offline_wait", max_wait_s=round(budget), detail=detail)
        await self._emit_reconnect(on_reconnect, {
            "type": "llm_reconnect", "state": "offline", "wait_s": round(budget),
            "error": f"本机网络不可用（{detail}），等待恢复后继续：{str(err)[:160]}"})
        recovered, waited = await wait_for_network(budget, interval=5.0)
        if recovered:
            logger.info("react_llm_network_back", waited_s=round(waited))
        return recovered

    @staticmethod
    async def _emit_reconnect(cb: Any, event: dict[str, Any]) -> None:
        if cb is None:
            return
        try:
            await cb(event)
        except Exception as e:          # pragma: no cover - 纯观测
            logger.warning("react_reconnect_emit_failed", error=str(e))

    # ── v1.7.0 单步超时 ────────────────────────────────────

    def _effective_timeout(self, args: dict[str, Any]) -> float:
        """本次工具调用的超时上限：模型可申请更长，但不越上限。

        ``timeout`` 是工具参数里已存在的约定（``terminal`` 就用它表达
        "这条命令要跑很久"）。这里把它同时用于 ReAct 的**等待上限**，
        避免出现"命令自己允许跑 600 秒，而外层 300 秒就把它掐了"的矛盾。
        """
        if not getattr(self, "tool_timeout", 0):
            return 0.0
        budget = float(self.tool_timeout)
        requested = args.get("timeout") if isinstance(args, dict) else None
        if requested is not None:
            try:
                want = float(requested)
            except (TypeError, ValueError):
                want = 0.0
            if want > 0:
                budget = max(budget, want)
        cap = float(getattr(self, "tool_timeout_max", 0) or 0)
        if cap > 0:
            budget = min(budget, cap)
        return budget

    def _tool_timeout_note(self, name: str, timeout: float) -> str:
        """超时喂回模型的说明 —— 必须包含"下一步怎么办"，否则它会原样重试。"""
        cap = float(getattr(self, "tool_timeout_max", 0) or 0)
        hint = (f"如需更长执行时间，可在调用参数里显式给 timeout（上限 {cap:.0f} 秒）；"
                if cap else "如需更长执行时间，可在调用参数里显式给 timeout；")
        return (
            f"工具「{name}」执行超过 {timeout:.0f} 秒仍未返回，已中止等待"
            f"（**本次输出已丢弃**，不要再原样重试同一组参数）。\n"
            f"建议：{hint}"
            f"若命令本身就该跑很久，改用后台通道 "
            f"terminal(command=..., background=true) 再轮询取其结果；"
            f"若是网络请求，缩小范围或先确认目标可达。")

    async def _emit_tool_timeout(self, cb: Any, tc: ToolCall,
                                 timeout: float, note: str) -> None:
        try:
            await cb({
                "type": "tool_timeout", "tool": tc.name,
                "timeout_s": timeout, "timeouts_total": self.tool_timeouts,
                "reason": note.splitlines()[0],
            })
        except Exception as e:          # pragma: no cover - 纯观测
            logger.warning("tool_timeout_emit_failed", tool=tc.name, error=str(e))

    # ── v1.7.0 无进展治理的小接口（老实例缺属性时全部安全降级）──

    def _progress_check(self, tc: ToolCall) -> tuple[str, str]:
        guard = getattr(self, "progress", None)
        if guard is None:
            return "run", ""
        try:
            verdict, advice = guard.check(
                tc.name, tc.arguments if isinstance(tc.arguments, dict) else {})
        except Exception as e:          # pragma: no cover - 治理失败不阻塞任务
            logger.warning("react_progress_check_failed", tool=tc.name, error=str(e))
            return "run", ""
        if verdict == "guide":
            logger.info("react_repeat_action_guided", tool=tc.name)
        elif verdict == "block":
            logger.warning("react_repeat_action_blocked", tool=tc.name,
                           blocked=guard.blocked)
        return verdict, advice

    def _cached_result(self, tc: ToolCall) -> ToolResult | None:
        """只读工具的同参调用 → 复用上次结果（不执行、不重发内容）。"""
        guard = getattr(self, "progress", None)
        if guard is None or not self._is_read_only(tc.name):
            return None
        try:
            hit = guard.cached(tc.name,
                               tc.arguments if isinstance(tc.arguments, dict) else {})
        except Exception:               # pragma: no cover
            return None
        if hit is None:
            return None
        logger.info("react_result_reused", tool=tc.name)
        return hit

    def _remember_result(self, tc: ToolCall, result: ToolResult) -> None:
        guard = getattr(self, "progress", None)
        if guard is None or not result.success or not self._is_read_only(tc.name):
            return
        try:
            guard.remember(tc.name,
                           tc.arguments if isinstance(tc.arguments, dict) else {},
                           result)
        except Exception:               # pragma: no cover
            pass

    def _is_read_only(self, name: str) -> bool:
        """该工具是否可安全复用结果（SAFE 级且无已知副作用）。"""
        if name in SIDE_EFFECT_TOOLS:
            return False
        try:
            tool = self.tool_registry.get(name)
        except Exception:
            return False
        from automind.core.types import PermissionTier
        return getattr(tool, "permission_tier", None) == PermissionTier.SAFE

    def _as_reuse(self, tc: ToolCall, cached: ToolResult,
                  advice: str) -> ToolResult:
        """把缓存结果包成一条"已复用"的观察（含去重说明，供模型与账目共用）。"""
        note = ("（结果与本次调用之前完全相同，已直接复用，未重复执行；"
                "如需最新内容请改变参数，例如换 offset/limit 或换路径）")
        out = cached.output
        try:
            if isinstance(out, dict):
                out = {**out, "reused_previous_result": True}
            else:
                out = f"{out}\n{note}"
        except Exception:               # pragma: no cover
            out = f"{out}\n{note}"
        return ToolResult(tool_name=tc.name, success=True, output=out,
                          duration_ms=0.0)

    @staticmethod
    def _with_advice(result: ToolResult, advice: str) -> ToolResult:
        """把纠偏提示附在结果尾部 —— 模型下一轮就会看到"你在重复"。"""
        if not advice:
            return result
        try:
            if isinstance(result.output, dict):
                result.output = {**result.output, "no_progress_warning": advice}
            else:
                result.output = f"{result.output}\n\n{advice}"
        except Exception:               # pragma: no cover
            pass
        return result

    async def _emit_no_progress(self, cb: Any, tc: ToolCall, advice: str) -> None:
        try:
            guard = getattr(self, "progress", None)
            await cb({
                "type": "react_no_progress", "tool": tc.name,
                "blocked": getattr(guard, "blocked", 0),
                "repeats": getattr(guard, "repeats", 0),
                "reason": advice.splitlines()[0] if advice else "重复动作已拦截",
            })
        except Exception as e:          # pragma: no cover - 纯观测
            logger.warning("react_no_progress_emit_failed", error=str(e))

    def _no_progress_exhausted(self) -> bool:
        if not self.no_progress_limit:
            return False
        guard = getattr(self, "progress", None)
        return bool(guard is not None and guard.blocked >= self.no_progress_limit)

    async def _report_truncations(self, before: int) -> None:
        """把本轮新增的"工具输出被夹住"账目累加进 token_savings。

        上层（agent）据此推 ``tool_output_truncated`` 事件 —— 让
        "这次任务有多少上下文是被截掉的"从隐形变成可见、可调参。
        """
        try:
            rep = self.fn_handler.savings_report()
            new = rep["truncated_results"] - before
            if new > 0:
                self.token_savings["truncated_results"] = rep["truncated_results"]
                self.token_savings["chars_dropped"] = rep["dropped_chars"]
                logger.info("react_tool_output_truncated", count=new,
                            chars_dropped=rep["dropped_chars"],
                            est_tokens_saved=rep["est_tokens_saved"])
        except Exception as e:            # pragma: no cover - 纯观测，失败不影响任务
            logger.warning("truncation_report_failed", error=str(e))

    #: 同一工具连续失败达到此次数即熔断，本轮任务内不再调用
    FAILURE_THRESHOLD = 3

    #: 连续这么多**轮**没有任何一次工具调用成功，即判定无进展并提前收尾。
    #: 为什么需要这条：熔断按工具名计数、无进展按动作指纹计数 —— 两个都能被
    #: "每一步换一个工具名/参数"绕开（实测：terminal → cmd /c →
    #: execute_shell_command → python_sandbox，熔断确实触发了，但任务没有停，
    #: 一路烧到 token 预算耗尽）。这条护栏看的是**结果**（有没有真的成事），
    #: 与模型换了多少种花样无关。
    NO_SUCCESS_LIMIT = 5

    #: 连续这么多轮收到"空回复/被截断的回复"即收尾。
    #: 判据是"没有正文且没有工具调用"—— 推理模型把 max_tokens 全花在
    #: reasoning 上时就是这个形态（见 _run 里的说明）。
    EMPTY_RESPONSE_LIMIT = 3

    #: v1.11.6：网络类失败（DNS / 连接重置 / 超时……）单独计数的熔断阈值。
    #: 网络抖动不代表工具坏了：按普通失败算，断网 30 秒就足以让 terminal 整轮作废。
    NET_FAILURE_THRESHOLD = 6
    #: 一轮任务里网络类失败的总上限：探测说"网通了"但命令仍报网络错（例如只有某个
    #: 境外源不通）时，半开放行不能无限循环
    NET_FAILURE_CAP = 12

    @staticmethod
    def _is_network_failure(result: Any) -> bool:
        if getattr(result, "success", False):
            return False
        md = getattr(result, "metadata", None) or {}
        if "network_error" in md:
            return bool(md["network_error"])
        from automind.core.netresilience import is_network_error, result_text
        return is_network_error(result_text(result))

    async def _network_half_open(self, name: str) -> bool:
        """熔断是网络类失败攒出来的、且网络现在已恢复 → 清零并放行一次（半开）。"""
        st = self._tool_failures.get(name) or {}
        if not st.get("network") or st.get("net_total", 0) >= self.NET_FAILURE_CAP:
            return False
        try:
            from automind.core.netresilience import network_up
            ok, _ = await network_up(timeout=2.0, fresh=True)
        except Exception:                          # pragma: no cover - 探测失败按未恢复
            return False
        if ok:
            st["streak"] = 0
            st["net_streak"] = 0
            logger.info("tool_circuit_half_open", tool=name, reason="network_recovered")
        return ok

    def _record_tool_outcome(self, name: str, success: bool, error: str,
                             network: bool = False) -> None:
        """记录一次工具调用结果；连续失败到阈值即熔断并留痕。

        网络类失败（``network=True``）记在 ``net_streak`` 上、阈值更高；熔断后只要
        网络恢复就半开放行（见 ``_network_half_open``）。普通失败照旧 3 次熔断。
        """
        st = self._tool_failures.setdefault(name, {"streak": 0, "last": ""})
        if success:
            st["streak"] = 0
            st["net_streak"] = 0
            st["network"] = False
            st["last"] = ""
            return
        if network:
            st["net_streak"] = st.get("net_streak", 0) + 1
            st["net_total"] = st.get("net_total", 0) + 1
            st["network"] = True
            st["last"] = (error or "")[:300]
            logger.warning("tool_call_network_failed", tool=name,
                           net_streak=st["net_streak"], error=st["last"])
            if st["net_streak"] >= self.NET_FAILURE_THRESHOLD or st["net_total"] >= self.NET_FAILURE_CAP:
                st["streak"] = max(st["streak"], self.FAILURE_THRESHOLD)
                logger.error("tool_circuit_open", tool=name, reason="network",
                             last_error=st["last"])
            return
        st["network"] = False
        st["streak"] += 1
        st["last"] = (error or "")[:300]
        logger.warning("tool_call_failed", tool=name,
                       streak=st["streak"], error=st["last"])
        if st["streak"] == self.FAILURE_THRESHOLD:
            logger.error("tool_circuit_open", tool=name,
                         threshold=self.FAILURE_THRESHOLD, last_error=st["last"])

    def _note_round_outcome(self, results: list[Any]) -> None:
        """记一轮（一次 THINK+ACT）里是否有任何工具调用成功。

        被权限拒绝、被无进展拦截、被熔断挡下、工具抛异常 —— 这些都不算成功。
        连续 ``NO_SUCCESS_LIMIT`` 轮毫无成功即认为任务在原地烧钱。
        """
        if any(getattr(r, "success", False) for r in results):
            self._rounds_without_success = 0
            return
        self._rounds_without_success = getattr(self, "_rounds_without_success", 0) + 1

    #: v1.11.7 步数窗口：步数用完但仍在推进时最多续加几次（每次 = max_iterations 步）
    MAX_ITERATION_EXTENSIONS = 2
    #: 判定"仍在推进"：最近这么多轮里至少有一轮有工具调用成功
    PROGRESS_WINDOW = 3

    def _should_extend_iterations(self) -> bool:
        if getattr(self, "iteration_extensions_used", 0) >= getattr(
                self, "max_iteration_extensions", self.MAX_ITERATION_EXTENSIONS):
            return False
        if getattr(self, "_rounds_without_success", 0) >= self.PROGRESS_WINDOW:
            return False
        # 有工具调用过才谈得上"在推进"；一直在空想的不续
        return bool(getattr(self, "actions", None))

    def _no_success_exhausted(self) -> bool:
        return getattr(self, "_rounds_without_success", 0) >= self.NO_SUCCESS_LIMIT

    def _breaker_reason(self, name: str) -> str:
        """熔断已触发时返回给模型看的说明，否则返回空串。"""
        st = self._tool_failures.get(name)
        if not st or st["streak"] < self.FAILURE_THRESHOLD:
            return ""
        if st.get("network"):
            return (f"工具「{name}」因网络中断已失败 {st.get('net_streak', 0)} 次，网络仍未恢复，"
                    f"暂停调用。最后一次的失败原因：{st['last']}。"
                    f"请先调用 wait_for_network 等网络恢复（恢复后会自动重新放行），"
                    f"或先完成不需要联网的步骤。")
        return (f"工具「{name}」已连续失败 {st['streak']} 次，已停止调用以免空转。"
                f"最后一次的失败原因：{st['last']}。"
                f"请改用其它方式完成该步骤，或先修复该工具的配置。")

    def tool_failure_report(self) -> dict[str, dict]:
        """本轮各工具的失败情况（供上层推送给前端标红）。"""
        return {k: dict(v) for k, v in self._tool_failures.items() if v["streak"]}

    def _normalize_tool_call(self, tc: ToolCall) -> str:
        """把 ``tc.name`` 就地改写成注册表里的真名；返回解析结果（可能为空串）。

        改的是 ``tc`` 本身：后续的门控、熔断记账、事件推送读的都是同一个
        名字，这样同一个工具的两种拼写才不会被记成两条独立记录。
        """
        try:
            if self.tool_registry.get(tc.name) is not None:
                return tc.name                 # 本来就是真名
        except Exception:
            pass
        resolved = self._resolve_tool_name(tc.name)
        if resolved and resolved != tc.name:
            logger.info("tool_name_normalized", requested=tc.name, resolved=resolved)
            tc.name = resolved
        return resolved

    def _resolve_tool_name(self, name: str) -> str:
        """把模型写的工具名解析成注册表里的**真名**（解析不了返回空串）。

        复用 ``tools/base.py`` 已有的 ``resolve_name``：它按"归一化名 → 词序 →
        相似度"三层匹配，且只在唯一命中时才替模型做主（有歧义就报错让它自己选）。
        """
        try:
            from automind.tools.base import resolve_name

            resolved, _suggestions = resolve_name(name, self.tool_registry.list_names())
            return resolved or ""
        except Exception as e:                        # pragma: no cover - 防御
            logger.warning("tool_name_resolve_failed", tool=name, error=str(e))
            return ""

    async def _gate(self, tc: ToolCall) -> tuple[bool, str]:
        """工具调用前的权限门控。返回 (是否允许, 原因)。"""
        if self.permissions is None:
            return True, ""
        from automind.core.types import PermissionTier

        # v1.7.5：工具名已在 `_normalize_tool_call` 里归一化过（在主循环里、
        # 先于任何判断执行），这里直接按真名取权限等级即可。
        try:
            tool = self.tool_registry.get(tc.name)
        except Exception:
            tool = None

        if tool is None:
            # 解析不出来 = 真的没有这个工具。按**最高风险**处理（DANGEROUS），
            # 而不是原先的 SENSITIVE —— 一个来路不明的名字不该比 terminal
            # 更"温和"；plan_executor 对同一情形用的也是 DANGEROUS。
            tier = PermissionTier.DANGEROUS
        else:
            tier = tool.permission_tier

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
            self.skipped_approvals = getattr(self, "skipped_approvals", 0) + 1
            logger.warning("approval_channel_missing", tool=tc.name,
                           decision="denied", tier=tier.value)
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
            if not outcome.approved:
                logger.info("approval_denied", tool=tc.name,
                            comment=outcome.comment or "")
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
