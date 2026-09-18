"""后台命令通道 — 长耗时命令（pip install / 编译 / 长测试）不再等死。

问题：`terminal` 工具超时后直接 kill，进程状态全丢、任务判败，模型只能盲目重跑
一遍 —— 又一次从头下载、又一次从头编译。对 pip install / 大型构建 / 完整测试
这类"本来就要几分钟"的命令，同步等超时是纯粹的时间与 token 双输。

做法：把长命令交给**进程内后台任务**执行，立即返回一个 ``task_id``；
模型随后用 ``terminal_background(action="poll"|"output"|"kill"|"list")`` 轮询结果，
或先去做别的步骤、稍后回来取。命令对象本身与调用方解耦，因此
（a）超时不再等于失败，（b）同一会话的多个后台任务互不干扰。

边界与诚实性：
  · 后台任务**不随会话结束而消失**（进程退出才会终止）—— 这是用户想要的
    "装完就好"，但也意味着它占用进程资源；因此有数量上限与最长存活时间。
  · 输出保留头尾各若干字符（与前台一致），不做无界累积。
  · 这里**不模拟"正在后台跑"的假象**：任务没结束就如实返回 running，任务崩了
    就返回异常原因，绝不返回一个看起来成功的空结果。
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any

from automind.core.logging import get_logger

logger = get_logger("automind.tools.background")

#: 单进程同时保留的后台任务上限（超出后拒绝新建，避免资源被吃干）
MAX_TASKS = 16

#: 后台任务结果里保留的 stdout/stderr 字符数（头尾各留一半）
KEEP_CHARS = 8000

#: 后台任务最长存活时间（秒）—— 超过后强制终止，避免僵尸任务
MAX_LIFETIME_S = 3600.0

_seq = itertools.count(1)


@dataclass
class BackgroundTask:
    """一个后台命令的执行状态。"""

    task_id: str
    command: str
    workdir: str
    started_at: float
    process: Any = None
    task: asyncio.Task | None = None
    status: str = "running"          # running | ok | failed | timeout | error
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    finished_at: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    #: 是否已被主动终止 —— 与"自己失败"必须区分：进程被杀后 exit_code 是非零，
    #: 若只按退出码判定，用户会在界面上看到 "failed"，永远不知道是自己停的。
    kill_requested: bool = False

    @property
    def elapsed_s(self) -> float:
        return round((self.finished_at or time.time()) - self.started_at, 2)

    def snapshot(self, tail_chars: int = 2000) -> dict[str, Any]:
        """给模型看的状态快照（输出截尾，避免轮询本身撑爆上下文）。"""
        def _tail(s: str) -> str:
            return s if len(s) <= tail_chars else "…[前文略]\n" + s[-tail_chars:]

        return {
            "task_id": self.task_id,
            "status": self.status,
            "command": self.command[:200],
            "workdir": self.workdir,
            "pid": self.meta.get("pid"),
            "exit_code": self.exit_code,
            "elapsed_s": self.elapsed_s,
            "stdout_tail": _tail(self.stdout),
            "stderr_tail": _tail(self.stderr),
            "error": self.error,
        }


#: task_id → 任务
_tasks: dict[str, BackgroundTask] = {}


def _prune() -> None:
    """清掉最旧的已结束任务（保留 running 的）。"""
    if len(_tasks) < MAX_TASKS:
        return
    finished = [t for t in _tasks.values() if t.status != "running"]
    finished.sort(key=lambda t: t.finished_at or 0.0)
    for t in finished[: max(1, len(finished) // 2)]:
        _tasks.pop(t.task_id, None)


async def _kill_process(proc: Any) -> bool:
    """终止子进程并**有界地**等待回收，返回是否确认它已结束。

    为什么不能直接 ``await proc.wait()``：``stdout=PIPE`` 时 ``wait()`` 必须等到
    管道关闭；而 shell 启动的命令会派生子进程**继承同一批管道句柄**——
    ``cmd.exe`` 被杀掉后它启动的孙子进程仍持有句柄，管道不关，``wait()``
    就永远不返回。在 Windows 上这会一路把进程卡到超时/被杀（实测如此），
    对"杀掉一个后台任务"这种动作代价完全不成比例。

    因此：先 kill，再**限时**等一小会儿（多半是立即返回），超时就放弃等待并
    如实记录。进程句柄由 asyncio 的传输层在回收时关闭，不需要靠 wait() 兜底。
    """
    if proc is None:
        return False
    try:
        proc.kill()
    except ProcessLookupError:
        return True
    except Exception as e:                        # pragma: no cover - 防御性
        logger.warning("background_kill_failed", error=str(e))
        return False
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=5.0)
        return True
    except TimeoutError:
        # 管道仍被子进程树持有 —— 不再等，避免把事件循环一起拖住。
        # （Python 3.11 起 asyncio.TimeoutError 就是内置 TimeoutError，
        #   不再需要并列写两个，否则 ruff UP041 会指出这一点。）
        logger.warning("background_reap_timeout",
                       note="进程已发送终止信号，但未在 5s 内回收（可能有子进程仍持有管道）")
        return False
    except Exception:
        return False


async def start(command: str, workdir: str, env: dict[str, str],
                timeout: float) -> BackgroundTask:
    """启动一个后台命令并立即返回（不等待它跑完）。

    **进程必须在返回之前就创建好**：早期实现在返回后才在后台协程里
    ``create_subprocess_shell``，于是调用方拿到 task_id 的那一刻
    ``task.process`` 还是 None —— ``kill`` 因为拿不到进程句柄而**静默什么都不做**
    （用户以为停掉了，命令其实还在跑），``poll`` 也看不到任何输出。
    现在先 spawn、再交给协程等待，句柄从第一刻起就可控。
    """
    _prune()
    running = sum(1 for t in _tasks.values() if t.status == "running")
    if running >= MAX_TASKS:
        raise RuntimeError(
            f"后台任务已达上限（{MAX_TASKS} 个运行中）；请先 poll/kill 已有任务。")
    task_id = f"bg{next(_seq)}"
    bg = BackgroundTask(task_id=task_id, command=command, workdir=workdir,
                        started_at=time.time())
    try:
        bg.process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env=env,
        )
    except Exception as e:
        bg.status = "error"
        bg.error = f"进程启动失败：{type(e).__name__}: {e}"
        bg.finished_at = time.time()
        _tasks[task_id] = bg
        logger.warning("background_task_spawn_failed", task_id=task_id,
                       error=bg.error)
        return bg
    bg.meta["pid"] = bg.process.pid
    _tasks[task_id] = bg
    bg.task = asyncio.create_task(_wait(bg, timeout))
    logger.info("background_task_started", task_id=task_id, pid=bg.process.pid,
                command=command[:120], timeout_s=timeout)
    return bg


async def _wait(bg: BackgroundTask, timeout: float) -> None:
    """等待已启动的后台进程结束 —— 所有异常都落到 bg 上，绝不逸出。"""
    limit = min(timeout, MAX_LIFETIME_S)
    try:
        try:
            out, err = await asyncio.wait_for(bg.process.communicate(), timeout=limit)
        except TimeoutError:
            await _kill_process(bg.process)
            bg.status = "timeout"
            bg.error = (f"后台命令在 {limit:.0f}s 内未结束，已终止。"
                        f"如需更长时间，请加大 timeout 或把命令拆小。")
            return
        bg.stdout = _clip(out.decode("utf-8", errors="replace").strip())
        bg.stderr = _clip(err.decode("utf-8", errors="replace").strip())
        bg.exit_code = bg.process.returncode
        if bg.kill_requested:
            # 是我们杀的：退出码非零是必然结果，不能记成"命令失败"
            bg.status = "killed"
            bg.error = "后台任务已被手动终止。"
        else:
            bg.status = "ok" if bg.process.returncode == 0 else "failed"
            if bg.status == "failed":
                bg.error = (f"退出码 {bg.process.returncode}"
                            + (f"；stderr：{bg.stderr[-300:]}" if bg.stderr else ""))
    except asyncio.CancelledError:
        # 事件循环关闭 / kill() 主动取消：尽力清理进程，别留下孤儿
        await _kill_process(bg.process)
        if bg.status == "running":
            bg.status = "cancelled"
            bg.error = bg.error or "后台任务被取消（服务关闭或任务被取消）"
        raise
    except Exception as e:
        bg.status = "error"
        bg.error = f"{type(e).__name__}: {e}"
        logger.warning("background_task_crashed", task_id=bg.task_id, error=bg.error)
    finally:
        bg.finished_at = bg.finished_at or time.time()
        logger.info("background_task_finished", task_id=bg.task_id,
                    status=bg.status, elapsed_s=bg.elapsed_s)


def _clip(text: str) -> str:
    if len(text) <= KEEP_CHARS:
        return text
    half = KEEP_CHARS // 2
    return (text[:half] + f"\n…[后台输出已截断，省略 {len(text) - KEEP_CHARS} 字符]…\n"
            + text[-half:])


def get(task_id: str) -> BackgroundTask | None:
    return _tasks.get(task_id)


def list_tasks() -> list[dict[str, Any]]:
    return [t.snapshot(tail_chars=400) for t in _tasks.values()]


async def kill(task_id: str) -> dict[str, Any]:
    """终止一个后台任务（幂等）。

    返回的状态是 ``killed``（被手动终止）。注意与 ``timeout``/``failed`` 的
    区别是**有意保留的**：用户需要能分辨"我停的"和"它自己坏的"。
    """
    bg = _tasks.get(task_id)
    if bg is None:
        return {"ok": False, "error": f"后台任务不存在：{task_id}"}
    if bg.status == "running" and bg.process is not None:
        # 先立旗，再杀：进程被杀后 exit_code 必然非零，不立旗就会被记成
        # "命令执行失败" —— 用户永远看不出是自己停的
        bg.kill_requested = True
        reaped = await _kill_process(bg.process)
        if bg.task is not None and not bg.task.done():
            # 取消读取协程；它自己的收尾分支会打印日志
            bg.task.cancel()
            try:
                await bg.task
            except asyncio.CancelledError:
                # 是我们刚取消的那个读取协程 → 正常收尾。但若**当前任务**自己
                # 也被取消了（读取协程并未 cancelled），必须继续抛出：原先这里
                # 笼统写成 ``except (CancelledError, Exception): pass``，会把
                # 调用方的取消一起吞掉 —— 那正是"取消不掉的任务"的来源。
                if not bg.task.cancelled():
                    raise
            except Exception:
                pass
        if bg.status != "killed":                   # 协程未及收尾时兜底
            bg.status = "killed"
            bg.finished_at = bg.finished_at or time.time()
            bg.error = ("后台任务已被手动终止。"
                        + ("" if reaped else "（进程已收到终止信号，但未能确认完全回收）"))
    return {"ok": True, **bg.snapshot(tail_chars=400)}


def reset_for_tests() -> None:
    """清空任务表（仅测试用；不主动杀进程）。"""
    _tasks.clear()


#: 输出里出现这些字样 → 命令像是"长耗时"的，超时回执会点名建议后台化
LONG_RUNNING_HINTS = (
    "pip install", "pip3 install", "npm install", "npm ci", "yarn install",
    "pnpm install", "conda install", "apt-get install", "apt install",
    "cargo build", "cargo test", "go build", "mvn ", "gradle", "docker build",
    "pytest", "make ", "cmake", "webpack", "vite build", "tsc ", "build",
    "compile", "编译", "构建", "打包", "训练", "train",
)


def looks_long_running(command: str) -> bool:
    low = (command or "").lower()
    return any(h in low for h in LONG_RUNNING_HINTS)
