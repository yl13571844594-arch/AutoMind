"""配置管理 — YAML/JSON/ENV 统一加载，Pydantic Settings 校验。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProviderConfig(BaseModel):
    """单个 LLM 提供商配置。"""

    provider: str = "openai"
    model: str = "gpt-4o"
    api_key: str = ""
    api_base: str = ""
    max_tokens: int = 8192
    temperature: float = 0.7
    top_p: float = 1.0
    timeout: float = 120.0
    max_retries: int = 2
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)


class PermissionPolicy(BaseModel):
    """权限策略配置。"""

    safe_patterns: list[str] = Field(default_factory=lambda: [
        r"^ls\b", r"^dir\b", r"^cat\b", r"^echo\b", r"^pwd\b",
        r"^mkdir\b", r"^cd\b", r"^cp\b", r"^mv\b",
        r"^git\s+status\b", r"^git\s+log\b", r"^git\s+diff\b",
        r"^python\s+--version\b", r"^pip\s+list\b", r"^which\b",
    ])
    sensitive_patterns: list[str] = Field(default_factory=lambda: [
        r"^pip\s+install\b", r"^npm\s+install\b", r"^git\s+commit\b",
        r"^git\s+push\b", r"^python\s+-m\s+pytest\b",
    ])
    dangerous_patterns: list[str] = Field(default_factory=lambda: [
        r"rm\s+-rf\b", r"sudo\b", r"chmod\b", r"chown\b",
        r">\s*/dev/", r"mkfs\.", r"dd\s+if=",
        r"git\s+push\s+--force\b", r"git\s+reset\s+--hard\b",
        r"docker\s+rm\b", r"docker\s+system\s+prune\b",
    ])
    allowed_paths: list[str] = Field(default_factory=list)
    denied_paths: list[str] = Field(default_factory=list)
    require_approval_for_tier: str = "sensitive"
    auto_approve_safe: bool = True


class MemoryConfig(BaseModel):
    """记忆系统配置。"""

    short_term_max_tokens: int = 128000
    short_term_summary_threshold: float = 0.8
    chroma_persist_dir: str = ".automind/chroma"
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    long_term_top_k: int = 5


class ExecutionConfig(BaseModel):
    """执行配置。"""

    mode: str = "plan_and_execute"
    max_iterations: int = 50
    max_retries: int = 3
    retry_delay_seconds: float = 2.0
    # tool_timeout_seconds 定义在下方「终端命令超时策略」一节 —— 只此一处。
    # （v1.6.4 曾在这里留下一份旧值 120.0，与下面的 300.0 重复定义；
    #   Pydantic 取后者，于是"读到的默认值"与"实际生效的默认值"自相矛盾。
    #   数值型配置尤其容易这样漂移，所以宁可把它挪到唯一一处并注明。）
    sandbox_timeout_seconds: float = 30.0
    checkpoint_enabled: bool = True
    checkpoint_dir: str = ".automind/checkpoints"
    auto_approve_safe: bool = True
    parallelism_enabled: bool = True
    # 审批模式: ask（询问）| auto（自动，默认）| approve_all（全批准）
    approval_mode: str = "auto"
    # Loop 工程：单次循环最大迭代次数
    loop_max_iterations: int = 8

    # ── 自主任务闭环（默认全开，可单独关闭）──
    # 多 Agent 审查：工作模式执行完成后由审阅者角色复核结果
    auto_review: bool = True
    # Loop 验证：工作/编程模式完成后语义验收，未达标自动带反馈重试
    auto_verify: bool = True
    # 验证重试上限（auto_verify 触发的补充轮数）
    auto_verify_max_rounds: int = 2
    # TDD 闭环：编程模式每次代码修改后自动语法/测试验证并反馈给模型
    auto_test: bool = True
    # 并行执行：计划中互不依赖的目标用 asyncio.gather 并发执行
    parallel_execution: bool = True
    # 子任务缓存：同一任务内相同的只读工具调用结果复用
    subtask_cache: bool = True
    # ReAct 单轮下发的工具 schema 上限。工具 schema 每一步都要重发一遍，
    # 31 个内置工具约 6k~8k token/步；只发相关的那批可省掉一半以上。
    # 设为 0 表示不限（全量下发，行为同 v1.6.2 及更早）。
    react_tool_budget: int = 14

    # ── v1.6.4 工具输出体积治理（token 成本与上下文超载的最大单一来源）──
    # 工具结果进上下文前的单条上限（字符）。此前 `str(result.output)` **原样**
    # 塞进下一轮请求：一次大文件读取 / 长网页抓取 / 数千行终端输出，下一轮
    # 就要整体重发一遍。折叠机制只作用于历史消息，管不住当前轮刚产生的大输出。
    # 0 = 不限制（恢复 v1.6.3 行为）。
    tool_output_max_chars: int = 12000
    # 超限时保留的头部字符数（报错与结论多在尾部，尾部另留 tail 配额）
    tool_output_head_chars: int = 7000
    # 超限时保留的尾部字符数
    tool_output_tail_chars: int = 3000
    # 单条工具结果按 token 估算的硬上限（字符数 / 3.5）；超出后再夹一次
    tool_output_max_tokens: int = 6000
    # 折叠**历史**观察时每条保留的字符数（ReActExecutor.OBS_KEEP_CHARS 默认 240）。
    # 与上面的"当前轮上限"互补：一个管进不来的大输出，一个管越攒越多的历史。
    # 0 表示用类默认值。
    compact_keep_obs_chars: int = 0

    # ── v1.6.4 终端命令超时策略 ──
    # 模型未显式给 timeout 时的默认值（秒）。旧的 120s 对 pip install /
    # 编译 / 长测试偏短，超时后状态丢失且任务判败，模型只能盲目重跑。
    # **本字段只在此处定义一次**（历史遗留的重复定义会让"默认值"名不副实）。
    tool_timeout_seconds: float = 300.0
    # 模型可通过 timeout 参数申请的上限（防止单条命令把并发槽占死）
    tool_timeout_max_seconds: float = 1800.0
    # 后台通道：超长命令可走后台执行并轮询，而不是同步等死
    terminal_background_enabled: bool = True

    # ── v1.6.4 审批等待占槽治理 ──
    # 「询问」模式下单次审批等待上限（秒）。0 = 用环境变量
    # AUTOMIND_APPROVAL_TIMEOUT / 内置 300s。
    approval_timeout_seconds: float = 300.0
    # 超时后的处置：reject（自动拒绝，默认）| approve（自动批准，慎用）
    approval_timeout_action: str = "reject"
    # 审批等待期间是否释放并发执行槽（挂起时不再占着名额）
    release_slot_on_approval_wait: bool = True

    # ── v1.6.4 目录级并发写冲突隔离 ──
    # 同进程内按路径加锁串行化写入；并对"别的会话刚写过、我没读过"的文件
    # 给出确定性冲突提示，避免静默覆盖对方成果后又被回滚链悄悄还原。
    write_conflict_policy: str = "warn"      # off | warn | block
    # 会话级独立工作目录：每个会话任务克隆一份独立工作区（真隔离，
    # 但磁盘与初始化成本更高）；**默认关闭** —— 它改变产物的落点，
    # 必须是用户的显式选择，不能悄悄替用户决定"东西写到哪去了"。
    isolate_workspace: bool = False
    # 每个会话保留的隔离工作目录数量上限（超出后淘汰最旧的）
    workspace_keep: int = 8

    # ── v1.6.4 执行证据落盘（session trace）──
    # 把 agent 行为逐条写成 JSONL（观测中心此前只在内存里，日志里只有
    # 访问日志，B 端排障 / SLA 举证 / 失败归因无据可查）。
    trace_enabled: bool = True
    trace_dir: str = ".automind/traces"
    # 单次运行轨迹文件的字符上限（超出后只记计数，不再追加正文）
    trace_max_run_bytes: int = 8 * 1024 * 1024
    # 每个会话保留的轨迹文件数上限
    trace_max_runs: int = 20


class TUIConfig(BaseModel):
    """终端 UI 配置。"""

    theme: str = "dark"
    show_plan_tree: bool = True
    show_token_usage: bool = True
    max_chat_history: int = 500


class AgentConfig(BaseSettings):
    """AutoMind Agent 完整配置。"""

    model_config = SettingsConfigDict(
        env_prefix="AUTOMIND_",
        env_nested_delimiter="__",
        yaml_file=None,
        json_file=None,
    )

    # LLM 配置
    llm: LLMProviderConfig = Field(default_factory=LLMProviderConfig)
    fallback_llms: list[LLMProviderConfig] = Field(default_factory=list)

    # 权限
    permissions: PermissionPolicy = Field(default_factory=PermissionPolicy)

    # 记忆
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    # 执行
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)

    # UI
    tui: TUIConfig = Field(default_factory=TUIConfig)

    # 杂项
    project_root: str = "."
    debug: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: str | Path) -> AgentConfig:
        """从 YAML 文件加载配置。"""
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, path: str | Path) -> AgentConfig:
        """从 JSON 文件加载配置。"""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls.model_validate(data)

    @classmethod
    def auto_load(cls, project_root: str | Path = ".") -> AgentConfig:
        """自动发现并加载配置: YAML > JSON > ENV > 默认。"""
        root = Path(project_root)
        for name in ("automind.yaml", "automind.yml", ".automind.yaml"):
            p = root / name
            if p.exists():
                cfg = cls.from_yaml(p)
                cfg.project_root = str(root)
                return cfg
        p = root / "automind.json"
        if p.exists():
            cfg = cls.from_json(p)
            cfg.project_root = str(root)
            return cfg
        return cls(project_root=str(root))

    def model_post_init(self, __context: Any) -> None:
        """初始化后从环境变量补充 API Key。"""
        # provider → 环境变量的映射此前在 config.py / server_store.py /
        # server.py 各写了一份，加一家提供商就得改三处；现统一到 resolver
        from automind.core.provider_resolver import ENV_KEY_MAP

        if not self.llm.api_key:
            env_var = ENV_KEY_MAP.get(self.llm.provider, "")
            if env_var:
                self.llm.api_key = os.environ.get(env_var, "")
        for fb in self.fallback_llms:
            if not fb.api_key:
                env_var = ENV_KEY_MAP.get(fb.provider, "")
                if env_var:
                    fb.api_key = os.environ.get(env_var, "")
