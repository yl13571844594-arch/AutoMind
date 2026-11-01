# v1.11.24 基准原始数据 · 字段说明与脱敏规则

> **状态：已放入脱敏数据（27 个数据文件 + SHA256SUMS，共 568,215 B）** ｜ 版本：v1.11.24 ｜ 归档：2026-10-31
> 主报告：[`../../BENCHMARK_v1.11.24.md`](../../BENCHMARK_v1.11.24.md) ｜
> 官网上线文案：[`../../BENCHMARK_WEBSITE.md`](../../BENCHMARK_WEBSITE.md)
>
> 本目录存放**经过脱敏、可对外发布**的 v1.11.24b 测评数据副本（run-id `v1.11.24b`，82/82 cell 完整）。
> **未脱敏的原始数据默认不进入版本库**（在 `.automind/edition_benchmark/<run-id>/`，已被 `.gitignore` 忽略）。
> 数据已通过完整性校验（`summary.json` → `validation.ok=true`）并完成 Key / 许可证 / 绝对路径脱敏；
> 文件完整性可用 `SHA256SUMS.txt` 复核。

## 1. 目录布局

一次 `scripts/edition_benchmark.py --live` 运行产出的完整结构（`<run>` 即输出目录，默认
`.automind/edition_benchmark/<run-id>`）：

| 路径 | 内容 | 本目录状态 |
| --- | --- | --- |
| `manifest.json` | 编排元数据：run schema、dry_run、核心版本、provider/model、Key 来源标签、套件/重复/协作模式、每版本隔离信息与状态、warnings/notes | ✅ 已放入（脱敏） |
| `plan.json` | dry-run 计划矩阵（版本/端口/隔离路径/期望 cell 数/依赖检查），不含 Key | ✅ 已放入（脱敏） |
| `isolation_matrix.json` | 三版本隔离探测矩阵：edition 观测值、27 项商业 feature 可用性、Pro 扩展包安装/兼容、许可证步骤、`force_community` | ✅ 已放入（`<TMP>` 等脱敏） |
| `environment.json` | 运行环境快照：OS / CPU / 内存 / Python / 模型与参数（temperature 0.7、max_tokens 8192、approve_all）/ run 窗口；`network` 明确记为未测量 | ✅ 已放入（无敏感信息） |
| `raw/<edition>/eval_rep1.json` … `eval_rep3.json` | 单版本每轮评测的**原始报告**：含 `output_preview`、工具参数、workspace 路径 —— 可能含敏感内容 | ✅ 已放入（路径/HOME 已脱敏，见 §4） |
| `raw/<edition>/eval_dryrun.json` | 评测端点 dry-run（target / cases 列表），无模型调用 | ✅ 已放入（脱敏） |
| `raw/<edition>/collab_benchmark.json` | 协作矩阵原始结果（`--live` 真实指标），可能含 error/notes 路径 | ✅ 已放入（脱敏） |
| `raw/<edition>/collab_benchmark.csv` | 协作矩阵的 CSV 形式（脚本直出） | ✅ 已放入（脱敏） |
| `report/summary.json` | `benchmark_report.py` 校验+聚合后的**机器可读汇总**（已自动脱敏） | ✅ 已放入 |
| `report/eval_matrix.csv` | 评测逐 cell 矩阵（已自动脱敏） | ✅ 已放入 |
| `report/collab_matrix.csv` | 协作逐 cell 矩阵（已自动脱敏） | ✅ 已放入 |
| `report/report.md` | 人读 Markdown 报告（已自动脱敏） | ✅ 已放入 |
| `report/validation.json` | **最终成功校验快照**：`ok=true`、无 error/warning，明确记录 `checked_eval_cells=54`、`checked_collab_cells=28`、`total_cells=82`（与 `summary.json` → `validation` 一致） | ✅ 已放入 |
| `SHA256SUMS.txt` | 本目录全部数据文件的 SHA-256 清单 | ✅ 已生成 |
| `run/<edition>/{home,data,project,logs}` | 隔离运行目录与服务日志（默认运行结束删除；`--keep-run-dirs` 保留） | ❌ 未放入（含真实路径、日志、数据库） |

