# 出站事件（Webhook）与外部审批回执

> 适用版本：v1.7.3 第 8 项 ｜ 实现：`automind/core/webhooks.py` ｜ 测试：`tests/core/test_webhooks.py`

## 1. 这个功能解决什么

在此之前，AutoMind 的所有状态都只能"人来轮询"：

- **任务跑完了、失败了**，只有 Web 界面上能看到；客户系统（工单、钉钉、飞书、SIEM）拿不到任何信号；
- **审批只能回 AutoMind 的弹窗前面点**。客户要求"在我们的工单系统里批"，以前做不到。

本模块让平台**主动往外说**（出站事件），并允许**外部系统回执审批**（入站回调）。

---

## 2. 快速开始

### 2.1 配置投递目标

配置全部走环境变量，**默认关闭**：不配 `AUTOMIND_WEBHOOKS` 就一个字节都不发、零成本。

```bash
# 写法一：JSON 数组（推荐，可分别指定密钥与订阅的事件）
AUTOMIND_WEBHOOKS='[
  {"url": "https://itsm.example.com/hooks/automind", "secret": "s3cret-1",
   "events": ["task_complete", "task_error", "approval_request"]},
  {"url": "https://siem.example.com/ingest", "secret": "s3cret-2"}
]'

# 写法二：简写 url|secret，逗号分隔（|secret 可省 = 不签名）
AUTOMIND_WEBHOOKS='https://itsm.example.com/hooks/automind|s3cret-1,https://siem.example.com/ingest'
```

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `AUTOMIND_WEBHOOKS` | 空（关闭） | 目标列表，见上 |
| `AUTOMIND_WEBHOOK_TIMEOUT` | `10` | **单次**请求超时（秒） |
| `AUTOMIND_WEBHOOK_RETRIES` | `3` | 失败后的重试次数（不含首次） |
| `AUTOMIND_WEBHOOK_BACKOFF` | `1.0` | 退避基数（秒），第 n 次重试前等 `backoff · 2^n` |
| `AUTOMIND_WEBHOOK_QUEUE_SIZE` | `256` | 有界队列容量，满了丢弃并计数 |
| `AUTOMIND_WEBHOOK_BASE_URL` | 空 | 生成**审批回执地址**用的外部可访问基址，见 §5 |
| `AUTOMIND_WEBHOOKS_ENABLED` | `1` | 设 `0` = 保留配置但停发 |

`events` 省略 = 订阅全部事件。`secret` 省略 = 该目标不签名。

### 2.2 事件类型

| `event` | 触发时机 |
| --- | --- |
| `task_start` | 任务开始执行 |
| `task_complete` | 任务成功结束 |
| `task_error` | 任务失败 |
| `task_cancelled` | 任务被取消/中断 |
| `approval_request` | 需要人工审批（含回执方式说明） |
| `approval_timeout` | 审批等待超时，按配置自动处置 |
| `approval_resolved` | 审批已结束（人工批准/拒绝） |

---

## 3. 请求格式

```
POST /your/endpoint HTTP/1.1
Content-Type: application/json; charset=utf-8
User-Agent: AutoMind-Webhook/1.0
X-AutoMind-Signature: sha256=<hex hmac>
X-AutoMind-Event: task_complete
X-AutoMind-Delivery: 8f3c…（本次投递 uuid，重试时保持不变）
X-AutoMind-Schema: 1
```

> **⚠️ 头名请按大小写不敏感处理。** HTTP 规定头名不区分大小写（RFC 9110），
> 且 CPython 的 `http.client` 在发送时会把 `X-AutoMind-Signature` 规范成
> `X-Automind-Signature`。**不要用裸字典精确匹配头名** —— 用框架提供的头集合
> （Python `email.Message` / FastAPI `request.headers` / Node `req.headers`
> 都是大小写不敏感的）。

### 3.1 载荷（body）

