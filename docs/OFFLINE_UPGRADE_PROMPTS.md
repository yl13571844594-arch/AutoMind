# AutoMind 离线 / 出网能力 —— 升级提示词包

> 用途：把「内网离线部署 + 机器人要联网」这条路线拆成**可独立粘贴、可独立验收**的提示词。
> 每一条都以 `docs/OFFLINE_DEPLOYMENT.md` 的分析为依据，标注了改动范围与验收命令。
>
> 怎么用：
> 1. **一次只喂一条**，按 §0 的依赖顺序来；一条做完验收通过再上下一条。
> 2. 每条提示词前**先粘 §1 的通用背景块**（它提供项目约束与关键机制，避免执行者踩 fail-open 之类的坑）。
> 3. 推荐在 `工作（/work）` 或 `编程（/coding）` 模式下执行；先让它读 `docs/OFFLINE_DEPLOYMENT.md`。
> 4. 每条都要求"自己跑验收并贴输出"——没有输出的交付不算完成。

---

## 0. 依赖顺序与规模

| 编号 | 任务 | 依赖 | 规模 | 关键产出 |
|---|---|---|---|---|
| **P0-1** | 离线总闸 `AUTOMIND_OFFLINE` | — | 0.5–1 天 | `core/offline.py` + 单测 |
| **P0-2** | 出网白名单中间件（插件）+ 白名单模板 | — | 1 天 | `builtin_plugins/egress_guard/` |
| **P0-3** | 探测可配置 + 三态语义（本地/网关/公网） | P0-1 | 0.5–1 天 | `netresilience` 改造 |
| **P1-1** | IM 长连接渠道升为一等公民 | P1-2 建议先做 | 3–5 天 | `bots/` 出站渠道 |
| **P1-2** | 非交互审批通道接通 | — | 0.5 天 | `/api/run` 审批可问出去 |
| **P2-1** | 出网审计留痕 | P0-2 | 1 天 | `egress_audit.jsonl` + 查询接口 |
| **P3-1** | DMZ relay 契约文档 | P0-2 | 0.5 天 | `docs/RELAY_CONTRACT.md` |
| **★** | 一次性完整版（P0 三条合并） | — | 2–3 天 | 见 §3 |

> 只做 P0 三条，就已经覆盖「内网能跑、不误报、出网有策略」这三件事，
> 建议先交付 P0 再谈 P1/P2。

---

## 1. 通用背景块（每条提示词前都粘这段）

```
【项目】AutoMind（Python 3.11+，仓库根 = 当前工作目录）。

【代码风格】注释与文档用中文，重解释"为什么这么设计"；不新增第三方依赖，优先纯标准库；
错误信息必须给出"下一步怎么办"，不允许只报"失败了"。

【运行约束】目标环境可能完全无外网。任何改动都必须保证：
断网也能启动、能跑工作流、能给出准确诊断，而不是超时或伪装成别的错误。

【必须遵守的关键机制】
1. 工具中间件：automind/tools/base.py 的 _execute_with_middlewares()。
   签名 async mw(tool_name, kwargs, call_next) -> ToolResult。
   ⚠️ 中间件抛异常会被吞掉并【跳过继续执行】（fail-open）。
   因此"拒绝"必须【返回 ToolResult(success=False)】且【不调用 call_next】，绝不能靠 raise。
2. 插件：automind/core/plugin.py。一个插件目录 = plugin.json + hooks.py，
   可提供 get_hooks() / get_tools() / get_middleware()。
   用户插件目录 ~/.automind/plugins 与内置 automind/builtin_plugins 同名时，用户版覆盖内置。
   插件注册的工具【不能顶替同名内置工具】（tools/base.py 的 sync_plugin_tools）。
3. 工具与结果类型：automind/tools/base.py 的 AbstractTool；automind/core/types.py 的
   ToolResult / PermissionTier。工具要有 name、description、parameters(JSON Schema)、
   permission_tier、risk_score。
4. 出站相关代码：automind/tools/net_tools.py（http_request / web_fetch / web_search，
   其中 check_url 默认拒绝私网/回环/云元数据）、automind/tools/browser.py。
5. 网络韧性：automind/core/netresilience.py（PROBE_HOSTS 硬编码公网目标、
   ResilientDispatch 自动等待重试）与内置插件 automind/builtin_plugins/net_resilience/。
6. 服务端：automind/server.py。管理动作清单在文件顶部（改配置/加 MCP/加载插件等），
   健康检查 /api/health，插件路由 /api/plugins。

【交付前必须自己跑，并在回复里贴出真实输出】
- python -m pytest tests -q
- python -m automind.eval run automind/eval/suites/smoke.yml --dry-run
- python -m automind.workflow run examples/06-workflow/hello.yaml --dry-run
（Windows 下若 pwsh 报 0xC0000142 之类，换普通 cmd/PowerShell 终端重跑，不要跳过验收。）

【交付格式】改动文件清单 + 每个文件一句话说明 + 验收命令与输出 + 已知限制。
```

