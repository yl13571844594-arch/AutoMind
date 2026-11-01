# AutoMind v1.11.24 基准测评 · 官网上线文案

> **状态：已回填（基于 v1.11.24b 真实运行，2026-10-31）** ｜ 版本：v1.11.24
> 主报告：[`BENCHMARK_v1.11.24.md`](BENCHMARK_v1.11.24.md) ｜ 数据副本：[`docs/benchmark/v1.11.24/`](benchmark/v1.11.24/README.md)
> ｜ 复跑说明：主报告 §11
>
> 本文档是**可直接粘贴到官网 / Release note / 公众号**的精简文案（数字均来自真实运行）。
> 发布前请走 §5 checklist；**必须保留失败案例与"不构成统计显著结论"的免责段落**。

## 1. 中文主文案（可直接粘贴）

### 1.1 标题与导语

**AutoMind v1.11.24：真实能力，用同一套任务测出来**

6 个工程任务 × 社区版 / 专业版 / 企业版三个隔离环境 × 3 轮重复 —— 方法公开、脚本可复跑、
原始数据可核对。我们不只展示"演示效果"，也公开**通过了什么、没通过什么、花了多少**。

### 1.2 为什么要做真实能力测评

演示视频证明不了工程能力。AutoMind 从 v1.11.24 起提供一套**可复跑的核心综合测评**：
固定任务、固定断言、固定隔离环境，每次运行都产出可核对的原始数据。
结果不靠形容词，而靠**通过率、耗时、Token 与估算成本**。

### 1.3 怎么测（方法摘要）

- **6 个计分任务 + 1 个反向探针**：需求分析、修 bug、多文件 Python 包、数据汇总（真实数值断言）、
  文档产出、诚实与安全；反向探针故意不可能满足，用来证明断言链路真的在工作（不计分，三版本 3/3 passed）。
- **三个版本真实隔离**：社区版（强制社区模式、无扩展）/ 专业版 / 企业版，各自独立数据目录、
  HOME、项目目录与端口；商业版使用 staging 扩展包 + 临时许可证，测完即清理。
  隔离探测实测（`isolation_matrix.json`，27 项商业 feature × 3 版本）：
  **社区 0/27 可用**、**专业 19/27**（8 项企业版专属功能关闭）、**企业 27/27**；
  专业/企业版扩展包均正确安装（1.11.24，api_version=1）且许可证 `active`，
  社区版 `force_community=true`、`license_step=need_package` —— 证明三版本不是共用同一份运行时。
- **同条件对比**：同一模型与参数（deepseek/deepseek-flash，实际 temperature 0.7）、同一套件、
  每版 **3 轮**、单任务 300 秒上限、统一审批模式（approve_all）。
- **完整性即正确性**：54 个评测 cell + 28 个协作 cell 一个不能少；实测 **82/82 完整**、
  `validation.ok=true`；缺失直接判失败，不用 0 补齐；报告对 API Key、许可证、绝对路径脱敏。

### 1.4 结果摘要（实测，2026-10-31）

| 版本 | 综合通过率 | 3 轮总耗时 | Token | 估算成本 | 协作模式可用性 |
| --- | --- | --- | --- | --- | --- |
| 社区版（Community） | 100.0%（18/18） | 331.38 s | 576,476 | 0.391097（估算） | 单 Agent（`single`） |
| 专业版（Pro） | 100.0%（18/18） | 358.30 s | 608,693 | 0.410784（估算） | `single` / `multi` / `subagent` |
| 企业版（Enterprise） | 100.0%（18/18） | 265.59 s | 545,589 | 0.354959（估算） | `single` / `multi` / `subagent` |

**协作矩阵实测（如实披露失败，不删减）**：

| 版本 | 模式 | 成功率 | 平均耗时 | Token | 说明 |
| --- | --- | --- | --- | --- | --- |
| community | single | 25.0%（1/4） | 41,495.75 ms | 248,455 | 其余 3 次产物已生成但任务级成功判定未通过 |
| pro | single | 25.0%（1/4） | 50,027.25 ms | 286,067 | — |
| pro | multi | **0.0%（0/4）** | 138,839.75 ms | 745,177 | 4 次均 `verification_failed` |
| pro | subagent | 100.0%（4/4） | 26,518.50 ms | 192,157 | — |
| enterprise | single | 75.0%（3/4） | 35,589.25 ms | 246,259 | — |
| enterprise | multi | 25.0%（1/4） | 112,276.50 ms | 627,868 | 3 次失败，其中 1 次产物缺失 |
| enterprise | subagent | 100.0%（4/4） | 37,808.00 ms | 309,380 | — |