```json
{
  "schema": "1",
  "event": "task_complete",
  "event_id": "9d0f…",
  "delivery_id": "8f3c…",
  "timestamp": 1789648692.402,
  "timestamp_iso": "2026-09-17T12:38:12Z",
  "session_id": "sess-1",
  "task": "整理季度报表",
  "status": "ok",
  "elapsed_ms": 3200,
  "tokens": {"prompt": 100, "completion": 50, "total": 150}
}
```

审批类事件额外带 `approval` 子对象：

```json
{
  "schema": "1",
  "event": "approval_request",
  "session_id": "sess-1",
  "task": "删除过期日志",
  "tool": "terminal",
  "tier": "danger",
  "approval": {
    "approval_id": "a1b2c3d4e5",
    "tool": "terminal",
    "tier": "danger",
    "reason": "将执行 rm -rf /var/log/*.gz",
    "arguments_summary": {"command": "rm -rf /var/log/*.gz", "timeout": "30"},
    "callback_url": "https://automind.example.com/api/approvals/a1b2c3d4e5",
    "callback_method": "POST",
    "callback_body": {"approved": "bool", "comment": "str", "arguments": "object|null"},
    "callback_auth": "Header: X-Admin-Token: <管理员令牌>",
    "callback_note": "回执只对**未决**审批生效；审批已超时/已结束时，端点会返回 approval_stale 明确拒绝，请勿重试。",
    "timeout_s": 300.0,
    "on_timeout": "reject"
  }
}
```

字段约定：

- **`schema`**：载荷结构版本，当前为 `"1"`。请按它做兼容分支；字段只增不改语义。
- **`delivery_id`**：投递 id，**重试时保持不变** —— 请以它做幂等去重（同一条事件可能收到多次）。
- **`event_id`**：事件自身 id，同一次业务事件只生成一次。
- **`elapsed_ms`** / **`tokens`**：拿不到就不出现（不会发 `0` 或空对象来误导）。
- **`arguments_summary`**：工具参数的**摘要**，不是原文 —— 值被截断到 200 字符、
  最多 20 个键，且密钥类字段已打码。**完整参数不会外发**。

### 3.2 响应约定

- **2xx** = 投递成功。其它状态码按下面的规则处理：
  - `4xx`（除 `408`/`429`）= **永久失败，不重试**（明确拒绝你的地址，重试没有意义）；
  - `5xx`、`408`、`429`、连接失败/超时 = **可重试**，按 `backoff · 2^n` 退避；
- 重试耗尽后计入 `failed`，**不影响任务本身**（投递失败绝不会让任务失败）。
- 建议在 1 秒内返回；慢响应会占用队列名额。

---

## 4. 安全

### 4.1 验签（强烈建议开启）

配置了 `secret` 时，每个请求都带 `X-AutoMind-Signature: sha256=<hmac>`，
其中 `<hmac>` = `HMAC-SHA256(secret, 原始 body 字节)`。

**必须对"原始 body 字节"验签**，不要对反序列化后再序列化的结果验签 ——
重新序列化几乎一定会改变字节（空格、键序、转义、Unicode），签名就再也对不上了。

Python（标准库，无第三方依赖）：

```python
import hashlib, hmac

SECRET = b"s3cret-1"          # 与 AUTOMIND_WEBHOOKS 里配的一致

def verify(raw_body: bytes, signature_header: str) -> bool:
    """raw_body 必须是未解析的原始请求体；signature_header 形如 'sha256=…'"""
    expected = hmac.new(SECRET, raw_body, hashlib.sha256).hexdigest()
    got = (signature_header or "").strip()
    if got.lower().startswith("sha256="):
        got = got[len("sha256="):]
    # 必须用 compare_digest（常量时间比较），不要用 ==（可被计时侧信道逐字节猜）
    return hmac.compare_digest(expected, got)

# Flask 示例
# raw = request.get_data()                       # 原始字节
# ok = verify(raw, request.headers.get("X-AutoMind-Signature", ""))
#
# FastAPI 示例（务必用 await request.body() 拿原始字节）
# raw = await request.body()
# ok = verify(raw, request.headers.get("x-automind-signature", ""))
```

