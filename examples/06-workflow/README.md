# 06 · 工作流即代码（Workflow as Code）

这个目录里是**工作流定义文件**，不是代码。一份 YAML 就是一条确定的流程：
可评审、可 diff、可进版本库、可在 CI 里被拒绝。

完整文档见 [`docs/WORKFLOWS.md`](../../docs/WORKFLOWS.md)。

## 文件

| 文件 | 用途 |
|---|---|
| `change_request.yaml` | 给客户看的核心样板：拉工单 → 查 CMDB → 生成变更单 → **人工审批** → 执行变更 → 回写工单 |
| `hello.yaml` | 最小示例：纯工具步骤（只写本地文件），离线、不需要凭据，适合当 CI 冒烟用例 |

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
