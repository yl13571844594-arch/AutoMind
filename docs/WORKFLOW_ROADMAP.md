# 工作流自动化 —— 版本发布需求与步骤

> 编制日期：2026-09-29 ｜ 基线版本：1.7.7（当前 1.11.16）｜ 维护方式：每个版本发布后回填第 0 节状态表与第 9 节变更记录
>
> 本文档管"**做什么、做到什么程度算完成**"；"**怎么打包发布**"见仓库根目录 [RELEASE.md](../RELEASE.md)，
> 工作流文件格式参考见 [WORKFLOWS.md](WORKFLOWS.md)，版本特性分层机制见 [EDITIONS.md](EDITIONS.md)。

---

## 0. 状态总览（每版发布后更新）

| 版本 | 主题 | 版本层级 | 需求编号 | 预估 | 状态 |
|---|---|---|---|---|---|
| **v1.8.0** | 工作流可用化（Web + 异步 + 续跑 + agent 步骤） | 社区版 | R-101 ~ R-110 | 9–11 人日 / 2–3 个会话 | 🟨 代码完成（2026-09-30）：R-101~R-110 全部实现并有自动化测试；待 7.3 真实 Key 冒烟与 7.4 打包发布 |
| **v1.9.0** | 无人值守自动化（触发器 + 并行 + 历史） | 专业版 | R-201 ~ R-209 | 7–9 人日 / 约 2 个会话 | 🟨 代码完成（2026-10-04）：R-201~R-209 全部实现并有自动化测试（社区 100 项 + 专业版 26 项）；待 7.3 真实 Key 冒烟（含专业版三项）与 7.4 打包发布（与 v1.8.0 一并） |
| **v1.10.0** | 治理与固化（发布审批 + 权限 + 审计 + 轨迹转工作流） | 企业版（固化为专业版） | R-301 ~ R-308 | 7–11 人日 / 2–3 个会话 | 🟨 代码完成（2026-10-04）：R-301~R-308 全部实现并有自动化测试（社区 44 项 + 专业 / 企业版 24 项）；待 7.3 真实 Key 冒烟（含 SSO 多身份审批）与 7.4 打包发布（与 v1.8.0 / v1.9.0 一并） |
| **v1.11.16** | 自动化强化（步骤可靠性 + 条件编排 + 运行治理 + 触发器可靠 + 可观测） | 社区版 + 专业版 | R-401 ~ R-405 | 1–2 人日 / 1 个会话 | 🟨 代码完成（2026-10-23）：R-401~R-405 全部实现并有自动化测试（`tests/workflow/test_automation_v1116.py` 56 项 + 既有 456 项全过）；待 7.3 真实 Key 冒烟与 7.4 打包发布 |

状态图例：⬜ 未开始 · 🟨 进行中 · ✅ 已发布 · ⏸ 暂缓（写明原因）

---

## 1. 现状基线（v1.7.7）

**已具备**（不要重复建设）：

| 能力 | 位置 |
|---|---|
| YAML/JSON 定义、行号定位的校验、重复键与未知字段拦截 | `automind/workflow/loader.py`、`schema.py` |
| 步骤类型 `tool` / `llm` / `human` / `branch`；失败策略 `abort` / `continue` / `retry(n)`；单步 `timeout` | `automind/workflow/schema.py` |
| 确定性执行器，失败不记成成功（`partial`），`human` 无人批按拒绝，取消向上传播 | `automind/workflow/executor.py` |
| 执行器事件回调 `run_workflow(..., on_event=...)`（**已存在，但 REST 未接**） | `executor.py` `run_workflow` |
| 模板只取值不求值（`{{ inputs.x }}`、`{{ steps.id.output.y }}`） | `automind/workflow/template.py` |
| CLI：`python -m automind.workflow run / validate / --dry-run` | `automind/workflow/__main__.py` |
| REST：`GET /api/workflow/list`、`POST /api/workflow/validate`、`POST /api/workflow/run`（管理动作） | `automind/server.py` |
| 审批链路：弹窗广播到会话页面 + 外部回执 `POST /api/approvals/{id}` + 超时处置 | `server.py` `make_approval_callback` |
| 检查点状态机（running / partial / interrupted / completed / resumed / dismissed） | `automind/state/checkpoint.py`（v1.7.7） |
| 出站事件（webhook） | `automind/core/webhooks.py` |
| 版本特性开关、限额 | `automind/core/edition.py`、`automind/core/quota.py` |
| 步骤级条件 `if` / `unless`、失败降级 `on_failure: fallback(...)`、整轮 `timeout_seconds`、整轮 / 单步 token 统计（v1.11.16） | `automind/workflow/schema.py`、`executor.py` |
| 触发器错过补偿（`catch_up` / `max_delay_seconds`）、webhook 重放保护（`dedupe_window`）、通知含耗时 / token / 最慢步骤（v1.11.16） | `automind/workflow/schema.py`、`pro/automind_pro/workflow_pro.py` |
| 测试 189 个 | `tests/workflow/` |

