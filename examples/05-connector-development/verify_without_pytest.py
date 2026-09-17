"""不改源码、不上 pytest 就能验证连接器 —— 沙箱/离线环境下的回归入口。

## 为什么需要它

主入口当然是 ::

    python -m pytest tests/tools/test_connectors.py -q --no-header

但在被收紧的 Windows 沙箱里（本仓的自动化环境就是），``pytest`` 会在给自己
建私有临时目录时被拒绝（``PermissionError: [WinError 5]`` 打在
``%LOCALAPPDATA%\\Temp\\pytest-of-*`` 上），**一条用例都跑不到**。此时如果
只有"跑不了"这一个结论，"连接器到底能不能用"就变成了没人验证过的事 ——
本仓反复吃过的正是这种亏（功能静默不存在）。

所以这里用**同一份测试文件**做一次独立复算：把 ``tests/tools/test_connectors.py``
里的用例逐个取出来直接执行，``tmp_path`` / ``monkeypatch`` 用标准库等价实现
（不再依赖 pytest 的临时目录机制），断言、夹具、用例体一行不改。

它**不替代** pytest：只跑这一个文件、只实现这两个夹具。pytest 能跑的地方
请照常用 pytest（那才是仓库的标准验收入口）。

用法::

    python examples/05-connector-development/verify_without_pytest.py

退出码 0 = 全部通过；1 = 有用例失败（失败详情会打出来）。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import inspect
import logging
import os
import shutil
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_FILE = REPO_ROOT / "tests" / "tools" / "test_connectors.py"


class _MonkeyPatch:
    """``monkeypatch`` 的最小等价物 —— 只实现用例真正用到的那几个。

    刻意用标准库自己实现，而不是去 import pytest：本文件的存在前提就是
    "pytest 在这个环境里跑不起来"。
    """

    def __init__(self) -> None:
        self._undo: list = []

    def setenv(self, name: str, value: str) -> None:
        old = os.environ.get(name)
        os.environ[name] = str(value)
        self._undo.append(lambda: os.environ.__setitem__(name, old)
                          if old is not None else os.environ.pop(name, None))

    def delenv(self, name: str, raising: bool = True) -> None:
        if name not in os.environ:
            if raising:
                raise KeyError(name)
            return
        old = os.environ.pop(name)
        self._undo.append(lambda: os.environ.__setitem__(name, old))

    def undo(self) -> None:
        for fn in reversed(self._undo):
            fn()
        self._undo.clear()


class _FakeCaplog:
    """``caplog`` 的最小等价物：``at_level()`` 上下文 + ``.text``。

    用真正的 logging handler 抓，所以模块里 ``logger.warning`` 写了什么，
    用例断言的就是什么 —— 不掺任何模拟。
    """

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []
        self._handler = logging.Handler()
        self._handler.emit = self.records.append  # type: ignore[method-assign]
        self._handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(self._handler)

    @contextlib.contextmanager
    def at_level(self, level: int, logger: str | None = None):
        root = logging.getLogger()
        old_root, old_target = root.level, None
        root.setLevel(min(level, old_root) if old_root else level)
        if logger:
            target = logging.getLogger(logger)
            old_target = target.level
            target.setLevel(level)
        try:
            yield self
        finally:
            root.setLevel(old_root)
            if logger and old_target is not None:
                logging.getLogger(logger).setLevel(old_target)

    @property
    def text(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)

    def close(self) -> None:
        logging.getLogger().removeHandler(self._handler)


def _fixture(*a, **kw):
    """``@pytest.fixture``（含 ``@pytest.fixture(...)`` 两种写法）。

    定义在模块级而不是 ``_stub_pytest()`` 里：类的函数体**看不到**外层函数的
    局部名（这是 Python 作用域规则，不是笔误），写成一个方法会在 import 期
    直接 NameError。
    """
    if a and callable(a[0]):                     # 裸装饰器 @pytest.fixture
        a[0].__is_fixture__ = True
        return a[0]

    def deco(fn):
        fn.__is_fixture__ = True
        return fn
    return deco


def _pytest_mark_skipif(condition, reason=""):
    def deco(fn):
        fn.__skip__ = bool(condition)
        fn.__skip_reason__ = reason
        return fn
    return deco


def _stub_pytest():
    """给测试文件一个最小的 ``pytest`` 门面（它只用到 fixture / mark.skipif）。

    注入到 ``sys.modules`` 再导入测试文件 —— 于是**同一份 tests/ 用例**既能在
    pytest 下跑，也能在这里跑，不存在"两份会各自漂移的测试"。
    """

    class _Mark:
        skipif = staticmethod(_pytest_mark_skipif)

    class _Module:
        mark = _Mark()
        fixture = staticmethod(_fixture)

    return _Module()


def _load_test_module():
    if not TEST_FILE.is_file():
        raise SystemExit(f"找不到测试文件：{TEST_FILE}")
    sys.modules.setdefault("pytest", _stub_pytest())      # type: ignore[arg-type]
    # 让 `import automind` 成立：从仓库根导入，而不是靠调用者的 cwd
    sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location("test_connectors_local", TEST_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_one(fn, module, tmp_path: Path, instance=None) -> None:
    """按用例签名准备夹具并执行（含 ``_isolated`` 这个 autouse 夹具的等价实现）。

    夹具解析刻意只认三种名字 + 一个 autouse 夹具：这个复算入口的全部意义是
    "在 pytest 跑不起来的环境里，用**同一份用例**再算一遍"，所以宁可在这里
    显式报错，也不去实现一个半吊子的 pytest。
    """
    monkeypatch = _MonkeyPatch()
    caplog = _FakeCaplog()
    fixtures: dict[str, object] = {"tmp_path": tmp_path, "monkeypatch": monkeypatch}
    teardown = []
    try:
        autouse = module.__dict__.get("_isolated")
        if autouse is not None and getattr(autouse, "__is_fixture__", False):
            produced = autouse(tmp_path, monkeypatch)
            if inspect.isgenerator(produced):           # yield 夹具：拿到产出再收尾
                gen = produced
                fixtures["_isolated"] = next(gen)
                teardown.append(lambda gen=gen: next(gen, None))
            else:
                fixtures["_isolated"] = produced

        kwargs = {"self": instance} if instance is not None else {}
        for name in inspect.signature(fn).parameters:
            if name in kwargs:
                continue
            if name == "caplog":
                kwargs[name] = caplog
            elif name in fixtures:
                kwargs[name] = fixtures[name]
            else:
                raise RuntimeError(f"本复算脚本不认识夹具 '{name}'（{fn.__qualname__}）")
        result = fn(**kwargs)
        if inspect.iscoroutine(result):                 # asyncio_mode = auto
            asyncio.run(result)
    finally:
        for step in reversed(teardown):
            step()
        caplog.close()
        monkeypatch.undo()


def main() -> int:
    module = _load_test_module()
    #: 用例目录建在**仓库根**下、逐个平铺，刻意不用 tempfile.mkdtemp()：
    #: 受限环境里 mkdtemp 建出来的目录所有者权限收得很紧（0o700），随后往里
    #: mkdir 会被拒（WinError 5）—— 那是沙箱的限制，不该冒充成用例失败。
    tmp_root = REPO_ROOT / f".connector-verify-{os.getpid()}"
    tmp_root.mkdir(parents=True, exist_ok=True)
    passed, failed, skipped = 0, [], 0
    try:
        cases = []
        for name, obj in vars(module).items():
            if name.startswith("test_") and inspect.isfunction(obj):
                cases.append((name, obj, None))
            elif name.startswith("Test") and inspect.isclass(obj):
                instance = obj()                        # 用例类都是无参构造
                for mname, m in vars(obj).items():
                    if mname.startswith("test_") and inspect.isfunction(m):
                        cases.append((f"{name}.{mname}", m, instance))
        for index, (label, fn, instance) in enumerate(cases):
            if getattr(fn, "__skip__", False):
                skipped += 1
                print(f"SKIP {label}（{getattr(fn, '__skip_reason__', '')}）")
                continue
            case_dir = tmp_root / f"case{index:03d}"
            case_dir.mkdir(exist_ok=True)
            try:
                _run_one(fn, module, case_dir, instance)
            except BaseException:                       # noqa: BLE001 - 报告用
                failed.append((label, traceback.format_exc()))
                print(f"FAIL {label}")
            else:
                passed += 1
                print(f"ok   {label}")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n{passed} passed, {len(failed)} failed, {skipped} skipped"
          f"（复算入口：examples/05-connector-development/verify_without_pytest.py）")
    for label, tb in failed:
        print(f"\n=== {label} ===\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
