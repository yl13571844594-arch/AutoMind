# AutoMind v1.11.23 审计与压力测试报告

> **状态：审计已完成，结果已回填（2026-10-31）** ｜ 版本：v1.11.23 ｜ 日期：2026-10-31
>
> 本文件的方法论/矩阵由 `docs-version` 建立，实测数字、发现与结论由 Lead 汇总四个审计面回填。
> 所有数字均来自本机可重复的 fake/scripted 测试；A1/A3/A4 已确认真实缺陷并修复，A2 压力测试完成且未发现需修改 `engineering.py` 的缺陷。
> 未在真实模型/真实网络/跨进程下复跑的项目见 §8 边界，不用“通过”冒充“无风险”。

---

## 1. 目的与范围

v1.11.23 是**审计 / 压力测试轮**，目标不是加功能，而是回答四个问题：

1. 多 Agent / 多连接**并发**下，共享状态（团队看板、运行上下文、子智能体注册表）会不会丢数据、重复认领、泄漏？
2. 长链路（工程协同 DAG、子智能体委派、WS 重连）在**规模/失败/预算耗尽**时是否仍收敛、可解释？
3. 既有修复（v1.11.20～v1.11.22）在压力下的**回归风险**在哪里？
4. 发现的真实缺陷是否被**最小修复 + 回归测试**锁住？

**本轮不做**：真实模型质量评测（那是 `docs/EVAL.md` / `docs/COLLABORATION.md` 的评测脚本范畴）、
跨平台真机矩阵、长时间稳定性浸泡（>1h）、性能基准横比。边界见 §8。

---

## 2. 方法论

| 原则 | 做法 |
| --- | --- |
| **零真实模型** | 全部用 fake / scripted LLM、fake parent/clone/child、fake WS fixture；不消耗 Token、不依赖外部服务 |
| **零真实等待** | 用 `asyncio`/事件同步代替 sleep；每条用例有耗时上限断言（避免"慢但过"掩盖问题） |
| **并发真的并发** | 线程（`ThreadPoolExecutor`）与协程（`asyncio.gather`）混合；读改写路径用 Barrier 同步起跑，尽量放大竞争窗口 |
| **非法状态可观测** | 断言不看"没抛异常"，而看**数据不变量**：任务数、认领归属、running 计数归零、锁未泄漏、事件不串台 |
| **先测后修** | 先写能复现的压力用例；确认是真实缺陷再改代码，改动**最小化**并保留既有 API/测试 |
| **不静默降级** | 并发冲突的结果必须**确定性**（明确成功/明确失败并给原因），不允许"偶尔覆盖也算过" |
| **如实记录** | 没复现出问题就写"未复现"，不写"无问题"；未测量的项留空 |

**判定口径**：

- **通过**：该领域全部压力用例通过 + 资源不变量成立；
- **发现**：记录了可复现的缺陷（附最小复现用例），并给出修复 / 不修的理由；
- **风险分级**：P0（数据丢失 / 越权 / 崩溃）> P1（偶发错误结果 / 资源泄漏）> P2（性能 / 可维护性）；
- **未发现 ≠ 无风险**：只代表当前用例与规模下未复现。

---

## 3. 审计范围与测试矩阵

> 测试文件路径见下表；实际用例数、耗时与结果以回填后的结果表（§5）为准。
> 四个审计面均已执行完毕：A1 / A3 / A4 的发现与修复见 §6，A2 未见需修改代码的缺陷。