---

## 2. 分阶段提示词

### P0-1 · 离线总闸

```
给我的 AutoMind 加一个"离线总闸"，让整套系统在无外网环境下的行为是可预期、不空转的。

【目标】环境变量 AUTOMIND_OFFLINE=1（取值 1/true/yes 均认）时：
1. 出站工具 http_request / web_fetch / web_search / browser 立即返回失败，
   【不发起任何连接】，错误信息要说明：当前处于离线模式，如确需出网请
   在 AUTOMIND_EGRESS_ALLOW 中放通目标域，或关闭 AUTOMIND_OFFLINE。
2. terminal 里的联网命令（pip / npm / git / curl 等）不再被 net_resilience 自动
   等待+重试（离线等待 60 秒 ×3 次纯属浪费预算），也不要把拒绝判成"网络故障"。
3. core/updater.py 的 check() 直接短路返回"离线模式跳过"，不发任何请求。
4. automind/core/netresilience.py 的 network_up() 在离线模式下不做公网 TCP 探测，
   直接返回 (False, "离线模式（AUTOMIND_OFFLINE=1），跳过网络探测")。

【实现要求】
- 新增 automind/core/offline.py 作为唯一判定与文案出口（例如 is_offline() /
  offline_hint()），禁止在各模块里散落 os.environ.get("AUTOMIND_OFFLINE")。
- 涉及模块：tools/net_tools.py、tools/browser.py、core/updater.py、
  core/netresilience.py；如需要，可在 tools/_toolkit.py 加一个统一的"出站前置检查"。
- 错误文案必须可执行：告诉用户三个出路（放通白名单 / 关总闸 / 换本地模型）。
- 保留现有行为：未设置 AUTOMIND_OFFLINE 时，一切与今天完全一致。

【测试】新增 tests/core/test_offline.py：
- 判定取值（1/true/yes/0/空/未设）；
- 每个出站工具在离线模式下都返回 success=False 且错误里含 "离线"；
- 用 monkeypatch 断言"没有发生真实网络调用"（例如替换 httpx.AsyncClient）；
- network_up() 在离线模式下不做 socket 连接（用 monkeypatch 断言 socket 未被调用）。

【验收】
1. python -m pytest tests/core/test_offline.py -q 全绿；
2. 设 AUTOMIND_OFFLINE=1 后跑 smoke.yml --dry-run，全绿且无网络访问；
3. 设 AUTOMIND_OFFLINE=1 时 /api/health 正常返回；
4. 不设该变量时，原有测试全绿（python -m pytest tests -q）。

【禁止】不要顺手改出网白名单逻辑（那是下一条任务）；不要改动默认行为。
```

---

### P0-2 · 出网白名单中间件 + 白名单模板

