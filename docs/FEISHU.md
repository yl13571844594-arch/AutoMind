# 飞书接入 AutoMind（MCP + 机器人）

> 针对本仓库（`automind-agent` v1.11.5）的实际接口写的接入说明。
> 两种需求方向完全不同，**先分清再动手**：

| | 路线 A：飞书 MCP | 路线 B：飞书机器人 |
|---|---|---|
| 数据方向 | AutoMind → 飞书（出站） | 飞书 → AutoMind（入站） |
| 效果 | Agent 能读飞书文档、发消息、查日历 | 在飞书里给机器人发消息，Agent 干活并把结果发回来 |
| 协议 | MCP（stdio 子进程） | 飞书长连接（WebSocket）或 HTTP 回调 |
| 要公网吗 | 不要 | **长连接不要**；HTTP 回调要 |
| 典型话术 | 「把这份周报发到项目群」 | 在群里 @机器人「查一下 D 盘还剩多少空间」 |

两条不冲突，可以同时上。A 是给 Agent 加"手"，B 是给 Agent 加"耳朵和嘴"。

---

## 0. 一句话选型

- 只想让 Agent **能发飞书消息 / 读飞书文档** → 做**路线 A**，15 分钟。
- 想在**飞书里指挥 Agent** → 做**路线 B**，免公网、免备案、免内网穿透（用长连接）。
- 两个都想要 → A + B，共用同一个飞书自建应用即可。

---

## 1. 前置：服务和鉴权（两条路线都要）

服务启动方式见根目录 `launch.bat`：

```bat
python -m automind.server --host 0.0.0.0 --port 8765
```

**注意这里绑的是 `0.0.0.0`。** 这个组合在没配令牌时是不安全的 ——
局域网内任何人都能 `POST /api/run` 让你的机器执行任意工具。
先按 `automind/server.py` 的既有约定把两把钥匙配好：

| 环境变量 | 作用 | 不配的后果 |
|---|---|---|
| `AUTOMIND_AUTH_TOKEN` | `/api/*`、`/v1/*`、`/metrics` 的访问令牌 | 局域网内任何人可驱动 Agent |
| `AUTOMIND_ADMIN_TOKEN` | 管理动作的第二把钥匙（`/api/mcp`、`/api/config`、`/api/plugins`…） | 远程无法改配置/加 MCP（本机回环仍放行） |

调用方式（见 `server.py:311` 的中间件）：

```
Authorization: Bearer <AUTOMIND_AUTH_TOKEN>
# 或 ?token=<AUTOMIND_AUTH_TOKEN>
```

管理动作的判定见 `_admin_ok()`（`server.py:251`）：**回环地址 = 本机 = 免管理员令牌**。
所以路线 B 的桥接进程只要跑在同一台机器上，走 `127.0.0.1` 就不需要 `X-Admin-Token`。

---

## 2. 路线 A：飞书 MCP —— 让 Agent 会操作飞书

### 2.1 先装 MCP SDK

AutoMind 的 MCP 客户端依赖 Python `mcp` 包，缺了会明确提示（`mcp_registry.py:267`）：

```bash
pip install mcp
```

同时需要 **Node.js ≥ 20**（官方飞书 MCP 是 npm 包）。

### 2.2 建飞书自建应用

