# 工作流即代码（Workflow as Code）

> 版本：AutoMind v1.7.4 · schema `version: 1` · 实现见 `automind/workflow/`

一句话：**把"先拉工单 → 查 CMDB → 生成变更单 → 等人批 → 执行 → 回写"这条流程，
从提示词里搬到一份 YAML 里。** 于是它可评审、可 diff、可进版本库、可在 CI 里被拒绝，
运行时报告还带着源文件摘要，能证明"跑的就是批的那版"。

---

## 1. 为什么需要它

AutoMind 原有的两条执行路径都是**运行时决定步骤**：

| 模式 | 谁决定下一步 | 入口 |
|---|---|---|
| ReAct | 模型，每一轮挑一个动作 | `automind/planning/react_executor.py` |
| Plan-and-Execute | 模型，先分解成目标树再执行 | `automind/planning/plan_executor.py` |
| **工作流（本页）** | **文件，写死在 YAML 里** | `automind/workflow/executor.py` |

前两者面对"没写过的任务"很强（"帮我看看这个仓库为什么慢"），但交付给客户时有两个
绕不过去的问题：

1. **没有承载物。** 客户要的是一条确定的流程。它只存在于提示词里 —— 改一个字就是改
   代码，客户无法评审，更无法证明"跑的就是我批的那版"。
2. **不可复现。** 同一句输入两次跑出的步骤可能不同，出了事故无法"照着上次那条路径重跑"。

工作流补上的正是这个承载物。它**不含任何规划调用**：步骤顺序由文件写死，
模型只在 `llm` 类型的步骤里被当作"一次文本生成"使用，它返回什么都不改变后续步骤的顺序。

### 什么时候用哪个

| 场景 | 选它 | 理由 |
|---|---|---|
| 客户交付、合规审计、变更管理流程 | **工作流** | 流程要能被逐行评审并签字 |
| 定时/触发式批处理、CI 里的固定动作 | **工作流** | 要可复现、要退出码 |
| 出事要能回答"上次到底做了什么" | **工作流** | 报告带每步状态、摘要与源文件 digest |
| 探索性任务（这个仓库为什么慢） | ReAct | 步骤事先未知，写不出来 |
| 目标要分解成子任务再并行 | Plan | 需要动态分解与回溯 |
| 一次性的数据处理、写个脚本的事 | 直接对话 | 建流程的成本不划算 |

### 组合用法（推荐）

ReAct / Plan 负责"想清楚"，工作流负责"照着做"：

```
客服工单进来
  ├─ ReAct：判断这是什么类型的问题、需要哪条流程        ← 需要判断力
  └─ 工作流：按选定的 YAML 严格执行（审批 / 变更 / 回写） ← 需要确定性
```

也就是说：**让模型选流程，让文件定步骤。** 模型选错了流程，是一个可以复盘的业务判断
错误；模型在流程中间即兴发挥，则是一次无法归因的生产事故。

---

## 2. 五分钟上手

```bash
# 1. 校验（不执行任何东西，CI 里最常用）
python -m automind.workflow validate examples/06-workflow/change_request.yaml

# 2. 试运行：渲染参数、列出将执行什么，但不调工具、不调模型
python -m automind.workflow run examples/06-workflow/change_request.yaml --dry-run \
  --input ticket_id=INC0012345

# 3. 真跑一个纯工具流程（本仓库自带的最小示例，离线、不需要凭据）
python -m automind.workflow run examples/06-workflow/hello.yaml \
  --input path=out/hello.txt --input content=你好
```

Python 里直接调：

```python
from automind.workflow import load_workflow, run_workflow

schema = load_workflow("examples/06-workflow/change_request.yaml")   # 失败抛 WorkflowLoadError
run = await run_workflow(
    schema,
    {"ticket_id": "INC0012345"},
    registry=agent.tool_registry,     # tool 步骤走它 dispatch
    llm=agent.llm,                    # llm 步骤用它 generate
    approval=my_approval_cb,          # human 步骤用它问人
)
print(run.status, run.exit_code)      # ok / partial / failed / cancelled / aborted / dry_run
```

---

## 3. 文件格式：完整字段参考

### 3.1 顶层

```yaml
version: 1              # 必填，整数。本实现只认 1；不认识的版本**明确报错**，绝不猜
name: 变更单处理         # 必填，用于报告与审计
description: ...        # 可选，给人看的一句话说明
inputs: {...}           # 可选，声明式入参
metadata: {...}         # 可选，自由扩展（谁维护、走什么评审），不参与执行但会进报告
steps: [...]            # 必填，至少一步
# ── 以下三项属于专业版（v1.9.0，见第 14 节）──
triggers: [...]         # 可选，定时（cron）/ 入站 webhook 触发器
on_restart: notify      # 可选，服务重启时被打断的触发运行：resume 自动续跑 / notify 只通知（默认）
notify: {...}           # 可选，触发运行的通知：{on: [failed, partial, interrupted, ok], channels: [webhook, im, desktop]}
```

> **为什么版本不认识要报错而不是"按最接近的版本跑"**：按错误的语义**静默跑完**一份
> 客户已批准的流程，比直接失败严重得多。前者会在生产系统上留下没人预期的结果。

### 3.2 `inputs` —— 声明式入参

```yaml
inputs:
  ticket_id:
    type: string          # string | integer | number | boolean | object | array
    required: true        # 缺了就在**执行之前**被拒绝（CLI 退出码 2）
    description: ITSM 工单号
  itsm_base:
    type: string
    required: false
    default: https://itsm.example.com
```

简写形式也认：`ticket_id: string`。

| 字段 | 必填 | 说明 |
|---|---|---|
| `type` | 否 | 默认 `string`。声明用；类型不符时由调用方转换，转不了就明确报错 |
| `required` | 否 | 默认 `false` |
| `default` | 否 | 缺省值。**与 `required: true` 同时写会报错**（语义矛盾：有默认值就永远不会缺失） |
| `description` | 否 | 给人看，缺参时的报错会带上它 |

### 3.3 `steps` —— 步骤

所有步骤共有的字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | **是** | 唯一标识。后续步骤用 `{{ steps.<id>.output }}` 引用它。不能含 `.` |
| `type` | **是** | `tool` \| `llm` \| `human` \| `branch` \| `agent`（v1.8.0）\| `foreach`（v1.8.0） |
| `timeout` | 否 | 秒。到点该步骤判超时失败（**步骤级**上限，会兜住多次重试的总时长） |
| `on_failure` | 否 | `abort`（默认）\| `continue` \| `retry(n)` |