**缺口**（本路线图要解决的）：Web 无入口；REST 运行同步阻塞（无运行 id / 进度 / 取消 / 历史）；
不能断点续跑；没有 `agent` 步骤；没有循环与并行；没有触发器；不走会话身份与 Token 预算。

---

## 2. 版本分层与限额（已定方案）

### 2.1 自动化分级

| 版本 | 自动化程度 | 触发方式 |
|---|---|---|
| 社区版 | 手动 / 外部驱动 | 界面「运行」、CLI、REST（外部 cron / CI 调用） |
| 专业版 | 内置无人值守 | 应用内定时（cron）、入站 webhook；失败自动重试与通知；重启后按策略续跑 |
| 企业版 | 受管控的无人值守 | 专业版全部 + 仅已批准版本可触发 + 按角色授权 + 全程审计 |

### 2.2 能力归属

| 版本 | 能力 |
|---|---|
| 社区版 | 引擎与校验；4 种基础步骤；Web 工作流视图；异步运行与进度；取消；断点续跑；`agent` 步骤；小规模 `foreach` |
| 专业版 | 触发器（cron / webhook）；`parallel` 并行组；大规模 `foreach`；运行历史 90 天与观测看板；触发运行的重启续跑策略；轨迹导出为工作流；自定义工作流模板 |
| 企业版 | 按 digest 绑定的发布审批；按工作流的 RBAC；多级 / 按角色 `human` 审批；运行审计导出；凭据保管；并发配额与排队 |

### 2.3 限额（写入 `automind/core/quota.py` 的 `QUOTA_RULES`）

| 限额项 | 键名 | 社区版 | 专业版 | 企业版 |
|---|---|---|---|---|
| 已保存工作流数 | `workflows` | 20 | 200 | 不限（`None`） |
| 活跃触发器数 | `workflow_triggers` | 0 | 30 | 不限 |
| 定时最小间隔（秒） | `workflow_min_interval` | — | 300 | 60 |
| 运行历史保留 | `workflow_history_days` | 最近 50 次 | 90 | 可配置 |
| `foreach` 单次最大迭代 | `workflow_foreach_max` | 20 | 1000 | 不限 |
| 单个工作流步骤数 | — | 不限 | 不限 | 不限 |
| 每次运行消耗每日任务额度 | — | 计 1 次（共用每日 100 次） | 不限 | 不限 |

**计数口径**：以项目 `workflows/` 目录下校验通过的文件为准。超出上限的文件**仍可列出、校验、dry-run**，
只是不能运行，界面标注"超出社区版上限（20 个），升级或删除其他工作流后可运行"。不在加载时隐藏。

### 2.4 新增特性键（v1.8.0 先登记在 `edition.py` 的 `PLANNED_FEATURES`，实现交付时移入 `COMMERCIAL_FEATURES`）

| 键 | 最低版本 | 中文名 |
|---|---|---|
| `workflow_triggers` | pro | 工作流触发器（定时 / webhook） |
| `workflow_parallel` | pro | 工作流并行执行 |
| `workflow_history` | pro | 工作流运行历史与看板 |
| `workflow_from_trace` | pro | 从执行轨迹生成工作流 |
| `workflow_governance` | enterprise | 工作流发布审批与权限 |

**拦截原则**：在**加载 / 校验阶段**拦截（例如"`parallel` 需要专业版，第 12 行"），不在运行到一半才失败；
dry-run 始终可用。

---

## 3. 开工前必须先定的设计规则

每条定稿后把"结论"一栏填上，再开始写对应需求的代码。

