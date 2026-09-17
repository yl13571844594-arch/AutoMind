# 连接器（Connector）— 不改源码给平台加工具

> 一句话：把 ``.py`` 扔进 ``~/.automind/connectors/``，你就多了一个模型能调用的工具。

内置 32 个工具，但你要接的多半是**自家系统**：工单、ERP、内部 HTTP API。
此前只有两条路 —— fork `automind/agent.py` 去改 `_register_default_tools`，
或者写一个 MCP server。前者意味着从此跟不上上游，后者意味着为了一个函数
要搭一个进程。连接器是第三条路：**写一个类，放一个文件。**

---

## 0. 先读这一节：连接器 = 受信任代码

**加载连接器就是执行 Python 代码**，它拥有与 AutoMind 服务进程完全相同的
权限：读写文件、访问网络、读取你环境变量里的所有凭证。这和 MCP server 是
同一性质（MCP server 也是你指定、平台直接启动的进程），平台**无法**把它
变安全 —— 一个要访问内网工单系统的工具，本质上就必须拿到网络和凭据。

所以规则只有一条，但它必须被当真：

> **连接器目录必须是只有你能写的目录。**
> 不要放在共享盘、不要放在别人能 push 的仓库里、不要放在 Web 可写的目录下。

在这个前提下，加载器把边界划清楚（这些**是**平台保证的）：

| 保证 | 说明 |
| --- | --- |
| 只扫你指定的目录 | 默认 `~/.automind/connectors/*.py`，**不递归**子目录、不读 `sys.path` 上恰好同名的模块 |
| 文件名过滤 | 只收 `*.py`；`_` 开头的（`_draft.py`、`__init__.py`）一律跳过 —— 想把草稿先放进去，名字前加个下划线即可 |
| 目录里的符号链接会被拒绝 | 链接指向目录之外时，按"越界"记账并拒绝执行（否则"只扫指定目录"就是一句空话） |
| 失败留痕 | 一个文件坏掉不会连累其它文件，也不会静默：见第 6 节的账目与日志 |

---

## 1. 30 秒上手

```bash
# ① 建目录
mkdir -p ~/.automind/connectors          # Windows PowerShell: mkdir "$env:USERPROFILE\.automind\connectors"

# ② 放一个文件（内容见下一节）
#    ~/.automind/connectors/hello.py

# ③ 重启 AutoMind，或在界面上点一次「重载工具」

# ④ 对模型说："用 hello 工具跟我打个招呼"
```

最少可用的一份代码（就这么多，没有别的仪式）：

```python
from typing import Any
from automind.core.types import ToolResult
from automind.tools.base import AbstractTool


class HelloTool(AbstractTool):
    name = "hello"                       # 模型调用时用的名字
    description = "打个招呼，用于验证连接器是否装好。"
    parameters = {
        "type": "object",
        "properties": {"who": {"type": "string", "description": "要打招呼的对象。"}},
        "required": ["who"],
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        who = kwargs.get("who") or "世界"
        return ToolResult(tool_name=self.name, success=True, output={"greeting": f"你好，{who}！"})
```

三点必须成立，缺一个就装不上：

1. 类继承 `automind.tools.base.AbstractTool`；
2. 类**不是**抽象类（`execute` 已实现），且 `name` 非空；
3. 类**定义在这个文件里**（从别处 `import` 进来的工具类会被忽略 —— 否则
   你只是 `from automind.tools.net_tools import HttpRequestTool` 复用一下，
   就会把内置工具又注册一遍）。

装好没有，看两处：进程日志里的 `connector_loaded`，以及工具面板
（`GET /api/tools`）里多出来的那个名字。用的是现成的示例？
`examples/05-connector-development/selfcheck.py` 会把整条链路真跑一遍。

---

## 2. 一份能直接用的完整示例

`examples/05-connector-development/ticket_status.py` 是一个"查客户工单系统"
的连接器，逐行中文注释，复制即可用。它演示了每个真实连接器都会遇到的四件事：
配置从环境变量读、网络异常翻成人话、写操作报高档位、返回值保持结构化。

```bash
cp examples/05-connector-development/ticket_status.py ~/.automind/connectors/
export TICKET_API_BASE="https://tickets.internal.example.com/api/v1"
export TICKET_API_TOKEN="你们的令牌"
```

---

## 3. 参数 schema 怎么写

`parameters` 是一段 JSON Schema，**模型的参数名、类型、必填项完全由它决定** ——
这是整个连接器里最影响"模型能不能一次调对"的部分。

```python
parameters = {
    "type": "object",
    "properties": {
        "ticket_id": {"type": "string", "description": "工单号，形如 TK-1024。"},
        "status": {
            "type": "string",
            "description": "状态过滤，只能是 open / pending / closed 之一。",
        },
        "limit": {"type": "number", "description": "返回条数上限，默认 20，最大 100。"},
    },
    "required": ["ticket_id"],
}
```

