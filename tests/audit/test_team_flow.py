"""v1.11.23 审计：团队任务看板并发（``TeamStore`` 读改写竞态）。

## 复现并锁住的问题

v1.11.15 起的 ``TeamStore.create/update/delete/comment/claim`` 都是
``load() → 改列表 → save()``，中间没有任何互斥；而默认存储的 ``save`` 是
``team_replace``（整表 DELETE + 重写）。两个并发写各自读到同一份旧快照，后
写者用「旧快照 + 自己的改动」覆盖前写者，表现为：

* **并发建任务丢任务**：16 线程并发 create 100 条，实测只剩 94 条；
* **并发认领被静默覆盖**：同一任务 8 个成员并发 claim，最后一位覆盖前面所有
  认领，且没有任何人收到冲突提示；
* **并发评论/更新/删除互相覆盖**：整表重写会把别人刚写入的字段一起抹掉。

v1.11.23 修复（``automind/core/team.py``）：

1. **模块级 RLock** 包住 ``load → 修改 → save`` 整段。必须是模块级而不是
   实例级：server 每个请求、AI 工具每次调用都会新建 ``TeamStore``，实例级
   锁保护不了「多个 store 实例共享同一份存储」；RLock 允许 store 内部的
   ``load()/save()`` 重入。
2. **claim 冲突有确定性结果**：任务已被**其他成员**认领（且未完成）时抛
   ``TaskClaimed``（API 层 400、AI 工具返回可读错误），不再静默覆盖；同一人
   重复认领保持幂等；已完成（done）任务保留「重新认领」的旧语义（返工场景）。

## 测试策略

* 内存 loader/saver：注入带**少量延迟**的读写，把竞态窗口放大到"必现" ——
  这不是造假，并发压测里注入延迟是让交错可复现的标准手段；串行执行时这些
  用例也全部成立。
* 默认 SQLite 存储单独一组：用 ``db.reset_for_tests(":memory:")`` 隔离，不碰
  仓库真实数据；延迟同样只注入到默认 loader/saver 外层。
* 线程（ThreadPoolExecutor/Barrier）与 asyncio 协程（``to_thread``）混合，
  覆盖"Web 并发请求 + AI 工具调用"的真实形态。
* 修复前后的实测数字写在报告里（本文件只断言修复后的契约）。

## 审计发现（修复范围之外，如实记录）

1. server 的 ``POST /api/team/tasks``（新建）与 ``POST /api/team/tasks/{tid}``
   （更新）仍在 store 方法之外做 ``load→改→save``。这两个 async 端点里 load
   与 save 之间没有 await，单事件循环下不会交错（实测并发 30 个 REST 建任务
   无丢失）；但那是隐式契约，将来插入 await 就会重新丢更新。建议后续改为
   ``store.create`` / ``store.update`` 一次调用（本任务写作用域不含 server）。
2. ``_STORE_LOCK`` 是进程内锁，不跨进程；多 worker 部署的并发写仍依赖数据库
   层事务（当前产品单进程，跨进程不在本版范围）。
3. ``tests/server/test_v09_features.py`` 的团队夹具只 patch 旧 JSON 路径
   （``server._TEAM_FILE``），而团队存储早已迁到 SQLite：在带真实看板数据的
   共享工作区里跑，断言"只有 1 条任务"会读到别人的任务。这是测试隔离问题，
   与本次修复无关；用 ``AUTOMIND_DATA_DIR=<空目录>`` 跑即可复现绿。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from automind.core import team

#: 注入到 loader/saver 的延迟（秒）。留出足够的交错窗口，同时让整套用例
#: 仍在 2~3 秒内跑完（hold 在锁内的时间 = 每个写操作的真实串行成本）。
LOAD_DELAY = 0.002


# ═══════════════════════════════════════════════════════════
# 测试辅助
# ═══════════════════════════════════════════════════════════


class _MemoryStorage:
    """线程可见的内存团队表（loader/saver 指向它）。

    真实 server 每个请求都会新建 TeamStore 但共享同一份 SQLite；这里用同一
    个 ``_MemoryStorage`` 被多个（或同一个）store 共享来等价模拟。
    """

    def __init__(self, delay: float = 0.0) -> None:
        self.items: list[dict] = []
        self.delay = delay

    def load(self) -> list[dict]:
        if self.delay:
            time.sleep(self.delay)
        return [dict(t) for t in self.items]

    def save(self, items: list[dict]) -> None:
        if self.delay:
            time.sleep(self.delay)
        self.items = [dict(t) for t in items]


def _store(delay: float = LOAD_DELAY) -> tuple[team.TeamStore, _MemoryStorage]:
    box = _MemoryStorage(delay=delay)
    return team.TeamStore(loader=box.load, saver=box.save), box


def _run_parallel(fn: Callable[[int], Any], count: int, *, workers: int = 16,
                  barrier: threading.Barrier | None = None) -> list[Any]:
    """在线程池里跑 ``count`` 次 ``fn(i)``；可选 Barrier 让它们真正同时起跑。

    返回成功结果列表（异常通过 ``asyncio`` 风格的 Future 结果收集在调用方：
    这里只返回成功值，失败项由用例自己捕获 —— 见 :func:`_run_parallel_capture`）。
    """
    results: list[Any] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        if barrier is not None:
            barrier.wait()
        value = fn(i)
        with lock:
            results.append(value)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, i) for i in range(count)]
        for future in futures:
            future.result()
    return results


def _run_parallel_capture(fn: Callable[[int], Any], count: int, *,
                          workers: int = 16,
                          barrier: threading.Barrier | None = None
                          ) -> tuple[list[Any], list[BaseException]]:
    """同 :func:`_run_parallel`，但把异常也收集回来（claim 冲突断言用）。"""
    ok: list[Any] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        if barrier is not None:
            barrier.wait()
        try:
            value = fn(i)
        except BaseException as e:          # noqa: BLE001 - 用例要分类断言
            with lock:
                errors.append(e)
            return
        with lock:
            ok.append(value)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, i) for i in range(count)]
        for future in futures:
            future.result()
    return ok, errors


# ═══════════════════════════════════════════════════════════
# 1. 并发新建：一条都不能丢
# ═══════════════════════════════════════════════════════════


class TestConcurrentCreate:
    def test_100_concurrent_creates_are_all_persisted(self):
        store, box = _store()
        total = 100

        _run_parallel(lambda i: store.create(title=f"并发任务-{i}"), total)

        saved = store.load()
        assert len(saved) == total, (
            f"并发创建丢任务：期望 {total} 条，实际 {len(saved)} 条"
            f"（丢失 {total - len(saved)} 条）")
        assert len(box.items) == total
        assert len({t["id"] for t in saved}) == total, "任务 id 必须唯一（不能重复覆盖）"
        assert {t["title"] for t in saved} == {f"并发任务-{i}" for i in range(total)}

    def test_concurrent_creates_keep_activity_and_defaults(self):
        store, _ = _store()
        _run_parallel(lambda i: store.create(title=f"任务-{i}", creator=f"u{i}"),
                      30, workers=10)
        for t in store.load():
            assert t["status"] == "todo" and t["priority"] == "normal"
            assert t["activity"] and t["activity"][-1]["kind"] == "created"
            assert t["creator"] == f"u{t['title'].split('-')[1]}"


# ═══════════════════════════════════════════════════════════
# 2. 并发认领：唯一赢家，其他人拿到确定性冲突
# ═══════════════════════════════════════════════════════════


class TestConcurrentClaim:
    def test_8_concurrent_claims_have_single_winner(self):
        store, _ = _store(delay=LOAD_DELAY)
        task = store.create(title="只能一个人认领")
        tid = task["id"]
        members = [f"m{i}" for i in range(8)]
        barrier = threading.Barrier(len(members))

        ok, errors = _run_parallel_capture(
            lambda i: store.claim(tid, members[i]), len(members),
            workers=len(members), barrier=barrier)

        assert len(ok) == 1, f"同一任务并发认领必须只有一个成功者，实际 {len(ok)} 个"
        assert len(errors) == len(members) - 1
        winner = ok[0]["assignee"]
        assert winner in members
        for err in errors:
            assert isinstance(err, team.TaskClaimed), \
                f"冲突必须是确定性的 TaskClaimed，实际 {type(err).__name__}: {err}"
            assert winner in str(err), "冲突消息要指出当前认领人，调用方才能处理"
        stored = store.load()[0]
        assert stored["assignee"] == winner
        assert stored["status"] == "doing"
        claim_acts = [a for a in stored["activity"] if a["kind"] == "claim"]
        assert len(claim_acts) == 1, "被拒绝的认领不能写入活动流"

    def test_claim_conflict_never_overwrites_existing_assignee(self):
        store, _ = _store(delay=0)
        task = store.create(title="已有人认领")
        store.claim(task["id"], "小王")

        with pytest.raises(team.TaskClaimed) as ei:
            store.claim(task["id"], "小李")

        assert "小王" in str(ei.value)
        stored = store.load()[0]
        assert stored["assignee"] == "小王", "冲突不能静默覆盖已有负责人"
        assert stored["status"] == "doing"
        # 活动流不能被失败的认领污染（仍只有 created + 小王 claim）
        assert [a["kind"] for a in stored["activity"]] == ["created", "claim"]

    def test_repeated_claim_by_same_member_is_idempotent(self):
        store, _ = _store(delay=0)
        task = store.create(title="重复认领")
        first = store.claim(task["id"], "小王")
        before = len(first["activity"])
        second = store.claim(task["id"], "小王")
        assert second["assignee"] == "小王" and second["status"] == "doing"
        assert len(second["activity"]) == before, "重复认领同一任务不应刷活动流"

    def test_done_task_can_be_reclaimed_by_another_member(self):
        """旧语义保留：已完成任务允许他人重新认领（返工/接手场景）。"""
        store, _ = _store(delay=0)
        task = store.create(title="已完成可重领")
        store.claim(task["id"], "小王")
        store.update(task["id"], {"status": "done"})

        reclaimed = store.claim(task["id"], "小李")

        assert reclaimed["assignee"] == "小李"
        assert reclaimed["status"] == "doing"
        assert reclaimed["activity"][-1]["kind"] == "claim"


# ═══════════════════════════════════════════════════════════
# 3. 并发评论 / 更新 / 删除：整表重写不能互相抹掉
# ═══════════════════════════════════════════════════════════


class TestConcurrentWrites:
    def test_50_concurrent_comments_are_all_retained(self):
        store, _ = _store()
        task = store.create(title="评论压测")
        tid = task["id"]
        total = 50

        _run_parallel(lambda i: store.comment(tid, f"评论-{i}", author=f"u{i}"), total)

        comments = store.load()[0]["comments"]
        assert len(comments) == total, \
            f"并发评论丢内容：期望 {total} 条，实际 {len(comments)} 条"
        assert {c["text"] for c in comments} == {f"评论-{i}" for i in range(total)}

    def test_concurrent_updates_on_distinct_tasks_do_not_lose_writes(self):
        store, _ = _store()
        total = 20
        tasks = [store.create(title=f"待更新-{i}") for i in range(total)]

        _run_parallel(
            lambda i: store.update(tasks[i]["id"],
                                   {"status": "doing", "assignee": f"u{i}"},
                                   actor=f"u{i}"),
            total)

        saved = {t["id"]: t for t in store.load()}
        assert len(saved) == total
        for i, task in enumerate(tasks):
            got = saved[task["id"]]
            assert got["status"] == "doing", f"任务 {i} 的状态更新被覆盖丢失"
            assert got["assignee"] == f"u{i}", f"任务 {i} 的负责人更新被覆盖丢失"
            # update_task 按 assign → status 顺序追加活动
            assert [a["kind"] for a in got["activity"]][-2:] == ["assign", "status"]

    def test_concurrent_deletes_do_not_lose_other_deletes(self):
        store, _ = _store()
        tasks = [store.create(title=f"待删除-{i}") for i in range(20)]
        victims = tasks[:10]

        _run_parallel(lambda i: store.delete(victims[i]["id"]), len(victims))

        remaining = {t["title"] for t in store.load()}
        assert remaining == {f"待删除-{i}" for i in range(10, 20)}, \
            "并发的整表重写导致别的删除被回滚/别的任务被复活"

    def test_create_update_delete_comment_mixed_burst(self):
        """混合写压测：不变量 = 每条成功写都被持久化，未成功的不留半截状态。

        写入分区避免"更新一个刚被并发删除的任务"这类业务竞争：
        0..4 只被更新/评论，5..9 只被删除，新增任务与它们互不相干。
        """
        store, _ = _store()
        seeded = [store.create(title=f"混合-{i}") for i in range(10)]

        def mixed(i: int) -> None:
            if i % 4 == 0:
                store.create(title=f"新增-{i}")
            elif i % 4 == 1:
                store.comment(seeded[i % 5]["id"], f"混合评论-{i}", "u")
            elif i % 4 == 2:
                store.update(seeded[i % 5]["id"], {"status": "doing"}, f"u{i}")
            elif i % 4 == 3 and i // 4 < 5:
                # 5..9 号种子各删一次（i=3,7,11,15,19），保证删除目标唯一
                store.delete(seeded[5 + i // 4]["id"])

        _run_parallel(mixed, 40)

        saved = {t["title"] for t in store.load()}
        expected = {f"混合-{i}" for i in range(5)}        # 存活种子
        expected |= {f"新增-{i}" for i in range(40) if i % 4 == 0}
        assert saved == expected, "混合并发后任务集合不符（有写入被整表重写吞掉）"
        surviving = {t["title"]: t for t in store.load()}
        for i in range(5):
            # 0..4 号种子都至少被 update 或 comment 过：写结果必须留下来
            assert surviving[f"混合-{i}"]["activity"], "顺序写入的活动记录被覆盖"


# ═══════════════════════════════════════════════════════════
# 4. 默认 SQLite 存储：注入到真实默认读写层上跑并发
# ═══════════════════════════════════════════════════════════


@pytest.fixture()
def sqlite_store(monkeypatch):
    """默认 SQLite 存储（内存库，不碰仓库真实数据）+ 放大的读写窗口。"""
    from automind.core import db as db_mod

    previous = db_mod.reset_for_tests(":memory:")
    real_load = team._default_load
    real_save = team._default_save

    def slow_load() -> list[dict]:
        time.sleep(LOAD_DELAY)
        return real_load()

    def slow_save(items: list[dict]) -> None:
        time.sleep(LOAD_DELAY)
        real_save(items)

    monkeypatch.setattr(team, "_default_load", slow_load)
    monkeypatch.setattr(team, "_default_save", slow_save)
    # 不让默认 loader 把仓库里的旧 JSON 迁进测试库（正常也不存在，这里钉住隔离）
    from automind.core import paths as paths_mod
    monkeypatch.setattr(paths_mod, "legacy_file",
                        lambda name: previous.path.parent / f"no-legacy-{name}")
    yield team.TeamStore()
    db_mod.reset_for_tests(None)


class TestDefaultSqliteStore:
    def test_sqlite_concurrent_creates_are_all_persisted(self, sqlite_store):
        store = sqlite_store
        total = 60

        _run_parallel(lambda i: store.create(title=f"sqlite-{i}"), total)

        assert len(store.load()) == total, "默认 SQLite 存储并发建任务丢任务"
        from automind.core import db as db_mod
        rows = db_mod.get_db().team_load()
        assert len(rows) == total, "SQLite 表里实际落盘条数与 store 视图不一致"
        assert len({r["id"] for r in rows}) == total

    def test_sqlite_concurrent_claim_single_winner(self, sqlite_store):
        store = sqlite_store
        task = store.create(title="sqlite-claim")
        tid = task["id"]
        members = [f"s{i}" for i in range(6)]
        barrier = threading.Barrier(len(members))

        ok, errors = _run_parallel_capture(
            lambda i: store.claim(tid, members[i]), len(members),
            workers=len(members), barrier=barrier)

        assert len(ok) == 1 and len(errors) == len(members) - 1
        winner = ok[0]["assignee"]
        assert store.load()[0]["assignee"] == winner
        from automind.core import db as db_mod
        assert db_mod.get_db().team_load()[0]["assignee"] == winner


# ═══════════════════════════════════════════════════════════
# 5. asyncio 协程 + 线程混合写
# ═══════════════════════════════════════════════════════════


class TestAsyncThreadMix:
    async def test_coroutines_via_to_thread_do_not_lose_writes(self):
        store, _ = _store()
        total = 40

        async def one(i: int) -> None:
            await asyncio.to_thread(store.create, title=f"协程-{i}")

        await asyncio.gather(*(one(i) for i in range(total)))

        assert len(store.load()) == total, "协程经线程池并发写时丢任务"

    async def test_async_claims_on_same_task_have_single_winner(self):
        store, _ = _store()
        task = store.create(title="协程抢认领")
        tid = task["id"]
        members = [f"a{i}" for i in range(6)]

        async def one(i: int) -> dict | BaseException:
            try:
                return await asyncio.to_thread(store.claim, tid, members[i])
            except BaseException as e:      # noqa: BLE001 - 用例要分类断言
                return e

        results = await asyncio.gather(*(one(i) for i in range(len(members))))
        winners = [r for r in results if isinstance(r, dict)]
        losers = [r for r in results if isinstance(r, BaseException)]
        assert len(winners) == 1 and len(losers) == len(members) - 1
        assert all(isinstance(e, team.TaskClaimed) for e in losers)
        assert store.load()[0]["assignee"] == winners[0]["assignee"]


# ═══════════════════════════════════════════════════════════
# 6. 顺序路径（修复不能改变既有单线程语义）
# ═══════════════════════════════════════════════════════════


class TestSequentialSemanticsUnchanged:
    def test_lifecycle_still_works(self):
        store, _ = _store(delay=0)
        task = store.create(title="生命周期", assignee="小王", priority="high")
        tid = task["id"]
        assert store.load()[0]["id"] == tid

        updated = store.update(tid, {"status": "doing"}, actor="小王")
        assert updated["status"] == "doing"
        commented = store.comment(tid, "先看接口", "小李")
        assert commented["comments"][-1]["text"] == "先看接口"
        claimed = store.claim(tid, "小王")            # 同一人重复认领：幂等
        assert claimed["assignee"] == "小王"
        assert [a["kind"] for a in claimed["activity"]] == ["created", "status"], \
            "重复认领不应追加活动记录"
        store.delete(tid)
        assert store.load() == []
        with pytest.raises(team.TaskNotFound):
            store.update("nope", {"status": "doing"})
        with pytest.raises(team.TaskNotFound):
            store.claim("nope", "小王")

    def test_claim_requires_member_and_task(self):
        store, _ = _store(delay=0)
        task = store.create(title="参数校验")
        with pytest.raises(team.TeamError):
            store.claim(task["id"], "")