| 编号 | 问题 | 建议方案 | 结论 |
|---|---|---|---|
| D1 | `agent` 步骤内部的工具审批，与工作流 `human` 步骤共用同一条审批链路，怎么区分、怎么限权？ | `agent` 步骤的工具集 = 工作流声明的 `allowed_tools` ∩ 全局可用工具；步骤内的审批事件带 `workflow_run_id` + `step_id`，界面显示"来自工作流 X 第 N 步"；无人值守运行时按该工作流的审批策略处置（默认拒绝） | ✅ 采纳（v1.8.0）。补充：未声明 `allowed_tools` 时只给 SAFE 级工具并在加载期提醒；白名单外的调用**不弹审批**直接拒绝并记入 `denied_tools`；审批走同一条链路（`make_approval_callback(extra=…)`） |
| D2 | `parallel` 组部分成功时，续跑从哪里开始？ | 以**子步骤**为续跑粒度：组内已 `ok` 的子步骤跳过，其余重跑；组内任一子步骤 `abort` 则整组失败；续跑前重新检查组的依赖输出 | ✅ 采纳（v1.9.0）。补充：每个子步骤结束即落盘（快照带序号，迟到的旧快照不覆盖新快照）；子步骤 abort 时**未启动的**不再启动、已在跑的跑完为止；任一子步骤失败整组记失败，组自己的 on_failure 决定整轮；整组已成功时续跑连子步骤输出一并恢复。foreach（含逐项并发）仍以整步为粒度 |
| D3 | 工作流运行算不算"任务"？是否消耗每日额度？ | 一次运行计 1 次任务（不按步骤计）；运行写入任务历史，`interaction = "workflow"` | ✅ 采纳。续跑也计 1 次；dry-run 不计；运行未能开始时退还 |
| D4 | 工作流运行用哪个 Agent 实例？ | 用会话克隆（`_acquire_run_agent`），与普通任务同样的会话身份、隔离工作目录、Token 预算；触发器发起的运行使用专用会话 `wf_<工作流名>` | ✅ 采纳并细化：克隆键用 `wf:<run_id>`（每次运行独享、结束即释放，避免与同会话对话任务共用 LLM 计数与执行态），会话身份 / 隔离目录仍按发起会话绑定；运行中挂忙标记防 LRU 淘汰。触发运行的专用会话 `wf_<文件名>`（纯中文文件名取摘要）于 v1.9.0 落地 |
| D5 | 服务重启后，未跑完的运行怎么办？ | 手动运行：只提示，不自动续跑（会花钱）；触发器运行：按工作流 `on_restart: resume / notify`（默认 `notify`，专业版） | ✅ 全部落地：手动运行只提示（v1.8.0）；触发运行按工作流顶层 `on_restart: resume / notify`（默认 notify）处置，两种都按 notify 配置发通知（v1.9.0，R-208） |
| D6 | `agent` 步骤的输出结构？ | `{answer, artifacts[], actions_ok, actions_failed, stop_reason}`，后续步骤用 `{{ steps.x.output.answer }}` 引用 | ✅ 采纳。成功判据为 `stop_reason = no_more_tools`；其余结束原因记失败但保留部分输出；`denied_tools` / `tokens` / `iterations` 进报告 `detail` |

---

## 4. v1.8.0（社区版）—— 工作流可用化

**目标**：普通用户在 Web 上就能写好、跑起、看懂、续上一条工作流；工作流里可以有一步"交给 Agent 自主完成"。

