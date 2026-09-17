"""工作流即代码（Workflow as Code）—— 确定性、可评审、可版本化的流程定义。

为什么需要这个包
----------------
AutoMind 原有的两条执行路径（ReAct / Plan-and-Execute）都是**运行时决定步骤**：
模型面对一句目标自己挑动作。它适合"没写过的任务"，但交付给客户时有两个
绕不过去的问题：

1. **没有承载物。** 客户要的是"先拉工单 → 查 CMDB → 生成变更单 → 等人批 →
   执行 → 回写工单"这样一条**确定的**流程。它现在只存在于提示词里 ——
   改一个字就是改代码，客户无从评审，更无法证明"你跑的就是我批的那版"。
2. **不可复现。** 同一句输入两次跑出的步骤可能不同，于是出了事故也无法
   "照着上次那条路径重跑一遍"。

本包补上那个承载物：**YAML 定义 + 校验器 + 确定性执行器**。

    from automind.workflow import load_workflow, run_workflow

    schema = load_workflow("examples/06-workflow/change_request.yaml")
    run = await run_workflow(schema, {"ticket_id": "INC0012345"},
                             registry=agent.tool_registry, llm=agent.llm,
                             approval=my_approval_cb)
    print(run.exit_code, run.status)

四个模块各管一段，边界清晰：

* `schema`    —— 数据结构与字段校验（"这份文件说清自己要干什么了吗"）
* `loader`    —— YAML/JSON 解析 + 行号定位 + 重复键检出
* `template`  —— `{{ }}` 严格渲染（**只取值不求值**，不给代码执行留通道）
* `executor`  —— 按文件顺序执行、失败策略、dry-run、结构化报告

刻意不包含的东西：**没有规划器**。工作流的步骤顺序由文件写死，模型只被当作
"一次文本生成"使用。这是特性，不是缺失 —— 理由见 executor 模块开头。
"""

from __future__ import annotations

from automind.workflow.exceptions import (
    Issue,
    Position,
    TemplateError,
    UnknownSchemaVersionError,
    WorkflowError,
    WorkflowExecutionError,
    WorkflowLoadError,
    suggest,
)
from automind.workflow.executor import (
    EXIT_BAD_WORKFLOW,
    EXIT_CANCELLED,
    EXIT_OK,
    EXIT_STEP_FAILED,
    StepReport,
    WorkflowExecutor,
    WorkflowRun,
    check_inputs,
    digest_of,
    report_to_json,
    run_workflow,
)
from automind.workflow.loader import (
    WorkflowLoader,
    load_version_checked,
    load_workflow,
    validate_file,
)
from automind.workflow.schema import (
    SUPPORTED_VERSIONS,
    FailurePolicy,
    InputSpec,
    StepSpec,
    WorkflowSchema,
)
from automind.workflow.template import render, render_structure, validate_template

__all__ = [
    "EXIT_BAD_WORKFLOW",
    "EXIT_CANCELLED",
    "EXIT_OK",
    "EXIT_STEP_FAILED",
    "SUPPORTED_VERSIONS",
    "FailurePolicy",
    "InputSpec",
    "Issue",
    "Position",
    "StepReport",
    "StepSpec",
    "TemplateError",
    "UnknownSchemaVersionError",
    "WorkflowError",
    "WorkflowExecutionError",
    "WorkflowExecutor",
    "WorkflowLoadError",
    "WorkflowLoader",
    "WorkflowRun",
    "WorkflowSchema",
    "check_inputs",
    "digest_of",
    "load_version_checked",
    "load_workflow",
    "render",
    "render_structure",
    "report_to_json",
    "run_workflow",
    "suggest",
    "validate_file",
    "validate_template",
]
