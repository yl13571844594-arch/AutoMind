# 评测与可重放轨迹（Evaluation & Replay）

> 版本：v1.7.4 · 相关代码：`automind/eval/`、`automind/core/replay.py`

这份文档回答四个问题：**为什么要评测**、**怎么跑冒烟评测**、**怎么写自己的套件**、
**怎么把它接进 CI**；最后两节讲报告字段与常见的坑。

---

## 1. 为什么需要它

在 v1.7.3 之前，这个仓库里**没有任何评测集**，也**无法重放**。这不是"少个工具"，
而是三件事同时不成立：

| 场景 | 没有评测时的真实状态 |
| --- | --- |
| 交付验收 | 客户能看到的只有"演示时效果不错"，拿不出一份可复跑的通过率 |
| 改动验证 | 改提示词、换模型、调温度，靠人工点几下看看 —— 退化往往几周后由用户发现 |
| badcase 归因 | 用户报的失败案例只用嘴描述，修完没法证明真的修好了 |

已有的 `automind/core/trace.py` 解决不了这个问题，它的职责是**取证**：
它证明"发生了什么"（调了哪些工具、成功与否、花了多少 token）。为此它必须做两件
对重放致命的事：

1. 把 `content` / `prompt` / `output` 这类字段**截断到 4000 字符**；
2. 把所有可能的敏感字段替换成 `***`。

于是它**重建不出"模型当时收到的确切输入"**。本次新增的两个部分正好互补：

```
trace.py        → 取证：这次任务做了什么（默认开启，截断 + 脱敏）
replay.py       → 重放：模型当时收到了什么（默认关闭，不截断，可重新发一次）
eval/           → 评测：一批任务 + 断言 → 通过率 / 耗时 / token / 成本报告
```

---

## 2. 五分钟跑通冒烟评测

### 2.1 先看清单（不需要 API Key）

```bash
python -m automind.eval run automind/eval/suites/smoke.yml --dry-run
```

输出会列出套件里每个任务的模式、断言类型，以及当前有没有可用凭据：

```
[dry-run] 套件 smoke（automind\eval\suites\smoke.yml）
模式 coding　任务 7 个　断言 31 条
模型 openai/gpt-4o　凭据 （无）　→ 缺失，无法评测
  - smoke_write_file [coding] 在当前目录创建 hello.txt，内容为一行：hello automind
      断言：tool_called, file_exists, contains, not_contains, regex, max_seconds, max_tokens
  ...
```

列出内置套件：

```bash
python -m automind.eval list
```

### 2.2 配一个有 Key 的模型，真跑

需要**任一**提供商的 API Key（OpenAI / DeepSeek / Kimi / 百炼 / 智谱 / 豆包 /
Anthropic / Google / Grok / Ollama 均可）。以 DeepSeek 为例：