| 编号 | 需求 | 涉及文件 | 验收标准 |
|---|---|---|---|
| R-101 | **异步运行**：`POST /api/workflow/run` 立即返回 `run_id`；执行放后台任务；WS 推送 `wf_run_start` / `wf_step_start` / `wf_step_done` / `wf_run_done`（接执行器已有的 `on_event`） | `server.py`、`web/src/ws.ts` | 一个 5 分钟的工作流，HTTP 请求在 1 秒内返回；界面逐步看到进度 |
| R-102 | **查询与取消**：`GET /api/workflow/runs/{run_id}`（状态 + 报告）、`POST /api/workflow/runs/{run_id}/cancel` | `server.py` | 运行中取消 → 当前步骤标 `cancelled`、整轮 `cancelled`，子进程被终止 |
| R-103 | **写入任务历史**：运行结果进 `task_history`（`interaction="workflow"`），消耗 1 次每日额度（D3） | `server.py`、`core/quota.py` | 历史视图里能看到工作流运行及其报告 |
| R-104 | **按步骤检查点 + 断点续跑**：每步完成后落盘（复用 `CheckpointManager` 与状态机）；`POST /api/workflow/runs/{run_id}/resume` 跳过 `ok` 的步骤、从报告恢复其输出 | `workflow/executor.py`、`state/checkpoint.py`、`server.py` | 第 5 步时断网或停止，续跑从第 5 步开始，第 1–4 步不重复执行（工具调用计数可证） |
| R-105 | **走会话体系**（D4）：工作流运行使用会话克隆、会话身份、隔离工作目录与 Token 预算 | `server.py`、`agent.py` | 与对话任务并发时 Token 统计互不串；开启隔离时产物进会话副本并出现产物卡片 |
| R-106 | **`agent` 步骤**：`type: agent`，字段 `goal`（必填）、`allowed_tools`、`max_steps`（默认 20）、`token_budget`、`mode: react`；输出结构见 D6；审批规则见 D1 | `workflow/schema.py`、`workflow/executor.py`、`planning/react_executor.py` | 示例：`tool` 拉数据 → `agent` 分析并写报告 → `human` 审批 → `tool` 发送；`allowed_tools` 之外的工具调用被拒绝并写进报告 |
| R-107 | **小规模 `foreach`**：`type: foreach`，`items: "{{ ... }}"`、`steps: [...]`、上限取 `workflow_foreach_max`（社区版 20） | `workflow/schema.py`、`executor.py` | 超出上限在校验阶段报错并给出行号 |
| R-108 | **Web 工作流视图**：侧栏「工作台」新增「🔀 工作流」；列表（名称 / 描述 / 校验状态 / 最近运行）；在线编辑与校验（错误定位到行）；按 `inputs` 自动生成入参表单；dry-run 预览；运行进度（复用执行轨迹气泡）；`human` 步骤复用审批弹窗；报告下载 | `web/src/components/views/WorkflowView.tsx`（新）、`Sidebar.tsx`、`store/app.ts` | 不写代码、不开命令行，完成"新建 → 校验 → 试运行 → 运行 → 审批 → 看报告" |
| R-109 | **加号菜单入口**：输入框「＋」增加「▶ 运行工作流」，选择后弹入参表单 | `web/src/components/chat/ChatPanel.tsx` | 对话中一步发起工作流，运行卡片出现在当前会话 |
| R-110 | **限额与拦截框架**：`QUOTA_RULES` 增加 2.3 节各键；`COMMERCIAL_FEATURES` 增加 2.4 节各键；校验阶段拦截；失败通知出站事件 `workflow.failed` / `workflow.partial` | `core/quota.py`、`core/edition.py`、`workflow/loader.py`、`core/webhooks.py` | 社区版第 21 个工作流可校验、可 dry-run、不能运行，提示语正确；失败运行触发出站事件 |

**v1.8.0 新增接口**（发布时更新 `tests/server/routes_snapshot.txt`）：

```
GET  /api/workflow/runs/{run_id}
POST /api/workflow/runs/{run_id}/cancel
POST /api/workflow/runs/{run_id}/resume
```

**`agent` 步骤示例**（写进 `docs/WORKFLOWS.md` 与 `examples/06-workflow/`）：

```yaml
- id: analyze
  type: agent
  goal: "阅读 {{ steps.fetch.output.path }}，找出错误率最高的 3 个接口并写成 report.md"
  allowed_tools: [file_read, file_write, python_sandbox]
  max_steps: 15
  token_budget: 80000
  on_failure: abort
```

---

## 5. v1.9.0（专业版）—— 无人值守自动化

**前置**：v1.8.0 已发布；先读 `pro/automind_pro` 里现有 scheduler 的接口（此前未审阅，可能增加 1–2 人日）。

> **审阅结论（2026-10-04）**：现有 `scheduler.py` 是"按固定间隔跑一句任务描述"，状态存在配置文件、
> 无 cron、无入参、无运行记录，与工作流触发器的需求（cron + 入参 + 续跑 + 运行记录）对不上，
> **不复用**；工作流触发器另起 `pro/automind_pro/workflow_pro.py`，经扩展上下文的 v1.9.0 追加键
> （`start_workflow` / `resume_workflow` / `workflow_catalog` / `workflow_store` / `workflow_running`）
> 走社区核心的同一条运行路径。旧的「定时任务」保持不变。
>
> **实现说明**：① 并发执行器是专业版特性 `workflow_parallel` 的实现，社区核心只有语义一致的逐个执行兜底
> （正常路径上加载期就拦了）；② R-206 条件扩展对**全部版本**开放（§2.2 未把它列为商业能力）；
> ③ R-205 的"与 parallel 组合"实现为 foreach 的 `max_concurrency`（逐项并发，每项独立上下文）；
> ④ R-209 的"失败重试"实现为触发器的 `retry` / `retry_delay`（从断点续跑，而不是从头重跑）；
> ⑤ 顺带修正 branch 的 then 排在 else 后面时两条路都不走的问题（加载期报错）。