Node（Express）：

```js
const crypto = require("crypto");

const SECRET = "s3cret-1";

function verify(rawBody, signatureHeader) {
  // rawBody 必须是 Buffer（app.use(express.json({ verify: (req, res, buf) => { req.rawBody = buf; } }))）
  const expected = crypto.createHmac("sha256", SECRET).update(rawBody).digest("hex");
  const got = String(signatureHeader || "").replace(/^sha256=/i, "").trim();
  const a = Buffer.from(expected, "utf8");
  const b = Buffer.from(got, "utf8");
  // 长度不等时 timingSafeEqual 会抛错，先判长度
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

// app.post("/hook", (req, res) => {
//   if (!verify(req.rawBody, req.get("X-AutoMind-Signature"))) return res.status(401).end();
//   ...
// });
```

未配置 `secret` 的目标**不会发送** `X-AutoMind-Signature` 头（不会发一个假的签名）。
此时建议改用 mTLS / 来源 IP 白名单 / 内网隔离来做鉴权。

### 4.2 只发摘要，不发全文

webhook 目标常常在**公网或第三方 SaaS**：载荷里的任务描述、工具参数都做了截断，
密钥类内容会打码（复用 `automind/core/redact.py`）。但请注意：

> **任务描述（`task`）本身仍会外发**（截断到 500 字符）。如果任务描述里可能包含
> 业务敏感信息，请把目标配到**内网地址**，或只订阅必要的事件类型。

**内网地址请显式配置**：`http://127.0.0.1:…`、`http://10.0.0.5:…` 都是合法目标，
不会被自动排除；平台只做协议白名单（仅允许 `http`/`https`，拒绝 `file:`/`ftp:` 等，
避免一个配错的 URL 变成任意本地文件读取）。

---

## 5. 外部审批回执

### 5.1 接线（部署方 / 集成方）

要让客户在**自己的工单系统里**批准，需要：

1. **配置外部可访问基址**（反代/网关之后的地址，进程自己只知道 `127.0.0.1`）：

   ```bash
   AUTOMIND_WEBHOOK_BASE_URL=https://automind.example.com
   ```

   配好后，`approval_request` 事件的 `approval.callback_url` 就是
   `https://automind.example.com/api/approvals/<approval_id>`；没配就为空串
   （宁可为空，也不给一个连不上的错地址）。

2. **配置管理员令牌**（服务端已有的管理员令牌机制）。回执端点必须校验它。

### 5.2 回执请求

```bash
curl -X POST https://automind.example.com/api/approvals/a1b2c3d4e5 \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Token: <管理员令牌>' \
  -d '{"approved": true, "comment": "工单 #42 已审批通过"}'
```

「改参数后批准」再带上 `arguments`（会**整体替换**原工具参数）：

```bash
curl -X POST https://automind.example.com/api/approvals/a1b2c3d4e5 \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Token: <管理员令牌>' \
  -d '{"approved": true, "comment": "缩小删除范围", "arguments": {"command": "rm -rf /var/log/app/*.gz"}}'
```

响应：

| 情况 | HTTP | 响应体 |
| --- | --- | --- |
| 受理 | `200` | `{"ok": true, "approval_id": "…", "approved": true, "modified": false}` |
| 审批已结束（**迟到回执**） | `409` | `{"ok": false, "type": "approval_stale", "message": "…已结束…不再生效"}` |
| 令牌不对/未配置 | `401` | `{"ok": false, "error": "unauthorized"}` |
| 审批不存在 | `404` | `{"ok": false, "error": "not_found"}` |

### 5.3 两条必须遵守的规则

