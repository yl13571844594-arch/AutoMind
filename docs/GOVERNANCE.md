# 执行治理参数手册（设置 → 执行治理）

> 面向：运维 / 排障 / 二次开发。说明 **26 个执行治理参数**的默认值、生效时机、
> 读取位置与调参建议；以及 v1.11.16 修掉了哪些"保存了但不生效"的问题。
>
> 设置界面：AutoMind → 左下角「设置」→「🛡 执行治理」。
> 接口：`GET/POST /api/config/autopilot`（POST 返回 `applied` / `rejected` /
> `effective`，每个参数都带生效时机与说明）。
>
> 诚实边界：标注「下次任务生效」的参数，对**正在执行的那一步/那个任务不追溯**；
> 界面上的标签与本文的"生效时机"列即用户能看到的事实，不用猜。

---

## 1. 生效时机一览（26 个）

| 分组 | 参数 | 默认 | 生效时机 | 读取位置 | v1.11.16 状态 |
|---|---|---|---|---|---|
| 自主闭环 | `auto_review` | `true` | 下次任务（收尾阶段读取） | `agent._autonomy_closure` | 有效（补界面提示） |
| 自主闭环 | `auto_verify` | `true` | 下次任务（收尾阶段读取） | `agent._autonomy_closure` | 有效（补界面提示） |
| 自主闭环 | `auto_test` | `true` | 下次任务 | `agent._run_react`（TDD 内环）、收尾测试 | 有效（补界面提示） |
| 自主闭环 | `parallel_execution` | `true` | 即时（下一次调度） | `PlanExecutor.parallel` | 有效（v1.7.6 已即时同步） |
| 自主闭环 | `subtask_cache` | `true` | 即时（下一次调度） | `PlanExecutor.use_cache` | 有效（v1.7.6 已即时同步） |
| 治理开关 | `isolate_workspace` | `false` | 下次任务 | `workspace.prepare` ← `agent._prepare_workspace` | 有效（补界面提示 + 克隆兜底） |
| 治理开关 | `terminal_background_enabled` | `true` | 下次工具调用 | `TerminalTool.background_enabled`（每次调用前读实时配置） | **修复：此前只在构造时读一次** |
| 治理开关 | `release_slot_on_approval_wait` | `true` | 下次审批 | `server._release_slot_on_approval_wait`（每次审批读实时配置） | 有效（补界面提示） |
| 治理开关 | `trace_enabled` | `true` | 即时（下一条轨迹事件） | `core.trace.TraceRecorder.enabled` | **修复：此前记录器读出厂默认值、关不掉** |
| 数值 | `tool_timeout_seconds` | `300` | 下次工具调用 | `TerminalTool.timeout` / `PwshTool.timeout` / 收尾测试超时 | **修复：此前只在构造时读一次** |
| 数值 | `tool_timeout_max_seconds` | `1800` | 下次工具调用 | `TerminalTool.max_timeout` / `PwshTool.max_timeout` / ReAct 单步上限 | **修复：此前只在构造时读一次** |
| 数值 | `approval_timeout_seconds` | `300` | 下次审批 | `server._approval_timeout_seconds`（每次审批读实时配置） | **修复：此前任务开始读一次** |
| 数值 | `tool_output_max_chars` | `12000` | 下次任务 | `output_budget.limits_from_config` → ReAct 执行器 | 有效（补界面提示） |
| 数值 | `auto_verify_max_rounds` | `2` | 下次任务（收尾阶段读取） | `agent._autonomy_closure` | 有效（补界面提示） |
| 数值 | `trace_max_runs` | `20` | 即时（下一次轮转） | `core.trace.TraceRecorder.max_runs` | **修复：此前改不动；界面也缺这个输入框** |
| 数值 | `goal_timeout_seconds` | `900` | 即时（下一次调度） | `PlanExecutor.goal_timeout` | 有效（v1.7.0 已即时同步） |
| 数值 | `react_tool_timeout_seconds` | `300` | 下次任务 | `ReActExecutor.tool_timeout` | 有效（补界面提示） |
| 数值 | `max_task_seconds` | `1800` | 下次任务 | `agent._begin_task_budget`（每任务算墙钟截止） | 有效（补界面提示） |
| 数值 | `react_tool_budget` | `14` | 下次任务 | `ReActExecutor.tool_budget` | 有效（补界面提示） |
| 数值 | `react_repeat_action_threshold` | `2` | 下次任务 | `ReActExecutor.progress`（重复动作阈值） | 有效（补界面提示） |
| 数值 | `react_no_progress_limit` | `12` | 下次任务 | `ReActExecutor.no_progress_limit` | 有效（补界面提示） |
| 数值 | `task_token_budget` | `500000` | 下次任务 | `ResourceManager.tokens.budget`（POST 时同步到全局与全部会话克隆） | **修复：此前改完旧实例仍用旧预算** |
| 数值 | `max_iterations` | `50` | 下次任务 | `ReActExecutor.max_iterations` | 有效（补界面提示） |
| 数值 | `budget_auto_continue` | `10` | 即时（下一个预算窗口） | `agent._budget_auto_continue`（每窗口读实时配置） | 有效（补界面提示） |
| 策略 | `approval_timeout_action` | `reject` | 下次审批 | `server._approval_timeout_action`（每次审批读实时配置） | **修复：此前任务开始读一次** |
| 策略 | `write_conflict_policy` | `warn` | 下次文件写入 | `file_editor._write_guard` → `WriteGuard.policy` | **修复：此前新造默认配置，改了不生效** |