经验规则（都是实际踩出来的）：

* **只写 `properties` 里有的字段**。模型看得见的只有这里，没写的它不敢传；
  写在 `required` 里的它才会必填。
* **枚举值写在 description 里**。"只能是 open / pending / closed 之一" 这句话
  能省掉一整轮重试；不写，模型会自己发明一个 `in_progress` 然后收到 400。
* **类型保持朴素**：`string` / `number` / `boolean` / `object` / `array` 足够。
  `oneOf`、联合类型、嵌套三层的对象，各家模型的函数调用支持度参差不齐 ——
  在你这儿能跑，不代表客户换的那个模型也能跑。
* **description 用用户会说的词**（中文可以）。它是给模型读的说明书，
  不是给你同事看的注释。
* **参数别超过 6~7 个**。超了说明这个工具该拆成两个（`ticket_get` /
  `ticket_list`），或者该用 `action` 字段分流（示例就是这么做的）。

写错了会怎样？平台的注册表会在**执行之前**拦下明显的参数名笔误
（`ticketid` → "你是不是想传 `ticket_id`？"），不会让工具拿着默认值
跑出一个"成功但答非所问"的结果。

### 怎么让它出现在模型的可选工具里

AutoMind 的 ReAct 循环默认**一轮只下发 14 个工具的完整 schema**
（配置项 `react_tool_budget`），其余工具会以一行式目录出现，模型在推理里
点名后下一轮补发。这意味着：

* **工具名取得贴切，第一次就会被选中**。挑选逻辑里权重最高的一条是
  "工具名出现在任务文本里"（+10），其次是隐藏触发词（`TOOL_HINTS`，+3），
  再其次是名字里的词（`ticket_status` 的 `ticket`，+2）。
  所以 `ticket_status` 这种"动词_名词 + 领域词"的命名，比 `crm_op` 高到不知哪里去。
* **description 的第一行很重要**：它在未下发时被截取成一行式目录
  （`- ticket_status: 查询客户内部工单系统……`），模型就是靠这一行决定要不要用你。
* 想强制它在场，把 `react_tool_budget` 调大（配置项），或者把工具做少做精。
* 只要注册上了，**模型就有机会用到它**，不存在"必须改 agent.py 才能被看见"。

---

## 4. 权限档位怎么选

`permission_tier` 决定这次调用**要不要人工审批**：

| 档位 | 什么时候用 | 典型例子 |
| --- | --- | --- |
| `PermissionTier.SAFE` | 只读、可重复执行、无副作用 | 查工单状态、读配置、搜索 |
| `PermissionTier.SENSITIVE` | 会改变本机或外部系统的状态 | 写文件、POST/PUT、加工单备注、发通知 |
| `PermissionTier.DANGEROUS` | 不可逆或对外发声 | 删数据、发邮件给客户、群发消息、付款 |

```python
from automind.core.types import PermissionTier

class TicketStatusTool(AbstractTool):
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 40          # 0-100，仅用于风险排序与展示
```

两个必须知道的点：

* **档位是整个工具一个，不能按 action 分开声明**。所以只要有一项动作会改数据，
  整个工具就得报 `SENSITIVE`（示例的 `get`/`list` 因此也被一起报高了）。
  真想让读操作免审批，就**拆成两个工具** —— 这也是推荐做法。
* **千万不要为了"少弹审批"把写操作报成 SAFE**。那等于让模型可以不经确认去改
  客户的系统，而且界面上看不出任何异常。安全默认值不是用来说明书写好看的。

---

## 5. 需要网络 / 凭证时怎么办

### 凭证：一律走环境变量

```python
import os

def _token() -> str:
    token = (os.environ.get("TICKET_API_TOKEN") or "").strip()
    if not token:
        raise RuntimeError(
            "缺少 TICKET_API_TOKEN。请在启动 AutoMind 的环境里设置它，"
            "例如 PowerShell: $env:TICKET_API_TOKEN='xxx'；"
            "或 Windows: setx TICKET_API_TOKEN \"xxx\"（设完要重启 AutoMind）。")
    return token
```

* **别写进代码**：连接器是独立分发的文件，凭证写进去迟早进仓库。
* **错误信息里给出"怎么设"**，不要只报 `KeyError`。用户看到的那句话通常就是
  模型转述给他的那句话 —— 把下一步动作写进去，一轮就能修好。
* 读 `automind` 的配置文件也是一条路（`getattr(ex, "字段名", 默认值)`），
  但那要求客户同时改配置结构；连接器自包含更容易交付。