| 编号 | 需求 | 涉及文件 | 验收标准 |
|---|---|---|---|
| R-201 | **定时触发器**：工作流顶层 `triggers: [{cron: "0 9 * * 1-5", inputs: {...}}]`；接入专业版 scheduler；最小间隔见 2.3 | `workflow/schema.py`、`pro/automind_pro/…scheduler` | 到点自动运行；间隔低于下限在校验阶段报错 |
| R-202 | **入站 webhook 触发器**：`POST /api/workflow/hooks/{hook_id}`，带令牌校验；请求体映射为 `inputs` | `server.py`、`pro/…` | 无令牌或令牌错误返回 401；正确时返回 `run_id` |
| R-203 | **触发器管理**：界面列出、启停、查看下次运行时间；数量受 `workflow_triggers` 限额 | `WorkflowView.tsx`、`server.py` | 第 31 个触发器创建失败并提示升级企业版 |
| R-204 | **`parallel` 并行组**：`type: parallel`，`max_concurrency`（默认 4），子步骤并发；续跑粒度见 D2 | `workflow/executor.py` | 3 个各 10 秒的子步骤总耗时约 10 秒；部分失败后续跑只重跑失败的子步骤 |
| R-205 | **大规模 `foreach`**：上限提升到 1000，可与 `parallel` 组合 | `workflow/executor.py` | 1000 项迭代内存稳定，报告按项汇总 |
| R-206 | **条件扩展**：`branch` 增加 `in` / `>` / `<` / `>=` / `<=` / `exists`，仍为白名单、不求值 | `workflow/schema.py`、`template.py` | 未知运算符在校验阶段报错并给出可选值 |
| R-207 | **运行历史 90 天 + 看板**：按工作流统计成功率、平均耗时、Token 消耗；接入观测中心 | `server.py`、`pro/…observability` | 看板数据与报告逐条一致 |
| R-208 | **重启续跑策略**（D5）：`on_restart: resume / notify` | `server.py` 启动流程 | 触发器运行被重启打断后，按策略自动续跑或发出通知 |
| R-209 | **失败重试与通知**：触发器运行 `partial` / `failed` 时按工作流 `notify` 配置发出站事件或 IM 消息 | `core/webhooks.py`、`tools/…notify` | 失败后 1 分钟内收到通知，内容含失败步骤与原因 |

---

## 5.5 v1.11.16（社区版 + 专业版）—— 自动化强化

> 背景：v1.8.0 ~ v1.10.0 把"能不能跑、能不能无人值守、能不能治理"补齐了；
> 本版补的是**真实生产里反复遇到的那几类自动化缺口**（步骤坏了只会整轮中止、
> 条件只能靠 branch 跳转、触发器错过/重放没有策略、看板 token 恒为 0）。
> 设计原则不变：能在加载期拒绝的绝不留给运行时；失败不许伪装成成功。

| 编号 | 需求 | 版本 | 验收标准 |
|---|---|---|---|
| R-401 | **步骤级条件执行**：`if` / `unless`（与 branch 共用白名单比较，不做表达式求值）；条件不成立记 `skipped`、输出为 null（后续引用明确报错）；条件无法判定记失败；foreach / parallel 子步骤同样支持；dry-run 只渲染预览不跳过 | 社区 | 条件真/假两条路径都真实执行；跳过步骤不写空串；`tests/workflow/test_automation_v1116.py::TestCondition*` |
| R-402 | **失败降级分支**：`on_failure: fallback(<步骤id>)`，失败后跳到同文件内更靠后的步骤做降级，跳转区间标 `skipped`，失败步骤仍记 `failed`（整轮 partial）；加载期校验目标存在 / 向后 / 非子步骤 | 社区 | 失败后降级步骤真实执行、依赖步骤被跳过、恢复后继续；目标不合法加载期报错 |
| R-403 | **整轮硬超时**：顶层 `timeout_seconds`；到点当前步骤 `aborted`、剩余 `skipped`、整轮 `aborted` 并写明上限 | 社区 | 慢工具真实中止（耗时 < 上限 + 容差），不是只写字段；`0` 保持旧行为 |
| R-404 | **可观测**：每步 / 整轮 token 统计写入报告（llm 取响应 usage、agent 取 D6 `tokens`）；通知模板补整轮耗时 / token / 最慢步骤；看板 token 不再恒为 0 | 社区 + 专业 | 报告 `tokens.total` 与运行记录摘要一致；通知正文含三项数字 |
| R-405 | **触发器可靠性**：cron 错过补偿 `catch_up: once\|skip` + `max_delay_seconds`（迟到多久内才补跑，默认补跑一次 = 旧隐式行为）；webhook 重放保护 `dedupe_window`（Idempotency-Key 或请求体 sha256，窗口内重复返回 `duplicate: true` 且不重复触发） | 专业（沿用 `workflow_triggers` 门控） | 停机一夜恢复只补一次 / 可配置不补；重复 webhook 只触发一次；状态字段可在 `GET /api/workflow/triggers` 看到 |

