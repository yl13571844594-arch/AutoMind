"""评测报告 —— 结构、JSON 序列化与人类可读渲染。

报告字段的约定（也是对外承诺，接进 CI 后不应随意改名）：

  · ``status``        ``passed`` / ``failed`` / ``error``
      - ``passed``：任务跑完且**全部**断言通过；
      - ``failed``：任务跑完但有断言不通过（含超时）；
      - ``error`` ：任务本身没跑起来（执行器抛异常、事故性错误）。
      把"跑不起来"与"跑了没过"分开，是为了不让一个坏掉的执行器伪装成
      "模型退化"—— 两者的处理动作完全不同。
  · ``pass_rate``     通过任务数 / 总任务数（``error`` 计为不通过）。
  · ``total_tokens`` / ``estimated_cost_usd``
      全部任务的用量与**估算**成本（见 ``pricing.py``：价格表会过时，
      仅供成本控制，不等于账单）。
  · ``assertions``    逐条断言结果：``type/expected/actual/passed/detail``，
      失败时 ``expected`` 与 ``actual`` 必须同时可读 —— 这是报告的全部价值所在。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from automind.eval.pricing import AS_OF

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_ERROR = "error"

_STATUS_ICON = {STATUS_PASSED: "✓", STATUS_FAILED: "✗", STATUS_ERROR: "!"}


@dataclass
class CaseResult:
    """单个任务的执行与判定结果。"""

    id: str
    status: str = STATUS_FAILED
    mode: str = ""
    seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    assertions: list[dict[str, Any]] = field(default_factory=list)
    output_preview: str = ""
    error: str = ""
    timed_out: bool = False
    workspace: str = ""
    #: 该任务是"反向断言"（期望有断言失败，见 suite.EvalCase.expect_fail）。
    #: 报告里必须显式标出，否则读者会把它的 ✗ 明细当成真实退化。
    expect_fail: bool = False

    @property
    def passed(self) -> bool:
        return self.status == STATUS_PASSED

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "status": self.status, "passed": self.passed,
            "mode": self.mode, "seconds": round(self.seconds, 3),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "tool_calls": self.tool_calls,
            "assertions": self.assertions,
            "failed_assertions": [a for a in self.assertions if not a.get("passed")],
            "output_preview": self.output_preview,
            "error": self.error,
            "timed_out": self.timed_out,
            "workspace": self.workspace,
            "expect_fail": self.expect_fail,
        }


@dataclass
class EvalReport:
    """一次套件运行的完整报告。"""

    suite: str
    path: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    model: str = ""
    provider: str = ""
    cases: list[CaseResult] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""
    notes: list[str] = field(default_factory=list)

    # ── 汇总 ────────────────────────────────────────────────

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed_cases(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def failed_cases(self) -> int:
        return sum(1 for c in self.cases if c.status == STATUS_FAILED)

    @property
    def error_cases(self) -> int:
        return sum(1 for c in self.cases if c.status == STATUS_ERROR)

    @property
    def pass_rate(self) -> float:
        return round(self.passed_cases / self.total, 4) if self.total else 0.0

    @property
    def total_seconds(self) -> float:
        end = self.finished_at or time.time()
        return (end - self.started_at) if self.started_at else 0.0

    @property
    def total_tokens(self) -> int:
        return sum(c.total_tokens for c in self.cases)

    @property
    def total_prompt_tokens(self) -> int:
        return sum(c.prompt_tokens for c in self.cases)

    @property
    def total_completion_tokens(self) -> int:
        return sum(c.completion_tokens for c in self.cases)

    @property
    def estimated_cost_usd(self) -> float:
        return sum(c.estimated_cost_usd for c in self.cases)

    @property
    def all_passed(self) -> bool:
        return bool(self.cases) and self.passed_cases == self.total and not self.aborted

    def as_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "path": self.path,
            "model": self.model,
            "provider": self.provider,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_seconds": round(self.total_seconds, 3),
            "total": self.total,
            "passed": self.passed_cases,
            "failed": self.failed_cases,
            "errors": self.error_cases,
            "pass_rate": self.pass_rate,
            "all_passed": self.all_passed,
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "pricing_as_of": AS_OF,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "notes": self.notes,
            "cases": [c.as_dict() for c in self.cases],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=indent)

    def write(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")
        return p

    # ── 人类可读 ────────────────────────────────────────────

    def render(self) -> str:
        lines: list[str] = []
        lines.append("=" * 72)
        lines.append(f"套件 {self.suite}　模型 {self.provider}/{self.model}")
        lines.append(f"通过 {self.passed_cases}/{self.total}　"
                     f"通过率 {self.pass_rate * 100:.1f}%　"
                     f"耗时 {self.total_seconds:.1f}s　"
                     f"Token {self.total_tokens}"
                     f"（prompt {self.total_prompt_tokens} + "
                     f"completion {self.total_completion_tokens}）　"
                     f"估算成本 ${self.estimated_cost_usd:.4f}")
        lines.append(f"（成本为估算值，价格表口径 {AS_OF}，仅供成本控制参考）")
        if self.aborted:
            lines.append(f"!! 评测中止：{self.abort_reason}")
        for note in self.notes:
            lines.append(f"注：{note}")
        lines.append("-" * 72)
        for c in self.cases:
            icon = _STATUS_ICON.get(c.status, "?")
            tag = "（反向断言：期望失败）" if c.expect_fail else ""
            lines.append(f"{icon} {c.id}{tag}　{c.seconds:.1f}s　{c.total_tokens} tok　"
                         f"${c.estimated_cost_usd:.4f}　{c.status}")
            if c.tool_calls:
                names = [tc.get("name", "?") for tc in c.tool_calls]
                lines.append(f"    工具调用：{names}")
            for a in c.assertions:
                mark = "✓" if a.get("passed") else "✗"
                lines.append(f"    {mark} [{a.get('type')}] 期望：{a.get('expected')}")
                if not a.get("passed"):
                    lines.append(f"        实际：{a.get('actual')}")
                    detail = a.get("detail") or {}
                    if detail:
                        lines.append(f"        细节：{detail}")
            if c.error:
                lines.append(f"    错误：{c.error}")
        lines.append("=" * 72)
        return "\n".join(lines)