| # | 领域 | 被审计对象 | 测试文件 | 关键场景 |
| --- | --- | --- | --- | --- |
| A1 | 团队任务看板并发与流程 | `automind/core/team.py`（`TeamStore`、SQLite 存储） | `tests/audit/test_team_flow.py` | 100 并发 create；并发 claim 同一任务；并发 update/comment；线程/协程混合；默认存储与 SQLite 至少一种 |
| A2 | 多智能体工程协同压力 | `pro/automind_pro/multiagent/engineering.py` | `pro/tests/test_engineering_stress.py` | 6 节点 DAG 多次运行；并行/依赖；同路径写锁竞争；reviewer 反复不通过；验证失败修复；角色预算耗尽；文本 fallback；断点续跑 |
| A3 | 子智能体委派压力与生命周期 | `automind/planning/subagents.py` | `tests/planning/test_subagent_stress_audit.py` | `MAX_CONCURRENT=4`；深度 1 拒绝；第 5 个拒绝后完成释放；50 次连续派发；结果保留上限 20 / 旧结果淘汰；`list/get/stop/send` 生命周期；stop 未完成状态；预算/输出截断/事件 sink |
| A4 | WebSocket / 团队在线与并发 | `automind/server.py`（LiveRun / WS / presence） | `tests/server/test_ws_stress_audit.py` | 多连接注册/绑定/断开；团队 presence 聚合；同一会话重复 run 拒绝；run→断线→重连接管/终态回放；stop/cancel；并发 connect/disconnect（约 20 轮）；僵尸 LiveRun / running 计数 / 事件串台 |

**验收门禁挂载建议**（Lead 维护 `scripts/acceptance.py` 的 `FAST_TEST_PATHS`）：

- `tests/audit/test_team_flow.py`、`tests/planning/test_subagent_stress_audit.py`、
  `tests/server/test_ws_stress_audit.py` → 视耗时决定进 fast 或留在 full；
- `pro/tests/test_engineering_stress.py` → `pro` 门禁全量覆盖；
- 若无审计测试进 fast，则至少进 `full`，且本报告 §9 的复跑命令必须可执行。

---

## 4. 执行环境与隔离

| 项 | 值 |
| --- | --- |
| 平台 | Windows（工作目录 `C:\Users\Administrator\Desktop\自动化`） |
| Python | 3.12 |
| 测试框架 | pytest（fake/scripted，无真实模型） |
| 数据隔离 | 临时 `AUTOMIND_DATA_DIR` / tmp 工作区，不污染仓库 `.automind/` |
| 外部依赖 | 无网络、无真实模型、无浏览器 |
| 不跑项 | 全量 `pytest`、`vite build`、`scripts/acceptance.py --profile full`（由 Lead 在发版验收时统一跑） |

---

## 5. 结果汇总

> 数字均为本轮本机实测；未测量的项明确保留为“未测量”，不估、不编。

### 5.1 团队任务看板并发与流程（A1）

| 指标 | 结果 |
| --- | --- |
| 用例数 / 通过 / 失败 / 跳过 | **16 / 16 / 0 / 0** |
| 并发 create 规模（100 并发，16 线程） | 修复前无注入延迟实测 **94/100**；注入 2ms 读写延迟放大竞态后仅 **11/100** |
| 并发 create 后任务条数是否等于预期 | 修复后 **100/100**（592ms） |
| 并发 claim 同一任务 | 修复前 **8/8 都“成功”，最后 assignee 覆盖前一位**；修复后 **1 成功 + 7 个确定性 `TaskClaimed`**（32ms），唯一赢家，活动流仅 1 条 claim |
| 并发 update / comment 是否丢失更新 | 修复前：50 并发 comment 仅 6 条、20 任务并发 update 仅 2 个生效；修复后 50/50、20/20，混合 40 写 15/15 |
| 默认存储 / SQLite 存储覆盖情况 | 两者均覆盖；默认 SQLite 60 并发 create 修复前 4/60，修复后 **60/60**（364ms） |
| 耗时（整文件） | 16 passed in **2.44s** |
| 发现 / 修复 | AUD-001（P1，已修复）；另发现 server 新建/更新端点依赖“无 await 窗口”的隐式原子性，Lead 已改为走 `store.create/update` 锁内路径；AUD-005（P2 测试隔离）已修 |

### 5.2 多智能体工程协同压力（A2）