#### `type: tool`

```yaml
- id: fetch_ticket
  type: tool
  tool: http_request                 # 工具名（也允许写模板，运行时才定）
  args:                              # 工具参数，值里可嵌模板
    url: "{{ inputs.itsm_base }}/api/tickets/{{ inputs.ticket_id }}"
    method: GET
    allow_private: true
```

#### `type: llm` —— 只做一次生成

```yaml
- id: draft
  type: llm
  system: 你是严谨的 IT 变更管理员。   # 可选
  prompt: |
    根据以下信息生成变更单草稿：
    【工单】{{ steps.fetch_ticket.output }}
```

它**不参与规划**：返回什么都不改变下一步是谁。

#### `type: human` —— 等人批

```yaml
- id: approve
  type: human
  prompt: "请审批：{{ steps.draft.output }}"
  timeout: 3600
```

**没有注入审批回调 = 拒绝**（fail-closed），不是放行。详见第 6 节。

企业版（v1.10.0）可以指定审批人，逐级按角色审批（见第 15 节）：

```yaml
- id: approve
  type: human
  prompt: "付款 {{ inputs.amount }} 元"
  approvers: [role:finance, role:manager]   # role:<角色> 或 user:<用户名>，最多 5 个
  mode: all                                 # all：按顺序逐级都要通过（默认）；any：任一人通过
```

#### `type: branch` —— 受限比较 + 单向跳转

```yaml
- id: decide
  type: branch
  condition: "{{ steps.check.output.status }} == ok"   # 白名单算子，见下表
  then: good_path
  else: bad_path
```

| 算子 | 含义 | 说明 |
|---|---|---|
| `==` `!=` | 渲染后的**文本**相等 / 不等 | 没有隐式类型转换的坑（`1 == "1"` 在 YAML 里不该是个要推理的问题） |
| `contains` | 左侧文本包含右侧文本 | |
| `contains_any` | 左侧文本包含右侧列表中的**任一**项（v1.11.5） | 右侧写法同 `in`：`{{ inputs.message }} contains_any [退款, 退货, 退钱]`；适合关键词路由，见 `examples/06-workflow/customer_service.yaml` |
| `>` `<` `>=` `<=` | 数值比较（v1.9.0） | 两侧都必须能解析成数字；**不是数字时该步骤判失败**，不静默走 else |
| `in` | 左侧是右侧列表中的一项（v1.9.0） | 右侧写字面量列表 `[high, urgent]`，或渲染成列表的模板；不支持 `not in`（对调 then / else） |
| `exists` | 左侧变量取得到且不为空值（v1.9.0） | 只写左侧，且必须是**单个**占位符：`{{ steps.fetch.output.id }} exists` |

- 仍然**不做表达式求值**（没有算术、`and`/`or`、函数调用 —— 理由见第 5 节）；未知算子
  （`=~`、`matches`、`===`）在加载期报错并给出可选值；
- **`then` 必须排在 `else` 前面**（v1.9.0 起加载期检查；此前写反了会两条路都不走）；
- 跳转只能**向后**。往回跳 = 循环，加载期直接报错（v1 不支持循环，
  需要重复执行请用外层调度重跑整条流程）；
- **区间语义**：`branch` 之后是两个互斥区间 —— `then` 区间从 `then` 起、到 `else` 之前；
  `else` 区间从 `else` 起、到文件末尾。没被选中的那一侧在报告里标 `skipped`。

#### `type: agent` —— 把一个子目标交给 Agent（v1.8.0）

```yaml
- id: analyze
  type: agent
  goal: "阅读 {{ steps.fetch.output.path }}，找出错误率最高的 3 个接口并写成 report.md"
  allowed_tools: [file_read, file_write, python_sandbox]
  max_steps: 15
  token_budget: 80000
  on_failure: abort
```

这是整份流程里**唯一**由模型临场决定"怎么做"的一步，因此被关在三道评审方看得见的边界里：

| 字段 | 默认 | 说明 |
|---|---|---|
| `goal` | 必填 | 子目标（可用模板）。Agent 会以 ReAct 方式多轮"思考 → 调工具" |
| `allowed_tools` | 未声明 = **只给只读（SAFE 级）工具** | 工具白名单 ∩ 运行时实际可用的工具。白名单外的调用**不会执行**，并写进报告的 `detail.denied_tools` |
| `max_steps` | 20（上限 200） | 最多几轮"思考 → 调工具" |
| `token_budget` | 0 = 不单独设限 | 本步骤最多消耗的 token；用尽时交出已完成的部分（不丢进度），仍受整个任务的预算约束 |
| `mode` | `react` | 目前只支持 `react` |

输出结构（后续步骤用 `{{ steps.analyze.output.answer }}` 引用）：

```json
{"answer": "…", "artifacts": ["report.md"], "actions_ok": 5, "actions_failed": 0,
 "stop_reason": "no_more_tools"}
```

- **成功的判据**是 Agent 自己宣布完成（`stop_reason = no_more_tools`）。步数 / 预算用尽、原地打转
  时如实记**失败**，但部分结果仍保留在 `output` 里，`on_failure: continue` 时后续步骤读得到；
- `on_failure: retry(n)` 对 agent 与 llm 步骤生效（v1.8.0 起；此前 llm 写 retry 实际不重试）；
- **审批**：Agent 内部需要审批的工具调用与 `human` 步骤走同一条审批链路，弹窗标明
  "来自工作流 X 第 N 步"；白名单外的调用连审批都不弹（批了也不会执行）；
- **命令行不接模型**：含 agent 步骤的工作流在 CLI 里只能 `--dry-run`，真跑请用 Web 界面或 REST 接口。

#### `type: foreach` —— 对列表逐项执行（v1.8.0）

```yaml
- id: cards
  type: foreach
  items: "{{ inputs.topics }}"          # 模板（渲染结果须是列表）或字面量列表
  steps:
    - id: write_card
      type: tool
      tool: file_write
      args:
        path: "out/card-{{ loop.index }}.md"
        content: "# {{ loop.item }}"
    - id: check_card
      type: tool
      tool: file_read
      args: {path: "{{ steps.write_card.output.path }}"}
  on_failure: continue
```

- 子步骤里多一个根变量 `loop`：`{{ loop.item }}` 当前项（可继续 `.字段` 取值）、`{{ loop.index }}` 从 0 开始的序号；
- 同一项里，后面的子步骤可用 `{{ steps.<子步骤id>.output }}` 读前面子步骤的结果；**出了 foreach** 子步骤不可见，
  整步的输出是"每项一条记录"的列表：`[{index, item, status, outputs: {子步骤id: 输出}}]`；
