# 内网 / 离线部署设计 —— 「Agent 不联网，但机器人要联网」怎么落

> 场景：AutoMind 部署在没有外网（或只有受控外网）的内网机器上，
> 但 IM 机器人（飞书 / 钉钉 / 企业微信 / 微信）必须连公网才能收发消息。
>
> 本文给出的不是"要不要联网"的取舍，而是**把矛盾拆成可分别处置的链路**，
> 再逐条给出离线替代或受控通道。所有结论都对到本仓库的具体实现，
> 标注 `文件:行`，便于评审与改造。

---

## 0. 先拆链路：真正"必须出网"的只有两条

把"联网"当一件事看，就只能在"安全"和"能用"之间二选一。拆成链路就各有解法：

| # | 链路 | 方向 | 目标 | 是否真的必须 | 离线 / 受控替代 |
|---|---|---|---|---|---|
| ① | 模型推理 | 出站 | 云端 LLM API | 不一定 | 本地 Ollama（`core/llm.py:1002` 默认 `127.0.0.1:11434`）、内网 vLLM、内网模型网关 |
| ② | **机器人收消息** | **入站** | 平台把事件 POST 到本机 | **否** | **长连接（本进程主动出站）** —— `integrations/feishu_bridge.py` |
| ③ | 机器人发消息 | 出站 | 平台 OpenAPI | 是（用 IM 就必须） | 无替代；只能收敛到白名单出口 |
| ④ | 依赖 / 更新 | 出站 | pypi / npm / GitHub | 部署期需要，运行期不必 | 离线 wheel + 内网镜像；`updater` 可关 |
| ⑤ | 检索 / 知识库 | 出站 | embedding / 搜索 API | 否 | 特征哈希离线 embedding（`rag/kb.py:5`、`memory/long_term.py:199`）、自建 SearXNG |
| ⑥ | 许可证激活 | 双向 | 激活服务器 | 否 | **离线 Ed25519 许可证 v2**（`AMP2-…`，客户机只带公钥、不联网验签）；发行方用 `pro/tools/issue_license.py bundle` 出 `.ampkg`，客户拖进应用内「⚡ 一键升级 / 激活」面板完成装包+激活 —— 见 `docs/COMMERCIAL_ACTIVATION.md` |

**结论**：必须出网的只剩 **③（IM 出站）** 和 **①（若不用本地模型）**；
最容易致命、也最容易消掉的是 **②** —— 一旦选了 HTTP 回调模式，
内网机器就必须**对公网开放一个端口**，那才是"不能联网"与"机器人要用"真正冲突的地方。

> 判据：`docs/FEISHU.md:183` 那张表已经把这件事说清楚了 ——
> HTTP 回调要"公网可达的 HTTPS + 域名 + 证书 + 通常还要备案"，
> 长连接"什么都不用，进程主动外连"。

---

## 1. 五条设计原则

1. **入站为零（No Inbound）**：内网机器不对公网开任何端口。机器人靠长连接 / 轮询接入。
2. **出站白名单（Egress Allowlist）**：只允许到 N 个 `域名:443`，且必经统一正向代理；其余一律拒绝并给出可解释错误。
3. **出网收敛到网关（Gateway is the only egress）**：鉴权、审计、脱敏、限流、缓存、成本核算都在网关；agent 侧不持有上游 Key。
4. **能力可降级（Graceful Degradation）**：单条链路断掉时功能降级 + 明确提示，而不是整任务失败。
5. **离线优先（Offline-first）**：默认配置下不联网也能启动、能跑工作流、能给出"缺什么"的准确提示。

---

## 2. 四种部署形态（选型表）

| 形态 | 拓扑 | 适用 | AutoMind 侧要点 | 代价 |
|---|---|---|---|---|
| **A 全离线闭环** | 本机模型 + 无 IM（只用 Web UI / CLI） | 涉密、单机、演示 | `provider=ollama` + `api_base=http://127.0.0.1:11434`；关更新检查 | 机器人不可用 |
| **B 单向出网 + 白名单**（推荐起点） | 内网机 → 正向代理（域名白名单）→ 公网 | 大多数"内网办公 + 要 IM" | 代理环境变量 + `api_base` 指向内网网关 + 飞书长连接桥接 | 需运维一台出网口 |
| **C DMZ 双面网关**（推荐终态） | 内网 agent ↔ DMZ relay（OpenAI 兼容 + 消息队列）↔ 公网 | 多台 agent、要审计与配额 | agent 只认内网 relay 地址；IM 长连接由 relay 托管 | 要写/维护 relay |
| **D 反向隧道** | 公网入口 → frp/ngrok/Tailscale → 内网 8765 | **仅**当平台只能用 HTTP 回调 | 只暴露 `/hook/*`；必须登记 `AUTOMIND_TRUSTED_HOSTS`；加令牌 + 源 IP 限制 | 攻击面最大，能不用就不用 |

选型建议：**先 B，后 C**。A 是它们的降级态（出网口断掉时的行为），D 是最后的兜底。

---

## 3. 形态 C 参考架构