```
给 AutoMind 加一个"出站白名单"能力，作为内置插件交付，让内网部署时出网范围可收敛。

【目标】
1. 新增内置插件 automind/builtin_plugins/egress_guard/（plugin.json + hooks.py），
   通过 get_middleware() 提供策略中间件，作用于 http_request / web_fetch /
   web_search / browser 四个出站工具。
2. 白名单来源环境变量 AUTOMIND_EGRESS_ALLOW（逗号分隔，支持 *.example.com 通配，
   支持 host:port）。为空时表示"不启用白名单"（保持现有行为，不静默全禁）。
3. 未命中白名单时：返回 ToolResult(tool_name=..., success=False, error=...,
   metadata={"egress_denied": True, "host": host})，并且【绝不调用 call_next】
   —— 因为中间件 raise 会被跳过并继续执行（fail-open，见 tools/base.py）。
   错误文案要写清：目标域、当前白名单、以及怎么放通。
4. 目标域解析：从 kwargs 的 url / endpoint（及 web_search 的 endpoint）取 host，
   小写、去端口；解析不到 host 时按"拒绝"处理并说明原因（fail-closed）。

【必须写进注释/文档的话】本插件是【协作层】：它只能拦住"经工具调用"的出网，
拦不住 terminal 里的 curl、连接器和 MCP。真正的强制执行必须在网络层
（正向代理 / 白名单 / 防火墙）。请把这句写进插件模块 docstring。

【额外交付】一份出厂白名单模板 docs/EGRESS_ALLOWLIST.md，分组列出常见需要放通的域：
- 模型：api.deepseek.com、api.moonshot.cn、dashscope.aliyuncs.com、open.bigmodel.cn、
  ark.cn-beijing.volces.com、api.openai.com（按需）
- IM：open.feishu.cn / open.larksuite.com、api.dingtalk.com / oapi.dingtalk.com、
  qyapi.weixin.qq.com、api.weixin.qq.com
- 依赖：pypi.tuna.tsinghua.edu.cn、registry.npmmirror.com、pypi.org
- 说明每一条"为什么需要、不放通会怎样"。

【测试】新增 tests/tools/test_egress_guard.py：
- 白名单命中 → 透传（用假 tool 断言真的执行了，且计数 +1）；
- 白名单未命中 → 返回 egress_denied，且【假工具的执行计数为 0】（证明没执行）；
- 通配 *.example.com 命中 a.example.com 也命中 example.com；
- 白名单为空 → 不拦截；
- 缺 url 参数 → fail-closed 拒绝；
- AUTOMIND_OFFLINE=1 时优先走离线文案（与 P0-1 协同，不出现两套矛盾提示）。

【验收】python -m pytest tests -q 全绿；在 Web 界面「插件」页能看到并加载 egress_guard；
设置 AUTOMIND_EGRESS_ALLOW=open.feishu.cn 后，让 agent 请求 https://example.com 被拒绝、
请求 https://open.feishu.cn 放行（贴出两次调用的返回）。

【禁止】不要改 http_request 等内置工具的实现（插件工具不能同名顶替，本次也不许改）；
不要新增第三方依赖。
```

---

### P0-3 · 探测目标可配置 + 三态语义

