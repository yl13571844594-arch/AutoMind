# AutoMind 版本说明 — 社区版 / 专业版 / 企业版

自 **v0.6.0** 起，AutoMind 采用「开源核心（Open-Core）」模式：

- **社区版（Community）** — 本仓库 `automind/` 目录，MIT 许可，永久免费开源；
- **专业版（Pro）/ 企业版（Enterprise）** — 商业扩展包 `automind-pro`（闭源，
  独立分发），安装并配置许可证后在运行时自动激活，社区核心零改动。

## 1. 功能对比

| 类别 | 功能 | 社区版 | 专业版 | 企业版 |
|------|------|:---:|:---:|:---:|
| 交互模式 | 💬 对话（流式、多模态、语音） | ✅ | ✅ | ✅ |
| | ⚙️ 工作（分层规划 + 符号验证） | ✅ | ✅ | ✅ |
| | 💻 编程（ReAct + TDD 内环） | ✅ | ✅ | ✅ |
| | 🤝 协同（多智能体编排） | — | ✅ | ✅ |
| | 🔁 循环（Loop Engineering） | — | ✅ | ✅ |
| 能力底座 | 工具 / 技能 / MCP / 插件 | ✅ | ✅ | ✅ |
| | 记忆（短期/长期/知识图谱） | ✅ | ✅ | ✅ |
| | 反思 / 自我纠错 / 自主任务闭环 | ✅ | ✅ | ✅ |
| 安全 | 工具审批（deny>ask>allow）· 安全审计 | ✅ | ✅ | ✅ |
| | 访问鉴权 · 限流 · 密钥脱敏 · CORS | ✅ | ✅ | ✅ |
| 运营 | 基础统计（/api/stats、Token 用量） | ✅ | ✅ | ✅ |
| | 📊 高级统计仪表盘（命中率/效率/趋势） | — | ✅ | ✅ |
| | ⏰ 定时任务 | — | ✅ | ✅ |
| | ⭐ 自定义模板（提示词资产，最多 100 个） | — | ✅ | ✅ |
| | 📄 审计报告导出（PDF / 打印版） | — | ✅ | ✅ |
| 协作 | 📄 Web 代码编辑器（Monaco + Diff） | ✅ | ✅ | ✅ |
| | 👥 任务分配看板 · 操作实时通知 | ✅ | ✅ | ✅ |
| 工作流 | 🔀 工作流引擎与校验 · tool/llm/human/branch/agent/foreach 步骤 | ✅ | ✅ | ✅ |
| | branch 条件扩展（`>` `<` `>=` `<=` `in` `exists`，v1.9.0） | ✅ | ✅ | ✅ |
| | Web 工作流视图 · 异步运行与进度 · 停止 · 断点续跑（v1.8.0） | ✅ | ✅ | ✅ |
| | 已保存工作流数 | 20 个 | 200 个 | 不限 |
| | foreach 单次最大迭代 | 20 项 | 1000 项 | 不限 |
| | 运行历史保留 · 📊 工作流看板（v1.9.0） | 最近 50 次 · — | 90 天 · ✅ | 可配置 · ✅ |
| | ⏰ 触发器（应用内定时 / 入站 webhook）· 失败自动续跑与通知 · 重启续跑策略（v1.9.0） | 外部 cron / CI 调 REST | ✅ | ✅ |
| | 活跃触发器数 · 定时最小间隔 | — | 30 个 · 5 分钟 | 不限 · 1 分钟 |
| | ⚡ parallel 并行组 · foreach 逐项并发（v1.9.0） | — | ✅ | ✅ |
| | 📦 工作流模板库 · 🧭 从执行轨迹生成工作流（v1.10.0） | 起步样板 | ✅ | ✅ |
| | 🛡 发布审批 · 按工作流权限 · 多级按角色审批 · 运行审计导出（v1.10.0） | — | — | ✅ |
| | 🔑 凭据保管 `{{ secrets.* }}` · 🚦 按团队并发配额与排队（v1.10.0） | — | — | ✅ |
| 用量 | 每日任务次数（工作流每次运行计 1 次） | 100 次/天 | 不限 | 不限 |
| | 工作区数量 | 3 个 | 30 个 | 不限 |
| 知识库 | 📚 RAG 知识库（上传/分段/embedding/对话自动检索） | 5 文档·10MB | 不限·200MB | 不限·不限 |
| | 多知识库管理 · Reranker 重排 · 引用溯源 · 定时重嵌入 | — | ✅ | ✅ |
| | 可选向量后端（Milvus / Pinecone / Qdrant） | — | ✅ | ✅ |
| | 混合检索（向量+词法）· 热度统计 · 检索审计 · 目录批量导入 | — | — | ✅ |
| 降本 | ⚡ 语义缓存（相似问题秒回，省 Token） | — | ✅ 基础 | ✅ 高级 |
| | 🧭 模型智能路由（按复杂度分级选模型） | — | ✅ 2 级 | ✅ N 级 |
| | 💰 成本仪表盘（模型成本/缓存节省分析） | — | — | ✅ |
| 专家 | 🎓 官方专家市场 + 自建专家（最多 3 个） | ✅ | ✅ | ✅ |
| | 🎓 专家进阶（无限创建/分享/导入导出/统计） | — | ✅ | ✅ |
| | 🏛️ 企业专家市场（管理员审批后发布） | — | — | ✅ |
| 多用户 | 对话历史按会话隔离 | ✅ | ✅ | ✅ |
| | 👥 会话级 Agent 池（执行态隔离） | — | — | ✅ |
| | 🔐 SSO / LDAP 单点登录（会话令牌接入既有鉴权） | — | — | ✅ |
| | 🧩 细粒度权限 RBAC（角色 × 动作矩阵，支持通配） | — | — | ✅ |
| | 🚪 私有模型网关（api_base 收敛 + 模型白名单） | — | — | ✅ |
| | 独立统计分析服务（analytics-service） | — | — | ✅ |