```
┌──────────── 内网（无外网出口，仅允许到 DMZ）────────────┐
│                                                          │
│  AutoMind 服务        127.0.0.1:8765（回环绑定 + 双令牌） │
│    ├─ Web UI / API      localhost 访问                    │
│    ├─ 本地模型 / vLLM   127.0.0.1:11434（可选）           │
│    ├─ 连接器            ~/.automind/connectors/*.py ──► 内网系统（工单/ERP）
│    └─ 桥接进程（出站长连接）
│                                                          │
└───────────┬──────────────────────────┬───────────────────┘
            │ ① 模型：HTTPS 到内网 relay │ ② IM：出站 WSS
            ▼                          ▼
┌──────────────── DMZ relay / 出网网关 ────────────────┐
│  · 域名白名单（api.deepseek.com / open.feishu.cn …） │
│  · 上游 Key 只存在这里；agent 侧只有 relay token      │
│  · 全量出网审计、脱敏、限流、配额、成本核算           │
│  · OpenAI 兼容端点 + IM 长连接托管 + 消息队列         │
└───────────────────────┬──────────────────────────────┘
                        ▼  公网
        LLM API / 飞书（长连接）/ 钉钉 Stream / 企微
```

两条数据流互不依赖：模型断了机器人还能回"模型不可用"，
机器人断了本地 Web UI 照常工作 —— 这是形态 C 相对"直接给出网口"的核心收益。

---

## 4. 落到本仓库的具体配置

| 目标 | 配置 / 做法 | 代码锚点 |
|---|---|---|
| 只服务本机 | `--host 127.0.0.1`；**不要**照抄 `launch.bat:32` 的 `0.0.0.0` | `core/http_guard.py:53` |
| 接口鉴权 | `AUTOMIND_AUTH_TOKEN`；管理动作另加 `AUTOMIND_ADMIN_TOKEN` | `server.py:55`、`server.py:125` |
| 反代 / 内网域名访问 | `AUTOMIND_TRUSTED_HOSTS=automind.corp.local`（否则 403，防 DNS 重绑定） | `core/http_guard.py:110` |
| 模型走内网网关 | `provider=custom` + `api_base=http://relay.corp.local/v1`（仅接受合法 URL） | `core/llm.py:578`、`server_store.py:105` |
| 机器人接入（推荐） | 出站长连接（服务进程托管，只做出站、不监听端口）：飞书 / 钉钉 Stream / 企微智能机器人；在设置 →「🤖 机器人」里选「长连接（推荐）」 | `bots/longconn.py`、`docs/IM_BOTS.md:20` |
| 机器人接入（回调模式） | 需 `public_base_url` + hook token，**必须**公网可达 | `bots/manager.py:55`、`server.py:325` |
| 审批落到人 | 机器人在聊天里审批（`/work` 模式）；或出站 webhook 到本机监听器 | `bots/manager.py:19`、`core/webhooks.py:326` |
| 出站事件到内网收集器 | `AUTOMIND_WEBHOOKS=http://127.0.0.1:8766/hook`（本机/内网直连，不走代理） | `core/webhooks.py:462` |
| 依赖镜像 / 断网退避 | `AUTOMIND_PIP_MIRROR` / `AUTOMIND_NPM_MIRROR` / `AUTOMIND_NET_MIRROR=0` | `core/netresilience.py` |
| 出网总闸 / 白名单 / 审计 | `AUTOMIND_OFFLINE=1` 或 `AUTOMIND_NETWORK_POLICY=offline\|allowlist\|open`；`AUTOMIND_EGRESS_ALLOW` / `AUTOMIND_EGRESS_ALLOW_FILE` / `AUTOMIND_EGRESS_DENY`；审计 `<数据目录>/logs/egress.log`（`AUTOMIND_EGRESS_AUDIT=0` 可关） | `core/offline.py`、`tools/net_tools.py`、`tools/browser.py` |
| 探测目标可配置 | `AUTOMIND_PROBE_HOSTS`（公网）/ `AUTOMIND_GATEWAY_PROBE`（网关）/ `AUTOMIND_LOCAL_PROBES`（本地服务）；三态见 `net_check` | `core/netresilience.py`、`builtin_plugins/net_resilience/hooks.py` |
| 关掉更新检查 | offline / allowlist 下 `updater.check()` 自动短路（不发请求，返回 `skipped=True` / "离线模式已跳过更新检查"） | `core/updater.py` |
| 内网搜索 | 自建 SearXNG + `AUTOMIND_SEARCH_PROVIDER=searxng` + `AUTOMIND_SEARCH_ENDPOINT`（内网目标在出网策略里默认放行） | `tools/net_tools.py` |
| 访问内网系统 | 连接器（`~/.automind/connectors/*.py`）优于 `http_request allow_private=true` | `docs/CONNECTORS.md:12`、`tools/net_tools.py` |
| 数据 / 工作区 | `AUTOMIND_DATA_DIR`、`AUTOMIND_FS_ROOTS`、工作区隔离 | `core/paths.py:47`、`server.py:2032` |
| 脱敏与留痕 | `AUTOMIND_REDACT_SECRETS=1`、`/api/audit`、`AUTOMIND_TRACE` / `AUTOMIND_REPLAY`；出网留痕见 `egress.log` | `server.py:87`、`core/trace.py:78`、`core/offline.py` |
| 许可（无需联网） | **离线 Ed25519 许可证 v2**：客户机只内置验签公钥（私钥不随包），完全断网也能验签激活；旧 v1 `AMP-…`（对称 HMAC）默认拒绝。升级包 `.ampkg`（wheel + 许可证 + 清单）用 `python pro/tools/issue_license.py bundle` 生成，客户拖进应用内「⚡ 一键升级 / 激活」面板即可装包+激活；扩展包也可放 `<数据目录>/extensions/` | `docs/COMMERCIAL_ACTIVATION.md`、`docs/EDITIONS.md:85`、`pro/tools/issue_license.py` |
| 离线安装 / 备份 / 回滚 / 验收 | `scripts\offline\install.ps1`（离线 wheel 安装 + 写策略 + 可选自启）→ `backup.ps1`（zip + SHA-256 清单 + 保留策略）→ `rollback.ps1`（先备份当前状态再原子替换）→ `verify.ps1`（一键验收自检）；全程 PS 5.1、支持 `-WhatIf` | `scripts/offline/`、`docs/OFFLINE_OPS.md` |

