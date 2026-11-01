# 多智能体工程协同（Collaboration）

> 版本：v1.11.22 ｜ 相关代码：`automind/agent.py`（`run_multi` / `run_subtask`）、
> `automind/core/config.py`（`multi_agent_*`）、`pro/automind_pro/multiagent/`、
> `automind/tools/write_guard.py`、`scripts/collab_benchmark.py`
> 相关文档：[EVAL.md](EVAL.md)（评测框架）· [GOVERNANCE.md](GOVERNANCE.md)（治理与审批）·
> [EDITIONS.md](EDITIONS.md)（社区版 / 专业版 / 企业版）

这份文档回答四个问题：**工程协同是什么、它怎么运转、它的能力边界在哪、
怎么用同一批任务对比 single / multi / subagent 三种执行模式**。

最后一条尤其重要：本文档**不包含任何真实基准数字**。仓库里没有提交过 benchmark
结果，也没有"多智能体提升 X%"这类结论 —— 想知道差异，请在本地用
`scripts/collab_benchmark.py --live` 自己跑（见第 5 节）。

---

## 1. 它解决什么问题：从"多角色文本流水线"到"能动手的工程团队"

v1.11.21 及之前的协同模式（`MultiAgentOrchestrator`）是一条**文本流水线**：

```
协调者拆解 → 角色 A 写一段文本 → 角色 B 基于白板写一段文本 → … → 综合者汇总
```

它能"分工讨论"，但**角色不能真正动手**：不能读写文件、不能跑测试、不能并行、
没有角色级预算，也没有"改错了回炉"和"收尾验证"。于是它适合写方案 / 做评审，
不适合"把这个仓库的 bug 修掉"。

v1.11.22 新增 `EngineeringOrchestrator`（专业版特性 `multi_agent` 的新实现），
把协同升级成**受控的工程团队**：

| 能力 | 旧：文本流水线 | 新：工程协同（v1.11.22） |
| --- | --- | --- |
| 任务拆解 | 线性角色清单 | **DAG**（`depends_on` 表达依赖），2–6 个节点 |
| 角色能否动手 | 不能，只能输出文本 | 能：以受限工具白名单跑 `run_subtask`（读 / 写 / 执行） |
| 执行方式 | 逐个角色串行 | **按 DAG 并行**（默认同时 2 个角色；写范围冲突自动串行） |
| 工作区 | 各自文本输出 | **共享同一个 `project_root`**（真实文件系统） |
| 写冲突 | 不存在 | `write_paths` 归一化 + `write_guard.path_lock` 加锁 |
| 预算 | 全任务共享 | **角色级预算**（步数 / token / 墙钟各自独立） |
| 审查 | 无 | 写角色完成后跑只读 reviewer，未通过**有界回炉** |
| 验收 | 无 | 收尾跑项目测试 + 语义验收，失败最多再修 1 轮 |
| 可观测 | 文本 steps | `ma_plan` / `ma_step_*` / `ma_review` / `ma_rework` / `ma_verify` 事件 + `metrics` |

纯文本任务（没有 coder、没有写文件、任务里也没有代码 / 文件 / 命令 / 测试 /
构建这类关键词）仍然走**旧文本流水线** —— 避免为"写一份方案"启动一整套工程设备。

---

## 2. 架构与执行模型

### 2.1 分层

```
┌──────────────────────────────────────────────────────────────┐
│ 社区核心（开源，MIT）                                          │
│   AutoMindAgent.run_multi()     多智能体入口：预算 / 检查点 / 续跑│
│   AutoMindAgent.run_subtask()   受限工具 + 独立预算的角色执行器  │
│   ExecutionConfig.multi_agent_* 角色预算 / 并行 / 回炉 / 验证参数 │
│   server.py                     task 记录带 verification/metrics │
│   Web 前端                       协同卡片：DAG / 预算 / 验证摘要   │
└───────────────────────────┬──────────────────────────────────┘
                            │ 扩展协议 v1：feature.create(llm, agent=…)
┌───────────────────────────▼──────────────────────────────────┐
│ 商业扩展包（pro/，专业版 / 企业版）                             │
│   MultiAgentFeature.create(llm, agent=None)                   │
│     ├─ agent 存在 → EngineeringOrchestrator（本版新增）         │
│     └─ agent 缺省 → MultiAgentOrchestrator（旧文本流水线，保留）  │
│   EngineeringOrchestrator：拆解 DAG → 角色执行 → 审查回炉        │
│                            → 验证闭环 → 综合与返回               │
└──────────────────────────────────────────────────────────────┘
```