> 测试模型：deepseek / deepseek-flash（实际 temperature 0.7，**未固定为 0**）｜ 测试日期：2026-10-31
> ｜ 环境：Microsoft Windows 10 专业版 19045 / Python 3.12.10 / Intel Core i3-14100F（4C8T）/ 63.8 GB
> ｜ 网络：未测量（不宣称网络条件） ｜ 数据完整性：ok=true（82/82 cell 完整）
> ｜ 核心版本：1.11.24 ｜ 审批：approve_all

**数字怎么读（请原样保留这段）**：

- 通过率 = 该版本在 6 个计分任务 × 3 轮中通过的 cell 占比；反向探针不计分（三版本 3/3 passed）；
- 耗时、Token 为 3 轮合计；**估算成本**按内置价格表估算（价格表日期 2025-01），**不是账单**；
- 协作矩阵**每个 task × mode 仅 1 次**（n=1），**不构成统计显著结论**，也不代表真实用户任务分布；
- 社区版没有 `multi` / `subagent` 是**产品设计（未授权）**，不是失败；
- 本轮 multi 模式出现真实失败（pro 0/4、enterprise 1/4），我们**原样公开**；失败原因见主报告 §9。

### 1.5 我们同时公开的边界

- 结果只对**该模型、该环境、该日期**成立；换模型/环境需重跑，不能平移；
- **协作矩阵 n=1、不构成统计显著结论** —— 我们不会用一次协作结果宣称"多智能体提升 X%"；
  本轮 multi 的失败（pro 0/4、enterprise 1/4）与 subagent 的 4/4 都原样列出；
- 成本是估算值，不是账单；测评本身不评估安全性（安全审计见 `docs/AUDIT_v1.11.23.md`）；
- 网络本轮**未测量**（不宣称网络条件）；其余环境（OS / Python / CPU / 内存 / 模型参数 / run 窗口）
  已记录在 `docs/benchmark/v1.11.24/environment.json`；
- 专业版 / 企业版能力需单独的 `automind-pro` 扩展包与有效许可证，**商业版不随社区版分发**。

### 1.6 底部说明（发布时保留）

> 完整方法、全部失败案例与限制见 [v1.11.24 基准报告](BENCHMARK_v1.11.24.md)；
> 复跑命令见报告 §11；隔离矩阵、原始/聚合数据（已脱敏）与字段说明见
> [docs/benchmark/v1.11.24/](benchmark/v1.11.24/README.md)。
> 所有数字均来自 v1.11.24b 真实运行，未使用估算或补值。

## 2. 英文版文案（可选，可整段粘贴）

**AutoMind v1.11.24 — Real capability, measured on one fixed test set**

Six engineering tasks × three isolated editions (Community / Pro / Enterprise) × three repeats —
open methodology, reproducible scripts, checkable raw data. We publish not just the demo,
but **what passed, what failed, and what it cost**.

- **6 scored tasks + 1 probe**: requirements analysis, bug fix, multi-file package, data aggregation
  (real-number assertions), document output, honesty & safety; a deliberately unsatisfiable probe
  proves the assertion chain actually runs (not scored).
- **Three truly isolated editions**: separate data dir / HOME / project / ports; Community runs forced
  community mode with no extensions; Pro/Enterprise use a staging extension wheel + temporary license,
  cleaned up afterwards. The isolation probe measured **27 commercial features × 3 editions**:
  **Community 0/27**, **Pro 19/27** (8 Enterprise-only features off), **Enterprise 27/27**; Pro/Enterprise
  packages installed correctly (1.11.24, api_version=1) with `license_step=active`, Community
  `force_community=true` — proof the three editions were genuinely isolated, not one shared runtime.