```powershell
$env:AUTOMIND_DEEPSEEK_API_KEY = "sk-..."
python -m automind.eval run automind/eval/suites/smoke.yml --include smoke_write_file,smoke_run_command
```

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--model gpt-4o-mini` | 覆盖模型名（套件里不写死模型是有意的：同一套件要在贵/便宜模型上都跑得动） |
| `--provider deepseek` | 覆盖提供商 |
| `--include a,b` | 只跑指定任务 id（改一处提示词时只跑相关几条） |
| `--limit 3` | 只跑前 3 个任务 |
| `--timeout 120` | 任务级默认超时（秒），任务里写了的以任务为准 |
| `--out report.json` | 报告落盘（CI 里做归档/对比用） |
| `--json` | 报告打到 stdout（机器消费） |
| `--keep-workspace` | 保留任务工作目录，排障用 |

**没有 Key 时会明确报错并退出码 2**，不会把"跑不了"记成"全部失败"或"全部通过"：

```
$ python -m automind.eval run automind/eval/suites/smoke.yml
LLM 未配置，无法评测：LLM 未配置：provider='openai' 没有可用凭据（期望环境变量 OPENAI_API_KEY）...
（评测不会把'跑不了'记成'全部失败'或'全部通过'——请先配置凭据。）
$ echo $LASTEXITCODE
2
```

### 2.3 退出码约定

| 退出码 | 含义 | 排查方向 |
| --- | --- | --- |
| `0` | 全部任务通过 | —— |
| `1` | 有任务未通过（断言失败 / 超时） | 模型或提示词退化 |
| `2` | 配置/用法问题（无 Key、套件格式错误、`--include` 的 id 不存在） | 环境与套件，**不是**模型 |
| `3` | `python -m automind.eval` 被中断 | —— |

把 1 与 2 分开是有意的：看到 1 应该去查提示词/模型，看到 2 应该去查环境；
混在一起会让排查方向从一开始就是错的。

---

## 3. 可重放轨迹（Replay）

### 3.1 开启记录（默认关闭）

完整提示词里含客户数据（代码、文档、业务数据），因此**默认不落盘**。开启方式：

```powershell
$env:AUTOMIND_REPLAY = "1"          # 或配置 ExecutionConfig.replay_capture = true
```

可选环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AUTOMIND_REPLAY` | 关闭 | `1` 开启 |
| `AUTOMIND_REPLAY_DIR` | `<data_dir>/traces` | 落盘根目录 |
| `AUTOMIND_REPLAY_MAX_BYTES` | 64 MiB | 单文件上限 |
| `AUTOMIND_REPLAY_ROTATE` | `1` | 超上限后轮转到新文件（`0` = 只记计数不再写正文） |

轨迹落在 `<data_dir>/traces/<session>/<run_id>.replay.jsonl`，一次 LLM 调用一行：

```json
{
  "ts": 1770000000.123, "session_id": "s1", "run_id": "run3", "type": "llm_call", "seq": 1,
  "request": {
    "provider": "deepseek", "model": "deepseek-chat",
    "temperature": 0.3, "max_tokens": 8192, "top_p": 1.0,
    "messages": [ { "role": "system", "content": "……完整原文，不截断……" } ],
    "tools": [ { "name": "file_write", "description": "…", "parameters": { } } ]
  },
  "response": {
    "text": "…", "tool_calls": [ { "id": "call_1", "name": "file_write", "arguments": { } } ],
    "finish_reason": "stop", "prompt_tokens": 1234, "completion_tokens": 56
  },
  "usage": { "prompt_tokens": 1234, "completion_tokens": 56, "total_tokens": 1290 },
  "elapsed_seconds": 1.87, "error": ""
}
```

**记录前就过滤密钥**：key 名命中 `api_key` / `authorization` / `token` 之类的整条丢弃，
字符串值再过一遍 `core/redact.py` 的正则（防止密钥被粘进普通 `content`）。
**关闭时 `record_call` 只有一次布尔判断**就返回，因此它可以安全地待在每次 LLM 调用的热路径上。

### 3.2 重放

```bash
# 只列清单，不联网、不需要 Key
python -m automind.core.replay <trace.replay.jsonl> --dry-run

# 真跑：按记录里的 messages / tools 重新请求，逐条比对响应对齐情况
python -m automind.core.replay <trace.replay.jsonl> --model gpt-4o-mini --limit 5

# 也可以直接给目录（自动展开轮转出来的分片 r.replay.jsonl / r.replay.2.jsonl / …）
python -m automind.core.replay .automind/traces/s1/
```

真跑输出每条调用的对齐情况，结尾给汇总：

```
[replay] 目标模型: deepseek/deepseek-chat   调用数: 3
  #1  ≈ 相似度=0.947 tools 期望=['file_write'] 实际=['file_write'] 名称一致=True prompt Δ=+0 completion Δ=-3 (1.9s)
[replay] 平均文本相似度 0.947　tool_calls 名称一致率 1.000　调用失败 0 次
```

判定口径（为什么不是"逐字一致"）：LLM 采样本身不确定，逐字比对只会永远红灯。
这里给的是可设阈值的连续分：