```
修掉 AutoMind 在内网环境下"必然误报网络故障"的问题。

【背景/问题】automind/core/netresilience.py 的 PROBE_HOSTS 硬编码公网目标
（baidu / pypi / github / 1.1.1.1）。内网部署时它永远返回"全部不可达"，
于是 net_check 给出错误建议，ResilientDispatch 还会"等 60 秒 × 最多 3 次"白烧预算，
把普通失败误判成网络抖动。

【目标】
1. PROBE_HOSTS 支持环境变量 AUTOMIND_NET_PROBE 覆盖，格式 "host:port,host:port"；
   解析失败时回落到内置默认值并打 warning（不要静默）。
2. 探测结果按三态归类并分别给出结论与建议：
   - local：本地服务（127.0.0.1 / localhost）；
   - gateway：内网网关 / relay —— 目标取当前配置的模型 api_base 主机
     （automind/core/config.py 的 llm.api_base，或 server_store 里保存的 api_base），
     以及 AUTOMIND_NET_PROBE 指定的目标；
   - public：默认的公网目标。
3. net_check 的 advice 必须区分两种情况（这是本次的核心价值）：
   - 公网不可达但 gateway 可达 → "预计如此，无需处理"（明确写"这是预期，不是故障"）；
   - gateway 不可达 → "故障：模型网关不可达，请检查 relay / 代理"。
4. AUTOMIND_OFFLINE=1 时（配合 P0-1）：net_check 直接给"离线模式（预期）"结论，
   ResilientDispatch 不重试、不等待。
5. is_network_error() 不得把"离线/白名单拒绝"这类确定性拒绝判成网络错误。

【测试】新增 tests/core/test_netresilience_offline.py：
- AUTOMIND_NET_PROBE 覆盖生效、非法值回落；
- 三态归类函数（可抽出纯函数便于测试）；
- 公网不可达 + gateway 可达 → advice 含"预期"；gateway 不可达 → advice 含"故障"；
- 离线模式下 ResilientDispatch 不调用 wait_for_network（monkeypatch 断言）。

【验收】python -m pytest tests -q 全绿；贴出 net_check 在
（a）正常联网、（b）模拟公网不可达、（c）AUTOMIND_OFFLINE=1 三种情况下的输出。

【禁止】不要改动工具重试的"哪些调用可安全重做"白名单逻辑（is_retry_safe），
那部分已经过验证，本次只改探测与判定。
```

---

### P1-1 · IM 长连接渠道升为一等公民

```
把"免公网接入 IM"从仓库外挂脚本提升为 AutoMind 的一等能力。

【现状】automind/bots/channels.py + automind/bots/manager.py 的四个渠道
（飞书 / 钉钉 / 企业微信 / 通用 Webhook）都是 HTTP 回调形态，需要平台能访问
public_base_url。真正免公网的长连接只存在于 integrations/feishu_bridge.py
（外挂脚本，且走 POST /api/run，会掉进 docs/FEISHU.md §3.4 的审批陷阱）。

【目标】
1. 在 automind/bots/ 中新增"出站长连接"渠道类型（先做飞书，命名建议 feishu_ws；
   钉钉 Stream 留出同样的扩展位）。要求复用现有的 Incoming / send / 会话 id /
   进度节流 / 聊天内审批链路，与回调渠道行为一致。
2. 长连接由【服务进程托管】（server 启动时按配置拉起、停止时优雅关闭、
   失败自动重连并记录原因），【不要放进插件】—— 插件没有生命周期，
   unload 只是从字典删一项，会泄漏且无人监管。
3. 依赖处理：lark-oapi 之类的第三方库必须是【可选依赖】。未安装时：
   渠道状态显示"缺少依赖"，并明确给出 pip 安装命令；不允许启动即崩。
4. 安全默认值沿用现有风格（fail-closed）：allow_users 白名单为空时拒绝服务；
   群聊必须 @机器人；只做出站连接，不监听任何端口。
5. 配置能在 Web「机器人」页里增删改（与现有渠道相同的字段 + 必要的长连接字段），
   并复用现有的 hook_token / 启用开关语义。

【验收】
1. python -m pytest tests -q 全绿（含新增 tests/bots/ 用例）；
2. 未安装 lark-oapi 时启动不崩、界面提示明确；
3. 装好依赖后：飞书里发消息 → agent 执行 → 结果回到同一会话；
   /work 模式下高危工具能在聊天里问出审批并生效；
   断网 30 秒再恢复，桥接自动重连且消息不丢（有去重）；
4. 用 netstat / 资源监视器确认进程【没有监听任何对外端口】。

【交付】改动文件清单 + 配置说明（追加到 docs/FEISHU.md 或新建 docs/BOTS.md）
+ 验收输出。禁止把 integrations/feishu_bridge.py 直接删掉，先保留并在文档里
标注"旧方式，建议迁移"。
```