| 指标 | 结果 |
| --- | --- |
| 用例数 / 通过 / 失败 / 跳过 | **29 / 29 / 0 / 0**（`pro/tests/test_engineering_stress.py`） |
| 6 节点 DAG 运行次数 | 6 节点 DAG × **10 轮**复用同一编排器（每轮 6 角色 / 4 批次 / 2 次父 LLM，账目逐轮精确） |
| 并行 / 依赖批次数观测 | `max_parallel=1/2/3` 均满足依赖顺序与并行上限；6 个独立节点单批全并行（max_active=6） |
| 同路径写锁竞争是否串行、锁是否全部释放 | 单编排器 3 个 coder 写同一文件：3 批串行、时间区间零重叠；两个独立编排器并发写同一文件也由跨实例 `path_lock` 串行；锁表 3 键、无 locked 残留 |
| reviewer 反复不通过 / 回炉上限 | `max_rework=1/3` 每档 3 轮，执行次数严格 `1+max_rework`；审查不可用不回炉；回炉后通过恰 2 次执行 |
| 验证失败修复 / 角色预算耗尽 / 文本 fallback / 断点续跑 | 验证失败修复严格 1 轮；角色/父调用预算耗尽均抛 `BudgetExhaustedError` + progress，按 progress 续跑成功节点跳过、失败链补跑；文本 fallback ×10 轮 0 次 clone/工具角色；取消运行锁释放、无任务泄漏 |
| 断言上限（llm 调用 / 步数 / 耗时） | 父 LLM 每轮 ≤2；每节点执行 ≤1+max_rework+1；`max_steps/max_tokens/extension=0` 逐项断言；单轮 duration<5s |
| 输出 / metrics / token_usage 正确性 | `token_usage` 逐字段 = 父调用固定用量 + Σ角色 usage；metrics 全字段校验；25 组固定种子随机 DAG 模糊压测恒成立 |
| 发现 / 修复 | **AUD-004：压力测试完成，未发现需要修改 `engineering.py` 的缺陷（engineering.py 零改动）**；报告了 4 条非 bug 边界，见 §8 |

### 5.3 子智能体委派压力与生命周期（A3）

| 指标 | 结果 |
| --- | --- |
| 用例数 / 通过 / 失败 / 跳过 | **30 / 30 / 0 / 0** |
| 并发 4 / 第 5 个拒绝后是否释放槽位 | 4 个并发放行；第 5 个 `SubagentLimitError`（manager 与工具两层）；完成释放后第 5 个可再派；8 并发 start 也只有 4 个放行 |
| 深度 1 拒绝行为 | manager/tool 两层均拒绝，且拒绝发生在 clone 之前（不白花预算） |
| 50 次连续派发后 running 是否归零 | 50 次连续派发 + 批量 50 个均无 running 泄漏、无悬挂后台 task，均 <5s |
| 结果保留上限 20 / 旧结果淘汰 | `_by_session`/`_index` 封顶 20，旧 id 的 get/send/stop 均为可解释失败 |
| 生命周期（list/get/stop/send）与 stop 未完成状态 | spawn → list → send → detail → stop 全链路；stop 超时返回 `status=running + 可能仍在收尾`，并发 stop 幂等 |
| 预算 / 输出截断 / 事件 sink | max_steps/max_tokens 夹取；60k 输出在记录 20k / 详情 / 事件 4k / started task 2k 四处截断并标注原长；budget_exhausted 保留部分产出 |
| 耗时 | 新增文件 **0.35s**；子智能体相关 94 passed / 0.65s；整个 `tests/planning` 306 passed / 43.37s |
| 发现 / 修复 | **AUD-002（P1，5 处真实 bug，已修复）**：派发取消孤儿 running、stop 卡住记录不结算、两处缺 `stop_reason="cancelled"`、终态事件重复 |

### 5.4 WebSocket / 团队在线与并发（A4）