> 统计：5 个布尔开关 + 4 个布尔附加项 + 15 个数值 + 2 个策略 = **26 个**。
> 设置面板与后端共用同一份清单（`server._AUTOPILOT_*`），新增参数只需改一处。

---

## 2. v1.11.16 修复清单（之前"设了不生效"）

| 参数 | 之前的问题 | 现在的做法 |
|---|---|---|
| `trace_enabled` / `trace_max_runs` | 轨迹记录器懒构造一次、`_cfg()` 读的是 `ExecutionConfig()` **出厂默认值**；设置里改完，下一条事件照样落盘 / 保留数不变 | Agent 构建时注册配置提供者（`trace.set_config_provider`）+ POST 时 `trace.apply_execution_config` 实时更新 `enabled/max_runs` |
| `terminal_background_enabled` | `TerminalTool` 在 Agent 构造时把开关抄成实例属性；注册表在会话克隆间共享，改设置到不了旧实例 | 终端/PowerShell 工具**每次执行前**通过 `live_execution_config()` 读当前任务配置并刷新 |
| `tool_timeout_seconds` / `tool_timeout_max_seconds` | 同上（超时抄成构造时快照） | 同上；另 `PwshTool.max_chars` 也随 `tool_output_max_chars` 实时刷新 |
| `write_conflict_policy` | `file_editor._write_guard()` 每次 `ExecutionConfig()` 新建默认配置；改成 `block` 仍按 `warn` 放行 | 守卫从当前任务绑定 Agent 的实时配置取策略（`session_ctx.agent()`），下一次写入即按新策略 |
| `approval_timeout_seconds` / `approval_timeout_action` | 在任务开始时读一次并闭包捕获；任务中途改设置对后续审批不生效，界面却显示"已保存" | 每次审批回调触发时现读（等待上限、超时处置、前端倒计时/提示同一份值） |
| `task_token_budget` | 预算在 Agent / 会话克隆构造时写进 `ResourceManager`；设置里改完旧实例一直用旧预算 | POST 时同步到全局 Agent 与所有已存在克隆的 `ResourceManager.tokens.budget`；每个任务开始时重置用量（窗口按新预算计） |
| 其余 20 个 | 本身已生效，但界面没有任何"生效时机"提示，用户以为改完立刻作用于正在跑的任务 | POST/GET 返回 `effective`（时机 + 说明）；设置面板在每个参数后标注「即时生效 / 下次任务生效 / 下次审批生效 / 下次调用生效 / 下次写入生效」 |

