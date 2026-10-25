<div align="center">

# AutoMind

**通用自动化 Agent 框架 · 社区版**

聊天、干活、写代码 —— 一个能自己规划、调工具、纠错的本地 Agent 工作台。

[![CI](https://github.com/yl13571844594-arch/AutoMind/actions/workflows/ci.yml/badge.svg)](https://github.com/yl13571844594-arch/AutoMind/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/automind-agent?logo=pypi&logoColor=white)](https://pypi.org/project/automind-agent/)
[![Python](https://img.shields.io/pypi/pyversions/automind-agent?logo=python&logoColor=white)](https://pypi.org/project/automind-agent/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Downloads](https://static.pepy.tech/badge/automind-agent)](https://pepy.tech/project/automind-agent)
[![Release](https://img.shields.io/github/v/release/yl13571844594-arch/AutoMind?logo=github)](https://github.com/yl13571844594-arch/AutoMind/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)](https://github.com/yl13571844594-arch/AutoMind/releases/latest)

[English](README.en.md) · [使用手册](使用手册.md) · [更新日志](CHANGELOG.md) · [下载安装包](https://github.com/yl13571844594-arch/AutoMind/releases/latest)

</div>

![对话工作台](docs/images/chat.png)

融合 Claude Code、OpenAI Codex 与 Reasonix 的核心能力，
支持 MCP 协议、Skill 技能系统、分层规划、符号推理与自我纠错。
内置 **Web 工作台**（v1.0 起为 React 18 + TypeScript + Ant Design 现代化前端，
源码在 `web/`，构建产物随包内置开箱即用），可聊天、可工作、可编程，
并带 **📚 RAG 知识库**（上传文档，对话自动检索引用）。

> **数据不出本机。** 模型 API Key 存在本地配置文件，任务历史存在本地 SQLite，
> 知识库向量索引存在本地目录 —— 除了你自己配置的模型服务商，不向任何第三方发送数据。

## ✨ v1.11.18 更新亮点

- 🛡 **评测 / 无人值守审批，修掉"终端被拒 → 反复重试 → 超时爆 Token"**：评测执行器此前把审批模式配成
  `auto`，`terminal` 的敏感调用在**没有审批通道**的评测进程里被按拒绝处理，模型只能反复重试 ——
  标准工具赛道 `smoke_run_command` 因此超时 **237.2s**、烧掉 **436,698** token。现在评测默认
  **`approve_all` 无人值守审批**（`--approval-mode` 可覆盖，报告 notes 标注口径），执行器同时按任务
  `timeout_seconds` **硬性收敛**，评测不再挂死。
- 🪙 **中文写入一次完成**：`file_write` 成功后**从磁盘回读校验**，回执带
  `size_bytes` / `chars` / `encoding` / `verified` / `readback_matches` —— 写好就是"已验证"，模型不需要再
  `file_read` 或开终端复核（这是简单任务 Token 超限的主因：`smoke_chinese_content` 两条赛道
  180,170 / 139,053 token）。工具描述与编程提示词也明确支持中文文件名 / 内容（UTF-8），
  并加入"简单任务验证一次即收尾"的纪律。
- 🧰 **多任务与协作能力补齐**：`pwsh` 新增原生后台通道（`background=true` 立即返回 `task_id`，用
  `terminal_background` poll / output / list / kill，长耗时 PowerShell 命令不再同步等死）；
  `subagent_list` 支持 `agent_id` 查看单个子智能体的**完整输出**（超长标注截断）；
  新增团队共享任务工具 **`team_task`**（list / summary / create / update / claim / comment），
  接入团队任务看板 SQLite 存储并实时广播到 Web 看板 —— Agent 可以自己查重、认领、更新状态、留痕，
  和人在同一块看板上协作。
- 🚀 **v1.11.18 发布**：核心 / 商业扩展包 / Web / Windows 安装器 / 使用手册统一 **1.11.18**；
  本版为补丁发版，商业扩展包与前端无功能变更；版本一致性由 `tests/test_version_consistency.py` 守住（六处同号）。

## 界面一览

<table>
<tr>
<td width="50%"><img src="docs/images/tools.png" alt="工具面板"><br>
<b>🔧 工具面板</b> —— 41 个内置工具（含团队任务 / 后台 pwsh / 文件回读校验等新能力）、技能、MCP 与插件，按来源标注，可搜索可开关</td>
<td width="50%"><img src="docs/images/workflow.png" alt="工作流"><br>
<b>🧩 工作流</b> —— YAML 写死每一步：可评审、可 diff、可进版本库，长流程稳定可复现</td>
</tr>
<tr>
<td width="50%"><img src="docs/images/team.png" alt="团队协作"><br>
<b>👥 团队协作</b> —— 在线成员、共享任务看板、评论与状态流转，人机在同一块看板上协作</td>
<td width="50%"><img src="docs/images/kb.png" alt="知识库"><br>
<b>📚 RAG 知识库</b> —— 传文档进去，对话时自动检索引用</td>
</tr>
</table>

## 版本说明：社区版 / 专业版 / 企业版

本仓库开源部分为 **社区版**（MIT 许可，免费）。商业能力由独立的
`automind-pro` 扩展包提供，安装并配置许可证后自动激活，详见
[docs/EDITIONS.md](docs/EDITIONS.md)。

| 功能 | 社区版（免费开源） | 专业版 | 企业版 |
|------|:---:|:---:|:---:|
| 💬 对话 / ⚙️ 工作 / 💻 编程 三种模式 | ✅ | ✅ | ✅ |
| 工具 / 技能 / MCP / 插件系统 | ✅ | ✅ | ✅ |
| 分层规划 · 符号推理 · 自我纠错 · 记忆系统 | ✅ | ✅ | ✅ |
| Web 工作台 + CLI + 自主任务闭环 | ✅ | ✅ | ✅ |
| 工具审批 · 安全审计 · 鉴权 / 限流 / 脱敏 | ✅ | ✅ | ✅ |
| 基础统计（任务/成功率/Token/工具排行） | ✅ | ✅ | ✅ |
| 📚 RAG 知识库（分段/embedding/对话自动检索） | 5 文档·10MB | 无限·200MB | 全部不限 |
| 📚 多知识库 · Reranker · 引用溯源 · 定时重嵌入 | — | ✅ | ✅ |
| 📚 混合检索 · 热度统计 · 检索审计 · 批量导入 | — | — | ✅ |
| ⚡ 语义缓存（相似问题秒回省 Token） | — | ✅ 基础 | ✅ 高级 |
| 🧭 模型智能路由（按复杂度分级选模型） | — | ✅ 2 级 | ✅ N 级 |
| 💰 成本仪表盘（模型成本/缓存节省分析） | — | — | ✅ |
| 📏 每日任务 / 工作区数量 | 100 次·3 个 | 不限·30 个 | 全部不限 |
| 🤝 协同模式（多智能体编排） | — | ✅ | ✅ |
| 🔁 循环模式（Loop Engineering） | — | ✅ | ✅ |
| ⏰ 定时任务 | — | ✅ | ✅ |
| 📊 高级统计仪表盘（命中率/效率/趋势） | — | ✅ | ✅ |
| 📈 观测中心（执行流程 DAG + 实时看板） | 当前任务 DAG（只读） | + 看板/历史 200/导出 | + 会话维度·历史 2000 |
| ⭐ 自定义模板（提示词资产沉淀） | — | ✅ | ✅ |
| 📄 审计报告导出（PDF） | — | ✅ | ✅ |
| 🎓 专家进阶（无限创建/分享/导入导出/统计） | 基础（3 个） | ✅ | ✅ |
| 🏛️ 企业专家市场（审批发布） | — | — | ✅ |
| 👥 多用户会话池（执行态隔离） | — | — | ✅ |
| 🔐 SSO / LDAP 单点登录集成 | — | — | ✅ |
| 🧩 细粒度权限（RBAC 角色矩阵） | — | — | ✅ |
| 🚪 私有模型网关（出口收敛/模型白名单） | — | — | ✅ |
| 独立统计分析服务（analytics-service） | — | — | ✅ |

> 安全能力（鉴权、限流、密钥脱敏、审计）**全部保留在社区版** — 安全不设付费墙。

> 💼 **商业版激活（v1.11.14 起；v1.11.15 升级为许可证 v3）**：社区版 → 专业版 / 企业版的授权是一条
> **离线 Ed25519 许可证**（`AMP2-…`；客户机只内置验签公钥，断网也能校验，无法伪造 / 改档位 / 改到期日）。
> v1.11.15 支持**序列号、机器绑定、生效起始日**（v3，向后兼容 v2），并提供**批量发证 + 台账、
> 试用期 `--days`、离线 CRL 吊销、周期复检 + 宽限期、公钥平滑轮换** 等运营能力。
> 拿到发行方的 `.ampkg` 一键升级包后，在「设置 → ⭐ 升级专业版 / 企业版」里把文件拖进
> 「⚡ 一键升级」即完成**装包 + 激活** —— 不需要 pip、不需要改环境变量、不需要管理员权限。
> **升级换发与主程序同号版本的 `.ampkg`（当前 v1.11.18；本版未改商业扩展包，已换发 v1.11.17 的客户可继续使用）**（公钥未轮换 → 旧许可证继续有效；旧 `.ampkg` 仍可激活，
> 但只有当前版本的包内含 v3 许可证 / 机器绑定 / CRL / 周期复检等新扩展能力）。
> 发行侧出包前用 `issue_license.py doctor`（12 项）自检公钥 / 私钥 / wheel / 版本 / CRL / 轮换 / 复检一致性。
> 完整流程见 [docs/COMMERCIAL_ACTIVATION.md](docs/COMMERCIAL_ACTIVATION.md)，
> 运营 SOP 见 [docs/LICENSE_OPERATIONS.md](docs/LICENSE_OPERATIONS.md)。

## 五种交互模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| 💬 **对话** | 纯多轮对话，不调用工具，响应最快（支持图片输入/视觉模型） | 问答、咨询、头脑风暴 |
| ⚙️ **工作** | 分层规划 + 工具执行 + 符号验证 | 建项目、跑命令、改文件等自动化任务 |
| 💻 **编程** | ReAct 思考-行动循环，聚焦代码 | 阅读 / 编写 / 调试 / 重构 / 测试 |
| 🤝 **协同**（专业版） | 多智能体分工协作并综合 | 复杂、跨角色的长任务 |
| 🔁 **循环**（专业版） | Loop Engineering：自主"行动-观察-修正"闭环 | 需反复迭代直到达标的任务 |

## 工具审批模式（Approval）

顶部下拉可切换工具调用的审批策略（Reasonix 风格 `deny > ask > allow` 门控）：

- 🙋 **询问** — 除只读工具外，每次工具调用前弹窗请求人工批准。
- ⚡ **自动**（默认）— 自动批准普通/低风险工具，仅高危操作需确认。
- ✅ **全批准** — 跳过所有工具审批，完全自主运行（慎用）。

## 循环工程 / 定时任务 / 统计

- **🪙 Token 预算窗口与多模式续跑（v1.11.16）**：单任务 Token 预算是一个**窗口**，用满自动压缩上下文并
  续开新窗口；协同 / 循环 / 子智能体预算耗尽时不再静默失败，而是带进度落检查点，右栏出现
  「继续协同 / 继续循环」，接着做已完成的部分。详见下方「断点续跑」。
- **循环模式**（专业版）自带停止条件（任务完成 / 连续无进展 / 达到最大轮数）与人工中断，防止死循环。
- **⏰ 定时任务**（专业版）：按固定间隔自动执行某个任务（任意模式），后台调度并记录结果。
- **📊 统计分析**：社区版提供基础聚合（任务数、成功率、Token、耗时、工具排行）；
  命中率环图 / 上下文使用率 / 效率趋势等高级仪表盘为专业版功能。
- **📈 观测中心**：把执行过程实时画成 DAG（任务→计划步骤→工具调用），
  状态配色 + 耗时/失败原因，看清 Agent 卡在哪一步。社区版可看**当前任务**的
  实时 DAG（只读）；专业版加实时看板（成功率 / P50·P95 / 工具热度 / 失败归因）、
  运行历史回看与导出；企业版增加按会话维度的分组统计。

## 🧭 智能路由与成本（专业版起；v1.11.15 界面重做，v1.11.16 稳定性修复，v1.11.17 路由挂载修复）

侧边栏「🧭 路由与成本」提供语义缓存、模型智能路由与成本分析，v1.11.15 起整页视觉重做：

- **5 张 KPI 卡**：累计成本 / 总 Token / 缓存命中率 / 路由命中次数 / 近 30 天日均成本 ——
  成本与 Token 用真实 30 天序列画 mini 柱状图；缓存 / 路由无按日数据时用命中率条与档位分布条，
  **不画假曲线**。
- **语义缓存**：SVG 环形命中率仪表 + 容量条 + 估算节省金额（相似问题秒回，0 Token）。
- **模型路由**：0~100 复杂度阶梯可视化 + 可编辑档位（专业版 2 级 / 企业版 N 级）；
  「路由预演」输入任务文本，用复杂度圆环展示将命中哪一档。
- **企业版成本仪表盘**：金额 / Token 双视角 30 天柱状图、按模型成本占比、缓存节省与口径说明。
- **口径**：成本按内置单价表估算（未知模型按 0 成本计），`GET /api/costs` 在保留旧字段的前提下
  新增 `total_tokens` / `by_day` / `by_model[*].share` / `top_model` 等字段，页面与接口同口径。
- **权限状态**：社区 / 专业 / 企业三种能力卡统一样式，未解锁项显示升级入口而不是伪装成 0 数据。
- **稳定性与旧数据兼容（v1.11.16）**：修复旧版服务端 `/api/cache` 缺少 `threshold` 时 `toFixed` 崩溃
  导致的整页白屏；`/cache` `/router` `/costs` 响应先统一 sanitize（缺字段 / null / 字符串数字 / 坏档位），
  数字只在有效值后格式化；页面级 + 五个区块级错误边界，单卡异常只降级该卡并可重试。
- **路由挂载修复（v1.11.17）**：已激活 Pro / 企业版仍提示「该功能未开通或被版本门控（feature=semantic_cache）」
  的根因是社区核心手工维护的"可挂载特性"清单漏掉了所有自带 REST 路由的商业特性 —— 真实路由从未注册，
  请求只命中 403 降级路由。现在清单从 `COMMERCIAL_FEATURES` 派生，启动与热激活都会挂载真实路由；
  若界面再次出现 403 提示，文案会直接指向「重启服务 / 更新到与主程序同版本的扩展包」。

## 快速开始

```bash
# 方式 A：下载桌面安装包（推荐，不需要装 Python）
#   https://github.com/yl13571844594-arch/AutoMind/releases/latest
#   Windows .exe（代码签名） · macOS .dmg（通用二进制） · Linux .deb

# 方式 B：从源码安装
git clone https://github.com/yl13571844594-arch/AutoMind.git
cd AutoMind
pip install -e ".[web]"

# 方式 C：从 PyPI 安装
pip install "automind-agent[web]==1.11.18"     # 当前版本 · Web + OpenAI 兼容后端
# 升级：pip install -U "automind-agent[web]==1.11.18"；全部模型后端换成 [full]
```

> ✅ **v1.11.18 已发布**：PyPI 已同步，`pip install -U "automind-agent[web]"` 拿到的就是本版本；
> 桌面安装包见 [Releases](https://github.com/yl13571844594-arch/AutoMind/releases/latest)
> （Windows `.exe` 已代码签名 · macOS `.dmg` 通用二进制 · Linux `.deb`），
> 三平台包的 `SHA256SUMS` 随 Release 一并提供，可直接 `sha256sum -c` 校验。
>
> 内网 / 离线环境无法访问 PyPI 时，用 `pip download "automind-agent[web]==1.11.18"` 在联网机备好
> wheel 目录后拷入，再按 [docs/OFFLINE_OPS.md](docs/OFFLINE_OPS.md) 离线安装。

```bash
# 启动 Web 工作台（推荐）
python -m automind.server --port 8765
# 然后浏览器打开 http://localhost:8765

# Windows 一键启动
launch.bat

# CLI 交互模式
automind
automind "你的任务描述"
automind --version
```

### Docker

```bash
docker compose up --build
# Web UI: http://localhost:8765
```

## 模型配置

打开 Web 工作台后，点击左下角 **「⚙ 设置」→「🔑 API Keys」**：

- 支持 OpenAI / Claude / DeepSeek / Kimi / 百炼 / 智谱 / 豆包 / Gemini / Grok / Ollama。
- **自定义 OpenAI 标准接口（中转代理）**：在底部填写 `api_base`（如 `https://api.your-proxy.com/v1`）、
  默认模型与 API Key，即可对接任意兼容 OpenAI `/v1/chat/completions` 的服务或中转站。
- 每个提供商均可自定义具体使用的模型名（模型名输入框可直接输入任意模型）。
- **🧠 输入框模型选择器（v1.11.15）**：输入框旁的下拉直接切换模型 —— 默认模型 / 按提供商分组的
  已配置模型（只列出拿得到 Key 的提供商与 Ollama 本地模型，自定义收藏置顶）；选择对后续消息常驻生效
  （本地持久化），断点续跑 / 重试沿用当前选择。手动选择的模型初始化失败会在回执里说明原因并保持原模型，
  **不会静默换成你没选的模型**；`task_start` 回执会带上实际生效的 provider / model。
- **🗺 自动路由（专业版 / 企业版）**：模型选择器里可切「自动路由」—— 服务端按任务复杂度（0~100 启发式
  打分）命中路由档位并切换本次运行的模型，`task_start` 回执带档位 / 复杂度 / 目标 provider / model。
  社区版该项显示 🔒 并引导打开升级面板；服务端即使收到 auto 也只回执"需要专业版 / 企业版"。
  优先级：**显式手动选择 > 自动路由 > 全局默认模型**。
- API Key 仅保存在本地 `.automind_config.json`，不会上传。
  也可通过环境变量配置（如 `OPENAI_API_KEY`、`DEEPSEEK_API_KEY`、`MOONSHOT_API_KEY` 等）。

## 项目目录 / 工具 / 技能 / MCP

- **本地项目目录**：右上角 **📁** 徽标或「设置 → 通用 → 浏览」可选择本地目录作为 Agent 工作根目录。
- **自定义模型**：模型配置中输入模型名后点「➕ 添加」即可持久化到下拉列表（按提供商区分，可删除）。
- **工具面板**（侧边栏 🔧）分三个标签：
  - **工具** — 内置工具列表（含权限等级与风险分）。
  - **技能** — 内置技能 + 「加载本地技能目录」（扫描含 `AbstractSkill` 子类的 `.py`）。
  - **MCP** — 添加并连接 MCP 服务器（stdio / sse），自动发现其工具（需 `pip install mcp`）。

## 工作区 / 模板 / 主题 / 撤销

- **🗂 工作区**：右上角工作区徽标可管理/切换多个工作区，每个工作区 = 独立目录 +
  独立上下文，切换后 Agent 在新目录下操作，任务之间互不污染。
- **💬 会话管理（v1.11.17 分组 / 排序 / 筛选 / 搜索）**：左侧栏「会话」区新增 🔍 快速搜索；
  标题旁的 ⚙️ 或设置菜单「💬 会话管理」打开管理面板 —— 范围可切「当前 / 全部工作区」，
  实时搜索标题 / 要点摘要 / 会话 ID（空格分词、多关键词全部命中），筛选全部 / 置顶 / 运行中 / 已归档，
  分组支持不分组 / 按时间 / 按状态 / 按工作区，排序支持最近活动 / 创建时间 / 标题 / 轮数（置顶始终优先）；
  打开 / 重命名 / 置顶 / 归档 / 恢复 / 删除都在面板内完成，视图设置刷新后保留。
  归档仍会把要点写入本工作区长期记忆，新会话第一轮自动带上（见 8.7.1）。
- **📚 模板库**：内置 10 个基础模板（建站/脚手架/修 Bug/补测试/数据分析/爬虫等），
  欢迎页与 📚 按钮一键填入即可开跑；首次打开有 4 步**新手引导**（❓ 可重看）。
- **🌓 浅色模式**：左下角「⚙ 设置」菜单随时切换深/浅主题，跟随系统偏好自动初始化。
- **💰 实时成本**：右栏 Token 面板与每条结果显示预估花费（内置主流模型单价，可自定义）。
- **↩️ 撤销/回滚**：Agent 改过的文件记录在右栏「文件改动」，可单文件撤销或全部回滚。
- **▶ 断点续跑（v1.11.16 四模式）**：任务中断 / 出错 / Token 预算耗尽后，**计划 / ReAct / 协同 / 循环**
  四种模式都能从断点继续（进程重启后也能发现检查点）：只跑剩余步骤 / 角色 / 轮次，已完成的工作不重做；
  同毫秒检查点 id 冲突导致静默覆盖的问题已修复。
- **📄 代码编辑器（Web IDE）**：右栏「📄 代码」标签 = 文件树 + Monaco 编辑器 +
  改动 Diff 预览，浏览器直接改代码（Ctrl+S 保存、可撤销），聊天区不受影响。
- **🎓 专家市场**：官方精选 10 个专家一键安装、自建专家（社区版 3 个），
  激活后所有任务带角色设定执行；专业版无限创建/分享/导入导出/统计。
- **👥 团队协作**：v1.11.15 起升级为完整团队工作台（在线成员 / 四列看板 / 优先级 / 截止 / 评论 / 汇总），
  工作区 / 模板 / 专家为服务器级存储，同一部署天然团队共享，详见下节。
- **🔌 Agent 集成**：内置 OpenAI 兼容接口（`/v1/chat/completions`，SSE 流式）；
  左下角「⚙ 设置」→「🔌 Agent 集成」一键生成 **Continue.dev**（VS Code/JetBrains 面板）与 **Cline** 配置，
  Cline / Zed 等任何 OpenAI 客户端同样可接。

## 👥 团队协作工作台（v1.11.15）

同一台服务 = 同一个团队。侧边栏「👥 团队」现在是一个完整的团队工作台：

- **在线成员感知**：实时广播连接 / 断开 / 改昵称，在线成员条显示头像首字、在线点与任务数；
  多标签页按同一人聚合，昵称本地持久化、重启后重连可恢复。
- **任务模型扩展**：在标题 / 指派之外新增**优先级**（低 / 普通 / 高 / 紧急）、**截止日期**、**标签**、
  **评论**与**活动时间线**，并支持**阻塞**状态；旧任务读取时自动补默认值，零迁移、id 不变。
- **四列看板**：待办 / 进行中 / 阻塞 / 完成；卡片带优先级色条、逾期红色徽标、负责人、评论数与标签，
  支持搜索和按人 / 优先级 / 状态过滤，也可按截止日期排序。
- **实时与汇总**：创建 / 更新 / 评论 / 认领 / 流转全部实时广播给在线成员；5 张统计卡展示完成率、
  逾期、今日 / 近 7 天完成与最近活动，另有按人负载汇总。详情抽屉可编辑字段、移动状态、评论与删除确认。
- **单实例语义**：「在线成员」是**当前进程内**的 WebSocket 连接状态，不是跨机器集群；多实例部署下各自独立。

## 对话历史

对话模式的多轮记录持久化到 `.automind/chat_history.json`，刷新页面自动恢复；
点击 **🔄 新会话** 可删除当前对话记录。任务历史（侧边栏 📜）支持单条删除与全部清空。

## 流式输出 · 任务中断 · 中途插话

- **流式对话**：对话模式通过 WebSocket 逐字流式返回，带打字光标实时显示。
- **任务中断**：执行中点击 ■ 可真正取消后台任务（`asyncio.Task` 取消），对话/工作/编程均支持。
- **💬 中途插话（v1.7.2）**：AI 回答到一半发现它漏了一件事？**不用先停下** ——
  直接在输入框里补一句，按 `Ctrl/⌘+Enter`（或点「⤴ 插入补充」），它会把这句
  并进**正在生成的这一段回答**里继续写，你看到的是一段连续的回答。工作/编程模式
  同样有效（补充会在下一步开始前进入它的上下文）。气泡上会标记
  「补充 · 待本轮读取 → ✓ 已纳入本轮」；若这一轮在你补充之前就结束了，
  它会明确告诉你**这条没纳入**，而不是假装收到。
- WebSocket 不可用时自动回退到 REST 同步模式。

## 多模态 · 语音 · 预览 · Token 统计

- **🪙 Token 用量统计**：右栏实时显示每次任务的输入/输出 token 与累计总量、任务数，可一键重置。
- **🔍 HTML 预览**：模型输出的 ```html 代码块带「预览页面」按钮，在安全沙箱 iframe 中渲染；
  右栏「HTML 预览」列出项目目录中的 `.html` 文件，点击即可预览（`/api/preview/file`，限定项目目录防穿越）。
- **🎤 语音输入**：点击麦克风按钮用 Web Speech API 语音转文字（Chrome/Edge）。
- **📎 多模态**：可附加图片发送给视觉模型；输出中的图片/链接/表格自动渲染展示。

## 🤖 IM 机器人（飞书 / 企业微信 / 钉钉）

在「设置 → 机器人」里添加机器人，填好平台凭据 / Webhook 后，直接在那个聊天窗口里
发消息就能驱动本机 Agent 干活，执行过程与结果会回传到同一个聊天：

- **一个聊天 = 一个会话**：飞书、企业微信、钉钉、通用 Webhook 均支持；`/new` 开新会话，
  `/stop` 停止，`/mode` 切换对话 / 工作 / 编程，`/apply` 把隔离副本产物应用回项目。
- **审批在聊天里完成**：危险操作会发一条带编号的审批消息，回复「同意 / 拒绝」即可；
  超时按拒绝处理（fail-closed），不会悄悄放行。
- **工作区不混淆（v1.11.13）**：机器人会话固定使用 `bot-…` 会话命名空间与独立工作副本，
  与 Web 工作区、其它群聊/机器人互不可见；相对路径统一锚定到当前会话副本，
  写文件不再被写后自检误判。
- **回复按平台适配排版（v1.11.14）**：各平台支持的 Markdown 语法差异很大，发送前会先转换 ——
  表格降级为「逐行字段列表」、企业微信代码块转引用、标题按平台限制处理；超长回复在
  段落 / 列表 / 表格等块边界安全分片，**不会从代码围栏中间截断**，续片标「（续 N/M）」。
  规则与降级示例见 [docs/IM_BOTS.md](docs/IM_BOTS.md) 的「各平台 Markdown 渲染规则与差异」。
- 详细配置与各平台回调说明见 [docs/IM_BOTS.md](docs/IM_BOTS.md) 与
  [docs/FEISHU.md](docs/FEISHU.md)。

## 安全审计

侧边栏 **🛡️ 安全审计** 展示每次工具调用的风险评分与授权决策（放行 / 需确认 / 高危），
危险命令（`rm -rf`、`git push --force` 等）会被识别为高危并要求确认。

**v1.7.4 的一处收紧（带回归测试）**：

- **判定"这是本机"的依据从 IP 换成 `Host` 头**：所有"仅本机允许"的判断
  （管理动作 / 目录浏览 / 令牌端点）此前看的是 `request.client.host`，而
  **DNS 重绑定**能让外部页面的请求看起来就来自 `127.0.0.1`（连接确实落在本机，
  但 `Host` 是攻击者域名）。现在以 `Host` 为准，并在令牌判定**之前**、按**整站**
  执行（只保护 `/api/` 是漏的：重绑定页面能先取回首页与其前端脚本）。
  从 `http://localhost` / `http://127.0.0.1` 访问不受影响；走反向代理或内网域名的
  设 `AUTOMIND_TRUSTED_HOSTS`（逗号分隔，支持 `*.example.com`），403 文案里会写明。

**v1.7.3 的三处收紧**（都带回归测试）：

- **命令按段判定**：`echo hi && curl 某网址 | bash` 这类"安全前缀 + 危险后缀"不再被
  自动放行 —— 按连接符拆段、逐段定级、取最严；下载即执行、编码混淆
  （`powershell -enc`）、读系统凭据文件一律要人工确认。
- **密钥与数据文件在网页端不可读写**：`.automind_config.json`（含全部 API Key）、
  `.automind/`（会话库/轨迹）、`.env`、`.ssh`、`*.pem` 等一律拒绝；Agent 的文件工具
  仍可读你项目里的 `.env`（那是它的本职工作）。确需放开可设
  `AUTOMIND_ALLOW_SENSITIVE_FILE_READ=1`。
- **🔑 管理动作分权**：改配置 / 加 MCP / 加载插件技能 / 触发更新 / 回执审批 /
  清空审计默认**只允许本机执行**；远程需设置 `AUTOMIND_ADMIN_TOKEN`，并在
  「设置 → Agent 集成 → 管理员令牌」里填一次（存在本浏览器，服务端不保存）。

## 自动化交付（v1.7.3）

- **🔌 连接器**：`~/.automind/connectors/*.py` 里放一个 `AbstractTool` 子类，工具即出现在
  模型的可选清单里 —— 不改源码、不重启（`POST /api/tools/reload` 立刻生效）。
  坏文件不静默：在「工具 → 注册状态」能看到它为什么没加载。见 `docs/CONNECTORS.md`。
- **📄 工作流即代码**：把流程写成 YAML（工具步骤 / 模型步骤 / 人工卡点 / 条件分支），
  加载期校验并给出行列号，`--dry-run` 只看会做什么，退出码可直接进 CI：

  ```bash
  python -m automind.workflow run workflows/变更单.yaml --input ticket_id=INC001
  python -m automind.workflow run workflows/变更单.yaml --dry-run
  ```

  见 `docs/WORKFLOWS.md`。接口：`/api/workflow/run|validate|list`。
- **🔀 工作流自动化强化（v1.11.16）**：步骤级 `if` / `unless` 条件执行、失败降级
  `on_failure: fallback(<步骤id>)`、工作流级 `timeout_seconds` 整轮硬超时、每步 / 整轮 Token 统计进入
  报告与看板；专业版触发器支持 cron 错过补偿（`catch_up` / `max_delay_seconds`）与 webhook 重放保护
  （`dedupe_window`）。全部可选、默认等价旧行为。见 `docs/WORKFLOWS.md`。
- **📋 评测与重放**：`AUTOMIND_REPLAY=1` 记录每次模型调用的**完整输入输出**（默认关闭，
  关闭时零成本），`python -m automind.core.replay <文件>` 照着重跑并比对；
  `python -m automind.eval run smoke` 跑内置套件，给出通过率/耗时/成本，
  退出码 0/1/2/3 区分"全过 / 有未通过 / 配置问题 / 中断"。见 `docs/EVAL.md`。
- **📣 出站事件与外部审批**：任务与审批事件推送到你的工单/IM
  （`AUTOMIND_WEBHOOKS`，HMAC 签名 + 指数退避 + 有界队列，绝不阻塞任务）；
  审批可在外部系统里回执（`POST /api/approvals/{id}`，迟到回执明确 409）。
  见 `docs/WEBHOOKS.md`。
- **📊 运维接口**：`GET /metrics`（Prometheus 文本格式，零新依赖：任务数/耗时分布/
  工具调用/审批/插话/预算/token/webhook 投递）与 `GET /api/health/ready`
  （真检查数据目录/SQLite/项目目录/磁盘，任一不可用返回 **503**）。

## 商用部署 / 多用户

- **会话隔离**：每个浏览器拥有独立 `session_id`，对话历史互不可见、互不覆盖（持久化于 `.automind/chats/`）。
- **访问鉴权**（可选，默认关闭）：设置令牌后，所有 `/api/*` 与 `/ws` 需携带令牌：

  ```bash
  # Windows PowerShell
  $env:AUTOMIND_AUTH_TOKEN="your-secret-token"
  $env:AUTOMIND_CORS_ORIGINS="https://your-domain.com"   # 收紧跨域（可选）
  $env:AUTOMIND_MAX_CONCURRENT="16"                        # 并发任务上限（默认 8）
  python -m automind.server --host 0.0.0.0 --port 8765
  ```

  前端请求需带 `Authorization: Bearer <token>`，WebSocket 用 `ws://host/ws?token=<token>`。
- **Host 准入（v1.7.4）**：所有"仅本机允许"的判定（管理动作 / 目录浏览 / 令牌端点）
  以请求头 `Host` 为准 —— 挡住了 **DNS 重绑定**（把外部域名解析到 `127.0.0.1`，
  借"IP 是回环"冒充本机）。从 `localhost` / `127.0.0.1` 访问无感；
  走反向代理、隧道或内网域名的，把域名写进 `AUTOMIND_TRUSTED_HOSTS`
  （逗号分隔，支持 `*.example.com`），否则会被 403 —— 报错文案里会写明怎么填。
- **健康检查**：`GET /api/health`（无需鉴权）返回版本、运行任务数、并发上限、uptime，供探活/负载均衡。
- **资源保护**：并发任务超过 `AUTOMIND_MAX_CONCURRENT` 时返回 429。
- **🛡 执行治理参数（v1.11.16）**：「设置 → 执行治理」的 **26 个参数**逐项真正生效（此前轨迹开关、
  终端超时、写冲突策略、审批超时等 7 类"设了不生效"已修复），界面逐项标注「即时生效 / 下次任务生效 /
  下次审批生效 / 下次调用生效 / 下次写入生效」，接口返回 `applied / rejected / effective`；
  默认值、读取位置、生效时机与调参建议见 [docs/GOVERNANCE.md](docs/GOVERNANCE.md)。
- **商业版激活与升级（v1.11.15）**：内网 / 生产环境同样走离线流程 —— 把发行方给的 `.ampkg`
  拖进应用内「⭐ 升级专业版 / 企业版」面板即可（无需 pip、无需外网）；多用户部署一次激活全机生效，
  `GET /api/health` 的 `edition` 字段可确认当前档位（`community` / `pro` / `enterprise`）。
  激活 / 升级属于管理动作：本机可直接操作，远程需管理员令牌（`AUTOMIND_ADMIN_TOKEN`）。
- **许可证运营（v1.11.15）**：v3 许可证支持**序列号 / 机器绑定 / 生效起始日**；发行方可用
  `issue_license.py issue-batch` 批量发证并维护 12 列台账、`revoke` + CRL 离线吊销、`--days` 发试用、
  `--start` 指定生效日；客户端按 `AUTOMIND_LICENSE_RECHECK`（默认 3600 秒）周期复检，
  `AUTOMIND_LICENSE_GRACE_DAYS`（默认 0）配置宽限期，系统时钟回拨超过 1 天会判定异常并降级。
  详见 [docs/COMMERCIAL_ACTIVATION.md](docs/COMMERCIAL_ACTIVATION.md) 与
  [docs/LICENSE_OPERATIONS.md](docs/LICENSE_OPERATIONS.md)。

> 注：社区版对话模式为**完整多用户隔离**；工作/编程等执行态任务按会话缓存轻量克隆
> （`clone_for_session`），交互模式、上下文、token 计数互不污染。
> **目录仍是共享的** —— v1.6.4 起用「按路径串行化写入 + 外来改动检出 + 可选会话
> 独立工作目录」三层防护，避免并发改同一工作区时静默互相覆盖（见
> `execution.write_conflict_policy` / `execution.isolate_workspace`）。
> **企业版**提供会话级 Agent 池（Session-Agent-Pool），执行态也按用户完全隔离，
> 并可对接独立统计分析服务。详见 [docs/EDITIONS.md](docs/EDITIONS.md)。

## 演示

```bash
python demo/e2e_demo.py
```

## 开源说明

- **许可证**：社区版（本仓库全部代码）以 [MIT 许可](LICENSE) 开源 — 可自由使用、
  修改、分发及商用，仅需保留版权与许可声明；软件按"现状"提供，不附带任何担保。
- **安装**：`pip install automind-agent`（PyPI），或克隆本仓库 `pip install -e ".[web]"`。
- **版本边界**：商业功能（协同/循环/定时任务/高级统计/会话池）由闭源扩展包
  `automind-pro` 提供，**不在本仓库中**；社区核心通过
  [`automind/core/edition.py`](automind/core/edition.py) 的稳定扩展协议与其对接，
  未安装扩展时以社区版完整运行。安全能力永久保留在社区版。
- **隐私**：AutoMind 本地运行，API Key 与聊天记录只存在你自己的机器上
  （`.automind_config.json` / `.automind/`），不会上传任何遥测。
- **参与贡献**：见 [CONTRIBUTING.md](CONTRIBUTING.md)；版本变更见 [CHANGELOG.md](CHANGELOG.md)。

## 参与社区

| 我想…… | 去这里 |
|---|---|
| 🐛 报个 bug | [新建 Issue](https://github.com/yl13571844594-arch/AutoMind/issues/new?template=bug_report.yml) |
| ✨ 提个功能建议 | [新建 Issue](https://github.com/yl13571844594-arch/AutoMind/issues/new?template=feature_request.yml) |
| 💬 问怎么用 / 不确定是不是 bug | [Discussions](https://github.com/yl13571844594-arch/AutoMind/discussions) |
| 🔒 报告安全漏洞 | [私密通道](https://github.com/yl13571844594-arch/AutoMind/security/advisories/new) —— 请勿发公开 Issue |
| 🛠 提交代码 | [贡献指南](CONTRIBUTING.md) · [行为准则](.github/CODE_OF_CONDUCT.md) |

安全策略与威胁模型（哪些算漏洞、哪些是设计内行为）见
[SECURITY.md](.github/SECURITY.md)。
