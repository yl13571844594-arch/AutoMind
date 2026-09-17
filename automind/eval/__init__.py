"""评测框架（Eval）—— 把"改了提示词到底有没有退化"从感觉变成可复现的数字。

为什么需要它：本仓库此前**没有任何评测集**。于是三件事同时不成立：

  · **交付无法验收**：客户能看到的只有"演示时效果不错"，拿不出任何可复跑的
    证据；"这次改动让 X 类任务从 8/10 掉到 5/10"这种结论根本无法产生。
  · **改动无法验证**：改提示词、换模型、调温度，靠人工点几下看看 —— 退化
    往往在几周后由用户发现。
  · **badcase 无法固化**：用户报的失败案例只用嘴描述，修完没法证明真的修好了。

本包的思路很朴素：**一个套件 = 一批任务 + 每个任务的断言**，跑完给一份
包含通过率/逐任务明细/耗时/Token/成本的 JSON 报告。任务之间互不污染
（各自独立的临时工作目录），执行器可注入（离线测试用假 agent）。

模块分工：

  · ``suite.py``      —— YAML 套件格式与解析（格式错误要当场报，不能静默跳过）
  · ``assertions.py`` —— 断言实现（失败报告必须能看出"哪条、期望什么、实际什么"）
  · ``executors.py``  —— 执行器（默认走 AutoMindAgent，可注入假 agent）
  · ``pricing.py``    —— Token → 成本的估算
  · ``report.py``     —— 报告结构与 `--json` 输出
  · ``runner.py``     —— 编排 + CLI（``python -m automind.eval run <suite.yml>``）
"""

from __future__ import annotations

from automind.eval.report import EvalReport
from automind.eval.runner import run_suite
from automind.eval.suite import EvalCase, EvalSuite, load_suite

__all__ = ["EvalCase", "EvalReport", "EvalSuite", "load_suite", "run_suite"]