1. **必须带管理员令牌。** 这个端点能批准任意高风险工具调用（删库、改文件、跑命令），
   **绝不能**因为"管理员忘了配令牌"就变成公网上的开放审批后门：
   没配令牌时一律拒绝（fail-closed），而不是放行。

2. **迟到回执必须被明确拒绝，不能静默丢弃。**
   在工单系统里点了「批准」的人，如果什么反馈都没有，会合理地认为"批过了"；
   而真实情况可能是这次审批早已超时（按默认 `on_timeout: reject`）处理、任务已经失败。
   静默丢弃等于制造一个**没人知道的错误结论**。
   因此端点返回 `409` + `type: "approval_stale"`，与 Web 界面上的
   `approval_stale` 提示语义一致。

### 5.4 语义细节

- **回执只对未决审批生效**：审批一旦超时/已处理/任务中断，回执一律 `409`。
- **拒绝时 `arguments` 会被忽略**（即使传了）。否则"拒绝 + 参数"在某些调用点上
  会被误读成"改完参数批准了" —— 那是把拒绝当成了批准。
- **字符串布尔会被正确解释**：`"true"`/`"1"`/`"yes"`/`"批准"` → 批准；
  `"false"`/`"0"`/`"no"`/缺字段/`""` → 拒绝（`bool("false")` 在 Python 里是**真**，
  不显式归一化就会把拒绝读成批准，所以这条由服务端统一收口）。
- **`approved` 为 `true` 但没有 `arguments`** = 按原参数批准。
- 审批超时后的处置由 `execution.approval_timeout_action` 决定（默认 `reject`），
  载荷里的 `approval.on_timeout` 会如实告知外部系统当前配置。

---

## 6. 可观测性与排障

投递统计可并入 `/metrics`（键名见 `WebhookDispatcher.stats()`，前缀建议 `automind_webhook_`）：

| 指标 | 含义 | 异常判读 |
| --- | --- | --- |
| `enabled` | 是否真的会投递 | `false` = 没配目标或被 `AUTOMIND_WEBHOOKS_ENABLED=0` 关掉 |
| `targets` | 目标数量 | 与配置条数不一致 → 有目标因 URL 非法被拒（日志 `webhook_target_rejected`） |
| `queue_depth` / `queue_size` | 当前队列深度 / 容量 | 持续贴近上限 → 对端太慢或挂了，事件正在被丢弃 |
| `queued` | 入队事件数 | |
| `delivered` | 至少成功一次的事件数 | |
| `failed` | 重试耗尽后彻底失败数 | **>0 必须告警**，否则通知静默丢失 |
| `dropped` | 队列满被丢弃数 | **>0 说明已经在丢事件**，需检查对端或调大 `QUEUE_SIZE` |
| `retried` | 累计重试次数 | 持续增长 = 对端不稳定 |
| `skipped` | 无目标/被关闭时跳过数 | |
| `rejected` | 入队后订阅关系变化导致的跳过数 | 正常恒为 0，非 0 属异常信号 |
| `last_error` | 最近一次失败说明 | 直接给出 `事件 → URL：HTTP <码> <响应>` |
| `last_success_at` | 最近一次成功时间戳 | 长期不更新但 `enabled=true` → 投递链路有问题 |

日志事件名（结构化日志，可直接做告警规则）：

- `webhook_queue_full` —— **队列满丢弃**（含 `evt` / `url` / `queue_size`）
- `webhook_delivery_failed` —— 投递失败（含 `status` / `attempts` / `permanent`）
- `webhook_target_rejected` —— 配置里的 URL 被协议白名单拒绝
- `webhook_config_invalid` —— 配置解析失败或数值非法（回落默认值）
- `webhook_payload_encode_failed` —— 载荷无法序列化（属事件本身的问题，不重试）

### 队列满了会怎样

**丢弃 + 计数 + 记 warning**，不会静默丢、也不会无限增长把内存吃光。
代价是这些事件**不会补发** —— 发现 `dropped > 0` 就应当处理对端或调大队列。