### 依赖：用 `_toolkit` 的 `need` / `need_binary`，缺什么给什么

```python
from automind.tools._toolkit import need, need_binary

async def execute(self, **kwargs):
    try:
        need("httpx")                 # 缺库时抛 MissingDependency，消息是"pip install httpx>=0.27"
        import httpx
        ...
```

* `need("httpx")` —— Python 包。缺失时给出的是一句**可照抄的 pip 命令**。
* `need_binary("ffmpeg")` —— 外部程序（pip 装不了的那一半）。缺失时给出的是
  **该平台的安装办法**，而不是又一句 pip 命令。
* 两者混用最常见的坑是：`pip install pytesseract` 装上了壳、引擎还是没有。
  `need()` 已经会顺带检查 `MODULE_BINARIES` 里的外部命令，所以"装好了却还报
  缺依赖"这种死循环不会出现。
* 把 `execute` 整体包在 `try/except` 里，用
  `automind.tools._toolkit.err(self.name, e)` 收口，这些异常会被自动翻译成
  模型能看懂的失败结果（含 `missing_dependency` / `missing_binary` 字段）。

**内网地址与 SSRF**：平台的 `_toolkit.check_url` 默认**拒绝私网、回环与云元数据
地址**。要连内网就显式放行：

```python
from automind.tools._toolkit import BlockedTarget, check_url

try:
    check_url(url, allow_private=True)     # 放行内网，但仍然拦住 169.254.169.254
except BlockedTarget as e:
    return ToolResult(tool_name=self.name, success=False, error=str(e))
```

为什么不是"直接不检查"：模型可能被网页内容或用户文档诱导去请求
`http://169.254.169.254/`（云实例凭据）或本机端口。`allow_private=True` 是
"我知道自己在访问内网"，而不是"关掉防护"。

### 别阻塞事件循环

工具是 `async def`，但 `requests`、`urlopen`、`subprocess.run` 都是**阻塞**的。
直接在协程里调用它们会卡死整个进程的事件循环 —— 其它会话、心跳、进度推送
一起冻住，而且"给这一步设超时"也不会生效。

```python
import asyncio

data = await asyncio.to_thread(_blocking_request, url)     # 标准库做法
# 或者用平台的：from automind.tools._toolkit import run_blocking
#              data = await run_blocking(_blocking_request, url)
```

更省事的做法是用异步客户端（`httpx.AsyncClient` + `await`），那就完全不用线程。

---

## 6. 返回值约定

```python
from automind.core.types import ToolResult

return ToolResult(
    tool_name=self.name,
    success=True,                     # 失败时必须是 False —— 见下面的坑
    output={                          # 结构化结果，模型据此继续推理
        "ticket_id": "TK-1024",
        "state": "open",
        "assignee": "张工",
    },
    metadata={"source": "ticket_api"},   # 给日志/观测用的附加信息，可为空
)
```

* `output` 里放**结构化**数据（dict / list），键名用英文短词、值保持原始类型。
  模型会直接读它来决定下一步；上层界面也能拿去做展示。
* `error` 只在失败时有意义，且要写成**模型能据以改正**的一句话
  （"缺少 ticket_id，形如 TK-1024"），而不是一句 `KeyError: 'ticket_id'`。
* `execute` **不要抛异常**。真抛了也会被注册表兜住转成失败结果，但那层兜底
  只能看到一个异常文本 —— 你在本地能给出的说明要准确得多。

---

## 7. 如何自测（可复制的命令）

### ① 端到端自测（推荐先跑这个）

```bash
# 起一个假的工单系统，走平台自己的加载器 + dispatch 真跑一遍
python examples/05-connector-development/selfcheck.py
```

它会验证：**能加载 → 能注册 → 能调用 → 能拿到结构化数据 → reload 后仍可用**。
你把自己的连接器换进去同理：把文件复制进一个临时目录，把
`AUTOMIND_CONNECTORS_DIR` 指向它，再 `load_connectors(ToolRegistry())`。

### ② 用临时目录试装（不污染自己的连接器目录）

```bash
# PowerShell
$env:AUTOMIND_CONNECTORS_DIR = "C:\tmp\my-connectors"
python -c "from automind.tools.base import ToolRegistry; from automind.tools.connectors import load_connectors, load_failures; r=ToolRegistry(); print(load_connectors(r)); print(load_failures())"
```

第二行的两个输出就是全部答案：第一个是加载成功的工具名，第二个是失败账目
（空清单 = 都装好了）。

### ③ 跑平台的连接器用例（改加载器时跑）

```bash
python -m pytest tests/tools/test_connectors.py -q --no-header
python -m ruff check automind/tools/connectors.py tests/tools/test_connectors.py examples/05-connector-development
```