- 子步骤只支持 `tool` / `llm` / `human` / `agent`（不支持 `branch` 与嵌套 `foreach`）；子步骤与顶层步骤共用一个 id 命名空间；
- 失败语义：子步骤 `abort`（默认）失败 → 本 foreach **立即停止**、剩余项不执行；子步骤 `continue` 失败 → 该项记失败、继续；
  任一项失败 → 整步记失败（部分成功不记成成功），整步自己的 `on_failure` 决定整轮是否继续；
- **迭代上限**：社区版单次 20 项、专业版 1000 项。`items` 写成字面量列表时在**校验阶段**就报错（带行号）；
  写成模板时在展开之后、执行第一项之前判定 —— 超限**一项都不执行**；
- 上游工具把列表以 JSON 文本返回也可以（能无歧义解析成列表就认）；
- 断点续跑以整个 foreach 为粒度（没跑完的 foreach 续跑时整步重跑）；
- **逐项并发**（v1.9.0，专业版）：加 `max_concurrency: 4`（1–32）即同时跑多项。每一项有**自己的**
  上下文 —— `loop` 与本项子步骤的输出互不可见（不会串项）；结果仍按项的顺序汇总；某一项 abort 后
  还没开始的项不再开始。逐项明细在报告里只展开前 50 项，失败项的序号全部列出（`failed_indexes`）。

#### `type: parallel` —— 一组步骤同时跑（v1.9.0，专业版）

```yaml
- id: exports
  type: parallel
  max_concurrency: 3            # 可选，默认 4，上限 32
  steps:
    - {id: export_users,  type: tool, tool: http_request, args: {...}}
    - {id: export_orders, type: tool, tool: http_request, args: {...}, on_failure: retry(2)}
    - {id: export_items,  type: tool, tool: http_request, args: {...}}
- id: manifest
  type: tool
  tool: file_write
  args: {content: "{{ steps.export_users.output }} / {{ steps.exports.output.export_items }}"}
```

- 子步骤只支持 `tool` / `llm` / `human` / `agent`；子步骤与顶层步骤共用一个 id 命名空间；
- 组内子步骤**同时**执行，互相引用在加载期报错；组结束后，后面的步骤既可以直接引用
  `{{ steps.<子步骤>.output }}`，也可以引用整组输出 `{{ steps.<组>.output.<子步骤> }}`；
- 失败语义：子步骤 `abort`（默认）失败 → **还没启动的**子步骤不再启动，已在跑的跑完为止
  （中途掐断一个外部调用比让它跑完更难对账）；`continue` 失败 → 其他子步骤照常；任一子步骤失败 →
  整组记失败，组自己的 `on_failure` 决定整轮是否继续；
- **续跑粒度是子步骤**（D2）：组里每个子步骤结束即落盘，续跑时组里已成功的不再执行，只重跑其余的；
- 报告里只列顶层步骤（与文件逐行对应），子步骤的结果在组那一行的 `detail.children` 里。

#### 需要更高版本的写法：加载期拦截

`type: parallel`、foreach 的 `max_concurrency > 1`、顶层 `triggers` / `on_restart` / `notify` 属于专业版。
社区版里写了会在**校验阶段**直接报"需要专业版（第 N 行）"，而不是报"未知字段"或跑到一半才失败；
dry-run 同样不接受这些写法。

### 3.4 未知字段的处理：默认报错

**本实现不会静默忽略未知字段。** 默认按错误处理，报错里会给出最像的正确字段名。

```yaml
- id: a
  type: tool
  tool: http_request
  timout: 30          # ← 报错：未知字段 'timout'（你是不是想写：timeout？）
```

理由：`timout` 被静默忽略后，步骤照跑、只是没有超时保护，报告里也一样是"成功"——
客户对照文件逐条核对时看不出任何差别。**写错一个字段名就让行为与文件不一致，
这是"工作流即代码"最不能出的错。**

确需放行（例如给工作流加私有备注字段）：

```bash
python -m automind.workflow validate x.yaml --unknown-fields warning
```

```python
WorkflowLoader(unknown_fields="warning")   # 放行，但会在 schema.warnings 里如实列出
```

---

## 4. 模板：`{{ }}` 变量参考

| 写法 | 含义 |
|---|---|
| `{{ inputs.ticket_id }}` | 声明的入参 |
| `{{ steps.fetch.output }}` | 某步骤的输出 |
| `{{ steps.fetch.output.data.id }}` | 嵌套取值（dict 键、list 下标 `[0]`、引号键 `['a b']`） |
| `{{ steps.fetch.error }}` | 某步骤的**失败原因**（失败后仍可引用） |
| `{{ env.ITSM_TOKEN }}` | 进程环境变量（名字必须全大写字母/数字/下划线） |
| `\{{ 字面量 }}` | 转义：渲染成 `{{ 字面量 }}`，用于给提示词示范模板语法 |
| `{{ loop.item }}` / `{{ loop.index }}` | **仅 foreach 的子步骤内**：当前项 / 从 0 开始的序号（v1.8.0） |
| `{{ secrets.ITSM_TOKEN }}` | **企业版凭据**（v1.10.0）：只能用在 tool 步骤的 `args` 里，值不进报告与日志（见第 15 节） |

三个根变量（foreach 子步骤里多一个 `loop`，企业版多一个只能用在 tool 参数里的 `secrets`），没有别的。**刻意不暴露**执行器内部状态（时间、随机数、宿主路径）——
工作流的可复现性依赖"同样的输入产生同样的参数"，多一个变量就多一处不可复现的来源。

### 严格模式（默认且唯一）

引用不存在的变量**直接抛错**，绝不渲染成空字符串：

```
模板引用了不存在的变量：{{ inputs.ticketID }} —— 在 inputs.ticketID 处取不到键 'ticketID'
（该层可用键：ticket_id、itsm_base）（你是不是想写：ticket_id？）
当前可用变量：inputs.itsm_base、inputs.ticket_id、env.HOME、env.PATH、…
```

**为什么不能渲染成空串**：在自动化里空串会一路往下走 —— URL 变成
`https://itsm/api/tickets/`、SQL 变成 `WHERE id = `、变更单正文少了一段，
于是在**远端系统上留下一个看起来正常、实际串味的操作**。等到人发现时，
错的不是模板，是已经改掉的生产数据。宁可当场停。

### 类型保留

整串就是一个占位符时返回**原值**，不做字符串化：