---

### P1-2 · 非交互审批通道接通

```
修掉"用 /api/run 驱动的任务遇到需要审批的工具调用会被直接拒绝"这个问题。

【依据】docs/FEISHU.md §3.4：server.py 里 _headless_approval_callback() 是死代码
（全仓库无人调用），而 /api/run 这条路径从来不设置 agent.approval_callback，
于是非交互环境一律按拒绝处理（automind/state/human_loop.py 的 _cli_ask）。

【目标】
1. 在 api_run() 路径上设置 agent.approval_callback = _headless_approval_callback(...)，
   并在 finally 里清理（与 WebSocket 路径 server.py:7078 同口径，避免遗留给后续任务）。
2. 保证"问出去"真的能到人：与 AUTOMIND_WEBHOOKS 出站事件（approval_request /
   approval_resolved / approval_timeout）打通；回执端点
   POST /api/approvals/{approval_id} 保持管理动作保护。
3. 没有可用回执通道时维持 fail-closed：明确说"无回执通道，已按拒绝处理"，
   而不是含糊的"工具被拒绝"。
4. 审批超时（AUTOMIND_APPROVAL_TIMEOUT，默认 300s）后回执返回 409 approval_stale，
   行为不变。

【测试】新增 tests/server/test_headless_approval.py：
- 有回执通道：审批请求发出 → 回执批准 → 工具执行；
- 无回执通道：立即 fail-closed 拒绝且文案明确；
- 超时：拒绝 + stale 语义；
- 任务结束后 approval_callback 被清空（不泄漏到下一个任务）。

【验收】python -m pytest tests -q 全绿；贴出一次"发起 → webhook 收到 approval_request
→ POST 回执 → 工具执行成功"的完整日志。
```

---

### P2-1 · 出网审计留痕

```
给出网行为加审计，让"agent 到底访问了哪些外部地址"可追溯。

【目标】
1. 在 egress_guard 插件（P0-2）基础上增加审计：每次出站工具调用记录一行 JSONL 到
   <数据目录>/egress_audit.jsonl（数据目录见 automind/core/paths.py），字段：
   时间、session_id、tool、host、放行/拒绝、reason、请求体大小估算、脱敏标记。
2. 复用 automind/core/redact.py 做脱敏，禁止把 URL 查询串里的密钥原样落盘
   （参考 core/webhooks.py 里对日志脱敏的处理思路）。
3. 新增只读查询接口 GET /api/egress/audit（按 session / 时间过滤，分页），
   并按现有约定归入管理动作（远程需 X-Admin-Token）。
4. 文件要能轮转（按大小），避免长期运行把磁盘写满；轮转上限可配置。

【测试】新增 tests/tools/test_egress_audit.py：
- 放行与拒绝各写一行且字段完整；
- 含 sk-/Bearer 的 URL 落盘后已脱敏（断言原文不出现在文件里）；
- 超过大小阈值会轮转；
- 查询接口的鉴权与分页。

【验收】python -m pytest tests -q 全绿；跑一轮真实任务后，用 GET /api/egress/audit
列出域名清单，并给出"白名单最小集"建议。
```

---

### P3-1 · DMZ relay 契约文档

```
写一份 AutoMind 与「内网模型/出网 relay」之间的接口契约，供运维/客户实施参考。

【目标】新建 docs/RELAY_CONTRACT.md，内容至少包括：
1. 端点：OpenAI 兼容 /v1/chat/completions（含流式 SSE 的行为要求）、健康检查端点；
2. 鉴权：relay token 的传递方式；上游 Key 只存在 relay 侧，agent 侧不得持有；
3. 出网策略：relay 上的域名白名单、TLS 终止、CONNECT 代理语义；
4. 审计与配额：要求 relay 记录 请求方/模型/用量/目标域，支持按团队配额与排队；
5. 失败语义：区分"公网不可达""上游 4xx/5xx""配额超限""鉴权失败"，
   并约定 agent 侧应给用户的文案；
6. 模型白名单：relay 侧只放通允许的模型名（对应企业版 model_gateway 的收敛思路）。
【要求】只写文档与配置样例，不写实现代码；所有约定都要能对应到 AutoMind
现有配置项（api_base / AUTOMIND_* 环境变量），不要发明新的配置名。
```