---

## 7. 行为契约（可依赖的保证）

1. **默认关闭**：没配 `AUTOMIND_WEBHOOKS` → 完全零成本、零网络行为。
2. **绝不阻塞、绝不影响任务**：投递在后台协程里进行；投递失败、对端超时、
   载荷序列化失败都只记账 + 记日志，**不会**让任务失败或变慢。
3. **有界**：队列容量固定，满了丢弃并计数。
4. **单次超时**：每个请求都有独立超时（默认 10 秒），不会挂死。
5. **指数退避**：可重试错误按 `backoff · 2^n` 退避，次数可配；`4xx` 不重试。
6. **协议白名单**：只允许 `http`/`https`。
7. **只发摘要**：任务描述与工具参数截断、密钥打码；完整参数不外发。
8. **审批回执 fail-closed**：没配令牌不放行；拒绝不带参数；迟到回执明确拒绝。

---

## 8. 集成示例：在工单系统里审批

```python
# 客户侧：收到 approval_request 就在工单系统里建单
@app.post("/automind-webhook")
async def automind_webhook(request: Request):
    raw = await request.body()
    if not verify(raw, request.headers.get("x-automind-signature", "")):
        raise HTTPException(401)
    data = json.loads(raw)
    if data["event"] == "approval_request":
        a = data["approval"]
        ticket = itsm.create_ticket(
            title=f"待审批：{a['tool']}（{data['task']}）",
            body=(f"原因：{a['reason']}\n参数：{a['arguments_summary']}\n"
                  f"超时：{a.get('timeout_s')}s 后按 {a.get('on_timeout')} 处理"),
            approve_url=f"{a['callback_url']}?approved=true",   # 交给工单系统的按钮
            deny_url=f"{a['callback_url']}?approved=false",        )
    elif data["event"] in ("task_complete", "task_error"):
        itsm.comment_ticket(data["session_id"], f"任务{data['status']}：{data['task']}")
    return {"ok": True}
```

工单系统的「批准」按钮最终发出的是 §5.2 里那个带 `X-Admin-Token` 的 POST 请求。

> 上例里的 `?...&approved=true` 只是**你们自己工单系统**的按钮链接参数，用于让
> 工单系统在用户点击后去调回执接口；AutoMind 的回执端点只认 **POST 的 JSON body**
> （`{"approved": true}`），不解析查询串。别把查询串直接转发成 GET —— 端点不接受 GET。

---

## 9. 常见问题

**Q：配了目标但一条都没收到？**
按顺序查：① `AUTOMIND_WEBHOOKS_ENABLED` 是不是 `0`；② 目标 URL 是否被协议白名单
拒了（看日志 `webhook_target_rejected`）；③ 该目标的 `events` 是否包含你期待的事件；
④ `/metrics` 里 `delivered` / `failed` / `dropped` 分别是什么。

**Q：`failed` 一直在涨，`last_error` 显示 `HTTP 401`？**
对端拒绝了我们。确认对方是否需要签名（配 `secret`）或 IP 白名单。

**Q：`dropped` 在涨？**
队列满了，说明对端处理不过来（或已挂）。先修对端，再考虑调大
`AUTOMIND_WEBHOOK_QUEUE_SIZE` —— 单纯调大队列只是把"丢事件"推迟成"更晚丢事件"。

**Q：同一个事件收到了两次？**
重试机制的正常表现（对端返回了非 2xx 或响应超时，我们会重发）。请用
`delivery_id` 做幂等去重 —— 重试时它保持不变。

**Q：能只发某几种事件吗？**
能。在 JSON 写法里给目标加 `"events": ["task_complete", "approval_request"]`。
被过滤掉的事件**不会入队**，不占队列名额。

**Q：审批回执返回 409？**
审计过了：该审批已结束（超时/已处理/任务中断）。这是**有意的明确拒绝**，
不是故障；请看 §5.3 第 2 条。