```yaml
args:
  timeout: "{{ inputs.seconds }}"    # seconds 声明为 integer → 工具拿到 int 30，不是 "30"
```

带前后缀时只能是字符串（`"n={{ inputs.count }}"` → `"n=3"`）。

### 渲染结果不会被求值

**模板不是代码。** 下面这些都会报错，而不是被执行：

```
{{ 1 + 1 }}                       {{ inputs.a or inputs.b }}
{{ os.system('echo hi') }}        {{ __import__('os').getcwd() }}
{{ ''.__class__ }}                {{ 'a' if inputs.flag else 'b' }}
```

理由有三条：① 评审方看不懂 `{{ ''.__class__.__mro__ }}`，评审就退化成"信任"；
② 模板渲染发生在**执行器进程里**，能做属性链求值的引擎就是一条现成的代码执行通道；
③ 客户环境未必装了 Jinja2，工作流不该再多拖一个模板引擎的版本风险。

需要计算的值，请放到上游步骤里用一个工具算出来。

---

## 5. 失败策略

```yaml
on_failure: abort        # 默认：整轮中止，后续步骤标 skipped
on_failure: continue     # 本步记失败，后续照常跑；整轮状态变 partial
on_failure: retry(3)     # 最多重试 3 次（共最多执行 4 次）；仍失败则视为失败
```

写法容错（大小写不敏感）：`retry`（默认 1 次）、`retry(3)`、`retry:3`。
上限 100 次 —— 需要更多请用外层调度重跑，而不是让一步无限重试。

### 三条硬规则

1. **失败不许记成成功。** `on_failure: continue` 只是"不中断后续步骤"，
   该步骤在报告里**依然是 `failed`**，整轮状态是 `partial` 而不是 `ok`，
   CLI 退出码仍然是 1。把失败美化成成功，等于让 CI 和客户都失去判据。
2. **`retry(n)` 用尽后按 `abort` 处理。** 重试是给一次机会，不是"允许失败"。
3. **`branch` 的跳转目标不存在 = 控制流坏了 → 无条件中止。**
   此时无论 `on_failure` 写什么都不会放行：放行会让主循环从当前位置继续往下走，
   把两条互斥分支都执行掉 —— 那是"报告全绿、实际多做了事"的静默错误。

---

## 6. 接审批（human 步骤）

### 回调签名

```python
async def my_approval(step, rendered_prompt) -> bool | dict:
    # step            → StepSpec（step.id / step.type / …）
    # rendered_prompt → 已经渲染好的中文提示（模板已取值）
    return True                              # 批准
    return False                             # 拒绝
    return {"approved": True,  "comment": "同意"}
    return {"approved": False, "comment": "回滚方案缺失"}   # comment 会进报告
```

返回值经 `automind.state.human_loop.ApprovalOutcome.normalize()` 归一化，
与既有执行器（`plan_executor` 的 `approval_cb`）保持同一套语义。

### fail-closed：问不到人 = 拒绝

**没有注入审批回调时，`human` 步骤按拒绝处理并如实记录**，绝不默认放行：

```
[failed] approve —— 需要人工审批，但当前没有可用的审批通道，已按**拒绝**处理
（不会默认放行）。请在调用时注入 approval 回调…；无人值守的流程不应包含 human 步骤
```

审批通道**抛异常**（Web 层断连、弹窗送不出去）同样按拒绝处理。

为什么必须这样：执行器会被用在无人值守的场景（CI、定时任务、服务端后台）。
如果"问不到人就默认通过"，那么任何一次审批通道故障都会**静默地**把需要人批的
变更单直接执行掉 —— 而且报告里还是绿的。相比之下，"没人批 → 停下来"最坏只是
流程没走完，可以重跑；前者是不可逆的生产事故。
（同一条原则见 `automind/planning/plan_executor.py` 对 `ask_user` 的处置。）

### 两个实用做法

- **给足 `timeout`**：审批可能等很久（示例给 3600 秒）。超时会记成步骤失败，
  而不是悄悄放过 —— 这是刻意的，超时说明"没人管这件事"，不该被当成默认同意。
- **不要在无人值守的流程里放 `human` 步骤**。真的要跑，就明确给一个
  记录在案的"自动批准"回调（例如带审计日志的 bot 账号），而不是让它因为
  "没接回调"而失败 —— 后者会让人误以为流程有问题。

---

## 7. dry-run（试运行）

```bash
python -m automind.workflow run change_request.yaml --dry-run --input ticket_id=INC0012345
```

保证（`tests/workflow/test_executor.py::TestDryRun` 有断言）：

- **不调任何工具**（`registry.dispatch` 一次都不会被调用）；
- **不调模型**（不消耗 token）；
- **不弹审批窗**；
- **参数照常渲染** —— 模板引错变量、工具没挂上，都在这一步暴露；
- 输出一份可打印的"将执行什么"清单，含每个工具的 `get_execution_plan()` 预览。

dry-run 的整轮状态是 `dry_run`，CLI 退出码 **0**（试运行本身没有失败）。
若某步参数渲染失败，该步标 `failed`、退出码 1 —— 那正是它是主要用途：
**客户评审前先跑一遍**。

`human` 步骤在 dry-run 里按"已批准"记账（试运行本来就不会真的问人），
这样后面用 `branch` 判断审批结果时，试运行也能把路径走通。

---

## 8. 报告

`WorkflowRun`（`run.as_dict()` / `run.to_json()` 可直接交给前端或 CI）：

```json
{
  "workflow": "变更单处理",
  "version": 1,
  "run_id": "9aa3b523dabd",
  "status": "partial",
  "dry_run": false,
  "started_at": "2026-09-17T12:38:15.065+00:00",
  "finished_at": "2026-09-17T12:38:19.221+00:00",
  "duration_ms": 4156.2,
  "source": "examples/06-workflow/change_request.yaml",
  "source_digest": "ca4af675fc39…",
  "inputs": {"ticket_id": "INC0012345"},
  "error": "",
  "warnings": [],
  "summary": {"total": 6, "succeeded": 4, "failed": 1, "skipped": 1,
              "cancelled": 0, "ok": false, "exit_code": 1},
  "steps": [
    {"id": "fetch_ticket", "type": "tool", "status": "ok",
     "started_at": "…", "finished_at": "…", "duration_ms": 812.4,
     "output_digest": "b10af861768f0de0", "error": "", "retries": 0,
     "line": 45, "detail": {"tool": "http_request", "attempts": 1}}
  ]
}
```

