# 06 · 工作流即代码（Workflow as Code）

这个目录里是**工作流定义文件**，不是代码。一份 YAML 就是一条确定的流程：
可评审、可 diff、可进版本库、可在 CI 里被拒绝。

完整文档见 [`docs/WORKFLOWS.md`](../../docs/WORKFLOWS.md)。

## 文件

| 文件 | 用途 |
|---|---|
| `change_request.yaml` | 给客户看的核心样板：拉工单 → 查 CMDB → 生成变更单 → **人工审批** → 执行变更 → 回写工单 |
| `hello.yaml` | 最小示例：纯工具步骤（只写本地文件），离线、不需要凭据，适合当 CI 冒烟用例 |
| `agent_report.yaml` | v1.8.0 `agent` 步骤样板：读日志 → **Agent 在工具白名单内分析并写报告** → 人工审批 → 发通知 |
| `batch_notes.yaml` | v1.8.0 `foreach` 样板：对列表逐项写卡片、读回核对，最后生成索引（离线） |
| `sample-access.log` | `agent_report.yaml` 用的示例访问日志 |
| `triage.yaml` | v1.9.0 branch 条件扩展样板：`in` 字面量列表、`>=` 数值比较（离线，社区版可用） |
| `customer_service.yaml` | v1.11.5 **在线客服**：`contains_any` 关键词分流 → 投诉转人工（坐席确认）/ 退款·物流·发票固定话术 / 其余由 Agent 查知识库作答（社区版可用；界面里可一键安装） |
| `customer-service-kb.md` | `customer_service.yaml` 用的示例知识库 |
| `pro-edition/nightly_sync.yaml` | v1.9.0 **专业版**：工作日定时触发 + `parallel` 并行导出 + 失败从断点自动续跑 + 通知 + 重启续跑 |
| `pro-edition/inbound_tickets.yaml` | v1.9.0 **专业版**：入站 webhook 触发 + foreach 逐项并发 |
| `pro-edition/governed_payment.yaml` | v1.10.0 **企业版**：财务 → 经理两级按角色审批 + `{{ secrets.* }}` 凭据 + 只运行已发布版本 |

`pro-edition/` 下的样板需要专业版（`governed_payment.yaml` 需要企业版）许可证；社区版加载它们会在**校验阶段**被拦下并指出需要专业版的那一行
（`tests/workflow/test_examples.py` 只覆盖本目录顶层，`pro-edition/` 由 `pro/tests/test_workflow_pro.py` 覆盖）。

## 怎么跑

```bash
# 1. 校验（CI 里最常用；有问题会指出是第几行的哪个字段）
python -m automind.workflow validate examples/06-workflow/change_request.yaml

# 2. 试运行：渲染参数、列出将执行什么，但不调工具、不调模型、不弹审批
python -m automind.workflow run examples/06-workflow/change_request.yaml --dry-run \
  --input ticket_id=INC0012345

# 3. 真跑最小示例（会写一个文件到 examples/06-workflow/hello-output.txt）
python -m automind.workflow run examples/06-workflow/hello.yaml \
  --input content="你好，工作流"
```

```bash
# 4. 在线客服（走固定话术的分支命令行即可真跑；回复写到 examples/06-workflow/out/customer-service/）
python -m automind.workflow run examples/06-workflow/customer_service.yaml \
  --input message="快递到哪了"
```

在线客服的"投诉 / 转人工"路径含 `human` 步骤、"其他问题"路径含 `agent` 步骤：命令行没有审批通道也不接模型，
这两条路径请在界面里跑（「🔀 工作流 → ＋ 新建 → 在线客服 → 一键安装」，运行时会弹出坐席确认）。

退出码：`0` 全绿 / `1` 有步骤失败 / `2` 文件或校验错误 / `130` 被取消。

## change_request.yaml 的看点

这份文件刻意把"评审时要问的问题"都写在了明面上：

- **入参抽出来**（`itsm_base`）：同一份流程换环境只改入参，不维护两份文件；
- **失败策略逐条标注**：拉不到工单 `abort`（没有后续可言）；CMDB 查不到 `continue`
  （很多工单本来就不关联配置项，但报告里会留一行红——这是设计意图，不是漏报）；
  执行变更 `retry(1)`；回写失败 `continue`（变更已经做了，不回滚，但留痕）；
- **审批在动作之前**：`approve` 步骤排在 `execute_change` 之前。把它挪到后面，
  流程看上去一样齐全、也能跑通，但"人批过才动手"这条保证就没了 ——
  所以 `tests/workflow/test_examples.py` 专门断言这个顺序；
- **`allow_private: true` 是显式声明**：ITSM 通常在内网，而 `http_request`
  默认拒绝私网地址。这个字段本身是一次"我知道我在做什么"的声明，也会留在 diff 里。

## 改这份文件时

- 改完跑一遍 `validate` + `--dry-run`（CI 里已配好）；
- 运行时报告里的 `source_digest` 是文件内容的 sha256。审批通过的版本打 tag 之后，
  拿报告里的 digest 和 tag 对一下就能回答"跑的是不是批的那版"。