**为什么工程编排器需要父 Agent**：只有拿到父 `AutoMindAgent` 才能
`clone_for_session()`（每个角色一份受控会话）、共享工具注册表、并复用既有的
`_review_result()` / `_loop_verify()` / `_run_project_tests()` 验收方法。
社区 `run_multi` 用 `feature.create(self.llm, agent=self)` 传入父 Agent；
旧 Feature / 测试替身仍只接受 `llm` 时自动回退成 `create(self.llm)`。

### 2.2 一次工程协同的五个阶段

**A. 拆解 DAG（协调者）**

协调者 prompt 输出 JSON 数组（2–6 个节点），每个节点：

| 字段 | 含义 |
| --- | --- |
| `id` | 节点唯一标识（`s1` / `s2` …） |
| `role` | `planner` / `researcher` / `coder` / `writer` / `reviewer` |
| `subtask` | 该角色要完成的具体事项 |
| `depends_on` | 依赖的节点 id 列表（决定 DAG） |
| `write_paths` | 写作用域：项目内相对路径数组 |
| `allowed_tools` | 可选：该节点的工具白名单 |

校验：id 唯一、依赖存在且**无环**、角色合法；非法节点丢弃 / 规范化。
全部无效时退回 `planner → coder → reviewer` 顺序三角色兜底。
规范化后的计划通过 `ma_plan` 事件下发（`engineering: true`）。

**B. 角色执行（真正动手）**

每个节点：

1. `agent.clone_for_session()` 得到独立子 Agent；
   `session_id = f"{parent.session_id}:ma:{node_id}"`（会话隔离，按角色区分）。
2. 角色预算：`budget_auto_continue = 0`（**不续窗**）、
   `max_task_seconds = role_timeout_s`、`max_iterations = role_max_steps`。
3. 工具白名单按角色收紧（coder = 写 / 执行 + 读；writer = 写文件 / 读；
   其他角色只读 SAFE），最终再按父 Agent 实际注册的工具过滤；写角色若没有任何
   写工具则直接报错。
4. `child.run_subtask(..., max_steps=…, max_tokens=…, max_iteration_extensions=0,
   event_sink=emit, approval_callback=父审批回调)`。
5. 外层 `asyncio.wait_for(timeout=role_timeout_s)`：角色超时标记失败并继续
   （依赖它的节点 `skipped`），不无限等待。

每个角色开始 / 结束推送 `ma_step_start` / `ma_step_end`，step 记录含
`id/role/label/subtask/output/success/stop_reason/error/steps/tokens/llm_calls/
depends_on/write_paths/rework/review/verification`。

**C. 共享工作区 + 写冲突**

- 工作区**就是父 Agent 的 `project_root`**，不复制：角色看到的是同一批真实文件。
- `write_paths` 归一化为项目内绝对路径；写角色没有声明 scope 时使用全局键
  `__workspace__` —— 独占整片工作区，**牺牲并行换安全**（不知道写哪儿就别并排写）。
- 加锁用既有的 `automind.tools.write_guard.path_lock`：不同路径可并行，
  重叠 scope 串行；`AsyncExitStack` 保证异常路径也释放。
- 调度器按依赖 ready 集合取批，**批内避免写 scope 重叠**；重叠的角色排到后续批。

**D. reviewer 有界回炉**

- 写角色成功且 `multi_agent_review_writes=True` 时，调用父
  `agent._review_result(task, output)`（只读工具，既有方法）。
- 审查**不可用**（`available=False`，如模型没配好 / 审查设施故障）时只记录
  "审查不可用"，**不回炉、也不据此判失败** —— 设施故障不等于交付不合格。
- `approved=False` 且 `rework < multi_agent_max_rework`（默认 1）时，同一节点带着
  审查反馈重跑一次（仍受角色预算 / 锁 / 超时约束），rework 计数 +1。
- reviewer 角色本身不会被再次自动审查（避免无限套娃）。

**E. 验证闭环**

所有节点结束后：

1. `multi_agent_verify`（或既有 `auto_verify`）开启时跑语义验收
   `agent._loop_verify(task, combined_output)`；
2. `agent.config.execution.auto_test` 开启时跑项目测试
   `agent._run_project_tests()`（项目没有 tests 目录时返回 `None`）。
3. `verification = {"tests": …, "llm": verdict, "done": bool, "rounds": int}`；
   `done` 的口径：**测试通过 / 没有测试 / 测试设施不可用**，且（语义验收 done 或验收设施不可用；
   `verify=False` 时不跑语义验收、不据此判失败）。验收设施故障**不判失败**
   （与 v1.11.21 的自主闭环口径一致）；没有任何角色成功时验证标记 `skipped`
   （`done=False` + `skipped=True`，不当作验证通过）。