| 字段 | 说明 |
|---|---|
| `status` | 整轮：`ok` / `partial`（有失败但跑完了）/ `failed` / `aborted` / `cancelled` / `dry_run` |
| `steps[].status` | 单步：`ok` / `failed` / `skipped` / `aborted` / `cancelled`（`started` 只出现在进度事件里） |
| `output_digest` | 输出的稳定摘要（规范化 JSON 的 sha256 前 16 位）。全文用 `run.outputs[step_id]` 取 |
| `line` | 该步骤在文件里的行号（前端可做"点击报告跳到定义"） |
| `source_digest` | 源文件 sha256 —— **报告与文件版本对得上的凭据** |
| `retries` | 实际重试次数 |
| `detail` | 工具名、尝试次数、实际参数、审批结果、branch 判定等 |

`--include-outputs` 才会把各步完整输出放进 JSON（默认只有摘要：工具输出可能有
几十万字，报告要能进日志、进前端、进 CI 产物）。

**提示词原文明文不进报告**，只留长度与摘要 —— 工单内容、内部 URL 都可能带敏感信息。

### 取消

父任务取消（`asyncio.CancelledError`）时，执行器把当前步骤标 `cancelled`、
整轮标 `cancelled`，然后**原样抛出异常**。

⚠️ 注意：`run()` 在取消路径上**不会返回** `WorkflowRun`（异常会传播）。
需要拿到"停在哪一步"，请在调用侧持有 executor 实例并读 `executor.run_cancelled`，
或自行捕获 `CancelledError` 后重新规划。

---

## 9. 退出码与 CI

| 退出码 | 含义 |
|---|---|
| `0` | 全绿（含 dry-run） |
| `1` | 校验通过、也真的跑了，但**有步骤失败** |
| `2` | 文件不存在 / 校验失败 / 入参给错（**还没开始跑**） |
| `130` | 被取消 |

判据只看"有没有失败步骤"，不看整轮状态叫什么名字。把部分成功当成功放过去，
CI 门禁就成了摆设 —— 而这正是最容易犯的错。

### GitHub Actions 示例

```yaml
name: 校验工作流定义
on: [push, pull_request]

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install -e ".[dev]"

      # 1. 结构校验：写错了字段名 / 引用了不存在的步骤，这里就被拒
      - name: 校验全部工作流定义
        run: |
          fail=0
          for f in $(git ls-files '*.yaml' 'workflows/**/*.yml'); do
            python -m automind.workflow validate "$f" || fail=1
          done
          exit $fail

      # 2. 试运行：渲染参数、确认工具都挂着（不产生任何副作用）
      - name: 试运行
        run: |
          python -m automind.workflow run examples/06-workflow/change_request.yaml \
            --dry-run --input ticket_id=CI-DRYRUN

      # 3. 冒烟：真跑一个离线的最小流程
      - name: 离线冒烟
        run: python -m automind.workflow run examples/06-workflow/hello.yaml
```

### 评审建议

把工作流文件当代码审：

- **PR 里 diff 流程文件**，逐条看"这一步改了会不会动生产数据"；
- 把 `validate` + `--dry-run` 做成必需检查（required check）；
- 审批过的版本打 tag，`source_digest` 就是那份文件的指纹 —— 事后拿报告里的
  digest 与 tag 对一下，即可回答"跑的是不是批的那版"。

---

## 10. 安全注意事项

### 配置即权限

**一份 YAML 能配任意 URL、任意工具调用。** 它不再是一段"建议"，而是执行指令：

```yaml
- type: tool
  tool: http_request
  args: {url: "http://169.254.169.254/latest/meta-data/", allow_private: true}
```

因此：

- **工作流文件的写权限 = 执行权限。** 请把它放在与"能部署到生产"同级的受控仓库里，
  走 PR + 审批，而不是放在共享目录或工单附件里；
- **不要**把 `workflows/` 目录的写权限开放给不受信的人或自动化流程；
- 需要区分环境时，用 `inputs` 抽出入参（示例里的 `itsm_base` 就是这个用途），
  让同一份流程在不同环境跑不同的目标，而不是维护两份文件。

### 已有的防护仍然生效

工具调用走 `ToolRegistry.dispatch`，因此既有防线一条都不少：

- **SSRF 防护**：`http_request` / `web_search` 默认拒绝私网、回环与云元数据地址
  （见 `automind/tools/net_tools.py` 的模块说明）。要访问内网必须显式写
  `allow_private: true` —— 这个显式动作本身就是一次"我知道我在做什么"的声明，
  也会留在文件 diff 里；
- **权限分级**：`PermissionTier` 与 `PermissionEngine` 照常参与（若上层在
  dispatch 前做了权限检查）。工作流**不绕过**权限；
- **路径约束**：文件工具受 `project_root` 约束（见 `automind/tools/file_editor.py`）；
- **敏感文件**：`.automind/` 等密钥/数据目录拒绝读写。

### 模板不是代码通道

`{{ }}` 只做取值，不做求值（第 4 节）。这是刻意的安全边界：一旦支持表达式，
YAML 就成了代码，评审与权限模型同时失效。

### 报告里的敏感信息

- 提示词原文**不进**报告（只留长度与摘要）；
- 工具输出默认只留 `output_digest`，全文要显式 `--include-outputs` / `include_outputs=True`；
- 工具自带脱敏照常生效（见 `automind/core/redact.py`）。

### 环境变量

`{{ env.NAME }}` 读的是**执行器进程**的环境。不要把长期有效的凭据放进工作流文件，
而是通过环境变量或 `inputs` 注入 —— 前者留在部署配置里，后者留在调用方。

---

## 11. 边界与已知限制（v1）

写清楚"不做什么"，比让人自己踩出来强：

| 限制 | 说明 | 替代做法 |
|---|---|---|
| 不支持循环 | `branch` 只能向后跳；往回跳加载期报错 | 用外层调度重跑整条流程 |
| 并行属专业版 | 社区版严格按文件顺序执行；`parallel` 组 / foreach 逐项并发在加载期拦截 | 拆成多个工作流并发跑 |
| 不支持表达式 | 模板只取值；`branch` 只有白名单比较（9 个算子） | 把计算放进上游工具步骤 |
| 不支持子工作流 | 一个文件一条流程 | 用工具/服务编排 |
| 不支持动态步骤 | 步骤集合在加载期固定 | 需要动态时用 ReAct / Plan |
| 前向引用只警告 | `{{ steps.后面的步骤.output }}` 结构合法但当前路径多半取不到值 | 调整步骤顺序；执行时取不到会明确报错 |
| 续跑只在 Web / REST | v1.8.0 起支持断点续跑（见第 13 节），CLI 仍从头跑 | CLI 场景用 `idempotency` 类入参让流程可安全重跑 |
| 触发器属专业版 | `triggers` 在社区版校验阶段即拦截 | 外部 cron / CI 调用 REST 接口 |
| 定时不补跑 | 服务停机期间错过的定时触发不补；上一次没跑完时到点的那一次跳过 | 需要"必须跑"的用 `retry` + 外部监控 |
| foreach 迭代有上限 | 社区版 20 项 / 专业版 1000 项；foreach / parallel 内不支持 branch 与嵌套 | 上游步骤先筛选或分批 |

