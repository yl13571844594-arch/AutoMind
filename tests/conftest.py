"""pytest 公共夹具。"""

import os
import sys
from pathlib import Path

import pytest

# 确保可导入 automind 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ═══════════════════════════════════════════════════════════════
# 取消探针：async 测试"关循环"时把残留任务点名
# ═══════════════════════════════════════════════════════════════
#
# 为什么需要：pytest-asyncio 关事件循环时会取消该循环里的全部任务，然后
# ``gather`` 等它们结束。只要有一个任务**吞掉取消**（或取消后卡在别处），
# 这一等就是永远 —— 表现是 CI 上一条作业静静挂到被人工取消（我们真遇到过：
# py3.11 挂 6 小时，日志里连"哪个用例"都没有，因为卡在 fixture 收尾里）。
#
# 这个探针把"取消前的残留任务"打印出来，并且**只等一段有界的时间**：
# 超时就点名 + 报错，绝不把整条 CI 拖成挂死。默认关闭（不改变正常行为），
# CI 上通过 AUTOMIND_CANCEL_PROBE=1 打开 —— 由 tests/conftest.py 统一开关，
# 这样 async 泄漏在 CI 上永远是"一条带任务名的失败"，而不是"一条挂住的作业"。
if os.environ.get("AUTOMIND_CANCEL_PROBE") == "1":      # pragma: no cover - 测试基建
    import asyncio
    import asyncio.runners as _runners

    #: 取消后等待上限（秒）—— 超过即认定"取消不掉"，点名后如实报错
    _PROBE_GRACE_S = 10.0

    _orig_cancel_all_tasks = _runners._cancel_all_tasks

    def _probe_cancel_all_tasks(loop) -> None:
        tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
        if not tasks:
            return _orig_cancel_all_tasks(loop)
        print(f"\n[cancel-probe] 关循环时仍有 {len(tasks)} 个任务未结束：", flush=True)
        for t in tasks:
            print(f"  · {t.get_name()}  {t.get_coro()!r}", flush=True)
        for t in tasks:
            t.cancel()
        _, still = loop.run_until_complete(asyncio.wait(tasks, timeout=_PROBE_GRACE_S))
        if still:
            print(f"[cancel-probe] 其中 {len(still)} 个在取消后 "
                  f"{_PROBE_GRACE_S:g}s 仍未结束（就是它们把收尾挂住了）：",
                  flush=True)
            for t in still:
                print(f"  · {t.get_name()}  {t.get_coro()!r}", flush=True)
                try:
                    t.print_stack()
                except Exception:                        # pragma: no cover - 尽力而为
                    pass
            # 不再交给原实现（它会在这些任务上无限等）；如实报错，
            # 让"泄漏"显示为一条带任务名的失败，而不是一条挂死的作业。
            raise RuntimeError(
                "事件循环收尾被未结束的任务挂住："
                + ", ".join(t.get_name() for t in still)
                + "（见上方 [cancel-probe] 输出；已等待 "
                + f"{_PROBE_GRACE_S:g}s）")
        # 全部收干净了：退出前把循环恢复到"没有任务"的状态，
        # 让原实现正常走完（shutdown_asyncgens 等）—— 不改变正常路径的行为。
        return _orig_cancel_all_tasks(loop)

    _runners._cancel_all_tasks = _probe_cancel_all_tasks


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