4. 失败且还没修过 → 选一个 coder / reviewer 节点，带"测试 / 验收反馈"重跑
   **一次**（有界），再跑一遍 tests / verify；仍失败 →
   `success=False, stop_reason="verification_failed"`，输出如实说明。

事件：`ma_verify` / `ma_done`（有验证证据才推送，不伪造）。

**F. 综合与返回**

父 `llm.generate` 综合各角色输出 + 验证摘要（有证据才写进最终输出）。
返回：

```python
{
  "output": str, "plan": list, "steps": list, "scratch": str,
  "last_output": str, "success": bool, "stop_reason": str, "error": str,
  "verification": {"tests": ..., "llm": ..., "done": bool, "rounds": int},
  "metrics": {
      "duration_ms", "llm_calls", "tokens", "roles",
      "parallel_batches", "rework_count", "review_count",
      "review_unavailable", "verify_rounds", "tests_passed",
  },
  "token_usage": TokenUsage,   # 父调用 + 每个角色 usage/tokens 聚合
  "engineering": True,         # 纯文本任务走旧流水线时为 False 且 verification=None
}
```

角色预算耗尽时抛 `BudgetExhaustedError`，`details["progress"]` 带
plan / steps / scratch / last_output —— 由社区 `run_multi` 写成可续跑检查点
（`resumable=True`），不会把"预算耗尽"伪装成成功或普通失败。

### 2.3 参数速查（`ExecutionConfig`，可用 `getattr` 缺省，旧配置也能跑）

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `multi_agent_max_parallel` | 2 | 同时执行的角色数；写范围冲突时自动降为串行 |
| `multi_agent_role_max_steps` | 12 | 每个角色最多 ReAct 步数 |
| `multi_agent_role_max_tokens` | 120000 | **每个角色独立** token 预算 |
| `multi_agent_role_timeout_seconds` | 300 | 每个角色墙钟上限 |
| `multi_agent_max_rework` | 1 | reviewer 不通过时最多回炉次数 |
| `multi_agent_verify` | True | 收尾跑测试 / 产物 / 语义验收 |
| `multi_agent_review_writes` | True | 写角色完成后跑只读 reviewer |

角色预算**独立**的理由：一个角色吃光全局预算会让其余角色直接饿死，
从"某个子任务写崩了"升级成"整轮协同失败"。独立后单个角色最多花自己的额度，
父任务还有下一轮 / 续跑的机会。

---

## 3. 能力边界（诚实说明）

- **专业版特性**：工程协同由 `pro/` 的 `EngineeringOrchestrator` 提供。
  社区版没有它；未激活商业扩展时 `run_multi` 会抛
  `FeatureNotAvailable`（基准脚本把该组合记为 `unavailable`，不当作模型能力）。
- **审批与治理不因模式而放宽**：角色调用仍是真实工具调用，走同一套
  `PermissionEngine` 与分派级治理闸门（见 [GOVERNANCE.md](GOVERNANCE.md)）。
  角色没有"多智能体特权"，写 / 执行工具在非「全批准」模式下同样可能被拦。
- **写锁只保证进程内一致**：`path_lock` 管的是本进程内并发角色；
  用户 / 其它进程在协同运行中改同一个文件，编排器感知不到。
- **无 scope 的写角色串行**：为了不任意覆盖，未知写范围走 `__workspace__`
  独占锁 —— 这会显著降低并行度，是刻意的安全取舍。
- **角色预算独立 ≠ 总预算无限**：父任务预算 / 墙钟仍在；角色耗尽只影响该角色，
  但整轮聚合 token 仍计入父任务统计。
- **reviewer 是启发式审查**：模型判断，可能漏报 / 误报；`available=False` 时不判失败，
  也不代表"交付已验证"。
- **验收不是正确性证明**：测试通过只说明项目自己的测试通过；没有测试时
  `tests=None`（"没测"而不是"通过"）。语义验收同样可能把"看起来完成"判成完成。
- **工程协同更适合"有文件 / 有测试 / 有明确验收"的任务**；纯问答 / 纯方案类
  任务走单 Agent 或旧文本流水线更便宜。
- **成本更高**：角色数 × 每角色步数 × 综合调用。默认 2 并行 / 12 步 / 12 万 token
  是"能干活"的保守值，不代表跑得便宜 —— 见第 5 节的对比方法。