另外补了两处**一致性兜底**：

- `_sync_execution_config`：每次取会话执行器时把执行治理参数从全局 Agent 对齐到克隆
  （防止"配置改了但克隆是之后创建的 / 某次同步失败"沿用旧快照）；
- `_restore_autopilot_into_execution`：把"重启后恢复配置"从 `_rebuild_agent` 内联逻辑
  抽成可测试函数，非法旧值只记警告、不拖垮启动。

---

## 3. 调参建议（按场景）

| 场景 | 建议 |
|---|---|
| 安装/编译/长测试频繁超时 | `tool_timeout_seconds` 600~1200；超长命令用 `background=true`（保持 `terminal_background_enabled=true`） |
| 偶发命令挂死拖住任务 | 保持 `react_tool_timeout_seconds` 300；计划模式靠 `goal_timeout_seconds` 900 兜底 |
| 上下文/token 成本过高 | 调小 `tool_output_max_chars`（如 6000）与 `react_tool_budget`（如 8~10）；观察右栏"省 token 账目"再决定 |
| 长任务总时长不可控 | `max_task_seconds` 按业务上限设置（默认 1800）；纯后台批处理可设 0（不限）+ 依赖 `task_token_budget` 兜底 |
| 超长任务频繁中断 | 提高 `task_token_budget` 或 `budget_auto_continue`（总花费上限 ≈ 预算 × (1+续开次数)） |
| 多会话并发写同一项目 | `isolate_workspace=true`（产物在会话副本里，可回应用项目）或 `write_conflict_policy=block` |
| 无人值守/自动化流水线 | 审批模式设为「自动」或 `approval_timeout_action=reject`（fail-closed）；`release_slot_on_approval_wait=true` 避免审批占槽 |
| 生产取证/合规 | 保持 `trace_enabled=true`；`trace_max_runs` 按留存要求调整（20 ≈ 每会话 20 次任务，超出淘汰最旧） |
| 排查"改完没反应" | 看界面参数标签的生效时机；`GET /api/config/autopilot` 的 `effective` 字段是权威说明 |

---

## 4. 界面提示

「设置 → 执行治理」中：

- 每个布尔开关/数值/下拉标签后都有小字：`· 即时生效` / `· 下次任务生效` /
  `· 下次审批生效` / `· 下次调用生效` / `· 下次写入生效`；
- 悬停标签可看到更具体的一句说明（例如"ReAct 执行器按任务构造；当前任务不追溯"）；
- 保存后的 toast 会带上生效时机（例如"单任务最大步数 → 20（下次任务生效）"）；
- 底部提示改为："所有设置改动即时保存；生效时机以各参数标签为准
  （「下次任务生效」对进行中的任务不追溯）"。

---

## 5. 回归测试

```bash
python -m pytest tests/test_execution_governance.py -q   # 26 参数矩阵 + 行为断言，约 2s
python -m pytest tests/server/test_settings_autopilot.py tests/core/test_autonomy.py \
                 tests/core/test_v164_governance.py tests/core/test_workspace_governance.py \
                 tests/tools -q
```

`tests/test_execution_governance.py` 覆盖：

1. 26 个参数逐个通过 `POST /api/config/autopilot` 修改 → 全局 Agent、已存在克隆、
   GET 快照、持久化配置四处一致，且响应带 `effective`；
2. 行为级断言：轨迹开关/保留数、终端超时与后台通道、写冲突 block、
   审批超时/处置（同一回调中途改设置下一次审批生效）、并行/缓存/单步超时、
   ReAct 参数下次任务生效、整轮墙钟预算、token 预算同步、目录隔离、自主闭环开关；
3. 非法值 `rejected`、重启恢复、克隆兜底同步。