---

## 5. 缺口清单：已全部实现（v1.11.14）

> **状态（v1.11.14）：本节 6 条缺口已全部落地为可验证实现。** 每条给出环境变量 /
> 文件锚点；客户验收直接跑 `scripts\offline\verify.ps1`（见第 7 节），
> 运维步骤见 `docs/OFFLINE_OPS.md`。

1. **离线总闸 —— 已实现（v1.11.14）**。
   `AUTOMIND_OFFLINE=1` 等价 offline；`AUTOMIND_NETWORK_POLICY=open|allowlist|offline`
   为显式开关（优先级：显式 policy > OFFLINE 开关 > 默认 open；非法值 fail-closed 按 offline）。
   出站工具（`http_request` / `web_fetch` / `web_search` / `browser`）在非白名单目标上
   **当场返回失败结果**（不发起连接、不先超时 30 秒），错误信息含"当前策略 + 怎么放行"。
   - 实现：`automind/core/offline.py`（唯一判定出口：`policy_status` / `check_egress` /
     `is_offline` / `is_allowlist`）；`tools/_toolkit.py`（`check_url` 叠加策略层，
     SSRF 与出网策略两层独立）、`tools/net_tools.py`、`tools/browser.py`；
     `core/updater.py` 在 offline / allowlist 下短路更新检查。
   - 边界：应用层拦不住 `terminal` 里的 `curl`、连接器与 MCP —— 网络层（正向代理 /
     防火墙 / DNS）仍需同步配置白名单，两层叠加才叫设计（与 10.2 节一致）。

2. **`net_check` 误报 —— 已实现（v1.11.14）**。
   探测目标不再硬编码公网：`AUTOMIND_PROBE_HOSTS`（格式 `host:port,host:port`）覆盖公网
   探测，`AUTOMIND_GATEWAY_PROBE`（可选）探测模型网关 / relay，`AUTOMIND_LOCAL_PROBES`
   （可选）探测本地服务。结果三态：`online` / `expected_offline`（离线策略下公网不可达
   **是预期**）/ `degraded`（网关或本地服务不可达 = 真故障）。
   离线模式下 `network_up()` **不发起公网探测**、立即返回 expected_offline；
   `ResilientDispatch` 不再"等待网络恢复 60s × 3 次"空转。
   - 实现：`automind/core/netresilience.py`（`probe_hosts` / `gateway_probe` /
     `local_probes` / `classify_state` / `network_state` / `network_up`）、
     `automind/builtin_plugins/net_resilience/hooks.py`（net_check 三段式 + `policy` 字段）。

3. **出向白名单 + 出网审计 —— 已实现（v1.11.14）**。
   `AUTOMIND_EGRESS_ALLOW`（逗号 / 分号分隔；支持 `host`、`host:port`、`*.example.com`
   通配、IP / CIDR）可叠加 `AUTOMIND_EGRESS_ALLOW_FILE`（每行一条，# 注释；
   不分大小写），`AUTOMIND_EGRESS_DENY` 为可选拒绝列表（优先级最高）。
   与 SSRF 防护**两层独立**：`allow_private=true` 只放行私网校验，**不会**顺手关掉
   出网白名单；反过来白名单命中也不能绕过私网防护。重定向与搜索后端逐跳校验。
   审计：每次放行 / 拒绝写 `<数据目录>/logs/egress.log`（JSONL，
   `AUTOMIND_EGRESS_AUDIT=0` 可关），只记 `scheme://host:port/path` 前 200 字符，
   **query 与 userinfo 不落盘**（避免把 `?api_key=…` 抄进日志）。
   - 实现：`automind/core/offline.py`（`check_egress` / `sanitize_target` / 审计）、
     `tools/net_tools.py`（含每一跳重定向）、`tools/browser.py`（WebFetch 补上 SSRF +
     策略；Browser 导航接入策略）、`tools/_toolkit.py`。
   - 边界：白名单是**协作层**，强制层仍在代理 / 防火墙（同上）。