- `text_similarity`：字符级相似度（`difflib`），空对空 = 1.0；
- `tool_names_match`：tool_calls **名称集合**是否一致（顺序另记 `tool_order_match`）；
- `prompt_tokens_delta` / `completion_tokens_delta`：新旧用量之差。

退出码：`0` 成功；`1` 有调用失败；`2` 缺 API Key / 文件不存在 / 没有可重放的记录。

> 重放**不会**执行工具。它只回答"同一份输入再发一次，模型还给同样的东西吗"。
> 要验证"工具链整体还能不能干活"，用下面的评测框架。

---

## 4. 写自己的套件

套件是 YAML。最小形态：

```yaml
name: my-suite
description: 我的回归套件
mode: coding              # 套件默认模式：chat | work | coding
timeout_seconds: 180      # 单任务默认超时（可被任务覆盖）

cases:
  - id: create_readme
    prompt: 创建 README.md，第一行写 "# Demo"
    expect:
      - tool_called:
          name: file_write
          args: {path: README.md}
      - file_exists:
          path: README.md
          min_bytes: 8
      - contains: README.md
      - not_contains: Traceback
      - max_seconds: 120
      - max_tokens: 80000
```

### 4.1 任务字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | ✅ | 任务标识，报告与 `--include` 都用它定位；**不能重复** |
| `prompt` | ✅ | 发给 agent 的提示词。短提示词 = 便宜、稳定 |
| `mode` | | `chat` / `work` / `coding`，覆盖套件默认值 |
| `expect` | | 断言集合，见下 |
| `setup` | | `{相对路径: 内容}`，任务开始前预置文件 |
| `description` | | 人读的说明，会出现在报告里 |
| `timeout_seconds` | | 该任务的超时（秒） |
| `expect_fail` | | **反向断言**：期望这条任务有断言失败（见 §4.3） |

未知字段一律**报错并指出行号**（`automind/eval/suites/bad.yml:3` 这种格式）——
把 `expect` 写成 `expects` 却静默忽略，是最危险的一类套件 bug。

### 4.2 断言

| 断言 | 写法 | 通过条件 |
| --- | --- | --- |
| `contains` | `contains: 子串` / `contains: [a, b]` | 输出里出现该子串 |
| `not_contains` | `not_contains: 子串` | 输出里**不**出现该子串 |
| `regex` | `regex: "PY-\\w+"` | 输出匹配该正则（正则写错会明确报"正则本身非法"） |
| `file_exists` | `file_exists: path` / `{path: p, min_bytes: 20}` | 工作区内该文件存在且不小于 `min_bytes` |
| `tool_called` | `tool_called: file_write` / `{name: file_write, args: {path: a.py}}` | 调用过该工具；带 `args` 时要求参数是**子集**匹配 |
| `max_seconds` | `max_seconds: 120` | 单任务耗时 ≤ 120 秒 |
| `max_tokens` | `max_tokens: 80000` | 单任务 token 总量 ≤ 80000（成本上限） |

三种写法等价，可以混用：

```yaml
expect:
  contains: [done, 完成]          # 列表 = 多条断言
  tool_called: file_write         # 标量
  file_exists:                    # 映射 = 带参数
    path: out.json
    min_bytes: 10
  - regex: "ok"                   # 列表里的单条映射
  - type: max_seconds             # 显式 type/value 形态
    value: 120
```

**断言失败时报告里能看到什么** —— 这是评测报告的全部价值所在，每条断言都同时给
"期望"与"实际"：

```
✗ create_readme　12.3s　4521 tok　$0.0021　failed
    工具调用：['file_write']
    ✓ [tool_called] 期望：调用过工具 'file_write'（参数含 {'path': 'README.md'}）
    ✗ [contains] 期望：输出包含 'README.md'
        实际：未包含；实际输出：已创建文件并写入内容。
    ✗ [file_exists] 期望：存在文件 'README.md'（至少 8 字节）
        实际：文件不存在
        细节：{'artifacts': ['readme.md']}      ← 工作区里实际有哪些文件
```

最后那条 `artifacts` 是刻意加的：文件"没写成功"和"写到别的地方去了"是两种完全不同的
问题，只看 `file_exists` 分不出来。