> **受限环境里 pytest 可能起不来**：它的私有临时目录
> （`%LOCALAPPDATA%\Temp\pytest-of-*`）会被沙箱拒绝，报
> `PermissionError: [WinError 5]`。此时用同一份用例的复算入口：
> ```bash
> python examples/05-connector-development/verify_without_pytest.py
> ```
> 它跑的是 `tests/tools/test_connectors.py` 里的同一批用例（同一份断言），
> 只是把 `tmp_path` / `monkeypatch` 换成了标准库等价实现。

### ④ 在界面上确认

工具面板（`GET /api/tools`）会列出所有工具及其来源与档位；注册失败的账目
走 `GET /api/tools/registration` 那一路（父 agent 负责接线，见文末）。

---

## 8. 常见坑

| 现象 | 原因与修法 |
| --- | --- |
| 工具没出现，日志里有 `connector_load_failed` | 打开失败账目看 `error` 与 `hint`：`import` 阶段多半是缺依赖或语法错；`init` 阶段多半是构造函数里读了没设的环境变量 |
| 日志里出现 `connector_duplicate_tool` | 两个文件注册了同名工具，**只保留最后扫到的那个**。改名字或删掉多余的；注意扫到的顺序是"目录顺序 + 文件名排序" |
| 模型说"没有这个工具" | ① 名字是不是被下发预算挤掉了？任务里点名工具名即可补发；② 是不是 `_` 开头的文件名被跳过了？③ 装完没重启/没重载 |
| 调用返回 `success=True` 但 output 是空的 | 你在失败分支里忘了 `success=False`。这是最危险的一类错 —— 模型会拿着空结果继续往下做 |
| `TypeError: object ... can't be used in 'await' expression` | `execute` 写成了同步函数。必须是 `async def` |
| 报错 `缺少 xxx 依赖` 但明明装过了 | 连接器由 **AutoMind 服务进程**加载 —— 要用**它的**解释器装包，而不是你随手打开的那个终端的 |
| 改了代码没生效 | 调一次重载（`/api/tools/reload`），或者确认你改的是被加载的那个文件（`AUTOMIND_CONNECTORS_DIR` 指向哪就是哪） |
| 一个文件里定义了多个工具类 | 支持（每个都会被注册），但同名只留最后一个；推荐一个文件一个工具，或文件内名字互不相同 |
| 想拆成多个文件共享代码 | 当前只加载**平铺的单文件**，不支持子目录与相对 import。公共代码请复制，或做成一个正经的 Python 包再 `import`（包用 `need()` 那套方式引入） |

---

## 9. 三条 API（给接线的人）

```python
from automind.tools.connectors import load_connectors, reload_connectors, load_failures

load_connectors(registry)      # 启动时调用一次；返回 ["ticket_status", ...]
reload_connectors(registry)    # /api/tools/reload；返回 {"loaded": [...], "failed": [...], "removed": [...]}
load_failures()                # 供 /api/tools/registration 之类的端点展示失败账目
```

失败账目每条的字段：

```python
{
  "file": "ticket_status.py",        # 文件名（界面上一行显示这个）
  "path": "C:\\Users\\me\\.automind\\connectors\\ticket_status.py",
  "error": "ImportError: No module named 'httpx'",
  "hint": "缺依赖 httpx：pip install httpx（或改用标准库实现）",
  "stage": "import",                 # scan / spec / import / inspect / init / register
}
```

* `reload()` 会**先摘掉上次由连接器注册的工具、再重新扫描** —— 所以"删掉文件"
  和"改了工具名"都能正确处理，`removed` 里列出这次消失的名字。
* 它**只动连接器自己注册的工具**，不会碰内置工具。
* 账目按文件去重，并且每次扫描会同步到"当前目录里真实存在的文件"——
  删掉坏文件之后那条失败也会消失（挂着一条已修好的告警会训练用户忽略面板）。

---

## 10. 相关文件

| 路径 | 内容 |
| --- | --- |
| `automind/tools/connectors.py` | 加载器本体（发现 / 加载 / 失败账目 / reload） |
| `tests/tools/test_connectors.py` | 37 个用例：正常加载、坏文件留账、reload 移除与改名、同名覆盖、空目录、目录覆盖、重复启动加载幂等、安全边界 |
| `examples/05-connector-development/` | 示例连接器 + 端到端自测 + 无 pytest 复算入口 |
| `automind/tools/base.py` | `AbstractTool` / `ToolRegistry`（工具基类与注册表） |
| `automind/tools/_toolkit.py` | `need` / `need_binary` / `check_url` / `err` / `run_blocking` |
| `automind/core/plugin.py` | 插件系统（生命周期钩子，与连接器互补：插件管行为，连接器管工具） |