| 指标 | 结果 |
| --- | --- |
| 用例数 / 通过 / 失败 / 跳过 | **14 / 14 / 0 / 0**（`tests/server/test_ws_stress_audit.py`） |
| 连接规模 / 并发 connect-disconnect 轮数 | 20 轮 connect→bind→disconnect 连接风暴；3 标签页 presence 聚合；8 会话并发执行全部完成 |
| presence 聚合是否正确、断开后是否清理 | tabs 3→2→1→0 正确；换 member_id 拆/合正确；所有连接/会话/在线表归零 |
| 同一会话重复 run 拒绝 | 第二个 run 明确拒绝，第一个不受影响 |
| 断线→重连接管 / 终态回放 | running 回放、事件出口切到新连接、终态在新连接落地；TTL 内回放完整 output，之后 TTL 回收 |
| 僵尸 LiveRun / running 计数归零 / 事件串台 | 10 个孤儿 reattach 运行 grace 到点批量取消并回收；2 会话各 25 条事件零串台；所有注册表/计数归零 |
| 耗时 | 本文件 **~1.9–2.1s**（连跑 3 次稳定）；定向回归 255 passed / 28.4s；`tests/server` 全量 615 passed / 44.8s（隔离 data dir + 社区模式） |
| 发现 / 修复 | **AUD-003（P1，2 处真实 bug，已修复）**：重复 bind 幽灵连接引用泄漏、`task_start` 取消导致 `_running_tasks` 计数泄漏 |

### 5.5 汇总

| 指标 | 结果 |
| --- | --- |
| 审计用例总数 / 通过 / 失败 / 跳过 | **89 / 89 / 0 / 0**（A1 16 + A2 29 + A3 30 + A4 14） |
| 总耗时 | 四个审计文件合计约 **6.5s**（不含全量回归；pro 回归 68 passed/1.43s、server 全量 615 passed/44.8s） |
| P0 / P1 / P2 发现数 | **P0 = 0；P1 = 3 组（AUD-001/002/003，共 8 处修复）；P2 = 3（AUD-005 测试隔离、AUD-006 工作流摘要缓存、AUD-007 治理账本轮转排序）+ 若干非 bug 边界** |
| 已修复项数 | **6**（AUD-001、AUD-002、AUD-003、AUD-005、AUD-006、AUD-007；AUD-001 同时修复 server 端点隐式原子性） |
| 遗留风险项数 | **5 条**（见 §8） |

---

## 6. 已发现问题与修复记录

> 下表由 Lead 按四个审计面的实测结果汇总；A1/A3/A4 的修复均有对应回归测试，
> A2 压力测试完成后**未发现需要修改 `engineering.py` 的缺陷**（如实记录，不为改而改）。