---

## 12. 代码结构（给维护者）

| 文件 | 职责 |
|---|---|
| `automind/workflow/schema.py` | 数据结构 + 逐步字段校验（"这份文件说清自己要干什么了吗"） |
| `automind/workflow/loader.py` | YAML/JSON 解析、行号定位、重复键检出 |
| `automind/workflow/template.py` | `{{ }}` 严格渲染（**只取值不求值**） |
| `automind/workflow/executor.py` | 按文件顺序执行、失败策略、dry-run、结构化报告、续跑与按步快照 |
| `automind/workflow/agent_step.py` | `agent` 步骤的生产 runner：工具白名单注册表、步骤 token 预算、审批标注（v1.8.0） |
| `automind/workflow/runs.py` | 运行记录按步落盘、续跑计划、最近运行、按条数 / 天数保留（v1.8.0 / v1.9.0） |
| `automind/workflow/cron.py` | cron 解析、下一次触发时间、最小间隔（v1.9.0，加载期校验用） |
| `automind/workflow/conditions.py` | branch 条件的解析与判定规则（v1.9.0，校验与执行共用一份） |
| `pro/automind_pro/workflow_pro.py` | 专业版：触发器调度与入站 webhook、并发执行器、运行看板（v1.9.0，闭源） |
| `pro/automind_pro/workflow_governance.py` | 企业版：发布审批、按工作流权限、凭据、并发配额、运行审计（v1.10.0，闭源） |
| `pro/automind_pro/workflow_from_trace.py` / `workflow_templates.py` | 专业版：从执行轨迹生成草稿、模板库（v1.10.0，闭源） |
| `automind/workflow/exceptions.py` | 异常层次 + `file:line:column` 定位 + "你是不是想写 X"建议 |
| `automind/workflow/__main__.py` | CLI（`run` / `validate`）与退出码 |
| `tests/workflow/` | 全部离线测试（假工具 + 假模型，不联网、不需要凭据） |

两条贯穿始终的原则：

1. **能在加载期拒绝的，绝不留给运行时。** 一份 200 行的流程文件里，
   "第 5 步引用的第 2 步名字写错了"必须在你按下运行之前就被指出来。
2. **失败不许伪装成成功。** 报告、退出码、日志三处口径一致：
   有步骤失败就是有失败，`continue` 只是"不中断"，不是"没关系"。

---

## 13. 在 Web 界面与 REST 接口上运行（v1.8.0）

### 界面

侧栏「🔀 工作流」：列表（校验状态 / 最近运行 / 是否超出版本上限）→ 在线编辑（边写边校验，
错误定位到行）→ 按 `inputs` 声明自动生成入参表单 → 🧪 试运行 → ▶ 运行（逐步进度）→
`human` 步骤弹窗审批 → 报告下载 / ↻ 从断点续跑。输入框「＋ → ▶ 运行工作流」可在对话里发起，
运行卡片出现在当前会话。编辑的就是项目 `workflows/` 目录下的文件原文，保存即落盘。

### 接口

| 接口 | 说明 |
|---|---|
| `GET  /api/workflow/list` | 列出 `workflows/` 下的文件：校验结果、入参声明、`runnable` / `blocked_reason`、最近一次运行 |
| `GET  /api/workflow/source?file=` | 读原文（在线编辑用） |
| `POST /api/workflow/save` | `{file, yaml, overwrite}` 保存（校验不通过也能存草稿；覆盖需显式 `overwrite`） |
| `POST /api/workflow/delete` | `{file}` 删除文件（运行记录保留） |
| `POST /api/workflow/validate` | 只校验 |
| `POST /api/workflow/run` | `{file\|yaml, inputs, session_id, dry_run, wait}` —— **立即返回 `run_id`**；`dry_run` 同步返回报告；`wait: true` 保留"跑完再返回报告"的旧语义（CI 用） |
| `GET  /api/workflow/runs?file=&limit=` | 最近的运行（含 `resumable`） |
| `GET  /api/workflow/runs/{run_id}` | 进行中：实时进度（`live: true`、`current`、已结束的步骤）；结束后：完整报告 |
| `POST /api/workflow/runs/{run_id}/cancel` | 停止：当前步骤标 `cancelled`、整轮 `cancelled`（取消经 asyncio 传到工具层） |
| `POST /api/workflow/runs/{run_id}/resume` | 断点续跑，返回新的 `run_id` 与 `reused_steps` |
| `POST /api/workflow/runs/{run_id}/dismiss` | 不再提示续跑 |

`run` / `runs/*` / `save` / `delete` 属于**管理动作**：本机回环直接可用，远程调用须带 `X-Admin-Token`。

WebSocket 进度事件（按会话推送；页面打开或切换会话时前端发 `{"action": "bind", "session_id": …}`）：
`wf_run_start`（步骤清单）→ `wf_step_start` / `wf_step_done`（每步）→ `wf_iteration`（foreach 每项）
/ `wf_agent_action`（agent 每次工具调用）→ `wf_run_done`（状态、摘要、报告、是否可续跑）。

### 断点续跑

- **按步落盘**：每个顶层步骤结束后，已结束步骤的记录与**成功步骤的输出**写入
  `<数据目录>/workflow_runs/<run_id>.json`（原子替换，中途被杀也不会留下半截文件）；
- **续跑**：已成功的步骤直接沿用上次的输出，**一次都不重复执行**；`branch` 重新判定（它没有副作用，
  且判定依赖前面步骤的输出）；foreach 以整步为粒度；单个输出超过 1MB 的步骤不存全文、续跑时重跑；
- **文件改过就拒绝续跑**（`source_digest` 不同 → 409）：旧结果配新流程等于跑了一份谁都没批过的混合版本；
- 一次运行只能被续跑一次（源记录标 `resumed`），在最新的那次上继续即可；
- 服务重启时仍在跑的运行记为 `interrupted`，界面提示可续跑，**不会自动续跑**（续跑会真的执行、花钱）。

