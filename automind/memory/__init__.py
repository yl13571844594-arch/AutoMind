"""记忆系统 — 短期、长期、知识图谱、实体、项目记忆。"""


def __getattr__(name: str):
    import importlib
    _map = {
        "ShortTermMemory": "automind.memory.short_term",
        "LongTermMemory": "automind.memory.long_term",
        "KnowledgeGraph": "automind.memory.knowledge_graph",
        "EntityMemory": "automind.memory.entity_memory",
        "ProjectMemory": "automind.memory.project_memory",
        "MemoryManager": "automind.memory.manager",
    }
    if name in _map:
        module = importlib.import_module(_map[name])
        return getattr(module, name)
    raise AttributeError(f"module 'automind.memory' has no attribute '{name}'")


__all__ = [
    "ShortTermMemory",
    "LongTermMemory",
    "KnowledgeGraph",
    "EntityMemory",
    "ProjectMemory",
    "MemoryManager",
]