| ID | 领域 | 严重级 | 现象 / 复现 | 根因 | 修复（文件 / 改动） | 回归测试 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| AUD-001 | A1 团队任务看板 | P1（数据丢失/静默覆盖） | 并发 create 互相覆盖；并发 claim 同一任务"最后写入者赢"且输家无感知 | `TeamStore` 写操作无锁 `load→改→save`；SQLite `team_replace` 整表替换 | `automind/core/team.py`：模块级 `RLock` 串行化写操作与 load/save；`claim` 对他人在办任务抛 `TaskClaimed`（API 400 / 工具可读），同人幂等、done 保留重新认领；`server.py` 团队新建/更新端点改为走 `store.create/update` 锁内路径 | `tests/audit/test_team_flow.py`、`tests/test_team_collab.py` | **已修复**（16/16；100 create 后 100/100；8 claim 唯一赢家） |
| AUD-002 | A3 子智能体 | P1（名额泄漏/重复事件） | 派发取消留"运行中无执行体"孤儿；`subagent_done` 重复；stop 状态与原因不准 | 记录登记后的 `await` 被取消未清理；补结算与正常收尾双路径 | `automind/planning/subagents.py`：取消时清理记录/索引；`done_emitted` 去重终态事件；`stop` 前补结算卡住记录；停止路径补 `stop_reason="cancelled"` | `tests/planning/test_subagent_stress_audit.py` | **已修复**（30/30；5 处 bug） |
| AUD-003 | A4 WebSocket / 在线 | P1（引用泄漏/计数泄漏） | 重复 bind 使连接登记两份、断开只摘一份 → 幽灵引用累积；task_start 取消绕过 finally → running 计数永久 +1 | `_ws_sessions` 追加不幂等、`_ws_unbind` 只摘一次；发送在 try 之外 | `automind/server.py`：`_ws_bind_session` 幂等 + 清理旧残留；`_ws_unbind` 摘除同一连接全部残留；`task_start`（第一个 await）移入 try 内 | `tests/server/test_ws_stress_audit.py` | **已修复**（14/14；2 处 bug） |
| AUD-004 | A2 工程协同 | —（未发现需修复缺陷） | — | — | 压力测试完成，`engineering.py` 无需修改 | `pro/tests/test_engineering_stress.py` | **已完成**（29/29；6 节点 DAG×10、25 组随机 DAG） |
| AUD-005 | A1 测试隔离 | P2（测试污染/误报） | `tests/server/test_v09_features.py` 团队夹具只 patch 旧 JSON 路径，SQLite 已迁移 → 读到真实工作区看板或把测试任务写进用户库 | 测试隔离点停留在旧存储实现 | 给夹具注入内存 `TeamStore`（`server._team_store`），不再触碰真实 `.automind/automind.db`；同时清理本轮测试遗留的 5 条占位任务 | 该文件 15 passed（真实 data dir）；已清理残留 | **已修复** |
| AUD-006 | A2 工作流看板摘要 | P2（偶发空状态） | 工作流 start→update→finish 连续写后 `summaries()[0]["run_status"]` 偶发为空；全量 pro 套件复现 | `WorkflowRunStore._summary_cache` 只按 `mtime_ns` 失效；Windows 同一 mtime 粒度内多次写会命中启动时缓存 | `automind/workflow/runs.py`：`save()` 后显式 `_summary_cache.pop(path.name)`；单测连续 10 次稳定通过 | `pro/tests/test_workflow_pro.py::TestDashboard::test_summary_cache_tracks_file_changes` | **已修复** |
| AUD-007 | A1 治理账本轮转 | P2（误报断链） | 全量核心测试偶发 `verify_chain false / broken_at=1`，文件顺序为 `-2, base, -3` | `_files_by_mtime` 同 mtime 时按文件名字典序，`ledger-…-2` 排在 `ledger-….jsonl` 前 | `automind/core/governance.py`：同 mtime 兜底按“日期 + 轮转序号”排序（base=1、`-2`=2）；轮转测试连续 10 次通过 | 既有 `tests/core/test_governance.py::TestPersistence::test_rotation_keeps_chain_continuous` | **已修复** |

> 未发现缺陷时：本节写"本轮未复现（覆盖场景见 §3）"，不写"无问题"。

---

## 7. 审计结论

- **A1 团队看板**：**不通过 → 修复后通过**。并发覆盖/认领静默覆盖是真实 P1 数据缺陷（100 并发实测曾只剩 94/100、注入延迟后 11/100；8 人 claim 全部覆盖）；修复后有 16/16 压测、100/100 create、1 赢家 + 7 个确定性冲突。
- **A2 工程协同**：**通过（有边界）**。29/29 压力测试通过，6 节点 DAG 10 轮、并行/依赖/写锁/回炉/预算/续跑/模糊随机 DAG 均无泄漏或账目错误；`engineering.py` 零改动。残余边界见 §8（审查预算被吞、目录 scope 精确键等）。
- **A3 子智能体**：**不通过 → 修复后通过**。发现并修复 5 个真实生命周期 bug，30/30 压测、相关 94 passed、整个 planning 306 passed。
- **A4 WS / 在线**：**不通过 → 修复后通过**。发现并修复 2 个真实泄漏，14/14 压测、定向 255 passed、server 全量 615 passed。
- **整体结论**：**可发版（带条件）**。89/89 审计用例通过；P0 = 0，P1 三组 8 处 + P2 三组全部修复并有回归；
  全量 `scripts/acceptance.py --profile full` **OK（exit 0）**：社区 `tests/` **3426 passed / 6 skipped**、
  `pro/tests` **312 passed**、`doctor` 12/12、web 门禁通过；无真实模型/真机/跨进程压测覆盖，残余边界见 §8。

