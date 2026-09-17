"""CLI 测试 —— 退出码是核心接口。

为什么退出码值得单独一个文件：工作流有一半的价值在 **CI**。客户把
`change_request.yaml` 提交进仓库，流水线上每次改动都跑一遍；"合不进去"
这件事完全由退出码表达。退出码错了（比如把"有步骤失败"当成功），
门禁就成了摆设，而这件事在界面上看不出来、只有 CI 会漏。

判据（与 `automind/workflow/__main__.py` 的文档逐条对应）：
  0   全绿（含 dry-run）
  1   校验通过、也真的跑了，但有步骤失败
  2   文件不存在 / 校验失败 / 入参给错
130   被取消

两种调用方式都测：
  · 直接调 `main([...])` —— 断言精确、跑得快、在沙箱里一定能跑；
  · 真起子进程 —— 断言**进程退出码**，CI 实际依赖的是它。
    某些受限环境不允许创建子进程，此时按 skip 处理并把原因说清楚，
    而不是把"没测到"伪装成"通过了"。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from automind.workflow.__main__ import EXIT_CANCELLED, EXIT_USAGE, main

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "examples" / "06-workflow" / "hello.yaml"
CHANGE = REPO_ROOT / "examples" / "06-workflow" / "change_request.yaml"

#: 写进 examples 目录的临时工作流（该目录属于本次改动的文件边界之内）。
#: 名字带 `cli-` 前缀，一眼能与示例区分开。
FIXTURE_DIR = REPO_ROOT / "examples" / "06-workflow"

GOOD = """\
version: 1
name: cli-happy
inputs:
  value: {type: string, default: 甲}
  path: {type: string, default: examples/06-workflow/cli-output.txt}
steps:
  - id: first
    type: tool
    tool: file_write
    args:
      path: "{{ inputs.path }}"
      content: "{{ inputs.value }}"
  - id: second
    type: tool
    tool: file_read
    args: {path: "{{ inputs.path }}"}
"""

FAILS = """\
version: 1
name: cli-failing
steps:
  - id: bad
    type: tool
    tool: file_read
    args: {path: ".automind/绝对读不到的文件"}
    on_failure: continue
  - id: also_bad
    type: tool
    tool: file_read
    args: {path: ".automind/也读不到"}
"""

NO_CHANGE = """\
version: 1
name: cli-noop
steps:
  - id: only
    type: tool
    tool: file_read
    args: {path: "不存在的输入文件.txt"}
    on_failure: continue
"""

BAD_YAML = """\
version: 1
name: cli-bad
steps:
  - id: a
    type: nope
"""

NEEDS_INPUT = """\
version: 1
name: cli-needs-input
inputs:
  ticket_id: {type: string, required: true}
steps:
  - id: a
    type: tool
    tool: file_read
    args: {path: "{{ inputs.ticket_id }}"}