1. 到 [飞书开放平台](https://open.feishu.cn/) 创建**企业自建应用**，拿到 `App ID` / `App Secret`。
2. 按需开通权限（最小够用即可）：
   - 发消息：`im:message`、`im:message:send_as_bot`
   - 读消息/会话：`im:message:readonly`
   - 云文档：`docx:document:readonly`、`drive:drive:readonly`
3. **必须在「版本管理与发布」里创建版本并发布**，等企业管理员审核通过。
   没发布 = 权限不生效，这是最常见的"配了没反应"。

需要以"用户身份"访问个人资源（读你自己的文档、以你的名义发消息）时，先本地登录一次，
回调地址在控制台配 `http://localhost:3000/callback`：

```bash
npx -y @larksuiteoapi/lark-mcp login -a cli_xxxx -s yyyyy
```

### 2.3 接进 AutoMind

官方 MCP 包为 [`@larksuiteoapi/lark-mcp`](https://github.com/larksuite/lark-openapi-mcp)（当前 0.5.1，MIT，stdio）。

**方式一（推荐，今天就能用）**：界面 → 工具面板 → `🔌 MCP` → 粘贴框 → 「导入并连接」。
这条走 `POST /api/mcp/import`（`server.py:4087`），接受 Claude Desktop 格式：

```json
{
  "mcpServers": {
    "feishu": {
      "command": "npx",
      "args": [
        "-y", "@larksuiteoapi/lark-mcp", "mcp",
        "-a", "cli_xxxxxxxxxxxx",
        "-s", "<APP_SECRET>",
        "-t", "im.v1.message.create,im.v1.message.list,docx.v1.document.rawContent"
      ]
    }
  }
}
```

`-t` 是**工具白名单**，建议一定要写：不写会把该 MCP 的整套 OpenAPI 都暴露给模型，
工具越多、误调用面和提示词体积都越大。工具名单见官方
[presets 参考](https://github.com/larksuite/lark-openapi-mcp/blob/main/docs/reference/tool-presets/presets.md)。

用用户身份调用时加 `--oauth --token-mode user_access_token`；
国际版 Lark 走 `--domain https://open.larksuite.com`。

**方式二**：直接调 `POST /api/mcp`（管理动作：本机访问或带 `X-Admin-Token`）：

```bash
curl -X POST http://127.0.0.1:8765/api/mcp \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: $AUTOMIND_ADMIN_TOKEN" \
  -d '{"name":"feishu","transport":"stdio","command":"npx",
       "args":["-y","@larksuiteoapi/lark-mcp","mcp","-a","cli_x","-s","secret"]}'
```

### 2.4 ⚠️ 内置的飞书预设是错的（建议修）

`automind/tools/mcp_presets.py:82` 把飞书登记成了 **sse + 空 url**：

```python
{
    "id": "feishu",
    "name": "飞书（Lark）",
    "transport": "sse",     # ← 官方 lark-mcp 其实是 stdio
    "command": "",
    "args": [],
    "url": "",              # ← 空 url 的 sse 连不上，只能靠用户手填
    ...
}
```

点「一键添加」会得到一个没有端点的 sse 配置 —— 连不上，且报错信息不会指向真正原因。
官方 lark-mcp 是 **stdio 的 npx 子进程**，没有远端 SSE 端点可填。

建议的最小改动（同时把 `env_hints` 换成真正要用户准备的两项）：

```python
    {
        "id": "feishu",
        "name": "飞书（Lark）",
        "emoji": "🕊️",
        "category": "即时通讯",
        "description": "飞书消息 / 云文档 / 表格（官方 lark-openapi-mcp，需自建应用）",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@larksuiteoapi/lark-mcp", "mcp"],
        "url": "",
        "env_hints": [
            {"key": "LARK_APP_ID", "note": "飞书自建应用 App ID（cli_ 开头）"},
            {"key": "LARK_APP_SECRET", "note": "飞书自建应用 App Secret"},
        ],
        "docs": "https://github.com/larksuite/lark-openapi-mcp",
    },
```

> 但 lark-mcp 的 `-a` / `-s` 是命令行参数，不是环境变量；仅靠 `env_hints` 提示还不够。
> 想做到真正"点一下就能用"，得在预设里带上 `-a` / `-s` 占位，
> 或者把预设置换成"打开导入框并预填模板"。当前先按 §2.3 方式一手工粘贴最稳。

**安全提醒**：`-s` 写在命令行里，同机其他用户 `ps` 就能看到。所以
① 给 AutoMind 单独建一个飞书应用，别复用主应用；
② 该应用的权限只开必需项；
③ `mcp_servers` 配置存在 `.automind_config.json`（`core/paths.py:55`）里是**明文**，注意文件权限。

---

## 3. 路线 B：飞书机器人 —— 用飞书控制 Agent

### 3.1 为什么一定要用长连接

飞书的事件订阅有两种模式：

| 模式 | 要求 | 适合 |
|---|---|---|
| HTTP 回调 | **公网可达的 HTTPS 地址**（域名 + 证书 + 通常还要备案），需处理 challenge 校验、AES 解密、签名验证 | 已上线的正式服务 |
| **长连接（WebSocket）** | 什么都不用，进程主动外连飞书 | 本机 / 内网 / 开发 |

对"跑在自己电脑上的 AutoMind"来说，长连接是**唯一**现实的选择：
不需要公网 IP、不需要域名备案、不需要内网穿透。
飞书官方文档：[使用长连接接收事件](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case?lang=zh-CN)。

### 3.2 飞书侧配置

在 §2.2 那个应用上继续配（可以复用同一个应用）：

1. **添加「机器人」能力**。
2. **事件与回调 → 订阅方式 → 选「使用长连接接收事件」**。
3. **添加事件：`im.message.receive_v1`（接收消息）**。
4. 权限（按需，控制台会提示具体名称）：
   - 发消息：`im:message`、`im:message:send_as_bot`
   - 收单聊消息：`im:message.p2p_msg:readonly`
   - 收群聊 @ 消息：`im:message.group_at_msg:readonly`
5. **创建版本并发布**（同 §2.2，不发布一切都不生效）。
6. 把机器人拉进目标群，或在单聊里找它。

### 3.3 桥接脚本

仓库里已附一份可直接运行的桥接：**[`integrations/feishu_bridge.py`](../integrations/feishu_bridge.py)**。

```bash
pip install lark-oapi requests

set FEISHU_APP_ID=cli_xxxxxxxxxxxx
set FEISHU_APP_SECRET=<APP_SECRET>
set AUTOMIND_AUTH_TOKEN=<和 AutoMind 一致>
set FEISHU_ALLOWLIST=ou_你的open_id,oc_允许的群chat_id
set FEISHU_DEFAULT_MODE=chat

python integrations/feishu_bridge.py
```

它的数据流：

```
飞书用户发消息
   → 飞书长连接推给桥接进程
   → 去重 + 白名单校验 + 群聊必须 @机器人
   → POST http://127.0.0.1:8765/api/run
        { "task": "...", "interaction": "work", "session_id": "feishu:<chat_id>" }
   → 拿回 record["output"]
   → 调飞书 im/v1/messages 发回原会话
```

几个关键设计点：

- **`session_id` 用飞书会话 id**，这样同一个群/单聊天然是同一段多轮上下文
  （`api_run` 用 `session_id` 取 `_get_session_history()`，见 `server.py:5787`、`5810`）。
- **指令前缀切模式**：`/chat`（默认，纯对话不调工具）、`/work`、`/coding`、`/loop`、`/multi`。
  取值来自 `InteractionMode`（`automind/core/types.py:34`）。
- **白名单 fail-closed**：`FEISHU_ALLOWLIST` 为空时脚本拒绝启动（除非显式 `FEISHU_ALLOW_ALL=1`）。
  理由见 §3.4 —— 能跟机器人说话 = 能在你机器上执行工具。
- **去重**：飞书可能重投事件，按 `header.event_id` 做了有界去重。
- **不阻塞长连接线程**：任务丢进线程池执行。

### 3.4 ⚠️ 审批通道：这条路最容易踩的坑

**结论：用 `POST /api/run` 驱动的任务，遇到需要审批的工具调用会被直接拒绝。**

链路是这样的：

1. 权限判定 `PermissionEngine.check()`（`automind/tools/permissions.py:142`）：
   - `auto`（默认）：只读放行；敏感且 `risk < 60` 放行；`risk ≥ 60` 和高危 → **ASK_USER**
   - `ask`：除只读外**一律** ASK_USER
   - `approve_all`：全部放行
2. 需要审批时走 `AutoMind._request_approval()`（`automind/agent.py:1869`）：
   有 `approval_callback` 就用它，否则交给 `human_loop.request_approval()`。
3. `_cli_ask()` 在非交互环境（服务端没有 tty）**直接拒绝**：
   「无交互终端，无法获得人工批准（按拒绝处理）」（`automind/state/human_loop.py:220`）。

而 `/api/run` 这条 HTTP 路径**从来没有设置过 `agent.approval_callback`** ——
只有 WebSocket 路径会设（`server.py:6853`，并在 `7078` 清掉）。
仓库里那个 `_headless_approval_callback()`（`server.py:6538`）注释写着"给没有 WebSocket 的调用方用"，
但**全仓库没有任何一处调用它**，是死代码。

**三个选项：**

**选项 1：把审批模式设成 `approve_all`** —— 最快，但等于关掉审批，不推荐在能操作文件的机器人上用。

**选项 2：默认 `auto` + 只用 `/chat` 模式** —— 最安全，机器人只聊天不干活。脚本的默认值就是这个。

**选项 3（推荐，真正能用）：把 headless 审批回调接上。**
在 `automind/server.py` 的 `api_run()` 里补两行：

```python
    # 在 session_id = uuid.uuid4().hex[:12] 之后、try: 之前插入
    agent.approval_callback = _headless_approval_callback(chat_sid, session_id, agent)
```

```python
    finally:
        # 与 WS 路径同口径（server.py:7078），避免遗留给后续任务
        agent.approval_callback = None
        _running_tasks["count"] = max(0, _running_tasks["count"] - 1)
        ...
```

> 注意：这条只是把审批"问出去"的通道打开。若既没有界面也没有外部回执通道，
> `make_approval_callback` 依然会当场按拒绝收尾（`server.py:6625`，判据 `_external_approval_available()`）。
> 所以还必须配 `AUTOMIND_WEBHOOKS`（见下），否则补丁等于没打。

### 3.5 在飞书里点「批准」/「拒绝」（Phase 2）

AutoMind 已经有现成的外部审批回执端点 `POST /api/approvals/{approval_id}`（`server.py:6450`），
它就是为"在工单系统 / 群里批"设计的。配合出站 webhook 就能做成飞书卡片审批：

```
需要审批
 → 出站 webhook 推到桥接进程本地监听器
      AUTOMIND_WEBHOOKS=http://127.0.0.1:8766/hook
 → 桥接发飞书交互卡片（工具名 / 参数 / 风险等级 / 倒计时）
 → 用户点「批准」→ 卡片回调事件 card.action.trigger
 → 桥接 POST http://127.0.0.1:8765/api/approvals/<approval_id>
      Header: X-Admin-Token: <AUTOMIND_ADMIN_TOKEN>
      Body:   {"approved": true, "comment": "飞书群内批准",
               "approver": {"user": "ou_xxx", "role": "admin", "team": "default"}}
```

相关配置与语义：

- `AUTOMIND_WEBHOOKS` 目标列表，`AUTOMIND_WEBHOOK_BASE_URL` 用于生成回执地址 ——
  详见 [`docs/WEBHOOKS.md`](./WEBHOOKS.md) 与 `automind/core/webhooks.py`。
- 事件类型 `approval_request` / `approval_resolved` / `approval_timeout`（`WebhookEvent`）。
- 回执必须**及时**：审批超时（默认 300s，`AUTOMIND_APPROVAL_TIMEOUT`）后回执会被
  明确拒绝并返回 `409 approval_stale`（`webhooks.approval_receipt_state`）。
- 回执能批准任意高危工具调用，所以端点挂在管理动作清单里（`server.py:155`）：
  远程必须带 `X-Admin-Token`，本机回环放行。

---

## 4. 安全清单（上线前逐条确认）

- [ ] `AUTOMIND_AUTH_TOKEN` 已配。`launch.bat` 绑 `0.0.0.0`，不配令牌 = 把 Agent 交给整个局域网。
- [ ] `FEISHU_ALLOWLIST` 已配，只放行你自己的 `open_id` 和指定的群。
      **能跟机器人说话 = 能在你机器上执行工具**，这条是本路线最大的风险面。
- [ ] 群聊里要求 @机器人才响应（脚本已实现），避免群里任何人随口一句就跑任务。
- [ ] 桥接进程不要暴露端口、不要做端口转发；它只需要**出站**连飞书。
- [ ] 飞书应用权限按最小化开，`-t` 白名单限制 MCP 工具集。
- [ ] 高危工具保持需要审批（默认 `auto`），并按 §3.5 把审批落到人。
- [ ] 审计留痕：`GET /api/audit`、`GET /api/history`，以及 `webhooks` 的出站事件。
- [ ] 配置目录 `.automind_config.json` 权限收紧（存着 App Secret / API Key 明文）。

---

## 5. 验证步骤

**先验 AutoMind 本身**（绕开飞书，排除变量）：

```bash
curl http://127.0.0.1:8765/api/health

curl -X POST http://127.0.0.1:8765/api/run \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AUTOMIND_AUTH_TOKEN" \
  -d '{"task":"用一句话介绍你自己","interaction":"chat","session_id":"feishu:test"}'
```

返回体里有 `output` / `success` / `tokens` / `duration_ms`（`server.py:5987`）。

**再验 MCP**：

```bash
curl http://127.0.0.1:8765/api/mcp -H "Authorization: Bearer $AUTOMIND_AUTH_TOKEN"
```

`servers[].tools` 里应出现 `mcp__feishu__*`，`error` 为空。

**最后验机器人**：给机器人发「ping」，应回一条消息；再发 `/work 列出当前目录的文件`。

---

## 6. 排错对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| 「未检测到 MCP SDK」 | 没装 Python `mcp` | `pip install mcp` |
| MCP 服务器保存了但连不上 | 命令/参数写错，或 npx 不在 PATH | 看 `POST /api/mcp` 返回的 `error`；先在终端手跑一遍那条 npx |
| 点「一键添加」飞书后没反应 | 内置预设是 sse + 空 url（§2.4） | 用 §2.3 的导入 JSON，或打补丁 |
| 加 MCP 报 403 管理动作 | 远程访问且没带 `AUTOMIND_ADMIN_TOKEN` | 加 `X-Admin-Token`，或从本机操作 |
| 接口 401 | 没带 `AUTOMIND_AUTH_TOKEN` | `Authorization: Bearer ...` |
| 机器人完全没反应 | 应用版本没发布 / 没加 `im.message.receive_v1` / 订阅方式没选长连接 | 回开放平台逐项核对，并看桥接进程日志 |
| 机器人回「任务执行失败」 | LLM 未配置或 Key 无效 | `GET /api/status` 看 provider/model，界面里配 Key |
| 任务说「工具被拒绝」 | §3.4 的审批通道问题 | 用 `/chat` 模式，或按 §3.4/§3.5 补审批 |
| 群聊里没反应 | 没 @机器人 | 正常行为（脚本要求 @） |

---

## 附：相关源码位置

| 功能 | 位置 |
|---|---|
| HTTP 鉴权 / 管理动作 / Host 校验中间件 | `automind/server.py:311` |
| 任务执行入口 `POST /api/run` | `automind/server.py:5776` |
| OpenAI 兼容 `POST /v1/chat/completions` | `automind/server.py:7532` |
| 外部审批回执 `POST /api/approvals/{id}` | `automind/server.py:6450` |
| MCP 管理 REST | `automind/server.py:4006`–`4163` |
| MCP 注册中心（stdio / sse、工具代理） | `automind/tools/mcp_registry.py` |
| MCP 预设 | `automind/tools/mcp_presets.py` |
| 群机器人 Webhook（出站发消息，含飞书） | `automind/tools/collab_tools.py:396` |
| 出站事件与审批回执载荷 | `automind/core/webhooks.py` |
| 权限与审批模式 | `automind/tools/permissions.py:122` |
| 非交互审批 = 拒绝 | `automind/state/human_loop.py:220` |
| 配置文件位置 | `automind/core/paths.py:55` |