## 2. 原始数据字段说明

### 2.1 `manifest.json`（编排元数据）

| 字段 | 说明 |
| --- | --- |
| `schema` | 原始数据格式版本（当前 `automind.edition_benchmark/1`） |
| `generated_at` / `finished_at` | 运行开始/结束时间（UTC ISO8601） |
| `dry_run` | `false` 表示真实运行；`true` 只是计划 |
| `core_version` | 被测核心版本 |
| `provider` / `model` | 模型配置（实际生效值） |
| `api_key_source` | **Key 来源标签**（env / config / 显式传入），不含 Key 本身 |
| `eval_suite` | `{name, repeats, scoring_tasks[], probe_cases[], probe_rule}` |
| `collab` | `{tasks[], modes_by_edition{community:[single], pro:[...], enterprise:[...]}}` |
| `editions.<name>` | 每版本记录：`status`/`errors`/`isolation{home,data_dir,project,logs,force_community,pro_wheel{file,version,core_version,version_match},license{kind,tier,expiry,storage}}` 等（路径是 `<RUN>/...` 相对占位） |
| `warnings` / `notes` | 依赖警告、口径说明 |

### 2.1b `isolation_matrix.json`（三版本隔离探测，2026-10-31T15:03:49Z）

| 字段 | 说明 |
| --- | --- |
| `schema` | 隔离探测格式版本（`automind.edition_isolation/1`） |
| `generated_at` | 探测时间（UTC ISO8601） |
| `rows[]` | 每个版本一行 |
| `rows[].edition` / `expected` / `observed` | 被测版本 / 期望档位 / 实际 `get_edition()` 观测值（本版依次为 community / pro / enterprise，与预期一致） |
| `rows[].features` | 27 项商业 feature → 是否可用（bool）；community 全 false、pro 19 true、enterprise 全 true |
| `rows[].package` | Pro 扩展包：`installed` / `version`（1.11.24）/ `api_version`（1）/ `compatible` / `core_version` / `version_match`；`path` 已脱敏为 `<TMP>/...` |
| `rows[].license_step` | 许可证步骤：community=`need_package`；pro/enterprise=`active` |
| `rows[].force_community` | 是否强制社区版（仅 community=true） |
| `rows[].version_match` | 扩展包与核心版本是否一致（pro/enterprise=true） |

### 2.1c `environment.json`（运行环境快照）

| 字段 | 说明 |
| --- | --- |
| `schema` | 格式版本（`automind.benchmark_environment/1`） |
| `generated_at` | 快照生成时间（UTC+08:00） |
| `run_id` | 对应运行目录（`v1.11.24b`） |
| `os` | `name` / `version` / `build` / `architecture`（Windows 10 专业版 10.0.19045 Build 19045 / x64） |
| `cpu` | `model` / `physical_cores` / `logical_processors`（Intel Core i3-14100F / 4 / 8） |
| `memory_gb` | 物理内存（GB） |
| `python` | 运行解释器版本 |
| `model` | `provider` / `model` / `temperature` / `max_tokens` / `approval_mode`（模型实际生效配置） |
| `run` | `started_at` / `finished_at`（Asia/Shanghai）/ `timezone` / `note` |
| `network` | `measured=false` + `label="未测量"` + `note`：**本轮未测量网络指标，不宣称网络条件**；不补数字 |
| `notes` | 口径说明 |

### 2.2 `eval_repN.json`（单轮评测原始报告）

顶层字段（来自 `automind/eval/report.py` 的 `EvalReport`）：