**不做 / 剩余项**（明确记录，避免误以为"已全绿"）：

- **跨触发器 singleton**（同一工作流只允许一个运行、并发触发排队）：现有"同一触发器串行、繁忙跳过"
  只覆盖单触发器；跨触发器的进程级排队需要改服务端运行登记（`server.py` 不在本版改动范围），
  留待后续版本，或在网络层 / 外部调度侧用单实例约束解决；
- **手动重跑单个失败步骤**：断点续跑以"已成功步骤"为粒度，重跑失败步骤 = 续跑整份
  （失败步骤自然会重跑）；单独"只重跑这一步"没有明确语义（后续步骤依赖它的新输出），暂不做；
- **循环终止条件 / while**：v1 明确不支持循环（`branch`/`fallback` 只能向后跳），本版不变；
- **foreach 循环内 branch / 嵌套 foreach**：仍按原限制拒绝（报告逐项对照的语义优先）；
- **社区版触发器**：仍是专业版能力（加载期拦截），本版只增强专业版触发器可靠性。

---

## 6. v1.10.0（企业版为主）—— 治理与固化

| 编号 | 需求 | 版本 | 验收标准 |
|---|---|---|---|
| R-301 | **发布审批**：工作流有"草稿 / 已发布"两态；发布需审批人批准；运行时校验 digest 与已批准版本一致 | 企业 | 修改一个字后未经审批不能被触发器运行 |
| R-302 | **按工作流 RBAC**：谁能查看 / 编辑 / 运行 / 挂触发器（接现有 `rbac` 特性） | 企业 | 无运行权限的用户调用运行接口返回 403 |
| R-303 | **多级 / 按角色 `human` 审批**：`approvers: [role:finance, role:manager]`、`mode: all / any` | 企业 | 两级审批都通过才继续；任一拒绝即按失败策略处理 |
| R-304 | **运行审计导出**：每次运行的输入、每步输出摘要、审批人与时间，导出 PDF / JSON | 企业 | 导出内容与报告逐条对应 |
| R-305 | **凭据保管**：工作流内以 `{{ secrets.NAME }}` 引用，值不进报告与日志 | 企业 | 报告、日志、轨迹中均查不到明文 |
| R-306 | **并发配额与排队**：按团队限制同时运行数，超出排队 | 企业 | 并发上限为 2 时第 3 个运行进入排队并显示位次 |
| R-307 | **从执行轨迹生成工作流**：把一次成功的 ReAct 任务导出为 YAML 草稿（工具步骤 + 可选 `agent` 步骤），人审后入库 | 专业 | 导出的草稿能通过校验并 dry-run |
| R-308 | **自定义工作流模板库** | 专业 | 模板可保存、复用、导入导出 |