运行记录状态词与对话任务的检查点一致：`running` / `completed` / `partial` / `interrupted` / `resumed` / `dismissed`。

### 会话、额度与记账

- 每次运行（含续跑）计 **1 次**任务额度（不按步骤计），与对话任务共用每日额度；试运行不计；
- 运行结果写入「📜 任务历史」（`interaction = "workflow"`，附完整报告）；
- 运行用独立的会话克隆执行（模型连接、Token 计数与同会话的对话任务互不干扰），会话身份与
  隔离工作目录按发起会话绑定 —— 开启会话隔离时产物进该会话的工作副本，并出现产物卡片；
- 版本限额：社区版最多 **20** 个工作流（按文件名排序、只数校验通过的；超出的仍可列出、校验、
  试运行）、foreach 单次 ≤ **20** 项；专业版 200 个 / 1000 项；企业版不限。

### 出站事件

运行没有全绿时发出站 webhook（全绿不发）：

| 事件 | 触发 |
|---|---|
| `workflow.partial` | 跑完了但有步骤失败（`on_failure: continue`） |
| `workflow.failed` | 被 abort 中止或执行器出错 |

载荷在通用字段（`event` / `session_id` / `task`=工作流名 / `status` / `elapsed_ms` / `tokens` / `error`）
之外，`extra` 里带 `run_id`、`workflow`、`file`、`failed_step`、`summary`、`resumable`。

| `workflow.interrupted` | （专业版 notify）触发运行被停止或被服务重启打断 |
| `workflow.completed` | （专业版 notify）`notify.on` 里显式写了 `ok` 时，触发运行全绿 |

---

## 14. 无人值守：触发器、失败续跑与通知（v1.9.0，专业版）

社区版的自动化方式是"外部 cron / CI 调 `POST /api/workflow/run`"（`wait: true` 可同步拿报告）。
专业版把这件事做进应用里 —— 触发器的**声明**仍然写在工作流文件里（进版本库、可评审），
启停状态与 webhook 令牌存在服务端数据目录（`workflow_triggers.json`），不改文件。

### 14.1 声明

```yaml
triggers:
  - cron: "0 9 * * 1-5"         # 5 段：分 时 日 月 周；支持 * , - / 、月份 / 星期名、@daily 等宏
    inputs: {region: east}       # 触发时的入参（必须是已声明的入参）
    retry: 2                     # 可选：没全绿时从断点自动续跑的次数（0–5，默认 0）
    retry_delay: 120             # 可选：每次重试前等多少秒（10–86400，默认 60）
    description: 工作日早上的日报
    enabled: true                # 可选：文件里的默认启停；界面上的开关优先
  - id: from_ci                  # 可选：触发器 id（默认 t1 / t2 …），状态与令牌按"文件名 + id"记
    webhook: true                # 入站 webhook；令牌由服务端生成，绝不写进文件
on_restart: notify               # resume | notify（默认）
notify:
  on: [failed, partial, interrupted]   # 可选 ok；默认就是这三个
  channels: [webhook, im]               # webhook=出站事件；im=群机器人；desktop=本机通知
```

加载期就会拦下的写法：cron 写错（6 段 / `L` `W` `#` / 2 月 30 号这种永不触发的）、**定时间隔低于
版本下限**（专业版 5 分钟、企业版 1 分钟 —— 按一整年取样算出的最短间隔判定）、触发入参没声明过、
**定时触发没给全必填入参**（定时运行时没有人补参数）、一个触发器同时写了 cron 与 webhook。

- 时区是**服务进程的本地时间**；日与周同时受限时按 cron 传统取并集（"每月 1 号**或**每周一"）；
- 服务停机期间错过的不补跑；上一次触发的运行还没结束时，到点的那一次**跳过**并记录；
- 触发运行使用专用会话 `wf_<文件名>`（D4），与手动运行走**同一套**校验、入参、限额与每日额度判定。

### 14.2 入站 webhook

```bash
curl -X POST http://<服务>/api/workflow/hooks/<触发器键> \
  -H "X-AutoMind-Hook-Token: <令牌>" -H "Content-Type: application/json" \
  -d '{"region": "west"}'                 # 或 {"inputs": {"region": "west"}}
```

- 令牌在界面「⏰ 触发器 → 🔑 生成令牌」得到，**只显示这一次**；服务端只存 sha256 摘要，重置后旧令牌立即失效；
- 令牌缺失 / 错误 / 触发器不存在一律 **401**（不让外部探测哪些键存在）；触发器停用或超出上限 409；
  缺必填入参 422；成功返回 `run_id`；
- 这个路径**不需要**平台访问令牌（凭的是触发器自己的令牌），纳入速率限制；
- 触发器键由"文件名 + 触发器 id"算出：改文件名会换键，需要重新生成令牌。

### 14.3 管理接口

| 接口 | 说明 |
|---|---|
| `GET  /api/workflow/triggers?file=` | 全部触发器：是否生效及原因、下次运行时间、最近一次结果、活跃数与上限 |
| `POST /api/workflow/triggers/{key}/toggle` | `{enabled}` 启停；超出活跃上限时 403 + 升级提示 |
| `POST /api/workflow/triggers/{key}/token` | 生成 / 重置 webhook 令牌（明文只返回这一次） |
| `POST /api/workflow/triggers/{key}/run` | 立即触发一次（同样遵守"上一次未完则跳过"） |
| `GET  /api/workflow/dashboard?days=&file=` | 看板：按工作流的运行数、成功率、平均 / P95 耗时、Token、按天趋势、最常失败的步骤 |

`triggers/*` 的 POST 属于管理动作（远程须 `X-Admin-Token`）。活跃触发器上限：专业版 30 个
（按文件名与声明顺序排先后，超出的列出来并说明原因）、企业版不限。

### 14.4 失败重试、通知与重启

- **失败自动续跑**：触发运行 partial / failed 时，按该触发器的 `retry` 在 `retry_delay` 秒后**从断点续跑**
  —— 已成功的步骤（包括并行组里已成功的子步骤）不再执行；运行记录里的 `attempt` 记第几次尝试。
  被人为停止（cancelled）的不重试。
- **通知**：按 `notify.on` 判断要不要发、按 `channels` 发到哪；内容含失败步骤（行号）与原因、是否将自动续跑。
  `webhook` 通道就是出站事件（failed / partial 由社区核心发，interrupted / ok 由专业版补发）；
  `im` 用 `AUTOMIND_IM_WEBHOOK` / `AUTOMIND_IM_PROVIDER`（与 `im_integration` 工具同一份配置）；
  `desktop` 是本机桌面通知。手动运行不发通知（有人看着）。