| 字段 | 说明 |
| --- | --- |
| `suite` / `path` | 套件名（`comprehensive`）与套件路径（脱敏前为真实路径，需处理） |
| `model` / `provider` | 实际模型配置 |
| `started_at` / `finished_at` / `total_seconds` | 时间戳与总耗时（秒） |
| `total` / `passed` / `failed` / `errors` / `pass_rate` | 用例总数与通过/失败/错误统计（含探针；报告聚合时会排除探针） |
| `aborted` / `abort_reason` | 整体中止标记与原因（如无 Key） |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | 本次评测累计 token |
| `estimated_cost_usd` / `pricing_as_of` | 估算成本与价格表日期（**估算值，非账单**） |
| `notes` | 评测口径提示（如审批模式） |
| `cases[]` | 7 个 case（6 计分 + 1 探针） |

`cases[]` 每条 case 的字段：

| 字段 | 说明 |
| --- | --- |
| `id` / `mode` | 任务 id 与交互模式 |
| `status` | `passed` / `failed` / `error`（`error` 多为框架/环境问题） |
| `seconds` | 该任务耗时 |
| `prompt_tokens` / `completion_tokens` / `total_tokens` / `estimated_cost_usd` | 该任务用量与估算成本 |
| `tool_calls[]` | 工具调用记录：`name` / `arguments` / `success` / `seconds`（**arguments 可能含文件路径/命令，需脱敏**） |
| `assertions[]` | 断言明细：`type` / `passed` / `expected` / `actual` / `detail`（**actual/detail 可能含模型输出片段**） |
| `failed_assertions[]` | 失败断言列表 |
| `output_preview` | 模型输出预览（**可能含工作区文件内容，必须脱敏或清空**） |
| `error` | 任务级错误（可能含路径/堆栈） |
| `timed_out` | 是否超时 |
| `workspace` | 任务临时工作区路径（**必须替换为占位符**） |
| `expect_fail` | 是否为反向探针（`true` 不计分；必须判 `passed`，即"老实地失败") |

### 2.3 `collab_benchmark.json`（协作矩阵原始结果）

由 `scripts/collab_benchmark.py` 输出，主要字段：

| 字段 | 说明 |
| --- | --- |
| `schema` / `dry_run` / `provider` / `model` | 版本与配置 |
| `tasks[]` / `matrix[]` / `summary` | 固定任务集、任务×模式矩阵、聚合汇总 |
| `results[]` | 每次 (任务, 模式) 的结果行 |

`results[]` 行字段：`task_id` / `category` / `mode` / `status`（ok/unavailable/error）/
`success` / `artifacts_ok` / `duration_ms` / `tokens` / `llm_calls` / `steps` /
`verification_passed` / `rework_count` / `human_interventions` / `stop_reason` / `error` / `notes`。
`notes` 可能含 workspace 相对路径；`error` 可能含路径/命令输出，需脱敏。

### 2.4 `report/summary.json`（聚合汇总，已自动脱敏）

| 字段 | 说明 |
| --- | --- |
| `schema` / `source_schema` / `generated_at` / `dry_run` | 报告元信息 |
| `model` / `provider` | 实际模型配置 |
| `scope` | 被测版本、套件、重复次数、计分任务、探针规则、协作模式矩阵 |
| `validation` | `{ok, errors[], warnings[]}`：**`ok=false` 时数据不可用，不得发布** |
| `eval` | 综合评测汇总：`suite` / `repeats` / `scoring_tasks` / `probe` / `editions.<name>{pass_rate, repeats_present, by_task, total_tokens, estimated_cost_usd, seconds, probe_statuses}` / `matrix[]` |
| `collab` | 协作汇总：`tasks` / `modes_by_edition` / `editions.<name>{modes, measured_cells, by_mode.<mode>{runs, ok_runs, success_rate, avg_duration_ms, tokens_total, llm_calls_total, verification_pass_rate}}` / `matrix[]` |
| `notes` | 口径说明（含"缺失 cell 直接判失败、不用 0 补齐"） |

### 2.5 CSV 列