4. **长连接渠道 —— 已实现（v1.11.7 起，v1.11.14 文档同步）**。
   免公网入站的一等公民渠道已经落地：`automind/bots/longconn.py` 由服务进程托管出站
   长连接（飞书 / 钉钉 Stream / 企微智能机器人），断线按 1s→60s 指数退避重连，
   不监听任何端口；回调渠道继续保留。配置与排障见 `docs/IM_BOTS.md`。
   - 实现：`automind/bots/longconn.py`、`automind/bots/manager.py`、
     `automind/bots/channels.py`。

5. **运行期主动出网已收敛 —— 已实现（v1.11.14）**。
   更新检查在 offline / allowlist 下**不发任何请求**（`updater.check()` 返回
   `skipped=True`，理由"离线模式已跳过更新检查"；`apply_update()` 同样短路）；
   Monaco 编辑器已有本地回退。
   MCP 的 `npx` 首次拉包属于第三方进程行为，应用层无法可靠拦截 —— 离线交付时请把
   MCP server 依赖预置到内网镜像 / vendor 目录，并在网络层阻断。这是**已知边界**，
   不要把它当成"应用层已解决"。
   - 实现：`automind/core/updater.py`（`check` / `apply_update` 短路）。

6. **统一诊断 —— 已实现（v1.11.14）**。
   `net_check` 输出三段式（本地服务 / 网关 / 公网）+ 三态结论 + `policy` 字段，
   保留逐目标 DNS / 可达明细；`/api/health` 新增 `network_policy`（当前策略摘要）与
   `network`（三态快照；缓存 + 后台刷新，不拖慢探针）。运维一条命令看清断在哪。
   - 实现：`automind/core/netresilience.py`（`network_state` /
     `network_state_cached`）、`automind/server.py`（`/api/health`）、
     `automind/builtin_plugins/net_resilience/hooks.py`（net_check 输出）。

---

## 6. 离线部署特有的三个安全风险

1. **"隔离"是假象**。只要有一个出网口，被网页/文档内容注入的 agent 就可能把内网数据带出去
   （`tools/net_tools.py` 模块注释描述的就是这个攻击链）。
   → 出网白名单 + `pii_guard`/脱敏插件 + 出网审计 + 按权限档位区分"能读内网"和"能出公网"的工具。
2. **内网暴露面比公网更危险**（同网段可信度错觉）。`launch.bat:32` 绑 `0.0.0.0`
   且未配令牌时，**内网任何人都能驱动 agent 在本机执行工具**
   （`docs/FEISHU.md:34` 已警告）。
   → 回环绑定 + 双令牌 + `AUTOMIND_TRUSTED_HOSTS` 白名单 + 反代层源 IP 限制 + `AUTOMIND_RATE_LIMIT`。
3. **凭据散落**。`.automind_config.json` 明文存 API Key / AppSecret
   （`docs/FEISHU.md:171`）。
   → 文件权限收紧；上游 Key 只留在网关，agent 侧只持有网关 token（企业版
   `model_gateway` 的"api_base 收敛 + 模型白名单"就是干这个的，见 `docs/EDITIONS.md:58`）。

---

## 7. 验收：怎么证明"离线可用"且"出网受控"