---

## 3. 一次性完整版（想一把做完 P0）

```
请按 docs/OFFLINE_DEPLOYMENT.md 第 5 节的缺口 1–3 与第 10.4 节，为 AutoMind
实现内网离线部署所需的 P0 能力，一次性交付以下三项并逐项验收：

① 离线总闸：新增 automind/core/offline.py（唯一判定出口）+ AUTOMIND_OFFLINE
   开关，让出站工具、updater、netresilience 探测在离线模式下短路且不重试；
② 出网白名单：新增内置插件 automind/builtin_plugins/egress_guard/（中间件形式，
   AUTOMIND_EGRESS_ALLOW），未命中时【返回拒绝结果且不调用 call_next】，
   并交付 docs/EGRESS_ALLOWLIST.md 白名单模板；
③ 探测可配置：AUTOMIND_NET_PROBE 覆盖 PROBE_HOSTS，net_check 输出区分
   「公网不可达（预期）」与「网关不可达（故障）」，三态归类。

【硬性要求】
- 三项都要有单测（tests/core/test_offline.py、tests/tools/test_egress_guard.py、
  tests/core/test_netresilience_offline.py），并覆盖"拒绝时工具未执行"这条断言；
- 不新增第三方依赖；中文注释解释"为什么"；错误文案给出出路；
- 未设置任何新环境变量时，行为与今天完全一致；
- 交付时逐条贴出：改动清单、测试输出、离线冒烟（smoke.yml --dry-run）、
  工作流 dry-run（examples/06-workflow/hello.yaml）的真实输出。

【明确不做】不改 is_retry_safe 的重试白名单；不改 http_request 等内置工具实现；
不动 IM 长连接（那是 P1）。
```

---

## 4. 空白模板（以后自己写新功能提示词用）

```
【目标】一句话说清要交付什么能力，以及"做好之后用户能做什么"。

【背景/现状】现在是什么样、为什么不够（附文件:行），痛点用一句话收口。

【必须做到】
1. 具体行为（含边界与默认值）；
2. 失败时的行为（fail-closed 还是 fail-open，文案要给什么出路）；
3. 与既有机制的衔接（用哪个扩展点，不要另起一套）。

【改动范围】只允许改：<文件/目录清单>。范围外的东西不要顺手改。

【测试】新增/修改哪些测试文件，必须覆盖哪些断言（尤其"不该发生的事没发生"）。

【验收】写出可直接执行的命令 + 期望结果；要求执行者贴真实输出。

【禁止】明确列出不许做的事（引入依赖、改默认行为、动已验证模块……）。
```

---

## 5. 常见坑（写提示词时就要防住）

| 坑 | 后果 | 提示词里要怎么写 |
|---|---|---|
| 用 `raise` 实现"拒绝" | 中间件异常被吞并跳过 → **策略静默失效** | "必须返回 ToolResult(success=False) 且不调用 call_next" |
| 用钩子拦截 | 钩子返回值被忽略、异常被吞 → 只能告警 | "拦截用中间件，钩子只做审计" |
| 想让插件顶替 `http_request` | 同名不覆盖，白写 | "不改内置工具，用中间件包住" |
| 把长连接写进插件 | 无生命周期，卸载即泄漏 | "由服务进程托管，不放插件" |
| 忘了离线时的探测 | 每次失败等 60s×3 | "离线模式不探测、不重试" |
| 只写"实现完成" | 无法判断真伪 | "贴出 pytest 与 dry-run 的真实输出" |
| 改默认行为 | 现有用户被静默影响 | "未设置新变量时行为完全不变" |