### 4.3 反向断言：给评测框架自己做体检

评测最危险的失效形态是**永远全绿** —— 断言被跳过、执行器接错、"没配 Key"被写成
"全部通过"。它比任何一次红灯都糟，因为你不会去查。

所以内置的 `smoke.yml` 里有一条**故意不可能满足**的任务：

```yaml
  - id: smoke_assertion_failure_sample
    expect_fail: true            # 我期望它有断言失败
    prompt: 创建 marker.txt，内容为一行：alpha
    expect:
      - file_exists: marker.txt
      - contains: THIS_MARKER_MUST_NOT_APPEAR_9F3
```

判定逻辑：**它有失败断言才算通过；如果它竟然全过，说明判定链路断了，整条任务判红。**

### 4.4 内置套件

| 套件 | 任务数 | 覆盖 |
| --- | --- | --- |
| `automind/eval/suites/smoke.yml` | 7 | 文件写入 / 读取汇报 / 命令执行 / 脚本生成+运行 / 中文落盘 / 不存在的文件要如实说 / 断言机制自检 |

冒烟套件刻意用**最短的提示词**：它的目标是"这个版本还能干活吗"，不是"模型有多聪明"。
在 `gpt-4o-mini` / `deepseek-chat` 上全套通常远低于 $0.05。

---

## 5. 接进 CI

```yaml
# .github/workflows/eval.yml（示意）
name: eval
on: [pull_request]
jobs:
  smoke:
    runs-on: ubuntu-latest
    env:
      AUTOMIND_DEEPSEEK_API_KEY: ${{ secrets.AUTOMIND_DEEPSEEK_API_KEY }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"
      - name: 离线自检（不需要 Key）
        run: |
          python -m automind.eval run automind/eval/suites/smoke.yml --dry-run
          python -m automind.eval list
      - name: 冒烟评测
        run: |
          python -m automind.eval run automind/eval/suites/smoke.yml \
            --model deepseek-chat --out eval-report.json
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: eval-report
          path: eval-report.json
```

要点：

- **PR 上只跑便宜模型**（`--model deepseek-chat` 之类），贵的模型留给 nightly；
- **`--out` 一定带上**：出问题时报告是最有用的证物，而且可以对比两次 PR 的通过率；
- 用 `secrets` 存 Key；没有 Key 的仓库（比如 fork 的 PR）会以退出码 2 失败 —— 这本身
  就是正确的信号，不要用 `|| true` 把它吞掉，否则"没跑"会被当成"通过"；
- 只想在改动相关时跑，用 `--include` 按 id 挑几条，控制成本与时长。

本地一键自检：

```powershell
python -m pytest tests/core/test_replay.py tests/eval -q --no-header
python -m ruff check automind/core/replay.py automind/eval tests/core/test_replay.py tests/eval
python -m automind.eval run automind/eval/suites/smoke.yml --dry-run
```

---

## 6. 报告字段

`EvalReport.as_dict()` / `--json` 的稳定字段（接进 CI 后视为对外承诺，不轻易改名）：

| 字段 | 含义 |
| --- | --- |
| `suite` / `path` | 套件名与文件路径 |
| `model` / `provider` | 本次用的模型（**报告必须写清用什么跑的**，否则数字没有可比性） |
| `started_at` / `finished_at` / `total_seconds` | 时间与总耗时 |
| `total` / `passed` / `failed` / `errors` | 任务计数 |
| `pass_rate` | `passed / total`（`error` 计为不通过） |
| `all_passed` | 是否全部通过（`aborted` 时为 `false`） |
| `total_tokens` / `prompt_tokens` / `completion_tokens` | 总 token |
| `estimated_cost_usd` | **估算**成本（见下） |
| `pricing_as_of` | 价格表口径（当前 `2025-01`） |
| `aborted` / `abort_reason` | 是否因配置问题中止（无 Key） |
| `notes` | 额外提示（例如"全部任务都以 error 结束"） |
| `cases[]` | 逐任务明细 |

每个 case：