---

## 4. 在界面上看什么

协同模式（专业版）运行时的关键信息都在协同卡片里：

1. **计划 / 依赖**：`ma_plan` 下发 DAG，卡片按节点显示 `依赖 s1,s2`；
2. **写范围**：`write_paths` 小字标注 —— 看到两个角色写同一路径说明会串行；
3. **角色预算**：每个角色的步数 / LLM 调用 / token；
4. **回炉与审查**：`rework` 标记与 reviewer 意见（有才显示）；
5. **验证摘要**：卡片底部"测试通过 / 验收通过 / 审查有意见 / 验收未执行"等，
   **只有真实产生 `verification` 才显示，不伪造**；
6. **`skip` / `fail` 图标**：依赖失败的节点不会执行，如实标 `skipped`。

旧任务记录 / 旧后端只有 `index` 没有 `id` 时，前端按 index 定位并隐藏新字段，布局不变。

---

## 5. 用 `scripts/collab_benchmark.py` 对比三种执行模式

### 5.1 三种模式

| 模式 | 实际入口 | 说明 |
| --- | --- | --- |
| `single` | `agent.run(prompt)` | 单 Agent「工作模式」：分层规划 + 工具执行 |
| `multi` | `agent.run_multi(prompt)` | 工程协同（需专业版激活） |
| `subagent` | `agent.run_subtask(...)` | 父 Agent 派一个受限子任务（显式写 / 执行工具白名单，不续步） |

固定任务集覆盖四类场景（定义在 `scripts/collab_benchmark.py` 的 `TASKS`）：

| 任务 id | 类别 | 说明 |
| --- | --- | --- |
| `analysis_summary` | analysis | 纯文本分析，不要求写盘 |
| `codegen_fizzbuzz` | codegen | 生成 `fizzbuzz.py` + 测试并跑通 |
| `bugfix_calc` | bugfix | 预置一个失败测试，修复实现 |
| `multi_file_pkg` | multi_file | 多文件小型包 + `python -m` 入口 + 测试 |

### 5.2 先看矩阵（默认 dry-run，不需要 Key）

```bash
python scripts/collab_benchmark.py --dry-run
```

输出任务 × 模式的 12 个组合，并写出一份**空结果**报告：

```
.automind/collab_benchmark/collab_benchmark.json   # dry_run=true，matrix=12，results=[]
.automind/collab_benchmark/collab_benchmark.csv    # 只有表头
```

dry-run **不调用任何模型、不产生任何指标**；CSV / JSON 里的 `results` 为空是刻意的
—— 不伪造"示例数据"。

### 5.3 真跑（`--live`，会消耗 Token）

```bash
# 配置任一提供商的 Key（示例：DeepSeek），或以配置面板里的 Key 为准
$env:AUTOMIND_DEEPSEEK_API_KEY = "sk-..."

# 跑全部任务 × 全部模式（默认 approve_all 无人值守审批，超时 300s）
python scripts/collab_benchmark.py --live

# 只跑某一类 / 某几个任务、某个模式
python scripts/collab_benchmark.py --live --task codegen_fizzbuzz,bugfix_calc --mode single,multi
python scripts/collab_benchmark.py --live --provider deepseek --model deepseek-chat
```

行为约定：

- **检测不到凭据时退出码 2**，明确报"无法运行基准"——绝不把"跑不了"记成
  "协同失败"；
- 每个 (任务, 模式) 组合用**独立工作区**（`<out>/work/<task>-<mode>`），互不污染；
- 结果写 `<out>/collab_benchmark.json` 与 `.csv`（默认 `.automind/collab_benchmark/`，
  已被 `.gitignore` 忽略；**不要提交真实结果 / API Key**）；
- `multi` 未激活专业版时该组合记 `unavailable`，不计入成功率分母。

### 5.4 指标定义

| 字段 | 含义 | 注意 |
| --- | --- | --- |
| `status` | `ok` / `unavailable` / `error` | `unavailable`/`error` 不是模型能力 |
| `success` | Agent 自己报告的成败 | 与 `verification_passed` / `artifacts_ok` 分开看 |
| `duration_ms` | 墙钟耗时 | 含工具 / 测试真实时间；不同机器不可横比 |
| `tokens` | 本组合累计 token | 优先取 AgentResult / 编排器聚合 `token_usage` |
| `llm_calls` | LLM 调用次数 | 成本与"话多"的直接指标 |
| `steps` | 执行步数 | 单 Agent 用 `steps_executed`；协同用角色 step 数 |
| `verification_passed` | **Agent 自己的验证闭环**结论 | `None` = 该模式没这个环节 / 未测量 |
| `artifacts_ok` | benchmark 侧检查"期望文件是否存在" | `None` = 纯文本任务不检查 |
| `rework_count` | 编排器记录的回炉次数 | 单 Agent / 子任务恒为 0 |
| `human_interventions` | 中途人工介入次数（插话 + 审批回调） | 无人值守基准通常为 0，**不是统计缺失** |
| `manual_rework` | **人工回填**：这次交付你手工返工了吗 | CSV 列，脚本不填 |

