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

---

## 6. 运行底座治理：审计账本与分派级闸门（v1.11.19）

第 1–4 节讲的是「设置里的参数是否真的生效」。本节讲另一件事：**Agent 运行时到底
执行了什么、谁批准的、有没有绕过审批** —— 这是 B 端「出了事要说得清」的底线。

### 6.1 为什么需要：内存审计 + 审批旁路

- 旧实现的审批结果只存在 `PermissionEngine.audit_log`（内存）：进程一重启全丢，
  没有可校验的历史，无法证明「这条高危操作当时被拒绝过」。
- 审批只在 **ReAct / Plan 执行器内部**做；而 **工作流 `tool` 步骤**、**技能内部
  调用**、**服务端/库直接 `registry.dispatch`** 三条链路不经过执行器 ——
  高危工具在 `ask` 模式下也能被静默执行，且没有任何记录。

### 6.2 审计账本（`automind/core/governance.py`）

落盘位置：`<数据目录>/governance/ledger-YYYYMMDD.jsonl`（开发模式即
`./.automind/governance/`）。单文件 8MB 或跨天轮转（同日 `-2/-3…`），最多保留
10 个文件；`state.json{seq,last_hash,file}` 保证跨文件、跨重启的链与序号连续。

**哈希链**：每条 `hash = sha256(去掉 hash 后的紧凑规范 JSON)`，`prev` = 上一条
`hash`。任何人改动 / 删除 / 插入中间记录，`verify_chain()` 都能指出断点
（`broken_at` + `expected/actual`）。两类记录、18 个固定字段：

| kind | 含义 | 关键字段 |
|---|---|---|
| `decision` | 治理决策 | `decision`（allow / deny / approved / denied / modified / error / allow_unattended / deny_unattended）、`reason`、`tier`、`risk`、`governed`、`source` |
| `execution` | 实际执行结果 | `decision="executed"`、`success`、`duration_ms`、`error` |

公共字段：`seq / ts / session / tool / params`（脱敏+截断）/ `call_id`。
`source` 取值 `direct / react / plan / workflow / skill`，如实反映调用通道。

**安全与健壮性**：

- params 的敏感键名（`api_key` / `token` / `password` / `secret` …）整值打码，
  其余值先过 `core/redact.py` 再截断到 200 字符、最多 12 个键 —— 文件内容与
  密钥明文不落盘。
- 所有写入 **fail-open**：账本故障只记 warning，绝不把「写不进审计」变成任务失败。
- 环境变量：`AUTOMIND_GOVERNANCE_AUDIT=0` 关闭；`AUTOMIND_GOVERNANCE_DIR` /
  `AUTOMIND_GOVERNANCE_MAX_FILE_BYTES`（默认 8MB）/
  `AUTOMIND_GOVERNANCE_MAX_FILES`（默认 10）可调。

### 6.3 分派级治理闸门（堵住旁路）

`ToolRegistry` 新增 `set_governor()`；Agent 在构造与会话克隆时安装
`governance.make_unattended_governor()`。`dispatch()` 在参数校验通过、真正执行
之前统一问闸门：

| 调用来源 | 处理 |
|---|---|
| `react` / `plan` | 执行器内已逐次人工审批 → **不二次审批**（避免重复弹窗） |
| `direct` / `workflow` / `skill` | 无人值守通道：用与 `PermissionEngine` 同一套 `assess_risk` / `effective_tier` 算有效等级；有效等级 DANGEROUS 且审批模式不是「全批准」→ **拒绝执行**（失败关闭），其余放行 |

- 拒绝时返回可操作错误（说明来源、风险、如何改为经审批执行），
  `metadata.governance_denied = true`，且工具**不执行**。
- 风险评估异常时退回工具声明等级继续判定（算不出风险 ≠ 放行高危）；
  闸门自身异常按 fail-open 处理 —— 治理设施故障不能成为新的可用性单点。
- 每次判定与每次执行都写账本；裸 `ToolRegistry`（单测/库调用）不装闸门，
  既有行为零变化。
- 要让无人值守通道执行高危动作，唯一正路是**显式**把审批模式设为「全批准」
  （此时会记 `allow_unattended`，仍可审计），而不是把闸门关掉。

### 6.4 治理中心（接口 + 界面）

接口（与 `/api/audit` 同级管理员守卫：本机回环或 `X-Admin-Token`，**失败关闭**）：

| 接口 | 用途 |
|---|---|
| `GET /api/governance/summary` | 账本统计 + 实时策略快照 + 有界链路校验（首屏 2000 条） |
| `GET /api/governance/audit` | 记录过滤（tool / decision / kind / source / session）+ limit（≤1000） |
| `GET /api/governance/verify` | 完整哈希链校验（断点、原因、expected/actual） |
| `GET /api/governance/export` | 导出 JSON（社区版可用；PDF 导出仍属专业版 `audit_export`） |

界面：「🛡️ 安全审计」页顶部「🏛 运行底座治理」卡片 —— 策略快照、账本状态、
「🔗 验证审计链」「⬇ 导出 JSON」；账本为取证数据，界面**不提供清空**
（导出后离线留存）。治理接口不可用时回退内存审计并明确提示，不出现空白页。

### 6.5 验收与回归

```bash
# 定向（治理账本 / 分派闸门 / 治理中心）
python -m pytest tests/core/test_governance.py tests/tools/test_dispatch_governance.py \
                 tests/server/test_governance_api.py -q

# 社区版全量（本机有商业许可证时加 AUTOMIND_FORCE_COMMUNITY=1，
# 或直接用发布门禁脚本，它会自动注入临时数据目录与强制社区版）
python scripts/acceptance.py --profile full
```

> 发布前门禁的完整矩阵见 [ACCEPTANCE.md](ACCEPTANCE.md)。

### 6.6 已知边界（诚实说明）

- 账本是**单进程内**串行写入的哈希链；多进程同时写同一个数据目录不在支持范围
  （桌面版单进程、服务端单 worker 是默认部署形态）。
- 账本记录的是「治理层看到的事实」，不替代操作系统审计（进程级操作仍可能绕过
  Agent 工具层直接被外部执行）。
- 分派级闸门对**有效等级 DANGEROUS** 的操作失败关闭；对 SENSITIVE 放行是刻意
  取舍（保持工作流/技能可用），这些调用仍有完整决策与执行记录可追溯。
