# v1.11.25 多智能体真实复测报告（multi × 4 任务）

> **状态：已完成（2026-11-01，共 3 轮增强迭代 + 最终一致矩阵）。**
> 最终可对外引用矩阵：**PRO 4/4、ENTERPRISE 4/4**（均为单个 run 内完整 4 任务、
> 同一配置 30 步/300k，未拼接不同 run 的结果）。
> 本文所有数字来自 `.automind/multi_v11125/raw/<run_id>/collab_benchmark.json`
> 原始报告与基准侧独立复核；未测量的项明确标注，不编造。

## 0. 复测轮次与 run 清单

| 轮次 | 角色预算 | edition / 许可证来源 | run_id | 范围 | success |
|---|---|---|---|---|---|
| R1 | 12 步 / 120k | pro / 临时 PRO | `20261101-073643-pro` | 4 任务 × multi | 3/4 |
| R1 | 12 步 / 120k | enterprise / 本机真实 | `20261101-074435-real` | 4 任务 × multi | 3/4 |
| R2 | 20 步 / 200k | pro / 临时 PRO | `20261101-075645-pro` | 4 任务 × multi | 3/4 |
| R2 | 20 步 / 200k | enterprise / 本机真实 | `20261101-080559-real` | 4 任务 × multi | 3/4 |
| R3a | 30 步 / 300k | pro / 临时 PRO | `20261101-081939-pro` | 2 项（codegen+multi_file）× multi | 2/2 |
| R3a | 30 步 / 300k | enterprise / 本机真实 | `20261101-082717-real` | 2 项（codegen+multi_file）× multi | 2/2 |
| R3b | 30 步 / 300k | pro / 临时 PRO | `20261101-083304-pro` | 4 任务 × multi（额外样本） | 3/4 |
| **Final** | **30 步 / 300k** | **pro / 临时 PRO** | **`20261101-0830-final-pro`** | **4 任务 × multi（一致矩阵）** | **4/4** |
| **Final** | **30 步 / 300k** | **enterprise / 本机真实** | **`20261101-0840-final-real`** | **4 任务 × multi（一致矩阵）** | **4/4** |

- 任务：`analysis_summary` / `codegen_fizzbuzz` / `bugfix_calc` / `multi_file_pkg`；
  模式 `multi`；模型 `deepseek/deepseek-flash`（真实 API）；每轮命令：
  `python .automind/multi_v11125/run_recheck.py --live [...]`（默认 dry-run）。
- Final 两个一致矩阵由 Lead 并行使用同一编排脚本/同一配置执行（run_id 指定
  `final-*`）；其余轮次由本复测执行。两版配置相同（仅 edition/许可证不同），
  仅作对照，不混算成功率。
- 隔离：`AUTOMIND_DATA_DIR`/`HOME`/work 均在 `.automind/multi_v11125/<run_id>/`；
  不使用 8765；真实 `~/.automind` 仅只读许可证文件（内容不写报告/不打印）。
- 代码：core 1.11.25 / pro 1.11.25（仓库 HEAD `62eaf36`，工作区 dirty=True）。

## 1. 最终一致矩阵（30 步 / 300k）

### 1.1 PRO（临时 PRO 许可证；edition=pro；run_id `20261101-0830-final-pro`）

| 任务 | mode | status | success | artifacts_ok | verification_passed | stop_reason | 耗时(ms) | tokens | llm_calls | steps |
|---|---|---|---|---|---|---|---|---|---|---|
| analysis_summary | multi | ok | ✅ true | —（纯文本） | ✅ true | completed | 66952 | 33210 | 8 | 4 |
| codegen_fizzbuzz | multi | ok | ✅ true | true | ✅ true | completed | 210780 | 343633 | 48 | 4 |
| bugfix_calc | multi | ok | ✅ true | true | ✅ true | completed | 102563 | 99155 | 19 | 3 |
| multi_file_pkg | multi | ok | ✅ true | true | ✅ true | completed | 131984 | 172083 | 22 | 3 |

汇总：**success 4/4（100%）**；总 Token 648,081；LLM 调用 97；步数 14；
verification 已测 4/4 通过；artifacts 已测 3/3 通过；rework 1；墙钟 512.69s（8.5min）。
日志中**无任何角色预算/步数超限事件**。

### 1.2 ENTERPRISE（本机真实许可证；edition=enterprise；run_id `20261101-0840-final-real`）