"""


@pytest.fixture
def fixture_file(wx_tmp):
    """在 examples 目录里落一个临时工作流文件，用例结束即删。"""
    created: list[Path] = []

    def _make(name: str, text: str) -> Path:
        path = FIXTURE_DIR / f"cli-{name}.yaml"
        path.write_text(text, encoding="utf-8")
        created.append(path)
        return path

    yield _make
    for path in created:
        path.unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════
# 直接调 main()
# ═══════════════════════════════════════════════════════════════


class TestExitCodes:
    def test_dry_run_is_zero(self, capsys) -> None:
        assert main(["run", str(EXAMPLE), "--dry-run"]) == 0

    def test_real_run_all_green_is_zero(self, fixture_file, wx_tmp, capsys) -> None:
        target = wx_tmp / "out.txt"                 # 绝对路径，绕开 project_root 差异
        path = fixture_file(
            "happy",
            GOOD.replace('path: "{{ inputs.path }}"',
                         f'path: "{target.as_posix()}"'))
        code = main(["run", str(path), "--input", "value=你好"])
        assert code == 0
        assert target.read_text(encoding="utf-8") == "你好"

    def test_step_failure_is_one(self, fixture_file, capsys) -> None:
        """**有步骤失败就必须是 1** —— 哪怕整轮"跑完了"（on_failure: continue）。"""
        path = fixture_file("failing", FAILS)
        code = main(["run", str(path)])
        captured = capsys.readouterr()
        assert code == 1
        assert "失败" in captured.err

    def test_missing_file_is_two(self, capsys) -> None:
        code = main(["run", str(REPO_ROOT / "不存在的工作流.yaml")])
        captured = capsys.readouterr()
        assert code == EXIT_USAGE == 2
        assert "文件不存在" in captured.err

    def test_invalid_workflow_is_two(self, fixture_file, capsys) -> None:
        path = fixture_file("bad", BAD_YAML)
        code = main(["run", str(path)])
        captured = capsys.readouterr()
        assert code == 2
        assert "未知的步骤类型" in captured.err
        # 报错必须指到"哪一步的哪个字段"：只说"文件里有错"等于没说
        assert "steps[0](a).type" in captured.err

    def test_missing_required_input_is_two_and_runs_nothing(self, fixture_file,
                                                            capsys) -> None:
        path = fixture_file("needs-input", NEEDS_INPUT)
        code = main(["run", str(path)])
        captured = capsys.readouterr()
        assert code == 2
        assert "缺少必需入参" in captured.err
        assert "未执行任何步骤" in captured.err

    def test_input_typo_warns_but_runs(self, fixture_file, capsys) -> None:
        path = fixture_file("needs-input2", NEEDS_INPUT)
        code = main(["run", str(path), "--input", "ticketid=x", "--dry-run"])
        captured = capsys.readouterr()
        # 未声明的入参只是警告，但必需的那个仍然缺失 → 仍然是 2
        assert code == 2
        assert "ticket_id" in captured.err

    def test_bad_input_syntax_is_two(self, capsys) -> None:
        assert main(["run", str(EXAMPLE), "--input", "没有等号"]) == 2

    def test_bad_set_syntax_is_two(self, capsys) -> None:
        assert main(["run", str(EXAMPLE), "--set", "没有等号"]) == 2

    def test_no_subcommand_prints_help(self, capsys) -> None:
        code = main([])
        captured = capsys.readouterr()
        assert code == 2
        # 帮助走 stderr（stdout 留给 --json 的结果，不能掺东西进去）
        assert "usage" in captured.err
        assert "run" in captured.err

    def test_cancelled_constant(self) -> None:
        assert EXIT_CANCELLED == 130


class TestRunOutput:
    def test_json_report_shape(self, fixture_file, wx_tmp, capsys) -> None:
        path = fixture_file("json", GOOD)
        target = wx_tmp / "j.txt"
        code = main(["run", str(path), "--json", "--dry-run",
                     "--input", f"path={target.as_posix()}"])
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)              # stdout 必须是**纯 JSON**
        assert payload["workflow"] == "cli-happy"
        assert payload["dry_run"] is True
        assert payload["summary"]["exit_code"] == 0
        assert [s["id"] for s in payload["steps"]] == ["first", "second"]
        assert payload["source_digest"]                 # 报告能证明跑的是哪一版

    def test_json_error_output_when_invalid(self, fixture_file, capsys) -> None:
        path = fixture_file("jsonbad", BAD_YAML)
        code = main(["run", str(path), "--json"])
        captured = capsys.readouterr()
        assert code == 2
        payload = json.loads(captured.out)
        assert payload["ok"] is False
        assert payload["errors"]

    def test_dry_run_prints_plan_on_stdout(self, capsys) -> None:
        code = main(["run", str(CHANGE), "--dry-run", "--input", "ticket_id=INC1",
                     "--quiet"])
        captured = capsys.readouterr()
        assert code == 0
        assert "试运行计划" in captured.out
        assert "未执行任何工具" in captured.out
        assert "INC1" in captured.out                   # 参数确实渲染过

    def test_progress_goes_to_stderr_not_stdout(self, fixture_file, capsys) -> None:
        path = fixture_file("progress", GOOD)
        main(["run", str(path), "--dry-run"])
        captured = capsys.readouterr()
        assert "执行" in captured.err
        assert "执行" not in captured.out.split("──")[0]  # stdout 只放结果

    def test_raw_flag_keeps_strings(self, fixture_file, capsys) -> None:
        path = fixture_file("raw", GOOD)
        code = main(["run", str(path), "--dry-run", "--raw", "--input",
                     "value=123"])
        assert code == 0
        captured = capsys.readouterr()
        assert "123" in captured.out


class TestValidateCommand:
    def test_valid_file(self, capsys) -> None:
        assert main(["validate", str(CHANGE)]) == 0
        assert "校验通过" in capsys.readouterr().out

    def test_invalid_file(self, fixture_file, capsys) -> None:
        path = fixture_file("valbad", BAD_YAML)
        assert main(["validate", str(path)]) == 2
        assert "未知的步骤类型" in capsys.readouterr().err

    def test_json_mode(self, capsys) -> None:
        assert main(["validate", str(EXAMPLE), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["schema"]["name"]
        assert payload["schema"]["steps"]

    def test_unknown_fields_warning_mode(self, fixture_file, capsys) -> None:
        text = GOOD.replace("    tool: file_read",
                            "    tool: file_read\n    note: 给人看的备注")
        path = fixture_file("unknown", text)
        assert main(["validate", str(path)]) == 2          # 默认按错误处理
        capsys.readouterr()
        assert main(["validate", str(path), "--unknown-fields", "warning"]) == 0
        captured = capsys.readouterr()
        assert "未知字段 'note'" in captured.err            # 放行了也要如实列出

    def test_image_of_examples_pass(self) -> None:
        """仓库里的示例必须永远能过校验 —— 它们是最常被抄的模板。"""
        for name in ("hello.yaml", "change_request.yaml"):
            assert main(["validate", str(FIXTURE_DIR / name)]) == 0


# ═══════════════════════════════════════════════════════════════
# 真子进程：断言**进程退出码**
# ═══════════════════════════════════════════════════════════════


def _run_process(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """起一个真进程跑 CLI；环境不允许时返回 None（由调用方 skip）。

    为什么值得单独测：CI 依赖的是**操作系统看到的退出码**，而
    `main()` 的返回值到进程退出码之间还隔着 `sys.exit()` 这一层。
    """
    try:
        return subprocess.run(
            [sys.executable, "-m", "automind.workflow", *args],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)
    except (OSError, subprocess.SubprocessError, PermissionError):
        return None


class TestRealProcess:
    """进程级退出码。

    受限环境（如本机沙箱）可能直接拒绝创建子进程，此时 **skip 并把原因写明**：
    "没测到"和"测过了通过"是两件事，不许混为一谈。
    """

    @pytest.fixture(autouse=True)
    def _require_subprocess(self) -> None:
        probe = _run_process(["--help"])
        if probe is None:
            pytest.skip("当前环境不允许创建子进程，进程级退出码未能验证"
                        "（main() 返回值已由 TestExitCodes 覆盖）")

    def test_dry_run_exit_zero(self) -> None:
        proc = _run_process(["run", str(CHANGE), "--dry-run",
                             "--input", "ticket_id=INC0012345"])
        assert proc is not None
        assert proc.returncode == 0
        assert "试运行计划" in proc.stdout

    def test_missing_file_exit_two(self) -> None:
        proc = _run_process(["run", "根本不存在.yaml"])
        assert proc is not None
        assert proc.returncode == 2
        assert "文件不存在" in proc.stderr

    def test_step_failure_exit_one(self, fixture_file) -> None:
        path = fixture_file("proc-failing", FAILS)
        proc = _run_process(["run", str(path)])
        assert proc is not None
        assert proc.returncode == 1

    def test_module_help_lists_commands(self) -> None:
        proc = _run_process(["--help"])
        assert proc is not None
        assert "run" in proc.stdout and "validate" in proc.stdout
        assert "退出码" in proc.stdout
