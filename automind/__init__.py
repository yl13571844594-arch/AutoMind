"""AutoMind - 通用自动化 Agent 框架。

融合 Claude Code、OpenAI Codex 与 Reasonix 的核心能力，
支持 MCP 协议、Skill 技能系统、分层规划、符号推理与自我纠错。
"""

# 版本号唯一数据源 — server.py 与 pyproject.toml 均以此为准（§14.1）
__version__ = "1.0.0"
__all__ = [
    "AutoMindAgent",
    "AgentConfig",
    "LLMBackend",
    "LLMBackendFactory",
    "ToolRegistry",
    "SkillRegistry",
    "MemoryManager",
]


def __getattr__(name: str):
    """延迟导入 — 避免顶层导入触发依赖缺失。"""
    import importlib

    _import_map = {
        "AutoMindAgent": "automind.agent",
        "AgentConfig": "automind.core.config",
        "LLMBackend": "automind.core.llm",
        "LLMBackendFactory": "automind.core.llm",
        "ToolRegistry": "automind.tools.base",
        "SkillRegistry": "automind.skills.skill_registry",
        "MemoryManager": "automind.memory.manager",
    }

    if name in _import_map:
        module = importlib.import_module(_import_map[name])
        return getattr(module, name)
    raise AttributeError(f"module 'automind' has no attribute '{name}'")