**汇总口径**：

- 成功率 / 平均耗时等只统计 `status=ok` 的行；
- `verification_pass_rate` / `artifacts_pass_rate` 的分母是"真实测量到的行"，
  没测到就是 `null`（不是 0%，也不是 100%）；
- `manual_rework` 人工填写后再自己统计（例如 15 次交付里手工返工 3 次）。

CSV 里 `manual_rework` 留空即可，人工回填示例：`是（实现跑不通，手动修了 import）` /
`否`。

### 5.5 怎么读结果（避免自欺）

1. **先看 `status` 分布**：`unavailable` / `error` 一多，比较的是环境不是模型。
2. **成功率不是全部**：`success=true` 但 `artifacts_ok=false` 说明"它说做完了，
   文件却没写对" —— 这正是工程协同的验证闭环要堵的洞。
3. **成本一起看**：multi 通常更贵（角色 × 步数 × 综合）；只看成功率会得出
   "多智能体碾压"的片面结论。
4. **同条件对比**：同一 provider / model / 审批模式 / 超时 / 任务集，
   最好重复跑 3 次以上看波动；单次结果不足以当结论。
5. **人工返工率**：真实交付质量的人工口径写在 `manual_rework`；它比 `success`
   更接近"用户是否真的省事"。

### 5.6 当前状态（诚实声明）

- 仓库**未提交任何真实 benchmark 结果**：没有"多智能体 vs 单 Agent"的数字，
  `docs/` 与 README 里也不会出现这类结论。
- `tests/planning/test_collab_benchmark_metrics.py` 只测**指标聚合 / 矩阵 /
  CSV / dry-run**（fake 数据，不需要模型）；CI 不跑 `--live`。
- 本版只保证"评测方法可复跑"，不保证"跑出来的结果一定支持某个结论"。

---

## 6. 常见问题

**Q：协同模式点了没反应 / 提示未开通？**
A：工程协同是专业版特性。确认已激活 `automind-pro`（应用内「设置 → ⭐ 升级专业版」或
许可证方式），激活后设置页会显示专业版 / 企业版。

**Q：多个角色好像没有并行？**
A：检查是否写范围重叠：重叠（或都没声明 `write_paths` 走 `__workspace__`）时会串行；
`multi_agent_max_parallel` 默认 2，独立路径才会真正并行。

**Q：协同结果比单 Agent 慢很多 / token 很多，正常吗？**
A：可能正常：协同要跑拆解 + 多角色 + 审查 + 验收 + 综合。先用第 5 节的基准在同模型、
同任务集上量化，再决定这类任务值不值得用协同。

**Q：验证显示"未执行"是什么意思？**
A：验收设施不可用（模型没配好 / 调用失败）—— 是"没测"，不是"通过"也不是"不通过"。
单 Agent 的 `_verify_state` 与协同的 `verification.llm.available` 同一口径。

**Q：角色改坏了文件怎么办？**
A：共享工作区意味着角色写的是真实文件。运行前建议 git 提交 / 用工作区隔离副本；
事后可用「↩️ 文件改动」撤销，或 `git diff` 检查。写锁只防并发互踩，不防写错内容。

**Q：预算耗尽后还能接着跑吗？**
A：能。角色预算耗尽 → 编排器把 plan / steps / scratch 挂到异常 → `run_multi` 写可续跑
检查点（`resumable=True`）。按界面「从断点续跑」即可；续跑会重新计一份任务预算。

---

## 7. 相关文档

- [EVAL.md](EVAL.md)：评测框架（套件 / 断言 / 报告 / CI 接入）
- [GOVERNANCE.md](GOVERNANCE.md)：审批、治理账本、分派级闸门
- [EDITIONS.md](EDITIONS.md)：社区版 / 专业版 / 企业版能力矩阵
- [ACCEPTANCE.md](ACCEPTANCE.md)：发布验收门禁（`scripts/acceptance.py`）
- [CHANGELOG.md](../CHANGELOG.md)：v1.11.22 工程协同变更明细