| 文件 | 列 |
| --- | --- |
| `report/eval_matrix.csv` | `edition, repeat, task_id, status, passed, seconds, total_tokens, estimated_cost_usd` |
| `report/collab_matrix.csv` | `edition, task_id, mode, status, success, duration_ms, tokens, llm_calls, steps, verification_passed, stop_reason` |

## 3. 完整性口径（缺失即失败）

- **评测**：6 个计分任务 × 3 个版本 × 3 次重复 = **54** 个 cell（结构常量）；
  `assertion_probe` 是探针，不计分、不参与通过率，但每个版本必须 `passed`（即"老实地失败"），
  否则断言链路可疑、报告校验失败。
- **协作**：4 个任务 × 7 个 (版本, 模式) 组合 = **28** 个 cell（结构常量；community 只有 `single`）。
- **合计 82 个 cell**。任何缺失 / 重复 / 非法状态 → `benchmark_report.py` 退出码 `2`，
  写 `report/validation.json`，**不产出汇总**；不允许用 0 或默认值补齐。
- 本目录只放**完整通过校验**的数据（或保留 `validation.json` 说明该次不可用）。

## 4. 脱敏规则（发布前必须执行）

### 4.1 自动脱敏（`scripts/benchmark_report.py` 已内置）

| 类型 | 规则 | 替换为 |
| --- | --- | --- |
| 常见 API Key / Token | `sk-…`、`AKIA…`、`AIza…`、`gh[pousr]_…`、`xox[baprs]-…`、`Bearer …`、`api_key = …` | `[REDACTED_API_KEY]` / `[REDACTED_TOKEN]` |
| 许可证 | `AMP<digits>-…`（AMP2/AMP3 等） | `[REDACTED_LICENSE]` |
| 仓库根 / HOME / 临时目录 / 运行目录 | 精确路径前缀 | `<REPO>` / `<HOME>` / `<TMP>` / `<RUNn>` |
| 其它绝对路径 | Windows 盘符、UNC、`/home` `/Users` `/tmp` 等 POSIX 前缀 | `<ABS_PATH>` |

数字、任务 id、模型名不受影响；`summary.json` / `*.csv` / `report.md` 均为脱敏结果。

### 4.2 仍需人工检查的内容（自动脱敏覆盖不到）

- `output_preview`、`assertions[].expected/actual/detail`、`tool_calls[].arguments`、
  `error`、`notes` 可能包含：**工作区文件内容、用户数据、主机名/用户名、命令文本、内部路径**；
- 服务日志（`run/<edition>/logs/`）与运行目录**一律不发布**；
- 发布前对每个候选文件执行关键词抽检（脱敏占位符之外的敏感信息）：

```powershell
# 抽检：Key / 许可证 / 盘符路径 / 用户目录 / 主机名（按实际环境补充关键词）
Select-String -Path .\*.json,.\*.csv,.\*.md -Pattern 'sk-|AMP[0-9]|API_KEY|Bearer|[A-Za-z]:\\|\\\\|/home/|/Users/|''WORKGROUP|localhost:[0-9]+' |
    Select-Object Path, LineNumber
```

- 确认后再放入本目录；无法确认的文件**宁可不放**。

### 4.3 绝对不可发布 / 上传 / 入库

- API Key、许可证全文与私钥、机器指纹；
- 用户主目录 / 真实项目路径 / 主机名 / 用户名 / 内网地址；
- 模型输出中可能包含的用户文件内容（含 `output_preview` 原文）；
- `run/`、`logs/`、数据库、检查点等运行现场文件。

### 4.4 推荐操作流程

1. 先跑 `python scripts/benchmark_report.py --run <run-dir>` 生成脱敏聚合；
2. 只取 `report/` 下的 `summary.json` / `eval_matrix.csv` / `collab_matrix.csv` / `report.md`
   （如需附带脱敏后的 `manifest.json` / `plan.json` / `eval_rep*.json`，先按 §4.2 人工处理）；