| 任务 | mode | status | success | artifacts_ok | verification_passed | stop_reason | 耗时(ms) | tokens | llm_calls | steps |
|---|---|---|---|---|---|---|---|---|---|---|
| analysis_summary | multi | ok | ✅ true | —（纯文本） | ✅ true | completed | 206078 | 78241 | 12 | 3 |
| codegen_fizzbuzz | multi | ok | ✅ true | true | ✅ true | completed | 186250 | 378034 | 44 | 3 |
| bugfix_calc | multi | ok | ✅ true | true | ✅ true | completed | 101734 | 135182 | 25 | 3 |
| multi_file_pkg | multi | ok | ✅ true | true | ✅ true | completed | 263359 | 477635 | 52 | 4 |

汇总：**success 4/4（100%）**；总 Token 1,069,092；LLM 调用 133；步数 13；
verification 已测 4/4 通过；artifacts 已测 3/3 通过；rework 3；墙钟 757.94s（12.6min）。
日志中**无任何角色预算/步数超限事件**。

### 1.3 基准侧独立复核（不是 Agent 自述）

对最终矩阵的 codegen 工作区产物直接跑测试（由本复测执行）：

| 版本 | 独立 pytest |
|---|---|
| Final PRO | 8 passed |
| Final ENTERPRISE | 6 passed |

## 2. 回答核心问题：codegen 是否 4/4？

- **在最终两版完整矩阵中，codegen 均通过：PRO ✅ + ENTERPRISE ✅（2/2）**，
  且 artifacts_ok=true、verification_passed=true，无预算/步数超限事件。
- 最新配置（30 步/300k）下的全部样本里，codegen 共 **4/5 通过**：
  R3a PRO ✅、R3a ENT ✅、Final PRO ✅、Final ENT ✅；
  R3b（额外一次完整 PRO 样本）❌：coder 角色 Token 预算 **305,845/300,000** 超限
  （该次产物也落盘且独立测试 7 passed，但未进入验收）。
- 结论：**codegen 在最终矩阵中已通过，但 300k 角色预算余量仍偏紧**——
  同一配置下存在单次样本超限（305,845 ≈ 102% 预算），说明预算治理对
  "角色计划规模波动"仍不够稳。若要对外宣称稳定 4/4，建议留更多余量
  （如 400k）或把"交付回收"窄口径扩展到 `budget_exhausted`。

## 3. 迭代修复效果（失败如何被逐轮消除）

| 现象（轮次） | 原始证据 | 根因/归类 | 后续结果 |
|---|---|---|---|
| R1 pro codegen 失败 | 角色 s3 Token 预算 127,534/120,000 耗尽；artifacts_ok=true；独立测试 9 passed | 产品 bug（角色预算过小 + 预算耗尽直接判失败） | R2 提到 20/200k 后 codegen PRO/ENT 均通过 |
| R1 ent codegen 失败 | s2(coder) max_iterations → role_failed；artifacts=true、verification=true；独立测试 6 passed | 产品 bug（角色失败传播与任务级验收冲突） | 提步数 + 交付回收规则后 R2 codegen 通过 |
| R2 pro/ent multi_file 失败 | 角色预算 211,562 / 211,962 对 200,000 超限；artifacts=true；独立测试 6/9 passed | 产品 bug（角色预算仍不足） | R3a 提到 30/300k 后 2 项复验 PRO/ENT 2/2 通过；Final 4/4 |
| R3b pro codegen 失败（额外样本） | 角色 s2 预算 305,845/300,000 超限；artifacts=true；独立测试 7 passed | 产品风险（预算余量偏紧/波动） | Final 两版 4/4 通过；建议加大余量或泛化回收规则 |

两轮增强（默认角色预算 12/120k → 20/200k → 30/300k；max_iterations 但
write_paths 全落盘且 verification 完成的窄口径回收）**均被真实运行验证有效**：
早期 codegen / multi_file 的确定性失败在后续轮次消失，最终两版均 4/4。

## 4. 残余风险与产品发现（不影响最终 4/4 数字，但应记录）

### 4.1 角色预算余量偏紧（R3b 证据，建议 v1.11.26 处理）

- 同一配置下 R3b 的 codegen coder 角色用掉 305,845 tokens（预算 300,000）→
  `budget_exhausted`；而 Final 两版同任务未触发。说明角色计划/步数存在波动，
  当前 300k 余量不足以完全稳定。
- 建议：① 角色 Token 预算提供自动续一窗（类似任务预算的 window roll）或
  默认 400k；② 把 `_recover_delivered_after_limit` 的窄口径从
  "max_iterations" 扩展到 `budget_exhausted`（判据同样是 write_paths 全部
  真实落盘 + verification 完成），避免"交付物已可用却整任务判失败"。

### 4.2 指标聚合疑似少记（成本/用量报告失真）