- **服务重启**（D5，R-208）：上次没跑完的运行在启动时标为 `interrupted`；其中的**触发运行**按工作流的
  `on_restart` 处置 —— `resume` 自动从断点续跑，`notify`（默认）只发通知、等人决定。手动运行一律只提示。

### 14.5 运行历史与看板

运行记录保留：社区版最近 50 次；专业版 **90 天**；企业版按配置项 `workflow_history_days`（缺省 365 天）。
看板（列表顶部「📊 看板」或 `GET /api/workflow/dashboard`）的数据源就是运行记录 —— 每次运行只计一次、
状态取那次报告的整轮状态，数字能逐条对回报告；观测中心看板也多了一栏工作流统计。

---

## 15. 治理与固化（v1.10.0）

### 15.1 发布审批（企业版，R-301）

工作流有三态：**草稿 → 待审批 → 已发布**。提交时记下文件的 sha256 摘要；审批人批准后，这个摘要就是
"已发布版本"。规则：

- **触发器（定时 / webhook / 失败自动续跑 / 重启续跑）只运行已发布版本**。文件改了一个字，摘要变了，状态就成了
  "已修改未发布"，触发器列表直接显示原因、到点也不会运行，直到新版本再次获批；
- 批准时文件必须仍是提交的那一版（提交后又改过 → 409，请重新提交）；
- SSO 身份下**不能批准自己提交的版本**（四眼原则；配置 `allow_self_approve: true` 可放开）；
- 手动运行草稿默认允许（编写时要能试跑）；配置 `require_approval_for_manual: true` 后手动运行也只能跑已发布版本。

```json
{"workflow_governance": {"require_approval": true, "require_approval_for_manual": false,
                         "allow_self_approve": false,
                         "concurrency": {"default": null, "teams": {"finance": 2}}}}
```

接口：`GET /api/workflow/governance`（各文件状态、当前身份能否提交 / 批准）、
`POST /api/workflow/governance/{file}/submit|approve|reject`（`{comment}`）。

### 15.2 按工作流权限（企业版，R-302）

接现有 RBAC，动作名 `workflow:<动作>:<文件名>`（`*` 表示全部文件）：

| 动作 | 管什么 |
|---|---|
| `view` | 出现在列表里、读原文、看运行记录与报告、试运行、导出审计 |
| `edit` | 保存 / 删除、提交发布审批、从模板新建、生成轨迹草稿、运行直接粘贴的 YAML |
| `run` | 运行、停止、续跑、忽略 |
| `trigger` | 启停触发器、重置 webhook 令牌、立即触发 |
| `approve` | 批准 / 驳回发布 |

内置角色：admin（全部）、developer（view / edit / run / trigger）、approver（view / approve）、viewer（view）。
身份来自 SSO 会话；没有 SSO 时，本机请求与持平台令牌的请求按管理员处理（审计里记来源）。
SSO 登录用户的工作流动作由这里判定，不再要求管理员令牌；凭据与并发配额仍是管理动作。

### 15.3 多级 / 按角色审批（企业版，R-303）

见第 3.3 节 `human` 步骤的 `approvers` / `mode`。审批弹窗与审批请求（含出站审批事件）都带上
"这一级需要谁、第几级"；回答者身份不符时：界面提示"你不是这一级的指定审批人"，外部回执返回 403，
**审批继续等**（不当成拒绝）。外部系统经 `POST /api/approvals/{id}` 回执时可以说明审批人：
`{"approved": true, "approver": {"user": "alice", "role": "finance"}}`（该端点本身要求管理员令牌）。
没有启用 SSO 时，本机与持平台令牌的请求是"平台管理员"，可以代任何角色审批（审计里如实记为
local / token）；要做到真正的多人分级审批，请启用 SSO，让每个人以自己的身份登录审批。

### 15.4 运行审计导出（企业版，R-304）

`GET /api/workflow/runs/{run_id}/audit?format=json|pdf`：文件与摘要、是否已发布版本（及批准人）、触发方式与
第几次尝试、发起人、输入、每步（与报告逐条对应）的状态、起止时间、输出摘要、错误、审批记录。
装了 reportlab 输出 PDF，否则返回可打印的 HTML（浏览器"另存为 PDF"）。

### 15.5 凭据保管（企业版，R-305）

- 引用：`{{ secrets.NAME }}`，**只能放在 tool 步骤的 args 里**；
- 取值：环境变量 `AUTOMIND_SECRET_<NAME>` 优先，其次凭据文件（「🔑」里添加，只写不读，值至少 4 个字符）；
  设置 `AUTOMIND_SECRETS_KEY`（Fernet 密钥）后凭据文件存密文；
- 脱敏：执行器把凭据明文从报告、进度事件、落盘快照、日志里替换成 `***`；续跑时快照里的 `***` 不会被当成值。
- 缺凭据：该步骤失败并说明"凭据 X 未配置"；试运行不需要值（渲染成占位）。

### 15.6 并发配额与排队（企业版，R-306）

按团队限制同时运行的工作流数（`concurrency.default` / `concurrency.teams`）。团队取 SSO 用户的 `team`
（没配置按角色），触发运行归入 `triggers`。超出上限的运行先登记、再排队，`GET /api/workflow/runs/{id}`
与 WebSocket `wf_run_queued` 给出位次；排队时可以停止（记为已取消、退还额度）。

### 15.7 从执行轨迹生成工作流（专业版，R-307）

`GET /api/workflow/from-trace/candidates` 列出最近的执行轨迹；`POST /api/workflow/from-trace`
`{session_id, run_id, mode: tools|agent}` 生成草稿（不落盘），同时返回校验结果、试运行结果与需要人审的要点：

- 只有**成功完成**的任务能导出；失败后被重试的工具调用省略；
- 参数里的 `{{` 转义成字面量；像凭据的参数（token / password / api_key…）换成占位并提示改成 `{{ secrets.* }}`；
- 轨迹里参数被截断到 200 字符的地方会单独列出，请补全；会变的值请改成 `{{ inputs.x }}`。

### 15.8 工作流模板库（专业版，R-308）

`GET/POST /api/workflow/templates`、`DELETE …/{id}`、`POST …/{id}/use {file}`（复制进 workflows/）、
`GET …/export` / `POST …/import`（JSON 包，按内容摘要去重）。模板不会被运行、不占工作流数量额度；
"用它新建"之后就是普通工作流，与模板再无关联。