> **实现说明（2026-10-04）**：
> ① 身份：SSO 会话（新增 `team` 字段）→ 用户 / 角色 / 团队；没有 SSO 时本机与持平台令牌的请求记为 admin
> （审计写明来源）。审批回答此前不带身份，本版起 WebSocket 连接与外部回执都带审批人。
> ② R-301 的"已发布版本"以**文件摘要**为准；触发器、失败自动续跑、重启续跑都只跑已发布版本；手动运行
> 草稿默认允许（可配置）；四眼原则在 SSO 身份下生效。
> ③ R-302 动作命名 `workflow:<view|edit|run|trigger|approve>:<文件>`；SSO 用户的工作流动作改由它判定，
> 不再要求管理员令牌（凭据 / 并发配额仍是管理动作）。
> ④ R-303 `mode: all` 按声明顺序逐级；身份不符的回答拒收而不是当拒绝。
> ⑤ R-305 凭据只能进 tool 参数（进提示词 / 待批内容 / 条件都会泄露，加载期拒绝）；脱敏覆盖报告、事件、
> 快照、日志；存储可选 Fernet 静态加密，推荐环境变量注入。
> ⑥ R-306 的"团队"取 SSO 用户的 team（缺省按角色），触发运行归入 triggers；排队中的运行仍是 running
> 状态（可停止），位次单独给出。
> ⑦ R-307 的轨迹来源是执行轨迹 JSONL（`step_action` 事件，参数在轨迹里截到 200 字符 —— 草稿会标出来）。
> ⑧ R-308 另设特性键 `workflow_templates`（§2.2 已列为专业版能力，原 §2.4 未给键名）。

---

## 7. 每个版本的发布步骤（按顺序勾选）

> 通用打包、上传、签发步骤以 [RELEASE.md](../RELEASE.md) 为准；下面只列工作流版本额外要做的事。

### 7.1 开发阶段

- [x] 第 3 节中本版相关的设计规则已定稿并填入"结论"（v1.8.0：D1 / D3 / D4 / D5 手动部分 / D6）
- [x] 本版需求逐条实现，每条都有自动化测试（放在 `tests/workflow/`、`tests/server/`）
- [x] 新增的特性键与限额已写入 `edition.py` / `quota.py`，校验阶段拦截有测试
- [x] 新接口已加入路由快照：`python tests/server/test_route_inventory.py --update`，并 review 快照 diff
- [x] `examples/06-workflow/` 增加本版新能力的示例文件，且被 `tests/workflow/test_examples.py` 覆盖

### 7.2 版本号与文档

- [x] 复制 `_perfcheck/bump_to_1_7_7.py` 为 `bump_to_<新版本>.py`，修改 `OLD` / `NEW` / `DATE` / 摘要 / 条目后运行
      （它同时更新 6 处版本号与 3 份使用手册，只改当前版本标识，不改历史条目）
- [x] `CHANGELOG.md` 增加本版条目：根因 → 修复 / 新增 → 新接口 → 测试
- [x] `docs/WORKFLOWS.md` 补充新字段的参考说明与示例
- [x] `docs/EDITIONS.md` 更新版本能力对照表（涉及版本分层时）
- [ ] 本文档第 0 节状态改为 ✅，第 9 节追加变更记录
- [x] 核对 `RELEASE.md` 顶部"当前版本"与本版一致（v1.7.8 已修正此前停留在 v1.7.4 的问题）

### 7.3 验证

- [x] 前端：`cd web && npx tsc --noEmit && npm run build`（产物随包分发，不重建等于界面没更新）
- [x] 全量回归：`pytest -q && ruff check .`；涉及专业版时加 `pytest pro/tests -q`
- [x] `tests/test_version_consistency.py` 通过
- [ ] **真实 Key 冒烟**（不可省略，自动化测试没有真实模型）：
  - [ ] 运行一个含 `agent` 步骤的示例工作流，检查报告与产物
  - [ ] 运行到中途断网或点停止，再续跑，确认已完成步骤未重复执行
  - [ ] `human` 步骤：界面审批一次，外部回执接口审批一次
  - [ ] （专业版）定时触发器到点运行一次；webhook 触发一次；失败通知收到一次
- [x] 在隔离数据目录中验证界面（`AUTOMIND_DATA_DIR=<临时目录>`，从临时项目目录启动服务），不污染真实配置

### 7.4 发布

- [ ] 按 `RELEASE.md` 构建社区版产物（构建审计通过）、桌面安装包、商业包
- [ ] GitHub Release 正文粘贴 CHANGELOG 对应段落；校验和资产命名为 `SHA256SUMS`
- [ ] PyPI 上传（不可撤销，确认版本号）

---

## 8. 风险与估时