> 设计原则：**安全不设付费墙** — 鉴权、限流、脱敏、审计全部保留在社区版。
>
> 工作流的版本拦截发生在**加载 / 校验阶段**（例如"`parallel` 需要专业版，第 12 行"），
> 不会让流程跑到一半才因版本不够失败；超出数量上限的工作流仍可列出、校验与试运行。
> 特性键 `workflow_triggers` / `workflow_parallel` / `workflow_history` 于 v1.9.0 交付并移入
> `COMMERCIAL_FEATURES`（专业版 / 企业版许可证自动开启，实现在 `automind_pro.workflow_pro`）；
> v1.10.0 交付 `workflow_from_trace` / `workflow_templates`（专业版）与 `workflow_governance`（企业版），
> `PLANNED_FEATURES` 至此清空。
> 限额键（`workflows` / `workflow_triggers` / `workflow_min_interval` / `workflow_history` /
> `workflow_history_days` / `workflow_foreach_max`）在 `QUOTA_RULES`。详见 `docs/WORKFLOW_ROADMAP.md` 第 2 节。

## 2. 安装与激活

```bash
# 社区版（默认）
pip install -e ".[web]"
python -m automind.server --port 8765

# 专业版 / 企业版
pip install automind-pro          # 商业渠道获取的 wheel（或源码目录 ./pro）
set AUTOMIND_LICENSE=AMP2-...     # 或写入 .automind_license / ~/.automind/license
python -m automind.server --port 8765
```

> 许可证为 Ed25519 非对称签名（`AMP2-...`）。**v1 旧格式 `AMP-...`（HMAC）默认拒绝** ——
> 旧密钥历史上随包分发过，已等同公开；存量客户请在换证窗口期换发新证。
> 发行方签发与自检方式见 [RELEASE.md](RELEASE.md)「商业激活闭环」与
> [docs/COMMERCIAL_ACTIVATION.md](COMMERCIAL_ACTIVATION.md)。

**应用内升级（v1.11.1 起，推荐）**：Web 界面 → 设置 →「⭐ 升级专业版 / 企业版」（或点左上角版本徽标）。

> v1.11.7 起发行方交付的是**一个** `.ampkg`（wheel + 许可证合一）：客户拖进「⚡ 一键升级」
> 一次完成装包 + 激活，不需要 pip、不需要改环境变量。下面 1-3 步是分两步升级 / 手工配置
> 场景仍支持的旧路径。

1. 拖入 `automind_pro-*.whl`：解压到 `<数据目录>/extensions/`，启动时自动加入 `sys.path` ——
   **不需要 pip**，冻结的桌面版同样适用（接口 `POST /api/license/package`，只接受 automind_pro 本身）；
2. 粘贴许可证：当场离线校验，有效才写入 `~/.automind/license`（接口 `POST /api/license/activate`）；
3. 就地激活：新特性的路由热挂载、调度循环当场启动，**无需重启**。

`GET /api/license` 返回当前版本、扩展包状态与"卡在哪一步"（`need_package` / `incompatible` / `need_license` / `active`），
v1.11.14 起还给出**版本匹配信息**：`core_version` / `version_match` / `version_reason` / `version_step`
（扩展包与主程序不同号时提示换包，不破坏升级面板现有字段）。
三个接口都是管理动作：本机直接可用，远程需管理员令牌。环境变量 `AUTOMIND_LICENSE` 优先于界面里填写的许可证。

激活结果可通过 `GET /api/health` 的 `edition` 字段确认
（`community` / `pro` / `enterprise`）；Web 界面左上角也会显示对应徽标。

- 许可证无效或过期时**自动降级为社区版**，服务照常启动，不会报错退出。
- 社区版调用商业端点（如 `/api/schedule`、`/api/stats/detail`）返回
  `403 + 升级提示`；Web 界面对应入口显示 🔒。

## 3. 技术架构：如何做到互不影响