- **Same conditions**: one model and parameters (deepseek/deepseek-flash, effective temperature 0.7),
  3 repeats per edition, 300 s per task, identical approval mode (`approve_all`). Any missing cell fails
  the report — measured **82/82 complete** with `validation.ok=true`; no zero-filling; keys, licenses and
  absolute paths are redacted.

| Edition | Pass rate | 3-repeat duration | Tokens | Est. cost | Collaboration availability |
| --- | --- | --- | --- | --- | --- |
| Community | 100.0% (18/18) | 331.38 s | 576,476 | 0.391097 (est.) | single agent (`single`) |
| Pro | 100.0% (18/18) | 358.30 s | 608,693 | 0.410784 (est.) | `single` / `multi` / `subagent` |
| Enterprise | 100.0% (18/18) | 265.59 s | 545,589 | 0.354959 (est.) | `single` / `multi` / `subagent` |

**Collaboration matrix, measured (failures disclosed, not removed):**

| Edition | Mode | Success rate | Avg duration | Tokens | Note |
| --- | --- | --- | --- | --- | --- |
| community | single | 25.0% (1/4) | 41,495.75 ms | 248,455 | 3 tasks produced artifacts but failed task-level success |
| pro | single | 25.0% (1/4) | 50,027.25 ms | 286,067 | — |
| pro | multi | **0.0% (0/4)** | 138,839.75 ms | 745,177 | all 4 `verification_failed` |
| pro | subagent | 100.0% (4/4) | 26,518.50 ms | 192,157 | — |
| enterprise | single | 75.0% (3/4) | 35,589.25 ms | 246,259 | — |
| enterprise | multi | 25.0% (1/4) | 112,276.50 ms | 627,868 | 3 failures, 1 with missing artifacts |
| enterprise | subagent | 100.0% (4/4) | 37,808.00 ms | 309,380 | — |

> Model: deepseek / deepseek-flash (effective temperature 0.7, **not pinned to 0**) · Date: 2026-10-31
> · Environment: Microsoft Windows 10 Pro 19045 / Python 3.12.10 / Intel Core i3-14100F (4C8T) / 63.8 GB
> · Network: not measured (no network claim) · Data integrity: ok=true (82/82 cells complete)
> · Core version: 1.11.24 · Approval: approve_all

**How to read the numbers:** pass rate = passed cells over 6 tasks × 3 repeats (probe excluded; probe
passed 3/3 per edition); duration/tokens are 3-repeat sums; **cost is an estimate** from the built-in
price table (pricing as of 2025-01), not a bill. Each collaboration task × mode ran **once (n=1)**:
**not statistically significant** and not representative of real user workloads. Community lacking
`multi`/`subagent` is product gating, not a failure. Multi mode had real failures this round
(pro 0/4, enterprise 1/4) which we publish as-is; per-case reasons are in the full report §9.
Results hold only for this model/environment/date. The commercial edition requires the separate
`automind-pro` extension and a valid license and is **not distributed with the Community edition**.

## 3. CTA（行动入口，按实际链接替换）

| 目标 | 文案 | 链接/命令 |
| --- | --- | --- |
| 体验社区版 | 下载 AutoMind 社区版（MIT） | `https://github.com/yl13571844594-arch/AutoMind/releases/latest` |
| 从 PyPI 安装 | `pip install "automind-agent[web]"` | PyPI: automind-agent（v1.11.24 待发布；发布后更新此处） |
| 看方法/复跑 | 阅读 v1.11.24 基准报告并自行复跑 | `docs/BENCHMARK_v1.11.24.md` §11 |
| 看数据 | 隔离矩阵 / 聚合报告 / 脱敏原始数据 | `docs/benchmark/v1.11.24/`（含 `SHA256SUMS.txt`） |
| 看使用手册 | 零基础使用手册 | `使用手册.md` / `https://github.com/yl13571844594-arch/AutoMind` |
| 商业版 / 企业方案 | 联系发行方获取 `automind-pro` 扩展包与许可证 | 商业版**不随社区版分发**，需单独授权 |

## 4. 图表建议（上线时用真实数据生成）