---

## 8. 边界与已知局限（诚实声明）

- **不是模型质量评测**：本报告不回答"哪个模型/哪种协同更好"，那是
  `scripts/collab_benchmark.py --live` 与 `docs/EVAL.md` 的职责。
- **不覆盖真机/真网络**：WS 压测用进程内 fixture，不代表真实代理/弱网/浏览器休眠；
  桌面托盘、PyPI/安装包安装验证仍由 `.github/workflows/ci.yml` 与发行流程负责。
- **不覆盖跨进程真并发**：团队看板若多处进程同时写 SQLite，`_STORE_LOCK` 是进程内锁，
  跨进程写仍依赖数据库层事务（当前产品单进程，跨进程不在本版范围）。
- **协同边界（A2）**：① 审查阶段父预算耗尽会被既有 `_review_result` 的 `except Exception`
  吞成 `available=False` → 按"审查不可用"处理（不误判失败，也不回炉）；② 修复轮成功后，
  原先因依赖失败被 `skip` 的下游节点不会自动补跑，最终如实报 `role_failed`；
  ③ `path_lock` 只按精确文件键串行：一个角色声明目录 scope、另一编排器声明其子文件时，
  跨编排器仍可能并发（同编排器内批调度已按父子目录判重叠）；④ Windows Proactor 定时器
  粒度约 15.6ms，压力测试对 <=10ms 超时做了规避，产品默认 300s 不受影响。
- **时间盒**：压力规模是按"本地可重复、秒级到分钟级"选的（如 100 并发 / 50 次派发 /
  20 轮重连），不是吞吐量基准；更大规模需独立性能环境。
- **修复最小化**：只修可复现的真实缺陷；推断性重构、性能优化不在本轮范围。

**遗留风险清单（5 条）**：
1. 团队看板跨进程并发（进程内 RLock 不跨进程；单进程产品当前不受影响）。
2. 工程协同 `path_lock` 精确键不覆盖目录 scope ↔ 跨编排器子文件。
3. 审查阶段预算耗尽被吞成"审查不可用"（符合既有语义，但不是精确预算归因）。
4. 修复轮后 skip 下游不自动补跑（有界、如实，不伪造完成）。
5. subagent `stop` 超时后顽固子任务仍会跑到自然结束（如实返回 running，不谎报停止）。

---

## 9. 复跑方法

```powershell
# A1 团队任务看板并发与流程
python -m pytest tests/audit/test_team_flow.py -q

# A2 多智能体工程协同压力（需商业扩展包源码）
$env:PYTHONPATH='pro'
python -m pytest pro/tests/test_engineering_stress.py -q

# A3 子智能体委派压力与生命周期
python -m pytest tests/planning/test_subagent_stress_audit.py -q

# A4 WebSocket / 团队在线与并发
python -m pytest tests/server/test_ws_stress_audit.py -q

# 版本一致性（本报告随版本发布时校验）
python -m pytest tests/test_version_consistency.py -q
```

> 全部命令**不需要真实 API Key**；不得在本报告中记录任何 Key / 用户数据。
> 审计测试是否进 `fast` 档以 `scripts/acceptance.py` 的 `FAST_TEST_PATHS` 为准。

---

## 10. 变更记录

| 日期 | 变更 | 负责人 |
| --- | --- | --- |
| 2026-10-31 | 建立骨架：目的 / 方法论 / 范围矩阵 / 结果占位 / 边界 / 复跑命令 | docs-version |
| 2026-10-31 | 预填 A1 / A3 / A4 已确认修复项（按代码修复注释与回归测试整理；规模数字待核对） | docs-version |
| 2026-10-31 | 回填 A1～A4 结果、发现与修复、整体结论（89/89；AUD-001/002/003/005 已修复；AUD-004 未发现需修缺陷） | Lead |