| 风险 | 影响 | 应对 |
|---|---|---|
| `agent` 步骤的审批嵌套（D1）设计不清 | v1.8.0 延期 1–2 天 | 先定 D1 再写代码；无人值守默认拒绝 |
| `parallel` 与续跑交互（D2） | v1.9.0 延期 1–2 天 | 以子步骤为续跑粒度，先写失败场景测试 |
| 专业版 scheduler / rbac 现有接口未审阅 | v1.9.0、v1.10.0 各可能多 1–2 天 | v1.9.0 开工第一步先读 `pro/automind_pro` |
| 本地无真实模型，自动化测试覆盖不到行为质量 | 发布后才暴露问题 | 7.3 的真实 Key 冒烟列为发布门槛 |

**合计**：一人全职约 5–6 周；由 Claude 在会话中实现约 6–8 个会话（不含真实 Key 验收）。

---

## 9. 文档变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-23 | 1.11.16 | v1.11.16 自动化强化代码完成（R-401 ~ R-405）：步骤级 `if`/`unless`、失败降级 `on_failure: fallback(...)`、整轮 `timeout_seconds`、每步/整轮 token 统计与通知增强（社区）；cron 错过补偿 `catch_up`/`max_delay_seconds`、webhook 重放保护 `dedupe_window`、通知含耗时/token/最慢步骤（专业版，沿用 `workflow_triggers` 门控，**未新增特性键**）。新增 `tests/workflow/test_automation_v1116.py` 56 项；`tests/workflow` 合计 456 项全过。明确不做：跨触发器 singleton、单步手动重跑、while 循环。待 7.3 真实 Key 冒烟与 7.4 打包发布 |
| 2026-10-04 | 1.10.0 | v1.10.0 代码完成（R-301 ~ R-308），三版路线的规划项全部交付；第 0 节标 🟨（待真实 Key 冒烟与发布，与 v1.8.0 / v1.9.0 一并）；第 6 节补实现说明。新增接口 18 个（治理 / 凭据 / 并发 / 审计 / 轨迹草稿 / 模板库），社区版均为 403 + 升级提示；`workflow_from_trace` / `workflow_templates` / `workflow_governance` 移入 `COMMERCIAL_FEATURES`，`PLANNED_FEATURES` 清空；RBAC 内置角色增加工作流动作与 approver 角色。7.1 / 7.2 全部完成，7.3 的前端构建、全量回归、版本一致性已完成 |
| 2026-10-04 | 1.9.0 | v1.9.0 代码完成（R-201 ~ R-209）；D2 定稿、D5 触发器部分落地、D4 专用会话落地；第 0 节标 🟨（待真实 Key 冒烟与发布，与 v1.8.0 一并）；第 5 节补"scheduler 审阅结论"与实现说明。新增接口 6 个（`/api/workflow/triggers`、`…/{key}/toggle|token|run`、`/api/workflow/hooks/{key}`、`/api/workflow/dashboard`），社区版均为 403 + 升级提示；特性键 `workflow_triggers` / `workflow_parallel` / `workflow_history` 移入 `COMMERCIAL_FEATURES`；`QUOTA_RULES` 增加 `workflow_history_days`。7.1 / 7.2 全部完成，7.3 的前端构建、全量回归、版本一致性已完成 |
| 2026-09-30 | 1.8.0 | v1.8.0 代码完成（R-101 ~ R-110）；D1 / D3 / D4 / D6 定稿、D5 手动部分落地、D2 顺延 v1.9.0；第 0 节标 🟨（待真实 Key 冒烟与发布）；7.1 / 7.2 与 7.3 的前端、隔离目录验证已勾选。实现与路线图的差异：① 断点续跑存于 `workflow_runs/`（独立于 `CheckpointManager`，但复用同一套状态词，理由见 `automind/workflow/runs.py`）；② 另增 `GET /api/workflow/runs`、`…/dismiss`、`/api/workflow/source`、`/save`、`/delete` 五个接口供界面使用；③ foreach 续跑粒度为整步；④ 2.4 节的五个特性键先登记在 `edition.PLANNED_FEATURES`（带计划版本），各自实现交付时再移入 `COMMERCIAL_FEATURES` —— 直接放进后者会让企业版许可证的 `feature_flags()` 出现五个 False（pro/tests 断言全部为真）、界面显示 🔒 |
| 2026-09-29 | 1.7.8 | 基线顺延至 1.7.8（输入卡片改版，不影响路线图范围）；`RELEASE.md` 版本号已修正 |
| 2026-09-29 | 初稿（基线 1.7.7） | 建立三版路线：v1.8.0 社区版可用化、v1.9.0 专业版无人值守、v1.10.0 企业版治理；定分层、限额、特性键与设计规则 |