```
automind/            ← 社区核心（MIT 开源）
  core/edition.py    ← 稳定扩展协议 v1：特性注册表 + 扩展加载器
  core/prompts.py    ← 双方共用的角色提示词（稳定资源）
  agent.py           ← run_multi/run_loop 仅做特性门控与委派
  server.py          ← 商业路由由扩展注册；社区版注册 403 降级路由

pro/automind_pro/    ← 商业扩展（闭源，独立打包）
  licensing/         ← Ed25519 非对称许可证：验签公钥 + 读取顺序（v1.7.0 起，只有公钥）
  license.py         ← 兼容入口：re-export licensing（老代码 `from automind_pro.license import ...` 继续可用）
  multiagent/        ← 协同模式编排器
  loop_engine.py     ← 循环工程引擎
  scheduler.py       ← 定时任务（路由 + 后台调度）
  workflow_pro.py    ← 工作流触发器 / 入站 webhook / 并发执行器 / 运行看板（v1.9.0）
  workflow_governance.py ← 工作流发布审批 / 权限 / 凭据 / 并发配额 / 运行审计（v1.10.0，企业版）
  workflow_from_trace.py / workflow_templates.py ← 轨迹生成草稿 / 模板库（v1.10.0）
  stats_pro.py       ← 高级统计仪表盘路由
  templates_pro.py   ← 自定义模板（专业版）
  audit_export.py    ← 审计报告导出 PDF（专业版）
  session_pool.py    ← 企业版会话池
  sso_ldap.py        ← 企业版 SSO / LDAP 单点登录
  rbac.py            ← 企业版细粒度权限（RBAC）
  model_gateway.py   ← 企业版私有模型网关
```

约束与承诺：

1. **单向依赖**：商业包依赖社区核心，社区核心从不 import 商业包
   （仅在运行时按名探测 `automind_pro`）。
2. **稳定契约**：商业包只使用 `edition.py` 文档中标注为
   *扩展契约 v1* 的接口（特性注册表、server_ctx 键、Agent 执行原语、
   `core/prompts.py`）。社区版任意重构只要保持这些契约不变，
   已发布的专业版无需改动即可继续工作。
3. **版本协商**：双方各自声明 `EXTENSION_API_VERSION`；不一致时扩展
   拒绝加载并记录日志，绝不半激活。
4. **打包隔离**：社区版 `pyproject.toml` 只收 `automind*`；`pro/` 目录
   有独立的 `pyproject.toml` 与商业许可证（LICENSE-COMMERCIAL.md），
   开源发布脚本（`scripts/build_community.py`）会强制校验产物中
   不含任何商业代码。

## 4. 许可证格式（商业版）

```
AMP2-<KID>-<PAYLOAD>-<SIG>

KID     : 验签公钥指纹（sha256(公钥) 前 16 位 hex），排障时一眼看出"哪把钥匙签的"
PAYLOAD : base32 紧凑 JSON {v,tier,到期日,客户标识,席位,签发日}
SIG     : Ed25519 私钥对 "AMP2|KID|PAYLOAD" 的签名（base32）
```

- **非对称签名**：客户端只有验签公钥（`pro/automind_pro/licensing/keys.py` 的
  `PUBLIC_KEY_HEX`，v1.11.14 已置备真公钥），签发私钥只在发行方离线的
  `pro/tools/issue_license.py` 里，**不随 wheel / sdist / git 分发** ——
  客户无法伪造，也改不了档位、到期日、席位。
- **未置备公钥 = 一律验不过（fail-closed）**：宁可商业版起不来，也不能"验不了就算过"。
  环境变量 `AUTOMIND_LICENSE_PUBLIC_KEY` 仍优先于内置公钥（测试/应急用）。
- 发行方签发（许可证 + wheel 打成 `.ampkg`）：

  ```bash
  python pro/tools/issue_license.py bundle PRO 20271231 acme
  python pro/tools/issue_license.py verify AMP2-xxxx-...   # 签完自己验一遍
  python pro/tools/issue_license.py doctor                 # 发行前自检
  ```

- 客户侧读取顺序：环境变量 `AUTOMIND_LICENSE` → 当前目录 `.automind_license` →
  `~/.automind/license`（应用内激活写这一份）。
- 旧格式 `AMP-<TIER>-<EXPIRY>-<CUSTOMER>-<SIG>`（对称 HMAC）**默认拒绝** ——
  密钥历史上随包分发过，已等同公开；存量客户请换发新证，仅换证窗口期可用
  `AUTOMIND_LICENSE_ALLOW_LEGACY_HMAC=1` 临时过渡。

## 5. 常见问题

- **社区版会被削功能吗？** 不会。上表社区版列的所有功能承诺保持开源，
  后续新增的基础能力也进入社区版。
- **从社区版升级要迁移数据吗？** 不需要。配置（`.automind_config.json`）、
  对话历史（`.automind/`）、记忆数据完全共用。
- **专业版影响社区版更新吗？** 不影响。扩展协议向后兼容；
  若协议升级（v2），会同步发布新版 automind-pro。
- **填了许可证为什么还是社区版？** 看 `GET /api/license` 的 `step` / `reason_text`：
  `need_package`（没装扩展包）、`incompatible`（协议版本不匹配）、
  `need_license`（许可证无效/过期/公钥未置备）、`active`（已激活）。
  扩展包与主程序版本不一致时还会给 `version_match` / `version_reason`（v1.11.14 起）。