3. 跑 §4.2 的抽检命令并人工复核；
4. 放到 `docs/benchmark/v1.11.24/`，在本 README §6 记录数据状态、脱敏执行人与日期；
5. 发布文案数字必须与 `summary.json` 完全一致（见 `../../BENCHMARK_WEBSITE.md` checklist）。

> 修改原始数据来"修正"结果是不允许的：数字只能来自真实运行；重跑请用 `--overwrite` 生成新目录。

### 4.5 本次归档的脱敏执行记录（v1.11.24b）

- 自动脱敏：对全部 JSON 递归替换 Key / 许可证 / Windows 绝对路径 / POSIX 前缀；
  `isolation_matrix.json` 中的临时扩展路径替换为 `<TMP>/...`；`raw/` 中 `workspace`、
  `notes`、`tool_calls.arguments`、`assertions.actual` 等字符串统一处理。
- 人工复核：对归档目录递归扫描 `sk-` / `AMP<digits>-` / 盘符路径 / `/Users` `/home` `/tmp` /
  `Administrator` / `AppData` / `automind_iso_` 等模式，**0 命中**（`README.md` 中作为规则示例
  出现的路径前缀除外，非真实路径）。
- 完整性：`SHA256SUMS.txt` 覆盖 27 个数据文件，可用 `sha256sum -c` / `Get-FileHash` 复核。
- `run/`、`logs/`、数据库/检查点**未归档**。

## 5. 复现与重算

```powershell
# 1) 校验 + 聚合（只读原始报告，不跑模型）—— 本轮数据目录：v1.11.24b
python scripts/benchmark_report.py --run .automind\edition_benchmark\v1.11.24b

# 2) 或由编排器转调（同样只聚合）
python scripts/edition_benchmark.py --report-only --run .automind\edition_benchmark\v1.11.24b

# 3) 重新真实运行（消耗 Token；先确认端口/Key/staging wheel；用新目录，避免污染已有数据）
python scripts/edition_benchmark.py --live --out .automind\edition_benchmark\<新run-id>
```

完整性不通过时（退出码 2），查看 `<run>/report/validation.json` 的缺失清单，
**不要手工补 cell，也不要发布**；修正运行环境后整轮重跑。

## 6. 数据状态

| 项 | 值 |
| --- | --- |
| 数据状态 | 已放入，脱敏完成 |
| 运行目录 ID | v1.11.24b |
| 实际完成 cell 数 | 82 / 82（54 评测 + 28 协作） |
| 数据完整性 | ok=true（无 error/warning；`report/validation.json`：54 eval + 28 collab = 82 cell；与 `report/summary.json` → `validation` 一致） |
| 运行环境快照 | `environment.json`：Windows 10 19045 / i3-14100F 4C8T / 63.8 GB / Python 3.12.10 / temp 0.7 / max_tokens 8192 / approve_all；`network` 未测量 |
| 本目录数据文件数 / 总大小 | 27 个 / 568,215 B（不含本 README 与 `SHA256SUMS.txt`） |
| 脱敏执行人 / 日期 | docs-version（脚本脱敏 + 人工复核）/ 2026-10-31 |
| 对应主报告版本 | `automind.edition_benchmark_report/1` → 主报告 `docs/BENCHMARK_v1.11.24.md` |

## 7. 变更记录

| 日期 | 变更 | 负责人 |
| --- | --- | --- |
| 2026-10-31 | 建立骨架：目录布局 / 字段说明 / 完整性口径 / 脱敏规则 / 复现命令 / 数据状态占位 | docs-version |
| 2026-10-31 | 放入 v1.11.24b 脱敏数据（含 SHA256SUMS），补隔离矩阵字段说明与数据状态 | docs-version |
| 2026-10-31 | 补充 `environment.json`（OS/CPU/内存/Python/模型参数/run 窗口，network 未测量）与最终成功校验 `report/validation.json`（ok=true，54+28=82），SHA256SUMS 重算为 27 个数据文件（568,215 B） | docs-version |