| 图 | 内容 | 数据来源 | 图注必须包含 |
| --- | --- | --- | --- |
| 图 1 | 6 任务 × 3 版本通过率热力图 | `docs/benchmark/v1.11.24/report/eval_matrix.csv` | 模型 deepseek-flash、日期 2026-10-31、3 轮样本 |
| 图 2 | 版本汇总：通过率 vs 估算成本（散点/柱状） | `report/summary.json` → `eval.editions` | 成本为估算值，非账单 |
| 图 3 | 协作矩阵：可用模式 × 成功率（**含 multi 失败行**） | `report/collab_matrix.csv` | "每 cell n=1，不构成统计显著结论" |
| 图 4（可选） | 隔离矩阵：27 项商业 feature × 3 版本 | `docs/benchmark/v1.11.24/isolation_matrix.json` | 社区 0/27、专业 19/27、企业 27/27 |
| 图 5（可选） | 探针状态 / 数据完整性徽标 | `summary.json` → `validation`、`probe_status` | 探针必须 passed |

图表数字一律从 `summary.json` / `isolation_matrix.json` 生成，**禁止手工编数字**；过期的截图必须重新生成。

## 5. 发布前 checklist（全部勾选才可发布）

- [x] 数字已回填，且与 `docs/benchmark/v1.11.24/report/summary.json` 一致（27 个脱敏数据文件 + SHA256SUMS，含 `environment.json` 与最终 `validation.json`）
- [x] `summary.json` 的 `validation.ok == true`（82/82 cell 完整）
- [ ] §1.4 / §2 的"数字怎么读 / How to read"**免责段落原样保留**（发布时确认未被编辑）
- [ ] 明确保留"协作矩阵 n=1，不构成统计显著结论"
- [ ] 明确保留失败案例（pro multi 0/4、enterprise multi 1/4 等），不得删除或弱化
- [ ] 明确保留"商业版不随社区版分发、需单独授权"
- [ ] 明确保留"成本为估算、非账单；结果只对该模型/环境/日期成立"
- [ ] 没有跨模型排名、没有"超越某模型"、没有绝对保证
- [ ] 图表由汇总数据生成，截图与本次运行一致
- [ ] 审核人：未审核（发布前填写）；发布日期：未发布（发布时填写）

## 6. 禁用表述对照表

| ❌ 不要写 | ✅ 改成 |
| --- | --- |
| "多智能体比单 Agent 强 X%" | "在本模型 + 本轮次下观察到 …（样本小，不构成统计显著结论）" |
| "超过 / 碾压 GPT-4o、Claude 等" | 删除（本版无跨模型数据，不能排名） |
| "100% 通过 / 零失败" | "18/18 通过（样本 3 轮），仅代表该模型与套件" |
| "成本 $X" | "估算成本 $X（内置价格表，非账单）" |
| "企业版免费 / 社区版已含商业能力" | "商业版需单独授权，不随社区版分发" |
| "已全面验证安全" | "本报告不是安全审计；并发/生命周期审计另见 docs/AUDIT_v1.11.23.md" |
| "结果可推广到所有模型/任务" | "结果只对该模型、环境与日期成立；换模型需重跑" |

## 7. 数据溯源（回填已完成，保留供复核）

| 文案中的数字 | 实际值 | 来源 |
| --- | --- | --- |
| 三版本通过率 / 耗时 / Token / 成本 | 见 §1.4 / §2 表格 | `docs/benchmark/v1.11.24/report/summary.json` → `eval.editions` |
| 模型与环境 | deepseek / deepseek-flash；temperature 0.7；approval approve_all；Windows 10 专业版 19045；Python 3.12.10；i3-14100F（4C8T）；63.8 GB；network 未测量 | `manifest.json` + `docs/benchmark/v1.11.24/environment.json` |
| 数据完整性 | ok=true（82/82 cell 完整；最终 `validation.json`：54 eval + 28 collab = 82） | `summary.json` → `validation` + `report/validation.json` |
| 隔离矩阵 | 社区 0/27、专业 19/27、企业 27/27；license_step / package 状态 | `docs/benchmark/v1.11.24/isolation_matrix.json` |
| 协作矩阵失败 | pro multi 0/4、enterprise multi 1/4、community single 1/4 等 | `report/collab_matrix.csv` + 主报告 §9 |
| 发布后 PyPI 实际版本 | 未发布（发布后更新，不要提前写） | PyPI |
| 审核人与发布日期 | 未审核 / 未发布（发布前填写） | 人工 |
