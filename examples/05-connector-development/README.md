# 05 · 连接器开发（给平台加自己的工具）

内置 32 个工具，但你要接的多半是**自家系统**：工单、ERP、内部 HTTP API。
连接器就是那条"不改 AutoMind 源码也能加工具"的路：

```
~/.automind/connectors/ticket_status.py   ← 你写这一个文件
```

重启（或在界面上点一次「重载工具」）之后，模型就能调用它。
**不需要 fork `automind/agent.py`，也不需要写 MCP server。**

---

## 30 秒跑通（本目录自带自测）

```bash
# ① 端到端自测：起一个假的工单系统，真发一次 HTTP，走平台自己的加载器与调用路径
python examples/05-connector-development/selfcheck.py

# ② 把示例装到自己的连接器目录
mkdir -p ~/.automind/connectors            # Windows: %USERPROFILE%\.automind\connectors
cp examples/05-connector-development/ticket_status.py ~/.automind/connectors/

# ③ 告诉它你的工单系统在哪（Windows 用 setx，PowerShell 用 $env:）
export TICKET_API_BASE="https://tickets.internal.example.com/api/v1"
export TICKET_API_TOKEN="你们的令牌"

# ④ 重启 AutoMind 后，直接对模型说："帮我看下工单 TK-1024 的状态"
```

不想连真系统？第 ① 步的 `selfcheck.py` 会在本机起一个假服务，把整条链路
（加载 → 注册 → 调用 → 返回结构化数据 → reload）真跑一遍。

---

## 本目录内容

| 文件 | 作用 |
| --- | --- |
| `ticket_status.py` | **可直接复制使用**的示例连接器：查工单 / 列工单 / 加工单备注，逐行中文注释讲清 `name`、`description`、`parameters`、`permission_tier`、返回值约定 |
| `selfcheck.py` | 端到端自测：假工单系统 + 真 HTTP + 真加载器 + 真 dispatch |
| `verify_without_pytest.py` | 在 pytest 跑不起来的环境（受限沙箱）里，用**同一份** `tests/tools/test_connectors.py` 复算一遍 |

## 完整文档

参数 schema 怎么写、权限档位怎么选、凭证怎么放、常见坑与排查方法，见
[`docs/CONNECTORS.md`](../../docs/CONNECTORS.md)。

## 安全（一句话）

连接器就是**你自己的代码**，加载它等同于执行任意代码 —— 所以
`~/.automind/connectors/` 必须是**只有你能写**的目录，不要指向共享盘、
可被他人 push 的仓库或 Web 可写目录。详见文档首节。
