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
    tool_timeout_seconds: float = 120.0
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
        env_key_map = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GOOGLE_API_KEY",
            "grok": "GROK_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "kimi": "MOONSHOT_API_KEY",
            "bailian": "DASHSCOPE_API_KEY",
            "zhipu": "ZHIPU_API_KEY",
            "doubao": "DOUBAO_API_KEY",
        }
        if not self.llm.api_key:
            env_var = env_key_map.get(self.llm.provider, "")
            if env_var:
                self.llm.api_key = os.environ.get(env_var, "")
        for fb in self.fallback_llms:
            if not fb.api_key:
                env_var = env_key_map.get(fb.provider, "")
                if env_var:
                    fb.api_key = os.environ.get(env_var, "")