| 字段 | 含义 |
| --- | --- |
| `id` / `mode` / `status` / `passed` | 状态：`passed` = 跑完且断言全过 |
| `seconds` | 该任务耗时 |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | 该任务用量 |
| `estimated_cost_usd` | 该任务估算成本 |
| `tool_calls[]` | 工具调用序列（名称、参数、成功与否） |
| `assertions[]` | 每条断言的 `type` / `expected` / `actual` / `passed` / `detail` |
| `failed_assertions[]` | 只含失败的那些（CI 告警直接读这个） |
| `output_preview` | 模型输出前 2000 字符 |
| `error` / `timed_out` | 错误信息与是否超时 |
| `expect_fail` | 该任务是否是反向断言（读报告时别把它的 ✗ 当成退化） |
| `workspace` | 任务工作目录（默认运行后已删除；`--keep-workspace` 才保留） |

**状态三分**（这是报告语义的关键）：

- `passed`：任务跑完，且**全部**断言通过；
- `failed`：任务跑完但有断言不通过（含超时）—— 这是"模型没达标"；
- `error`：任务**没跑起来**（agent 建不起来、缺 LLM、执行器异常）—— 这是"框架/环境问题"。

混在一起会让两种排查方向同时失效，所以刻意分开；如果整场全是 `error`，报告会额外
补一条 `notes` 提醒你先查环境。

**关于成本**：`automind/eval/pricing.py` 里是一张按每百万 token 计价的表（口径见
`pricing_as_of`），**会过时**，也未收录所有模型。它只用于"同一套件前后两次跑谁更贵"这类
相对比较；精确账单以服务商为准。未收录的模型会退回一个偏保守（偏高）的默认价并标
`unknown_model: true` —— 宁可高估触发预算告警，也不要低估让人误以为便宜。

---

## 7. 常见的坑

**① 不确定的断言 = 假红灯。** 让模型"解释一下这段代码"，然后断言输出里必须出现某个
具体句子，几乎必然时红时绿 —— 采样本身就不确定。要断言的应该是**确定性事实**：
文件是否存在、工具是否被调用、输出里是否出现命令的真实结果（如 `PY-OK`）。
文本类断言优先用 `regex` 的宽松形态，或把 `contains` 落在你自己注入的标记串上
（`smoke_read_file` 就是这么做的：`setup` 写入 `ZEBRA-42`，再断言回答里有它）。

**② 需要网络的任务不要放进冒烟套件。** 搜网页、调外部 API 的任务会把"网络抖动"
记成"模型退化"。这类任务单独成一套件（例如 `web.yml`），并且给足 `--timeout`。

**③ 成本控制。** 三件事最有效：提示词短（一次说清，别长篇铺垫）；`--include` 只跑
相关任务；给每条任务都加 `max_tokens`。`max_tokens` 拿不到用量时会**判失败**而不是
放行 —— 成本约束无法验证 ≠ 满足约束。

**④ 任务之间必须互不污染。** runner 为每个任务建独立临时工作目录，并把
`AUTOMIND_DATA_DIR` 指向评测自己的目录（轨迹/检查点/会话库都落那儿，不会写进你的仓库）。
因此套件里的路径一律写**相对路径**；`file_exists` 会拒绝绝对路径与 `..` 穿越。

**⑤ 别把 `error` 当成 `failed`。** 报告里这两者是分开的；一个坏掉的执行器会让整场
评测以 `error` 收场，而不是伪装成"模型退化"。看到全 `error` 先看 `error` 字段。

**⑥ 反向断言不要删。** `smoke_assertion_failure_sample` 是唯一能证明"断言真的在判定"
的任务。删掉它，整套评测就失去了自我校验能力。

---

## 8. 和取证轨迹的分工（一句话）

```
trace.py   默认开  证明"这次任务做了什么"     → 排障 / 举证 / 成本归因
replay.py  默认关  保存"模型当时收到什么"     → 复现 / 重放 / 提示词对比
eval/      按需跑  把"分批任务 + 断言"变成数字  → 验收 / 回归 / CI 门禁
```