| 验收项 | 做法 | 期望 |
|---|---|---|
| 离线可启动 | 拔外网，冷启动服务 | 秒级就绪，无阻塞式重试，日志说明"离线模式" |
| 离线可干活 | `python -m automind.eval run automind/eval/suites/smoke.yml --dry-run`（`eval/executors.py:183` 明确支持无 Key）；工作流 `examples/06-workflow/hello.yaml` | 全绿，零网络访问 |
| 出网最小化 | 网关侧全量日志，跑一轮真实任务后导出域名清单 | 收敛到 ≤ 5 个域名，且都是白名单内 |
| 降级正确 | 关掉 relay | 界面/机器人回"网关不可达"，而非"任务失败/工具坏了" |
| 长连接韧性 | 网关重启 / 弱网 30 秒 | 桥接自动重连，消息不丢（去重仍生效） |
| 入站为零 | 从公网扫内网机端口 | 无监听、无映射、无隧道 |
| 诊断一致 | 离线状态下调用 `net_check` | 结论是"公网不可达（预期，不是故障）"，不诱导模型反复重试；输出含 `policy` 字段 |
| 策略生效（机器可读） | `python scripts\offline\smoke_offline.py --json`（`verify.ps1` 内部调用） | `policy.mode=offline`、`network.state=expected_offline`、`updater.skipped=true`、公网出网被拒绝 |
| **一键验收** | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\offline\verify.ps1 -ExpectMode offline` | 全部 PASS、退出码 0（失败逐项给修复建议）；逐条检查项见 `docs/OFFLINE_OPS.md` 第 9 节 |
| 备份可回滚 | `backup.ps1` → 修改数据 → `rollback.ps1 -Yes` | SHA-256 校验通过、数据恢复、回滚前状态另存一份 |

---

## 8. 分阶段落地

- **P0（约 1 天，纯配置）**：回环绑定 + 双令牌 + 关更新检查 + 离线 wheel/依赖预置。
  目标：内网能跑、不误报、不被同网段驱动。
- **P1（已完成，v1.11.7 起）**：`api_base` 指内网网关（或本地 Ollama）+ 出站长连接渠道
  （`automind/bots/longconn.py`）+ 聊天内审批。目标：机器人在无入站条件下真正可用。
- **P2（已完成，v1.11.14）**：`AUTOMIND_OFFLINE` 总闸 + 出网白名单 + `net_resilience`
  探测改造（三态）+ 出网审计 + 安装/备份/回滚/验收脚本（`scripts/offline/`与
  `docs/OFFLINE_OPS.md`）。目标：出网受控且可证明。
- **P3**：DMZ relay（OpenAI 兼容 + 长连接托管 + 队列）+ 多租户配额。
  目标：从"一台机器"扩到"一个机房"。

---

## 9. 如果"机器人"指的是硬件机器人

若场景是产线 / 服务机器人（机械臂、AGV、人形）本身需要联网，而 AutoMind 跑在离线工控机上，
则角色要反过来设计：

- **AutoMind = 离线大脑**：通过内网 / 串口 / CAN / ROS2 控制设备，控制面**永不出网**；
- **机器人上云走它自己的通道**（遥测、OTA、云平台），与 agent 的出网通道**物理或逻辑隔离**，
  不要让 agent 持有设备云平台凭据；
- **agent 只经设备网关间接控制**（MQTT / OPC-UA broker），不直连执行器；
- **安全动作本地确定性执行**：急停、限位、轨迹下发不能依赖云往返；
- 若 agent 需要读云端状态（设备清单、OTA 记录），仍按第 1–2 节设计：**只读 + 白名单 + 脱敏**。

> 判断标准只有一条：**"断网后设备还能不能安全停下来"**。能，才允许把该链路做成联网的。

---

## 10. 「加个插件就能联网吗」—— 能，但边界不在插件里

### 10.1 插件确实拿得到的三样权力（均已核对实现）

| 扩展点 | 能力 | 锚点 |
|---|---|---|
| `get_tools()` | 新增工具（**不能顶替同名内置工具**） | `tools/base.py:417-431`、`core/plugin.py:183` |
| `get_middleware()` | 包住**所有**工具分派（参数校验之后、计时之内）：可改写参数、重试、**拒绝** | `tools/base.py:582-596`、`core/plugin.py:184` |
| `get_hooks()` | 8 个生命周期钩子：`before_run … on_error` | `core/hooks.py:19-53` |
| （隐含） | 插件代码在**服务进程内** `exec`，权限与主进程相同 —— 插件自己就能发请求 | `core/plugin.py:16` |

补充一个很有用的机制：插件目录**内置在前、用户在后，同名时用户插件覆盖内置**
（`core/plugin.py:58-67`）。也就是说，改 `net_resilience` 这类内置插件的行为**不需要 fork 核心** ——
在 `~/.automind/plugins/net_resilience/` 放一份同名插件即可（这正好是第 5 节缺口 2 的低成本落点）。

### 10.2 插件做不到的三件事

1. **造不出物理链路**。机器没有到公网 / 网关的路由时，插件一样连不上。
   插件只能坐在**既有路径**上（正向代理、DMZ relay、出网跳板）。
2. **管不住进程内其他代码**。连接器、MCP server、`terminal` 里的 `curl`、别的插件，
   都能绕过你写的白名单直接出网。→ 白名单的**强制执行**必须在网络层（代理 / 防火墙 / DNS）。
3. **没有生命周期与配置面**。插件无后台任务托管、无路由注册，`unload()` 只是从字典里删一项
   （`core/plugin.py:222`）。把长连接放进插件里会**泄漏且无人监管** ——
   长连接应当独立成受监管的进程（如 `integrations/feishu_bridge.py`）。

### 10.3 两个反直觉细节：最容易写出「看起来生效、其实没有」的策略

1. **中间件是 fail-open 的**。中间件抛异常会被吞掉并**跳过，继续执行工具**
   （`tools/base.py:590-595`）。所以「用 `raise` 来拒绝」等于没拒绝；
   拒绝必须**返回**一个 `ToolResult(success=False)` 且**不调用** `call_next`。
2. **钩子只能产生副作用**。返回值被忽略、异常被吞（`core/hooks.py:56-66`）；
   `builtin_plugins/pii_guard/hooks.py:5` 明确写着"钩子机制只允许副作用……不做改写"。
   所以 `before_tool` **不能**拦截调用或改参数，只能审计告警。
3. 顺带两条操作约束：加载插件 = 执行任意代码，`/api/plugins` 属管理动作
   （`server.py:148`，远程需 `X-Admin-Token`）；插件工具与内置工具**同名不覆盖**
   （`tools/base.py:420`），所以想改 `http_request` 的行为只能靠中间件，不能同名顶替。

### 10.4 推荐组合：三个插件 + 一个不在插件里的边界

| 组件 | 形态 | 职责 |
|---|---|---|
| `egress_policy` | middleware | 对 `http_request` / `web_fetch` / `web_search` / `browser` 做出站白名单判定；未命中→**返回拒绝结果**；顺带注入代理环境变量 |
| `relay_tools` | `get_tools()` | 提供 `relay_fetch` 等工具，base 固定指向内网 relay，带审计元数据，引导模型优先用它而非通用 `http_request` |
| `egress_audit` | hooks + middleware | 记录"谁/哪个工具/目标域/字节数/是否脱敏"，出网行为可追溯（只告警，不改写） |
| **代理 / DMZ relay** | **不在插件里** | 域名白名单、全量出网审计、上游 Key、限流与配额 —— **强制层** |

骨架（可直接放进 `~/.automind/plugins/egress_policy/hooks.py`）：

```python
import os
from urllib.parse import urlparse