预算耗尽路径上，行级 tokens/calls 与角色实际用量严重不一致：
- R2 PRO multi_file：行 tokens=3,045 / calls=1，角色实际 211,562；
- R2 ENT multi_file：行 tokens=25,730 / calls=7，角色实际 211,962；
- R3b PRO codegen：行 tokens=3,760 / calls=3，角色实际 305,845。
（成功路径的行 tokens 看起来包含角色用量，例如 Final PRO codegen 行 343,633。）
建议 owner 核对 multi 行指标聚合逻辑：角色子任务的 token/calls 是否只在部分路径
汇总，否则对外成本/效率表会系统性偏低。

### 4.3 Windows 下 multi 检查点保存失败（贯穿全部轮次，未修复）

- 6 次运行共观察到 **32 次** `run_mode_checkpoint_failed`（每次每个任务 1 次）：
  `[Errno 22] Invalid argument: '.automind\\checkpoints\\ckpt_bench:<task>:multi_<…>.json.tmp'`
- 根因：multi 的 session_id 形如 `bench:<task>:multi` 被拼进 checkpoint 文件名，
  Windows 文件名不允许 `:` → **multi 断点续跑在 Windows 上不可用**。
- 叠加隔离问题：`checkpoint_dir` 默认是相对 cwd 的 `.automind/checkpoints`，
  不受 `AUTOMIND_DATA_DIR` 隔离；本轮在仓库 `.automind/checkpoints` 观察到
  4 个 hex-session 检查点文件（mtime 与本轮运行窗口重合，疑似多智能体子会话），
  `traces/` 也在窗口内有写入；因 Lead 在并行跑测试，无法 100% 归因到本复测。
  建议：checkpoint/轨迹目录跟随数据目录或工作区配置，session_id 做文件名清洗。
- 判定：产品 bug（Windows 兼容 + 隔离/续跑可靠性），不影响本次成功率，但影响
  "断点续跑"承诺。

### 4.4 观测噪音（非成败主因）

- 平台敏感文件守卫多次拒绝 agent 对平台数据文件的读/写（守卫按设计工作）；
- 少量 Unix 命令（`ls`/`tail`）在 Windows 失败、路径不存在：模型侧行为；
- 偶发审阅者未按 JSON 作答（`autopilot_review_unavailable`）：闭环降级但不改判定。

## 5. 结论

1. **最终一致矩阵：PRO 4/4、ENTERPRISE 4/4**（单 run、同配置 30 步/300k、
   两版 codegen/multi_file 均通过，verification 全部通过；未拼接跨 run 结果）。
2. 三轮预算/回收增强确实消除了此前 codegen、multi_file 的确定性失败；但最新
   配置仍存在一次 codegen 角色预算超限样本（R3b 305,845/300,000），说明
   **余量偏紧、存在波动**，建议加预算余量或泛化交付回收到 budget_exhausted。
3. 仍需处理：multi 行指标少记（成本报告）、Windows 检查点文件名/目录隔离
   （续跑可靠性）。
4. 不把本次 4/4 解读为"多智能体完全稳定"：每版只跑了 1 次完整矩阵，且
   4/5 的 codegen 样本说明预算敏感度仍在。

## 6. 局限与声明

- Final 每版仅 1 次完整 4 任务运行；模型/计划随机性与网络波动会影响结果；
- PRO 用临时 PRO 许可证、ENTERPRISE 用本机真实许可证；两版代码相同、edition
  不同，仅作对照，不混算成功率；许可证内容未写入任何报告；
- 未执行 single/subagent 对照；Token 为评测框架统计（存在 4.2 所述少记风险）；
  不估算金额；
- 原始 JSON 的 `notes` 含本机隔离工作区绝对路径，仅作内部证据；raw/meta 已检查
  不含 Key/许可证全文（日志/元数据写入前脱敏）。

## 7. 附录：原始证据（均在 `.automind/multi_v11125/` 下）

| 轮次 | 原始报告目录 |
|---|---|
| Final PRO | `raw/20261101-0830-final-pro/`（json/csv/run.log/failures.json/analysis.md） |
| Final ENT | `raw/20261101-0840-final-real/`（同上） |
| R3b 额外样本 | `raw/20261101-083304-pro/` |
| R3a 2 项复验 | `raw/20261101-081939-pro/`、`raw/20261101-082717-real/` |
| R2 | `raw/20261101-075645-pro/`、`raw/20261101-080559-real/` |
| R1 | `raw/20261101-073643-pro/`、`raw/20261101-074435-real/` |
| 元数据 | `meta/*.json`（含 edition/许可证来源类型/统计/命令）；`meta/series-*.json`（阶段决策） |
| 编排脚本/隔离方案 | `.automind/multi_v11125/run_recheck.py`、`README.md` |
