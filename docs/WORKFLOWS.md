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
| `type` | **是** | `tool` \| `llm` \| `human` \| `branch` |
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

#### `type: branch` —— 受限比较 + 单向跳转

```yaml
- id: decide
  type: branch
  condition: "{{ steps.check.output.status }} == ok"   # 只支持 == / != / contains
  then: good_path
  else: bad_path
```

- 两侧各自渲染成**文本**再比较（没有隐式类型转换的坑）；
- 支持 `==`、`!=`、`contains` 三种算子，**不做表达式求值**（没有算术、`and`/`or`、
  函数调用 —— 理由见第 5 节）；
- 跳转只能**向后**。往回跳 = 循环，加载期直接报错（v1 不支持循环，
  需要重复执行请用外层调度重跑整条流程）；
- **区间语义**：`branch` 之后是两个互斥区间 —— `then` 区间从 `then` 起、到 `else` 之前；
  `else` 区间从 `else` 起、到文件末尾。没被选中的那一侧在报告里标 `skipped`。

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

三个根变量，没有第四个。**刻意不暴露**执行器内部状态（时间、随机数、宿主路径）——
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
| 不支持并行 | 严格按文件顺序执行 | 拆成多个工作流并发跑 |
| 不支持表达式 | 模板只取值；`branch` 只有三种受限比较 | 把计算放进上游工具步骤 |
| 不支持子工作流 | 一个文件一条流程 | 用工具/服务编排 |
| 不支持动态步骤 | 步骤集合在加载期固定 | 需要动态时用 ReAct / Plan |
| 前向引用只警告 | `{{ steps.后面的步骤.output }}` 结构合法但当前路径多半取不到值 | 调整步骤顺序；执行时取不到会明确报错 |
| 失败不可重放 | 失败后从整条流程重跑，无断点续跑 | 用 `idempotency` 类入参让流程可安全重跑 |

---

## 12. 代码结构（给维护者）

| 文件 | 职责 |
|---|---|
| `automind/workflow/schema.py` | 数据结构 + 逐步字段校验（"这份文件说清自己要干什么了吗"） |
| `automind/workflow/loader.py` | YAML/JSON 解析、行号定位、重复键检出 |
| `automind/workflow/template.py` | `{{ }}` 严格渲染（**只取值不求值**） |
| `automind/workflow/executor.py` | 按文件顺序执行、失败策略、dry-run、结构化报告 |
| `automind/workflow/exceptions.py` | 异常层次 + `file:line:column` 定位 + "你是不是想写 X"建议 |
| `automind/workflow/__main__.py` | CLI（`run` / `validate`）与退出码 |
| `tests/workflow/` | 全部离线测试（假工具 + 假模型，不联网、不需要凭据） |

两条贯穿始终的原则：

1. **能在加载期拒绝的，绝不留给运行时。** 一份 200 行的流程文件里，
   "第 5 步引用的第 2 步名字写错了"必须在你按下运行之前就被指出来。
2. **失败不许伪装成成功。** 报告、退出码、日志三处口径一致：
   有步骤失败就是有失败，`continue` 只是"不中断"，不是"没关系"。