from automind.core.hooks import AgentHooks
from automind.core.types import ToolResult

_ALLOW = {h.strip().lower() for h in
          os.environ.get("AUTOMIND_EGRESS_ALLOW", "").split(",") if h.strip()}
_URL_TOOLS = ("http_request", "web_fetch", "web_search", "browser")


async def _policy(tool_name, kwargs, call_next):
    if tool_name in _URL_TOOLS:
        url = kwargs.get("url") or kwargs.get("endpoint") or ""
        host = (urlparse(str(url)).hostname or "").lower()
        if host and _ALLOW and host not in _ALLOW:
            # 关键：返回失败结果，而不是 raise —— raise 会被跳过并继续执行（base.py:590）
            return ToolResult(tool_name=tool_name, success=False,
                              error=f"出站被策略拒绝：{host} 不在白名单内。")
    return await call_next(kwargs)


def get_middleware():
    return _policy


def get_hooks() -> AgentHooks:
    return AgentHooks()
```

> 注意这层只是"协作"：`terminal` 里的 `curl` 与自定义连接器都绕得过去，
> 所以代理上必须**再拦一次**。两层都做才叫设计，只做插件层等于君子协定。

### 10.5 结论与顺序

- **配置能解决 ~80%**：代理环境变量 + `api_base` 指向内网网关 + IM 长连接桥接（第 4 节）。
- **插件用来补策略与审计**：白名单、relay 工具、出网留痕（本节 10.4）。
- **强制留在网络层**：代理白名单 / 防火墙 / DNS 是唯一能"真的拦住"的地方。
- **不要用插件去假装绕过"没有外网"这个事实** —— 那只会把问题从"连不上"变成"看起来连上了"。

---

## 11. 市场通用做法对照（现状盘点）

**一句话**：当前私有化 / 内网交付的**默认组合**是「**出站白名单代理** + **出站长连接**」；
反向隧道已从"主流"退化为"兜底"；VPN / 专线 / 云私有链路是大企业与金融政务的标准做法；
彻底不联网则走「本地推理 + 离线包 / 一体机」。

| 档位 | 做法 | 业内代表 | 什么时候用 | 代价 |
|---|---|---|---|---|
| **① 出站正向代理 + 域名白名单**（最普及） | 只允许到 N 个域名的 443，标准 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | Dify Enterprise 离线部署官方**只给两条路**：配域名白名单、或彻底隔离；SSO 流量明确走 `HTTPS_PROXY` 出去；现代形态是零信任出网（Zscaler ZIA / SASE） | 绝大多数企业内网 | 要运维一台出网口 + 白名单维护 |
| **② 出站长连接（免入站）** | 进程主动外连，不开任何监听端口 | 飞书「使用长连接接收事件」；钉钉事件订阅的 **Stream 模式**（官方菜单是 Stream / HTTP 回调 / SyncHTTP）；IoT 用 MQTT over TLS | 机器人 / IoT / 需要"有人跟它说话" | 进程常驻 + 断线重连 |
| **③ 反向隧道 / 内网穿透**（兜底） | frp / ngrok / Cloudflare Tunnel / Tailscale 把内网服务暴露到公网 | 早期主流，现多用于开发调试与过渡 | **仅当**平台只支持 HTTP 回调 | 等于开了入站口，攻击面最大 |
| **④ VPN / 专线 / 云私有链路** | IPsec/SSL VPN、MPLS 专线、AWS PrivateLink / Azure Private Link 直连模型服务 | 金融、政务、大型集团 | 合规重、预算足、多机房 | 成本高、开通慢 |
| **⑤ 完全离线产品化** | 本地推理 + 离线镜像仓库 + 离线安装包 / 一体机 | Ollama / vLLM + 私有镜像仓库（Dify 的 custom image repository 正是这个）；大模型一体机交付 | 涉密、单机、演示 | 能力受本地算力限制 |

**市场上几乎标配的配套细节**（比"用什么通道"更容易被忽略）：

- 一份**域名白名单模板**（出厂就告诉你需要放通哪几个域），而不是让客户自己抓包；
- 代理环境变量**由产品注入**，不是让用户改系统设置；
- 能联网的模块（应用市场、插件市场、更新检查）**默认关闭并可配置**
  —— Dify 就给了 `marketplace.enabled: false` 这个开关；
- 镜像仓库**可替换**（`docker.io` 不通时换私有 registry）；
- 许可证**离线激活**；
- 健康检查能区分「公网不可达（预期）」与「网关不可达（故障）」。

**与本文的对应关系**：形态 B = ① + ②；形态 C = ① + 自建 relay（把 ① 的强制点收得更紧）；
形态 D = ③（只作兜底）；形态 A = ⑤。也就是说，第 2 节的选型与市场主流是一致的，
差别只在**产品化程度**：market leader 把白名单、代理注入、开关、离线包都做成了出厂配置。

> 参考：[Dify Enterprise 离线部署](https://enterprise-docs.dify.ai/en/3.13.x/deploy/advanced-configuration/offline-deployment)、
> [飞书 使用长连接接收事件](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case?lang=zh-CN)、
> [钉钉 Server-Side Stream Mode](https://help.dingtalk.io/open/development/introduction-to-stream-mode)、
> [Zscaler Internet & SaaS (ZIA)](https://help.zscaler.com/pdf/com/en/1399436.pdf)、
> [W&B Dedicated Cloud 私有连接](https://docs.wandb.ai/ja/platform/hosting/data-security/private-connectivity)。

---

## 12. 变体：部署在云上（云主机 / 云电脑）

**结论**：云电脑 / 云主机把第 1 节里最难自建的那一层（**出口**）直接给了你，
所以"按文档做能不能联网"在云上通常变成**能**。但入站、凭据、策略三件事仍按本文执行，
且云电脑有三类专属坑（休眠、动态出口 IP、数据中心 IP 风控）。

### 12.1 先分清你需要哪一种云

| 诉求 | 推荐形态 | 理由 |
|---|---|---|
| 只求出网跑 agent，不碰桌面 | **云主机**（ECS / 轻量应用服务器） | 常驻稳定、无桌面休眠问题、成本更低 |
| 要操作桌面软件（微信、桌面 RPA） | **云电脑** | 阿里云 RPA 官方文档明确建议：条件允许时用无影云电脑作机器人运行主机，"云电脑已经保证了桌面渲染的实时保持"；ECS 需 console 会话方案，物理机需显示器常亮 |
| 要连回内网读数据 | 云上 VPC + VPN / 专线 | 否则云上的 file/terminal 工具够不着内网系统 |
| 数据不许出内网（涉密） | **不要上公有云** | 与原始约束直接冲突，回内网 + 代理出口 |

### 12.2 云电脑的五类专属坑（逐条决定成败）

1. **出网不是默认开**。无影办公网络的「公网访问控制」默认是**禁止访问互联网**；
   解绑精品带宽后同样回到"默认禁止"；**海外 / 港澳台地域未绑精品带宽完全不能上网**。
   → 必须在控制台显式开启，别以为"是云就一定通网"。
2. **出口 IP 可能不固定**。基础带宽 = 共享动态 IP（重启或重建后可能变化）；
   只有精品带宽才是独享固定 IP，且**按固定带宽计费时未绑定也持续计费**。
   → 对方平台要求 IP 白名单时**必须**买精品带宽；否则不要依赖源 IP，用域名白名单 + 代理。
3. **数据中心 IP 会被风控**。官方文档明确提示：云电脑公网 IP 属数据中心 IP，
   **腾讯系软件、电商平台等可能识别为 VPS / 异常环境而触发风控**。
   → 个人微信自动化在云上风险高（登录与封号）；飞书 / 钉钉 / 企业微信走官方 API 不受影响。
4. **休眠 / 自动关机**。云电脑普遍有闲置休眠策略，长连接桥接进程会掉线。
   → 必须设「始终运行 / 不自动休眠」，并把 AutoMind 与桥接进程配成**开机自启**。
5. **无人值守的桌面渲染**。模拟鼠标键盘 / 看屏幕要求桌面持续渲染：RDP 断开会导致渲染中断、
   自动化失败；RDP 期间分辨率与 DPI 变化也会干扰。→ 执行期间不要 RDP 进入；
   用 ECS 时需 Windows **console 会话 + Administrator**，且**不能用多 Session 模式**。

### 12.3 云上部署清单（AutoMind 侧）

1. 控制台：开启办公网络「公网访问控制」；需要固定出口 IP 就绑定精品带宽；
2. 关休眠 + 配开机自启（AutoMind 服务 + 长连接桥接进程）；
3. `--host 127.0.0.1` + `AUTOMIND_AUTH_TOKEN`，只经客户端 / Web 访问，**不开任何入站**（第 1 节原则不变）；
4. 出网收敛：`AUTOMIND_EGRESS_ALLOW` 白名单 + 代理环境变量（第 4 节）；
5. 数据：云盘加密 + 快照 + 许可证离线激活，确认**数据驻留地域**满足合规要求；
6. 验证：`net_check` 的三态结论、公网出口 IP、IM 收发、**重启云电脑后服务能否自启**。

### 12.4 一句话

**云电脑解决"出站"（本文的①），本文解决"入站为零 + 出网受控 + 离线可诊断"（②③）。**
两者叠加才是完整可用的形态；只上云不改配置，只会得到一台"能上网但没人管"的机器。

> 参考：[无影云电脑 管理公网带宽](https://www.alibabacloud.com/help/zh/wuying-workspace/user-guide/manage-internet-access)、
> [无影云电脑 网络安全](https://www.alibabacloud.com/help/zh/wuying-workspace/security-and-compliance/network-security)、
> [阿里云 RPA 使用桌面渲染保持](https://help.aliyun.com/zh/rpa/user-guide/rdp-instructions)、
> [天翼云电脑保活实践（社区）](https://github.com/muyicn/ctyun-dashboard)。

---

## 13. 边界方案定稿：要不要用穿透工具（花生壳类）

### 13.1 结论：**不要**，本方案里没有它的位置

**穿透工具（花生壳 / ngrok / frp）解决的是"入站"问题**——让公网能访问内网服务。
而本文的核心设计已经把入站需求**消掉**了：飞书长连接、钉钉 Stream 都是由本进程**主动外连**，
不需要公网 IP、域名、证书、备案。**没有入站需求，就不需要穿透。**

更关键的一条：AutoMind 是"**能执行工具、能读写文件、能跑命令**"的 agent。
把它通过穿透暴露到公网，等于把一台可执行命令的机器挂到网上 —— 这是整份文档里风险最大的一步，
`docs/FEISHU.md` 反复强调的也正是这一点（"能跟机器人说话 = 能在你机器上执行工具"）。

> 花生壳适合"暴露一个 NAS 管理页、一台摄像头"；
> **不适合**暴露一个 agent 控制面。这是性质差异，不是配置好坏问题。

### 13.2 推荐方案（唯一推荐，零穿透、零入站）

| 层 | 选定做法 |
|---|---|
| 部署位置 | 内网主机（数据不出内网时）或云主机（内网无出口时）；要操作微信桌面才用云电脑 |
| 机器人接入 | **出站长连接**（飞书长连接 / 钉钉 Stream），不监听任何端口 |
| 模型 | 内网网关 / 本地 Ollama 优先；云端 API 经统一出口 |
| 出网 | 正向代理 + 域名白名单（`AUTOMIND_EGRESS_ALLOW`） |
| 入站 | **0**：`--host 127.0.0.1` + `AUTOMIND_AUTH_TOKEN`，只经本机/组网访问 |
| 远程看 UI | **组网**（WireGuard / Tailscale / 蒲公英异地组网），不是穿透 |
| 穿透工具 | **不使用** |

成本：仅主机 + 少量带宽；风险：最低（无公网暴露面）。

### 13.3 只有两种情况下才真的需要穿透（诚实边界）

1. **平台只支持 HTTP 回调**：企业微信自建应用、微信公众号的服务器配置要求公网可达 URL；
   某些客户侧飞书应用被管理员限制为回调模式。
2. **你需要在外部随时访问 AutoMind Web UI** —— 但这种情况应该用**组网**（VPN 语义），
   而不是穿透（暴露语义）。

若确实落在情况 1，穿透工具的优选顺序：

| 工具 | 评价 |
|---|---|
| **Cloudflare Tunnel** | 免费、无需公网 IP、自带访问控制与 WAF；流量过 Cloudflare |
| **frp（自建 VPS）** | 完全自主可控、流量只过你自己的机器；要自己维护 VPS 与证书 |
| 花生壳 / ngrok 类 | 最省事，但流量经第三方；免费档在**流量、带宽、映射数**上有明确限制，域名/映射常需升级付费（官方 FAQ 就是"流量不够可升级专业版或买流量包"）；企业向有私有化/SDK 嵌入方案 |

### 13.4 若被迫暴露：最小暴露配方

```
① 只映射 /hook/* 一个路径前缀（反代剥路径），绝不整端口映射 8765
② 平台侧用独立子域 + 只对该路径生效的证书
③ AUTOMIND_TRUSTED_HOSTS=该子域（否则会被 Host 校验 403 —— 这是护栏不是 bug）
④ AUTOMIND_AUTH_TOKEN 与 AUTOMIND_ADMIN_TOKEN 分开设置；管理动作绝不放行
⑤ 反代层加源 IP 限制 + AUTOMIND_RATE_LIMIT 限流
⑥ 高危工具保持需审批，审批 fail-closed；无回执通道时明确拒绝
⑦ 开启 AUTOMIND_REDACT_SECRETS 与出网/入站审计
```

### 13.5 一句话决策树

```
需要操作桌面软件（微信等）吗？
├─ 否 ─► 内网/云主机 + 飞书长连接 ─────────► 【推荐，零穿透】
└─ 是 ─► 云电脑 + 飞书长连接 ─────────────► 【注意数据中心 IP 风控】

平台只支持 HTTP 回调吗？
├─ 否 ─► 不需要任何穿透工具
└─ 是 ─► Cloudflare Tunnel / frp，且只暴露 /hook/*（§13.4）

需要在外网访问 Web UI 吗？
└─ 是 ─► 用组网（WireGuard / Tailscale / 蒲公英），不要用穿透
```

> 顺带一个容易混淆的点：贝锐自家产品线里，**蒲公英（异地组网）**语义接近 VPN、
> 不把服务暴露到公网，比**花生壳（内网穿透 / 端口映射）**更适合"我要远程访问 UI"这个诉求。
> 结论不变：**能不暴露就不暴露**。
