"""pytest 公共夹具。"""

import sys
from pathlib import Path

import pytest

# 确保可导入 automind 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def sample_goal():
    """构造一棵简单的目标树用于测试。"""
    from automind.core.types import Action, Goal

    root = Goal(id="root", description="root task")
    a = Goal(id="a", description="step a",
             assigned_action=Action(tool_name="file_write",
                                    parameters={"path": "a.txt", "content": "x"}))
    b = Goal(id="b", description="step b")
    c = Goal(id="c", description="step c (child of b)")
    b.children = [c]
    root.children = [a, b]
    return root
