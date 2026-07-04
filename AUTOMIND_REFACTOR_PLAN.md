# AutoMind 重构优化方案

> 版本: 1.0 | 日期: 2026-06-27 | 基于 v0.1.0 源码全面分析

## 实施进度（持续更新）

**本轮已完成（Phase 1 安全切片 + UI 升级）：**

- ✅ **2.3 测试地基** — 新建 `tests/`，含 `test_json_utils`(13) / `test_permissions`(11) / `test_dependency_graph`(6) / `test_types`(8)，`pytest` 全绿（38 passed）。`pyproject.toml` 增加 pytest 配置。
- ✅ **3.2 增强 Loop 停止条件** — `agent.run_loop` 升级为三级停止：语义完成 / 输出收敛（difflib 相似度>0.95，无 embedding 依赖）/ 连续空转（两轮未执行任何工具操作），并保留无进展与最大轮数。UI 同步新增 `converged`/`idle` 停止原因展示。
- ✅ **3.3 结构化规划验证** — `HierarchicalPlanner._validate_goal_tree` + `_parse_tool_specs`：解析后校验叶子动作是否使用真实工具与合法参数名，违规则回退模板分解，避免执行非法动作。
- ✅ **UI 界面升级** — 设计令牌重做（分层背景、violet→blue 渐变主色、更柔阴影），侧边栏激活指示条 + 渐变 Logo，模式切换器/发送键/主按钮/用户气泡统一渐变，统计卡片描边与 hover。所有视图渲染正常、无控制台错误、功能零回归。

**第二轮（执行过程可视化 + §8/§9 体验项）：**

- ✅ **§8.5 执行可视化 / F-04** — 工作/编程/循环模式执行任务时**实时展示执行过程**：Agent 新增 `event_sink` 事件总线，ReAct 的思考(`step_thought`)/工具调用(`step_action`)、计划生成(`plan_created`)与逐步状态(`plan_step_start/end`)、回溯(`plan_backtrack`)经 WebSocket 流式推送；前端「⚙️ 执行过程」面板以彩色轨迹卡片实时渲染（思考=蓝、工具=绿、计划=紫、回溯=黄），计划步骤行实时 ○→◐→✓/✗。实测工作/编程模式均正常展示且文件真实创建。
- ✅ **§8.2 输出操作增强（F-02）** — 每条助手消息悬浮「⧉ 复制」按钮（复制整条），每个代码块带语言标签头 + 「⧉ 复制」按钮；剪贴板 API + execCommand 双重兜底。
- ✅ **§9.4 消息渲染优化** — 代码块独立卡片化（头部 + 圆角）、表格 `overflow-x:auto` 防溢出。
- ✅ **§9.3 快捷键（子集）** — `Esc` 关弹窗 / `Ctrl+.` 中断任务 / `Ctrl+L` 新会话。

**第三轮（商用能力加固）：**

- ✅ **§7 多用户会话隔离（安全切片）** — `agent.chat/chat_stream` 新增 `history` 入参；服务端按 `session_id` 维护独立对话历史（`.automind/chats/<sid>.json`），`/api/run`(REST+WS) 与 `/api/chat/history`(GET/DELETE) 均按会话隔离；前端用 `localStorage` 持久化每浏览器的 `SID` 并随请求携带。**实测**：A 会话有记录、B 会话为空、A 重置不影响 B（修复 §7 缺陷 2「对话历史全局共享」）。`default` 会话向后兼容旧单文件，单用户行为零变化。
- ✅ **商用安全：可选鉴权** — `AUTOMIND_AUTH_TOKEN`（环境变量或配置）开启后，所有 `/api/*` 与 `/ws` 需带 `Authorization: Bearer` 或 `?token=`；`/api/health` 与首页放开。`AUTOMIND_CORS_ORIGINS` 可收紧跨域。默认不鉴权，保持本地易用。
- ✅ **资源保护：并发上限** — `AUTOMIND_MAX_CONCURRENT`（默认 8）限制同时运行的任务数，超限返回 429，防止资源耗尽。
- ✅ **运维：健康检查** — `GET /api/health`（无需鉴权）返回版本/运行任务数/并发上限/uptime，供探活与负载均衡。
- ✅ **测试** — 新增 `tests/server/test_commercial.py`（6 例：健康检查、会话隔离、向后兼容、鉴权三态），**全套 55 测试通过**。

**第四轮（§11 高级统计 + §12 高端视觉，即 F-15 / F-16）：**

- ✅ **F-15 高级统计（§11）** — 新增三个端点：`/api/stats/detail`（上下文使用率、工具/计划/任务/自我修正命中率 + 综合平均、Token 效率、记忆系统指标）、`/api/stats/context`（实时上下文快照）、`/api/stats/history`（最近 50 次趋势）。数据全部取自系统已有但未汇总的真实来源（`context_mgr.get_stats` / `resources.get_stats` / `permissions.audit_log` / 计划树递归统计 / 记忆计数）。**实测**：跑一个工作任务后命中率正确填充至 100%。
- ✅ **F-15-4/5 前端仪表盘** — 「📊 统计分析」升级为高级仪表盘：4 个**命中率圆环图**（纯 CSS conic-gradient，按值变色）+ 综合平均胶囊、上下文**发光进度条**、效率/用量明细、**SVG 迷你趋势折线图**、记忆系统卡片。
- ✅ **F-16 高端视觉（§12）** — 全局**玻璃态**（侧边栏/头部/右栏/弹窗/助手气泡 `backdrop-filter: blur+saturate`）、**动态光晕环境背景**（body::before 缓动）、**Logo 渐变流光**、气泡 hover 微光、**模式按钮脉冲光晕**、消息入场弹性动画、弹窗缩放动画、**Inter 字体**、强调色微调（更通透）、圆环/发光条组件。
- ✅ **测试** — 全套 **55 测试通过**；新端点结构经 TestClient 验证；前端无控制台错误。

**第五轮（工具面板专业化 + SKILL.md 技能生态）：**

- ✅ **SKILL.md 技能支持** — 新增 `automind/skills/markdown_skill.py`（`MarkdownSkill`：解析 YAML frontmatter 取 name/description/emoji/依赖工具，正文作指令型技能）+ `SkillRegistry.discover_skill_md/discover_any`（递归扫描 `*/SKILL.md`，跳过 node_modules）。`/api/skills/load` 与 rebuild 同时加载 `.py` 与 `SKILL.md` 两种格式；`/api/skills` 暴露 emoji/type/依赖工具并排序。
- ✅ **导入桌面 skills** — 桌面 `skills` 文件夹的 **23 个 SKILL.md 技能全部导入**（agent-browser-basic 🌐、office-automation、wechat-automation 等），与 3 个内置技能共 26 个，重启自动恢复。新增「⬇️ 一键导入桌面 skills 文件夹」按钮。
- ✅ **工具面板专业化美化** — 🔧工具/✨技能/🔌MCP 升级为**分段控件（segmented control）**：图标 + 标签 + 实时数量角标 + 渐变高亮激活态 + 内阴影。技能卡片改为 **emoji 头像 + 类型徽章（内置/Python/SKILL.md）+ 依赖工具**，按内置→Markdown→Python 排序。
- ✅ **测试** — 新增 `tests/skills/test_markdown_skill.py`（4 例：frontmatter 解析、emoji 提取、技能加载、目录发现含 node_modules 跳过），**全套 59 测试通过**。

**第六轮（安全加固 + 真实缺陷修复，即 §14.11 / §14.1）：**

- ✅ **修复 PythonSandboxTool 致命缺陷（§14.11-2）** — 沙箱构建受限命名空间时用 `getattr(__builtins__, name)`，但模块被 `import` 时 `__builtins__` 是 **dict 而非 module**，导致 `AttributeError`，沙箱在**生产中每次执行必崩**。改为显式引用 `builtins` 模块；并将受控 `__import__` 注入到受限 `__builtins__` 中，使白名单内 `import math` 等语句真正可用、非白名单（`os`/`subprocess` 等）被拒。**实测**：基础执行、白名单 import、危险 import 拦截、`__import__` dunder 拦截全部正确。
- ✅ **文件工具路径穿越防护（§14.11-1）** — `FileReadTool`/`FileWriteTool`/`FileEditTool`/`FileMultiEditTool` 新增 `_RootGuard`：设置 `project_root` 后相对路径基于 root 解析，任何 `..` 穿越/符号链接逃逸到 root 之外均拒绝（用 `resolved == root or root in parents` 严格包含判断，规避 `startswith` 前缀碰撞）。`project_root=None` 时不限制，**向后兼容**库/测试用法。`agent._register_default_tools` 注册时传入 `project_root` 开启防护。
- ✅ **版本号统一（§14.1）** — `automind/__init__.py` 的 `__version__` 设为**唯一数据源**（0.2.0）；`server.py` 改为 `from automind import __version__`（原硬编码 0.3.0）；`pyproject.toml` 同步 0.2.0。`/api/health` 与 `/docs` 版本现一致。
- ✅ **测试** — 新增 `tests/tools/test_security_hardening.py`（13 例：沙箱 4 + RootGuard 4 + 文件工具 4 + 版本 1），**全套 72 测试通过**。

**第七轮（工程化与基础设施加固，即 §14.2 / §14.6 / §14.11 收尾）：**

- ✅ **输出敏感信息脱敏（§14.11-3）** — 新增 `automind/core/redact.py`：`redact_secrets` 识别并打码 OpenAI/Anthropic/AWS/Google/GitHub/Slack 密钥、`Bearer` 令牌、通用 `key/token/secret/password=...`、PEM 私钥块，保留首尾字符便于核对。服务端新增统一历史入口 `_push_history`，按环境变量 `AUTOMIND_REDACT_SECRETS` 对任务 `output` 脱敏（**默认关闭**，9 处历史写入全部归并至此单点）。
- ✅ **API 速率限制（§14.11-4）** — 新增 `automind/core/ratelimit.py`：纯内存 `SlidingWindowLimiter`（滑动窗口，无第三方依赖）。`/api/run` 入口在鉴权后按客户端 IP 限流，超限返回 429 + `Retry-After`。由 `AUTOMIND_RATE_LIMIT`（每分钟次数，**默认 0=关闭**）控制，健康检查不受影响。
- ✅ **WebSocket 源校验（§14.11-5）** — `/ws` 在握手阶段校验 `Origin` 头，不在白名单则以 4403 拒绝。由 `AUTOMIND_ALLOWED_ORIGINS`（逗号分隔，**默认空=不校验**）控制，防跨站 WS 劫持。
- ✅ **CI/CD（§14.2）** — 新增 `.github/workflows/ci.yml`：Python 3.11/3.12 矩阵，`ruff check` + `ruff format --check` + `pytest`。
- ✅ **打包元数据（§14.6）** — `pyproject.toml` 补全 `classifiers` 与 `[project.urls]`（Homepage/Repository/Issues），`readme` 已驱动 long_description，为 PyPI 发布就绪。
- ✅ **测试** — 新增 `tests/core/test_redact.py`（10）、`tests/core/test_ratelimit.py`（6）、`tests/server/test_security_features.py`（5），**全套 93 测试通过**。所有新能力默认关闭/向后兼容，单用户与既有 API 行为零变化。

**第八轮（§14.4 代码生成技能增强 — 提升编程/代码补全能力）：**

- ✅ **code_generator 技能全面增强（§14.4）** — 在"规格→代码→写文件"基础上升级为专业级代码生成器，**接口与结果结构保持不变、向后兼容旧调用**：
  - **语言自动检测**：依据 `output_file` 扩展名推断语言（`.ts`→typescript 等），扩展名优先于显式 `language`，避免误判。
  - **Markdown 围栏剥离**：`_extract_code` 自动从 LLM 的 ```lang 代码块中提取纯代码（修复"围栏混入源文件"的真实缺陷），优先匹配语言标签、否则取最长块。
  - **生成后语法校验 + 一次自我修复**：Python 走 `ast.parse`、JSON 走 `json.loads`；校验失败时让 LLM 修复并复校，成功则采用修复版本（`metadata.self_repaired`）。
  - **多模式**：`generate`（生成）/ `complete`（补全既有代码，代码补全能力）/ `scaffold`（脚手架）。
  - **增量与覆盖保护**：`overwrite`（默认 True 保持旧行为）/ `incremental`（追加）；`overwrite=False` 且文件存在时安全拒绝。
  - **专家级 system 提示**：要求完整可运行实现、禁止 TODO 占位、包含必要导入、遵循项目风格。
  - **无 LLM 模板兜底**：Python/JS/TS/HTML 生成语法合法的起始骨架。
- ✅ **测试** — 新增 `tests/skills/test_code_generator.py`（10 例：离线模板可解析、围栏剥离、自我修复、JSON 校验、语言检测、complete 补全、覆盖/增量保护、默认覆盖行为），**全套 117 测试通过**；内置技能注册与 `import automind` 正常。

**第九轮（§3.5 钩子 + §14.7 插件系统 + §14.8 更多内置技能）：**

- ✅ **生命周期钩子（§3.5 基础）** — 新增 `automind/core/hooks.py`：`AgentHooks`（before_run/after_parse/before_plan/after_plan/before_tool/after_tool/after_run/on_error，均可选、可同步或异步）、`invoke_hook`（**吞掉钩子异常，插件永不破坏主流程**）、`merge_hooks`（多插件同名钩子按序组合）。
- ✅ **插件系统（§14.7）** — 新增 `automind/core/plugin.py`：`PluginManager` 扫描 `~/.automind/plugins/*/plugin.json`，`load/unload/assemble_hooks/status`；入口 `entry_point="hooks:get_hooks"` 支持函数/实例/`AgentHooks` 子类三种形态。`AutoMindAgent` 增加 `self.hooks`/`self.plugin_manager`，`run()` 拆为薄包装（触发 before_run/after_run/on_error）+ `_run_impl`（原逻辑不变，另在解析后/计划后触发 after_parse/after_plan）；`apply_plugin_hooks()` 汇总生效。**默认无插件时零行为变化**。Web 新增 `GET /api/plugins`、`POST /api/plugins/{name}/load`、`POST /api/plugins/{name}/unload`。
- ✅ **更多内置技能（§14.8）** — 新增 3 个纯标准库、确定可测的内置技能：
  - **log_analyzer**：日志级别统计、错误样本、高频异常、归一化模式聚类，可导出 Markdown 报告。
  - **doc_generator**：基于 `ast` 从 Python 源码（文件/目录）提取模块/类/函数签名与 docstring，生成 Markdown API 文档。
  - **dep_audit**：解析 `requirements*.txt` 与 `pyproject.toml`（`tomllib`），报告依赖总数、未固定版本、重复声明、已安装版本对照。
  - 三者注册进 `register_builtin_skills`（内置技能由 3 → 6），`server._BUILTIN_SKILLS` 同步，前端技能面板自动展示。
- ✅ **测试** — 新增 `tests/core/test_plugins.py`（14：钩子同步/异步/异常吞并/合并、插件发现/加载/卸载/状态/类入口/损坏清单/Agent 集成）、`tests/skills/test_builtin_skills.py`（11：三技能核心行为 + 注册），**全套 142 测试通过**；`import automind`/`import automind.server` 与内置技能注册均正常。

**第十轮（§14.10 前端工程化阶段 1 — 单文件拆分 + 插件 UI + 版本动态化）：**

- ✅ **§14.10 阶段 1：单文件拆分** — `static/index.html`（2488 行）拆为 **150 行骨架 + 5 个 CSS 模块 + 6 个 JS 模块**，零构建步骤、零逻辑改动：
  - `css/`：`base.css`（设计令牌+全局）、`layout.css`（侧边栏+主区）、`components.css`（消息+工具面板）、`panels.css`（右栏+仪表盘）、`input-modal.css`（输入区+弹窗）。
  - `js/`：`core.js`（全局状态/会话隔离/Init/模式切换）、`ws.js`（WebSocket/协同/循环/执行面板/审批）、`chat.js`（发送/多模态/语音/预览/Token/消息渲染）、`settings.js`（模型/APIKey/通用/目录）、`panels.js`（统计/定时/工具·技能·MCP·插件）、`misc.js`（审计/历史/Toast/复制）。
  - **拆分方法学**：程序化按分节注释边界连续切割，**串接校验逐字节等于原块**（零漂移）；确认唯一加载时执行入口是 `DOMContentLoaded→init`（函数提升跨文件安全）后才实施。
  - 服务端 `app.mount("/static", StaticFiles(...))` 伺服模块（鉴权中间件仅护 /api/*，静态资源同 CDN 语义放行）。
- ✅ **插件界面配置入口** — 工具面板新增第 4 分段「🧩 插件」：目录说明卡（`~/.automind/plugins/<名>/plugin.json + hooks.py`）、重新扫描、插件卡片（名称/版本/描述/作者/已加载标签）+ 加载/卸载按钮，实测 load→unload 全链路 UI 与后端状态同步。
- ✅ **版本号动态化收尾（§14.1）** — 侧边栏 Logo 硬编码 `v0.3` 改为 `id="app-version"` 并在 init 时从 `/api/health` 读取（实测显示 v0.2.0），前后端版本单一数据源闭环。
- ✅ **测试与验证** — 新增 `tests/server/test_static_assets.py`（9 例：骨架引用完整、无内联块残留、全部资源存在且可伺服、模块无 `<script>/<style>` 杂质、**onclick 引用函数跨文件定义完整性**（防未来模块增删导致 ReferenceError）、关键入口函数存在）。浏览器实测：5 CSS + 6 JS 全加载、7 个侧边栏视图全渲染、设置弹窗开合、工具面板 4 分段正常、**零控制台错误、零失败请求**、视觉与拆分前一致。**全套 150 测试通过**。

**第十一轮（§14.3 Rich TUI + §14.5 英文文档 + §14.9 examples + 可观测性/资源清理/Docker）：**

- ✅ **可观测性：库层 print → 结构化 logger** — `agent.py` 执行回调（计划树/步骤结束/回溯）与 LLM 初始化失败全部改走 `get_logger("automind.agent")`（此前在 Web 服务下污染服务台输出）；CLI/审批交互的用户界面输出保留（其本身是终端 UI）。**发现并修复隐藏强依赖**：`core/logging.py` 顶层 `import structlog` 但该包未安装（此前从未被导入未暴露），重写为 **structlog 优先、缺失时优雅降级到标准库** 的 `_StdlibStructAdapter`（两种实现调用签名一致：`logger.info("event", key=value)`），核心库零强制日志依赖。
- ✅ **资源清理 close() 全链路** — `LongTermMemory.close()`（持有并释放 chromadb client）、`MemoryManager.close()`、`LLMBackend.close()`（基类 no-op）+ `OllamaProvider.close()`（httpx 连接池 aclose）、`AutoMindAgent.close()`（MCP disconnect_all → memory.close → llm.close，**幂等、单项失败不阻断**）；服务端新增 `@app.on_event("shutdown")` 退出前调用。实测重复 close 安全。
- ✅ **CLI 版本修复 + --version** — `cli/app.py` 硬编码 `v0.1.0` 改为 `from automind import __version__`；新增 `--version` 参数（实测输出 `automind 0.2.0`）。
- ✅ **§14.3 Rich TUI REPL** — `cli/tui.py` 新增 `run_rich_repl`：欢迎面板（版本/模式/模型/项目）、彩色提示符、slash 命令（/help /mode /tools /skills /stats /clear /exit）、任务结果 Markdown 面板渲染、每任务统计行、退出 Token 摘要 + 自动 `agent.close()`；**rich 缺失自动降级**到原 `agent.run_repl()`；`rich` 按计划提为核心依赖。**GBK 兼容**：去除 emoji/✓ 等 GBK 不可编码字符（中文用户 Windows 控制台实测通过）。CLI REPL 入口切换到新实现。
- ✅ **§14.5 英文文档** — 新增 `README.en.md`（五模式/审批/快速开始/Docker/模型配置/生产加固环境变量表/架构图/插件系统/内置技能/examples 索引/开发指南），中文 README 保持不动并互链。
- ✅ **§14.9 examples 教程目录** — 4 个自包含示例：`01-quick-start`（安装→启动→首任务）、`02-custom-model`（DeepSeek/Ollama/中转代理 + 可直接用的 config.yaml）、`03-skill-development`（可独立运行的 word_count_skill.py，含未安装时 sys.path 兜底）、`04-plugin-development`（完整 task-timer 插件：plugin.json + hooks.py + 安装/启用步骤）。
- ✅ **Docker 化** — `Dockerfile`（python:3.12-slim、非 root 用户、/data 卷、HEALTHCHECK 打 /api/health、构建缓存分层）+ `docker-compose.yml`（显式 `name: automind` 规避中文目录名、数据卷持久化、全部生产加固环境变量注释就绪、healthcheck/restart 策略）+ `.dockerignore`。`docker compose config` 校验通过（本机 Docker 引擎未运行，镜像构建留待引擎启动后验证）。
- ✅ **测试** — 新增 `tests/core/test_observability.py`（10 例：logger 降级签名/格式化/bind 兼容、agent.close 幂等、Chroma 释放、CLI 版本绑定、Rich REPL 可导入、示例技能可运行），**全套 160 测试通过**。

**第十二轮（v0.3.0 — 自主任务闭环：多Agent审查 + Loop验证 + TDD + 并行 + 缓存）：**

- ✅ **版本 0.3.0** — `__init__.py`/`pyproject.toml`/前端兜底同步；版本一致性测试改为"格式+单一数据源"断言，升版不再需要改测试。
- ✅ **配置开关（默认全开、可单独关闭）** — `ExecutionConfig` 新增 `auto_review`/`auto_verify`(+`auto_verify_max_rounds=2`)/`auto_test`/`parallel_execution`/`subtask_cache`；新端点 `GET/POST /api/config/autopilot`（持久化 + 即时生效）；「⚙ 设置→通用」新增「🔄 自主任务闭环」5 个开关（实测切换→后端同步→恢复全链路通）。
- ✅ **并行执行（§2.4 落地）** — `PlanExecutor.execute` 重写：收集全部就绪目标（PENDING+依赖满足），互不依赖的批次经 `asyncio.gather` 并发执行，批内结果按序处理保持确定性；与 B-01 纠错重试逻辑兼容。实测 3 个独立目标 max_concurrent≥2 且总耗时显著小于串行和。
- ✅ **子任务缓存** — 同一次计划执行内，**SAFE 级只读工具**同参调用直接复用结果（`cache_hits` 计数）；写类工具（SENSITIVE+）绝不缓存。实测同参命中 1 次、异参与写操作均真实执行。
- ✅ **TDD 闭环（编程模式）** — ①内环：`ReActExecutor` 每次 `file_write/file_edit/file_multi_edit` 写入 `.py` 后立即 `ast.parse` 语法验证，`syntax_check: OK/FAILED` 注入观察结果，模型下一轮即见即修（编辑→验证→修复）；②收尾：`_run_project_tests` 项目存在 `tests/` 时自动跑 `pytest -q`，失败摘要并入验收反馈。编程系统提示新增第 7/8 条引导。
- ✅ **多 Agent 审查融入工作模式** — `_review_result`：审阅者角色（复用 orchestrator ROLE_PROMPTS）复核任务结果，**共享只读（SAFE）工具**（同一 registry，MCP 注册的只读工具同样可用，§3.6 部分落地）——审阅者可真实调用工具核实文件状态后输出 `{approved, issues}`；审查异常不阻断主流程。实测审阅者真实调用了共享工具。
- ✅ **Loop 验证融入工作/编程模式** — `_autonomy_closure`：复用 `_loop_verify` 语义验收；未过（或审查有意见/测试未过）→ 带反馈自动补充 ReAct 修复轮（≤`auto_verify_max_rounds`，有界防死循环）→ 重新验收；输出末尾附「🔄 自主闭环」摘要；全程 `autopilot` 事件推送 event_sink。实测：首轮不过→修复轮携带反馈执行→二轮验收通过→输出更新；达上限正确放弃。
- ✅ **编程模式代码生成增强** — 新增 `code_generate` 工具（`_CodeGenerateTool` 适配器），把 code_generator 技能（语言检测/围栏剥离/语法校验+自修复/complete 补全）直接暴露给 ReAct 循环。实测经 dispatch 真实生成合法文件。
- ✅ **测试** — 新增 `tests/core/test_autonomy.py`（16 例：开关默认值/版本、并发实测（max_concurrent+耗时）、串行开关、缓存命中/写不缓存/禁用、TDD 语法 OK/FAILED/跳过、闭环首轮通过/修复轮/上限放弃、审阅者工具调用、code_generate 注册与执行、API GET/POST 持久化），**全套 176 测试通过**；浏览器实测设置开关与 UI 零控制台错误。
- ✅ **手册更新** — `使用手册.md` 新增 §8.0 自主任务闭环（流水线图+开关表+TDD 内环说明+安全边界）；`manual.html`/`使用手册.html` 同步新增 SVG 流水线图 8-1 + 开关表 + 目录锚点，版本徽标 v0.3.0。

**第十三轮（前端安全加固 + 模式栏分组 + 设置美化，版本 0.3.0）：**

- ✅ **前端 XSS 修复（P0 安全）** — 审计拆分后 6 个 JS 模块的全部注入点：
  - `esc()` 从只转义 `& < >` **增强**为覆盖 `& < > " ' \``（此前属性上下文 `title="${esc}"`、markdown `alt=`/`href=` 可被 `"` 注入执行）。
  - 新增 `jsq()`（JS-字符串-in-HTML属性 转义器）：同时防 JS 串逃逸（`\` `'`）与 HTML 属性逃逸（`" < > &`），修复 `onclick="fn('${esc(name)}')"` **双上下文注入**——技能/MCP/插件名、文件/目录路径、审批/历史 id 共 **13 处**内联处理器全部改用 `jsq()`；消息缩略图 URL 经 `isSafeUrl` 过滤。
  - **浏览器实测 4 大向量全部中和**：恶意 markdown 图片→无 `onerror` 属性（载荷落入 alt 文本）、恶意链接→无事件属性、恶意技能名→惰性字符串参数、`<script>` 注入→0 个 script 元素。
  - iframe 预览沙箱显式去除 `allow-popups`、补 `referrerpolicy="no-referrer"`；确认**不含 `allow-same-origin`**（脚本运行于 null 源，无法读父页 cookie/DOM/localStorage）。
- ✅ **静态资源版本化 cache-bust** — `/` 路由用正则给全部 `css/js` 注入 `?v={__version__}`，版本升级即自动失效旧缓存（根治拆分后"改了 JS 用户仍见旧版"）。
- ✅ **模式栏分组（3 主 + 分隔线 + 2 高级）** — 默认仅显示 对话/工作/编程，分隔线后「⋯ 高级」按钮折叠 协同/循环；选中高级模式自动展开保证可见。新手不被 5 按钮吓到、老用户全火力。
- ✅ **设置模块美化** — 选项卡升级为**段控式渐变胶囊**（玻璃底 + active 渐变高亮）、标题渐变强调条、圆角玻璃弹窗 + 自定义滚动条、新增右上角 ✕ 关闭按钮（旋转 hover）。浏览器实测渲染正确。
- ✅ **版本 0.3.0** — `__init__.py`/`pyproject.toml`/前端徽标统一；版本测试改为正则校验单一数据源（不锁死具体号）。
- ✅ **测试** — 新增 `tests/server/test_frontend_security.py`（9 例：esc 引号转义、jsq 双上下文、无裸 esc/crude 剥离残留、iframe 无 same-origin、缩略图过滤），**全套 185 测试通过**；浏览器零控制台错误、全功能零回归。

**第十四轮（v0.4.0 前后端加固 — 2 XSS / WS 退避 / 内存泄漏 / 安全头 / 真实嵌入 / 检查点恢复 / lifespan）：**

- ✅ **2 个残余 XSS 修复** — ① Markdown 链接：新增 `isSafeHref`（仅 http(s)），拒绝 `data:`/`javascript:`（`data:image/svg` 在 `<a>` 点击后会执行脚本）；href/src 再经 `esc()` 双重转义。② 自定义模型名 `onclick` 注入：`value='${m}'` / `removeCustomModel('${m}')` 改用 `jsq()`。浏览器实测：data: 链接不生成 `<a>`、模型名载荷成为惰性字符串。
- ✅ **WebSocket 指数退避重连** — `connectWS` 从固定 3s 重连改为 `min(30s, 2^n)` + 抖动 + 去重排程 + `onerror→close`，断线时不再对服务端造成重连风暴。
- ✅ **消息渲染内存泄漏修复** — `_htmlBlocks`/`_codeBlocks` 此前跨消息无限增长；改为代码块内联入 DOM、html 块内容存入按钮 `data-hblk` 属性（新增 `previewHtmlData`），每次 `formatContent` 后清空两数组。
- ✅ **流式气泡残留修复** — 任务出错/取消时 `finalizeStream(null)`：有部分内容则定格渲染，否则移除空光标气泡（此前残留）。
- ✅ **localStorage 对话上限** — 单模式 300KB / 总量 1.2MB，超限保留尾部最新内容并逐模式回收；配额溢出兜底仅保当前模式。
- ✅ **appendMessage/appendMessageTo 去重** — 抽出统一 `buildMessageEl(role,content,images)`（core.js），两处委托，消除 90% 重复 + `appendMessageTo` 的恒等 if/else。
- ✅ **桌面 skills 路径去硬编码** — 前端候选路径从硬编码 `C:\Users\Administrator\...` 改为 `~/Desktop/skills` 等（后端已 `expanduser`，跨平台跨用户）。
- ✅ **安全响应头** — 中间件对所有响应加 `X-Content-Type-Options: nosniff`、`X-Frame-Options: SAMEORIGIN`、`Referrer-Policy: no-referrer`；首页额外 CSP（`default-src 'self'` + `frame-ancestors 'self'` + `unsafe-inline` 兼容内联事件）。
- ✅ **真实语义嵌入替换 SHA256** — `_SimpleEmbedder` 从"整串 SHA256 伪嵌入"（1 字符差异即零相似）改为**特征哈希**（词 + 字符三元组、带符号哈希、L2 归一化）。实测：相似文本余弦 0.57、无关文本 -0.11、自相似 1.0——真正反映语义相似性；确定、离线、无需 Key。
- ✅ **检查点恢复 CLI（--restore 不再是 TODO）** — 新增 `AutoMindAgent.from_checkpoint` / `resume_from_checkpoint`，CLI 载入状态（上下文/计划/对话历史）并继续未完成计划。实测 save→restore 往返正确。
- ✅ **FastAPI on_event → lifespan** — 两个 `@app.on_event` 合并为 `@asynccontextmanager` 生命周期并挂到 `app.router.lifespan_context`，消除弃用告警。
- ✅ **静态资源版本化** — `/` 路由给 css/js 注入 `?v={__version__}`，版本升级自动 cache-bust。
- ✅ **测试** — 新增 `tests/core/test_v04_fixes.py`（23 例：真实嵌入相似度/确定性、检查点往返、安全头、WS 退避/内存清空/气泡移除/localStorage 上限/去重/无硬编码路径静态断言）+ 扩充前端安全测试；**全套 199 测试通过**，浏览器零控制台错误。

**第十五轮（PyPI 发布准备 + 日志全量接入 + server.py 拆分）：**

- ✅ **PyPI 发布准备（§14.6）** — 修复真实打包缺陷（`dependencies` 被错误嵌套进 `[project.urls]`，会致 `pip install` 失败）；`python -m build` 构建 sdist+wheel、`twine check` 双双 PASSED；确认 `automind` 名称在 PyPI **可用**；wheel 元数据（名/版本/依赖/`automind` 控制台脚本/markdown README）正确。**未代为上传**——PyPI 已禁用密码上传须用 API Token 且发布不可逆，出具 `RELEASE.md` 指导用户自行用 token 上传，并提示轮换已泄露的密码 + 开 2FA。
- ✅ **日志全量接入** — server.py 此前**零日志**（33 处静默 `except: pass`）；接入 `get_logger("automind.server")` 并在全生命周期打点：`server_startup/shutdown`、`agent_rebuilt`、`mcp_connected`、`scheduler_started`、`scheduled_run/done/failed`、`task_start/end/failed`、`auth_denied`；`tools/base.py` 的 `ToolRegistry.dispatch` 对**每次工具调用**记 `tool_dispatch`（名/成功/耗时）。与既有 agent 层日志（step_end/backtrack/plan/llm_init_failed/checkpoint_restored/agent_closed）合流为全链路可观测。structlog 缺失时自动降级标准库（第十一轮已做）。
- ✅ **server.py 拆分（安全增量）** — 2368 → 2223 行，抽出两个内聚、独立可测的模块：
  - `automind/server_web.py`：安全响应头 + CSP + 静态资源版本化（纯函数）。
  - `automind/server_store.py`：`Store` 类内聚配置/API Key/提供商/对话历史/会话隔离的持久化（原先 5 个分散全局 + TTL 缓存）；`config_file` 用**属性封装赋值即失效缓存**（比裸全局更安全）。server.py 实例化单例并保留 **19 个同名委托别名**，故 40+ 路由调用点**零改动**；仅更新 6 处测试补丁点（`srv._config_file`→`srv._store.config_file` 等）。
  - **验证**：全套 **209 测试通过**（+10 新增 store/web 单测）；浏览器端到端冒烟（状态/会话历史/安全头/技能面板）全绿、零控制台错误；store 功能冒烟（配置往返、缓存失效、会话隔离、sid 目录穿越防护）通过。诚实说明：完整的 `server/` 包化 + AppContext 仍是更大迁移，本轮完成了**低耦合模块的安全抽取**这一实质性第一步。

**第十六轮（v0.5.0：会话 Agent 池 + 前端性能 + long_term 日志 + GitHub 就绪）：**

- ✅ **Session-Agent-Pool 执行态多用户隔离（§7.4）** — 新增 `automind/server_pool.py`：`SessionAgentPool`（按 `session_id` 维护独立 Agent 实例，容量上限 + LRU 逐出）。`/api/run` 在执行前经 `_acquire_run_agent(agent, sid)` 取用会话 Agent，使并发会话的 `_current_plan`/ReAct 态/审批回调互不污染。**默认关闭**（`AUTOMIND_SESSION_POOL=1` 开启），关闭时原样返回全局 agent → **零回归**；lifespan 关停时 `aclose_all` 释放全部会话 Agent。8 例单测覆盖创建/复用/隔离/LRU逐出/释放/关停。
- ✅ **前端流式增量渲染** — `chat_chunk` 此前每个 delta 都 `formatContent(整个buffer)`（O(n²)）；改为 chunk 仅 O(1) 累加 + `scheduleStreamRender` **节流至 ~20fps**（合并高频 chunk），长回复不再随长度平方级变慢；`chat_done` 仍做最终整渲。实测 50 chunk 连续追加渲染正确。
- ✅ **消息列表"虚拟滚动"** — `.msg` 加 `content-visibility: auto; contain-intrinsic-size: auto 90px`，浏览器**跳过屏幕外消息的布局/绘制**，长对话保持流畅——虚拟滚动级性能、零 JS 重写风险、不动既有 transcript 持久化。实测计算样式生效。
- ✅ **long_term.py 6 处静默异常 → 日志** — chroma 初始化/写入/检索/删除/计数/缓存清理失败均改为 `logger.warning/info/debug`（含降级到内存的提示），此前静默吞没使 ChromaDB 故障无从察觉。
- ✅ **server.py 继续拆分** — 再抽出 `server_pool.py`（会话池），延续第十五轮的 `server_store.py`/`server_web.py`。
- ✅ **FastAPI on_event → lifespan** — 复核确认已于第十一轮完成（当前无任何 `@app.on_event`）；本轮补充池清理入 lifespan。
- ✅ **GitHub 就绪（防泄密 + 防警告）** — `git init` + `.gitignore`（**排除 `.automind_config.json`/`.automind/`/dist/缓存等**，修复了"gitignore 不支持行内注释"导致密钥曾被暂存的问题）+ `.gitattributes`（`* text=auto eol=lf` 消除 Windows CRLF 警告）+ `LICENSE`（MIT，GitHub 可识别许可证）。扫描确认待提交文件**无 API Key**。出具 `GITHUB.md` 指导用户用自己的 PAT 推送。**未代为 push**（需用户凭证，且推送不可逆/外向）。
- ✅ **版本 0.5.0**；**全套 217 测试通过**（+8 池测试），浏览器端 v0.5.0、流式/虚拟滚动实测正常、零控制台错误。

**未做（本轮明确说明的高风险项）：** **server.py 完整拆包 + AppContext 消除全局状态** —— 该重构需改动 40+ 路由并解耦 8 个模块级全局，而现有测试契约深度依赖 `srv._config_file`/`srv._agent` 等模块属性的可变覆盖语义；一次性迁移回归面极大、与"确保其他功能完整"冲突，应作为**独立专项**（先重构测试夹具再拆包）。本轮已完成该簇的**低风险现代化**部分（lifespan、安全头、静态版本化）。其余：虚拟滚动（消息窗口化，当前量级下非瓶颈）、server.py 完整路由拆分（已抽存储层与 cache-bust，路由域拆分留待专项）、前端 ES Module 化（阶段 2）。以下为更早期条目： 2.1 拆分 server.py、2.2 AppContext、2.4 并行执行、2.5 检查点恢复、3.1 LLM Protocol、3.4 真实 Embedding、3.5 Hook、3.6 多智能体工具、第 4 章架构演进、§7 完整 Session-Agent-Pool（执行态隔离，对话态已隔离）；§8/§9 其余项（任务模板 F-01、文件树 F-03、亮色主题 F-05、命令面板 F-06、F-16-3 Canvas 粒子背景等）。建议按本文路线图与优先级逐步推进。

---


---

## 目录

- [0. 总体策略](#0-总体策略)
- [1. 目标态目录结构](#1-目标态目录结构)
- [2. 7.1 高优先级重构（影响稳定性与可维护性）](#2-71-高优先级重构影响稳定性与可维护性)
  - [2.1 拆分 server.py](#21-拆分-serverpy)
  - [2.2 消除全局状态](#22-消除全局状态)
  - [2.3 添加核心测试](#23-添加核心测试)
  - [2.4 实现真正的并行执行](#24-实现真正的并行执行)
  - [2.5 完善检查点恢复](#25-完善检查点恢复)
- [3. 7.2 中优先级重构（提升能力与体验）](#3-72-中优先级重构提升能力与体验)
  - [3.1 统一 LLM 类型接口](#31-统一-llm-类型接口)
  - [3.2 增强 Loop 停止条件](#32-增强-loop-停止条件)
  - [3.3 结构化规划验证](#33-结构化规划验证)
  - [3.4 记忆系统使用真实 Embedding](#34-记忆系统使用真实-embedding)
  - [3.5 引入 Hook 中间件](#35-引入-hook-中间件)
  - [3.6 多智能体工具能力](#36-多智能体工具能力)
- [4. 7.3 低优先级重构（架构演进）](#4-73-低优先级重构架构演进)
  - [4.1 轻量级依赖注入](#41-轻量级依赖注入)
  - [4.2 配置源统一](#42-配置源统一)
  - [4.3 OpenTelemetry 追踪](#43-opentelemetry-追踪)
  - [4.4 Docker 沙箱隔离](#44-docker-沙箱隔离)
  - [4.5 Agent 作为独立库](#45-agent-作为独立库)
- [5. 缺陷覆盖矩阵](#5-缺陷覆盖矩阵)
- [6. 执行路线图](#6-执行路线图)

---

## 0. 总体策略

重构核心原则：

1. **不动核心业务逻辑** — 规划引擎、ReAct 循环、反思机制等算法已正确，重构聚焦工程层面
2. **渐进式交付** — 每阶段产出可独立验证的成果，不搞大爆炸式迁移
3. **向后兼容** — CLI 和 Web API 的公开接口签名保持不变
4. **测试先行** — 任何重构前先补上目标模块的特征测试（characterization test）

---

## 1. 目标态目录结构

```
automind/
├── core/                    # 基础类型、配置、LLM 后端、事件总线（不动）
├── agent.py                 # AutoMindAgent（精简，委托给子模块）
├── planning/                # 规划与执行（不动）
├── reflection/              # 反思与纠错（不动）
├── memory/                  # 记忆系统（不动）
├── tools/                   # 工具注册、权限、终端、文件、沙箱、MCP（不动）
├── skills/                  # 技能系统（不动）
├── context/                 # 上下文管理、环境检测（不动）
├── symbolic/                # Datalog 推理（不动）
├── multiagent/              # 多智能体（不动）
├── state/                   # 检查点、人机协同、资源管理（不动）
├── server/                  # 【新增】Web 层，从 server.py 拆分
│   ├── __init__.py
│   ├── app.py               # FastAPI 实例 + CORS + 启动
│   ├── deps.py              # AppContext + FastAPI Depends
│   ├── routes/
│   │   ├── status.py        # /api/status, /api/tokens, /api/stats
│   │   ├── config.py        # /api/config/*
│   │   ├── run.py           # /api/run, /api/chat, /api/chat/stream
│   │   ├── mcp.py           # /api/mcp/*
│   │   ├── schedule.py      # /api/schedule/*
│   │   ├── fs.py            # /api/fs/*, /api/preview/*
│   │   ├── skills.py        # /api/skills/*
│   │   └── providers.py     # /api/providers, /api/models
│   ├── ws.py                # WebSocket 管理
│   ├── scheduler.py         # 定时任务调度循环
│   ├── config_store.py      # 配置持久化（_read_config / _write_config 等）
│   └── chat_store.py        # 对话历史持久化
├── cli/                     # CLI（不动）
├── main.py
└── static/
tests/                       # 【新增】测试目录
├── conftest.py
├── core/
│   ├── test_config.py
│   ├── test_types.py
│   ├── test_llm.py
│   └── test_json_utils.py
├── planning/
│   ├── test_plan_executor.py
│   └── test_dependency_graph.py
├── tools/
│   ├── test_registry.py
│   ├── test_permissions.py
│   └── test_terminal.py
├── memory/
│   └── test_manager.py
├── reflection/
│   └── test_quality_assessor.py
├── server/
│   └── test_routes.py
└── integration/
    └── test_e2e.py
```

---

## 2. 7.1 高优先级重构（影响稳定性与可维护性）

### 2.1 拆分 server.py

**对应缺陷**: 6.1-1 — server.py 过于臃肿（约 1600 行单文件）

**现状**：`automind/server.py` 混合了：
- FastAPI 应用初始化
- 40+ 个路由处理函数
- 配置持久化（`_read_config`/`_write_config`/`_save_api_keys` 等 10+ 函数）
- 对话历史持久化
- 定时任务存储与调度
- WebSocket 管理
- Agent 生命周期（`_rebuild_agent`）

**目标**：拆分为 `server/` 包，每个文件 ≤ 200 行。

**关键约束**：所有路由路径、响应格式、查询参数保持不变，前端零改动。

**拆分步骤**：

| 步骤 | 内容 | 验证方式 |
|------|------|---------|
| 1 | 提取 `config_store.py`：移动 `_read_config`/`_write_config`/`_load_api_keys`/`_save_api_keys`/`_load_providers`/`_save_provider_cfg`/`_load_active`/`_save_active`/`_custom_models`/`_add_custom_model`/`_remove_custom_model`/`_ENV_KEY_MAP`/`_env_api_key` | `curl /api/config/apikeys` 验证数据完整往返 |
| 2 | 提取 `chat_store.py`：移动 `_CHAT_FILE`/`_load_chat_history`/`_save_chat_history` | 发送聊天消息，重启进程验证恢复 |
| 3 | 提取 `server/deps.py`：创建 `AppContext` dataclass 替代全局变量 | 单元测试验证创建/注入 |
| 4 | 提取 `server/scheduler.py`：移动 `_scheduled`/`_load_scheduled`/`_persist_scheduled`/`_scheduler_loop`/`_run_scheduled` | 创建定时任务，验证调度执行 |
| 5 | 提取 `server/ws.py`：移动 `_ws_clients`/`_broadcast`/WebSocket 端点 | WebSocket 连接测试消息推送 |
| 6 | 按功能域拆分路由到 `server/routes/*.py`（status / config / run / mcp / schedule / fs / skills / providers） | 逐个 endpoint smoke test |
| 7 | `server/app.py` 只保留 FastAPI 实例化、CORS、`@app.on_event("startup")` | 完整启动流程冒烟测试 |

---

### 2.2 消除全局状态

**对应缺陷**: 6.1-2 — 全局状态过多（8 个模块级可变全局变量）

**现状**：

```python
_agent: Any = None
_active_sessions: dict[str, dict] = {}
_ws_clients: dict[str, list[WebSocket]] = {}
_task_history: list[dict] = []
_config_file = Path(".automind_config.json")
_token_totals = {"prompt": 0, "completion": 0, "total": 0, "tasks": 0}
_scheduled: dict[str, dict] = {}
```

**问题**：
- 无法并行运行多实例（如测试环境）
- 测试之间必须手动清理状态
- 隐式依赖导致函数难以独立测试

**方案**：引入 `AppContext` 统一管理

```python
# automind/server/deps.py

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

@dataclass
class AppContext:
    """Web 层全局状态容器，通过 FastAPI Depends 注入。"""
    config_file: Path = field(default_factory=lambda: Path(".automind_config.json"))
    agent: Any = None
    active_sessions: dict[str, dict] = field(default_factory=dict)
    ws_clients: dict[str, list] = field(default_factory=dict)
    task_history: list[dict] = field(default_factory=list)
    token_totals: dict = field(default_factory=lambda: {
        "prompt": 0, "completion": 0, "total": 0, "tasks": 0
    })
    scheduled: dict[str, dict] = field(default_factory=dict)

    def reset(self):
        """仅用于测试"""
        self.agent = None
        self.active_sessions.clear()
        self.ws_clients.clear()
        self.task_history.clear()
        self.token_totals = {"prompt": 0, "completion": 0, "total": 0, "tasks": 0}
        self.scheduled.clear()
```

**迁移步骤**：

| 步骤 | 内容 |
|------|------|
| 1 | 创建 `AppContext`，在 `app.py` 中实例化为单例 |
| 2 | 将 `get_agent()` 改为接收 `ctx` 参数 |
| 3 | 逐个迁移路由处理器签名，添加 `ctx: AppContext = Depends(get_ctx)` |
| 4 | 删除所有模块级 globals |
| 5 | `conftest.py` 中构造干净的 `AppContext` 用于测试 |

---

### 2.3 添加核心测试

**对应缺陷**: 6.3-12 — 测试覆盖严重不足

#### 第一层：纯函数单元测试（无外部依赖，可立即写）

| 测试文件 | 覆盖内容 |
|---------|---------|
| `tests/core/test_json_utils.py` | `extract_json` 三层策略（直接解析、去围栏、平衡截取）+ 边界（空字符串、嵌套对象、数组、含特殊字符） |
| `tests/core/test_types.py` | `Goal.all_children()`/`leaf_goals()` 递归正确性、`Predicate.to_datalog()` 转换、`AgentState` 序列化 |
| `tests/planning/test_dependency_graph.py` | `SimpleDiGraph` 拓扑排序、DFS 环检测、最长路径、后代/祖先查询、并行组检测 |
| `tests/tools/test_permissions.py` | `PermissionEngine.check` 三种审批模式（ask/auto/approve_all）、预检正则匹配、路径穿越防护 |
| `tests/core/test_config.py` | `AgentConfig.auto_load` 优先级（YAML > JSON > ENV > 默认）、`model_post_init` 环境变量补充 |

#### 第二层：带 Mock 的组件测试

| 测试文件 | 覆盖内容 |
|---------|---------|
| `tests/tools/test_registry.py` | 工具注册/分派/去注册/调度异常/ToolNotFoundError |
| `tests/memory/test_manager.py` | 多源检索融合去重、交互存储、压缩触发条件 |
| `tests/reflection/test_quality_assessor.py` | 简单规则降级评估（错误标记检测、完整性判断） |
| `tests/planning/test_plan_executor.py` | 权限门控流程、重试次数上限、自我修正（mock LLM） |

#### 第三层：集成测试

| 测试文件 | 覆盖内容 |
|---------|---------|
| `tests/integration/test_e2e.py` | CLI 端到端（使用 Ollama 或 mock LLM） |
| `tests/server/test_routes.py` | FastAPI TestClient 覆盖所有端点（状态码、响应格式） |

#### 可立即执行的示例代码

```python
# tests/core/test_json_utils.py

import pytest
from automind.core.json_utils import extract_json


class TestExtractJson:
    """extract_json 单元测试 — 覆盖三层容错策略。"""

    def test_direct_valid_object(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_direct_valid_array(self):
        assert extract_json('[1, 2, 3]') == [1, 2, 3]

    def test_json_in_code_fence(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_in_plain_fence(self):
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_json_amidst_text(self):
        result = extract_json('Here is the plan:\n{"goal": "test"}\nEnd.')
        assert result == {"goal": "test"}

    def test_empty_string(self):
        assert extract_json("") is None

    def test_none_input(self):
        assert extract_json(None) is None

    def test_balanced_array_in_text(self):
        assert extract_json("text [1,2,3] more") == [1, 2, 3]

    def test_nested_braces(self):
        assert extract_json('{"outer": {"inner": 1}}') == {"outer": {"inner": 1}}

    def test_unicode_chinese(self):
        result = extract_json('{"目标": "测试"}')
        assert result == {"目标": "测试"}

    def test_escaped_quotes_in_balanced_slice(self):
        result = extract_json('{"key": "val\\"ue"}')
        assert result == {"key": 'val"ue'}

    def test_non_json_text_returns_none(self):
        assert extract_json("Hello, world!") is None
```

---

### 2.4 实现真正的并行执行

**对应缺陷**: 6.1-4 — `parallel_groups` 被检测但实际串行执行

**现状**：`HierarchicalPlanner` 正确计算了 `parallel_groups`，但 `PlanExecutor.execute()` 使用 `while` 循环串行取下一个 goal。

**方案**：

```python
# automind/planning/plan_executor.py

async def _get_ready_parallel_goals(self, plan: HierarchicalPlan) -> list[Goal]:
    """获取当前可并行执行的所有 PENDING 叶子目标。"""
    ready = []
    for goal_id in plan.execution_order:
        goal = self.hierarchical_planner._find_goal(plan.root_goal, goal_id)
        if goal and goal.status == GoalStatus.PENDING:
            if self.hierarchical_planner._dependencies_met(goal, plan.root_goal):
                ready.append(goal)
    return ready

async def execute(self, plan, ...):
    # ... 初始化 ...
    while True:
        ready_goals = self._get_ready_parallel_goals(plan)
        if not ready_goals:
            break
        # 并发执行互不依赖的目标
        results = await asyncio.gather(*[
            self._execute_goal(g, on_approval_needed) for g in ready_goals
        ])
        for g, r in zip(ready_goals, results):
            report.steps.append(r)
            if r.success:
                report.completed_steps += 1
                self.hierarchical_planner.update_goal_status(plan, g.id, GoalStatus.COMPLETED)
            else:
                # 失败处理（保持原有回溯逻辑）
                ...
```

**前提条件**：必须在 `_dependencies_met` 中保证并行组内不存在共享资源依赖（当前宽松策略已满足）。

---

### 2.5 完善检查点恢复

**对应缺陷**: 6.2-10 — CLI 的 `--restore` 命令恢复逻辑是 TODO 状态

**方案**：

```python
# automind/agent.py

@classmethod
async def from_checkpoint(cls, checkpoint_id: str, config: AgentConfig) -> "AutoMindAgent":
    """从检查点恢复 Agent 实例。"""
    agent = cls(config)
    state = await agent.checkpoint_mgr.load(checkpoint_id)
    agent._agent_state = state
    agent._current_plan = state.plan
    # 恢复上下文消息
    for msg in state.messages:
        agent.context_mgr.add(msg)
    # 恢复对话历史（兼容 Web 层）
    agent._chat_history = [
        m.to_dict() for m in state.messages
        if m.role.value in ("user", "assistant")
    ]
    return agent


async def resume_from_checkpoint(self, checkpoint_id: str) -> AgentResult:
    """从检查点继续执行未完成的任务。"""
    state = await self.checkpoint_mgr.load(checkpoint_id)
    plan = state.plan
    if plan is None or plan.status.value in ("completed", "aborted"):
        return AgentResult(success=False, output="无可恢复的进行中任务")
    # 继续执行未完成的目标
    return await self.run(f"继续执行: {plan.task_description}")
```

CLI 命令补全：

```python
# automind/cli/app.py（--restore 分支）
if args.restore:
    agent = await AutoMindAgent.from_checkpoint(args.restore, config)
    print(f"已从检查点恢复: {args.restore}")
    result = await agent.resume_from_checkpoint(args.restore)
    print(f"\n{result.output}")
    return 0 if result.success else 1
```

---

## 3. 7.2 中优先级重构（提升能力与体验）

### 3.1 统一 LLM 类型接口

**对应缺陷**: 6.3-13 — 大量 `Any` 类型标注，LLM 参数无类型约束

**方案**：

```python
# automind/core/llm.py 新增

from typing import Protocol, AsyncIterator, runtime_checkable

@runtime_checkable
class LLMBackendProtocol(Protocol):
    """LLM 后端结构类型接口。解决当前所有调用方使用 `Any` 的问题。"""
    config: LLMProviderConfig

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse: ...

    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    def token_count(self, text: str) -> int: ...
```

迁移范围：
- `agent.py`: `self.llm: LLMBackendProtocol | None`
- `planning/hierarchical_planner.py`: `llm: LLMBackendProtocol | None`
- `planning/plan_executor.py`: `llm: LLMBackendProtocol | None`
- `planning/react_executor.py`: `llm: LLMBackendProtocol`
- `planning/reasoning.py`: `llm: LLMBackendProtocol | None`
- `reflection/reflexion.py`: `llm: LLMBackendProtocol | None`
- `reflection/quality_assessor.py`: `llm: LLMBackendProtocol | None`
- `reflection/self_correction.py`: `llm: LLMBackendProtocol | None`
- `multiagent/orchestrator.py`: `llm: LLMBackendProtocol`

---

### 3.2 增强 Loop 停止条件

**对应缺陷**: 6.2-6 — Loop 模式仅靠"连续无进展"检测死循环，缺乏智能收敛判断

**方案**：三级停止条件判断

```python
async def _loop_verify(self, task: str, output: str) -> dict:
    """三级停止判断：LLM 语义判断 → 相似度收敛检测 → 无变更检测。"""
    # 1. LLM 语义判断（现有逻辑）
    verdict = await self._llm_verdict(task, output)
    if verdict["done"]:
        return verdict

    # 2. 相似度检测：连续两轮输出高度相似 → 提示收敛
    if hasattr(self, '_last_loop_embedding') and self._last_loop_embedding is not None:
        try:
            current_emb = self._compute_embedding(output)
            sim = self._cosine_similarity(current_emb, self._last_loop_embedding)
            if sim > 0.95:
                return {
                    "done": False,
                    "reason": f"连续输出高度相似 (cosine={sim:.2f})，可能已收敛。建议检查: {verdict['reason']}"
                }
            self._last_loop_embedding = current_emb
        except Exception:
            pass

    # 3. 操作变更检测：本轮没有触发任何文件修改/命令执行
    if self.react_executor and not self.react_executor.actions:
        return {
            "done": False,
            "reason": f"本轮未执行任何操作。建议: {verdict['reason']}"
        }

    return verdict
```

---

### 3.3 结构化规划验证

**对应缺陷**: 6.2-7 — LLM 生成的 JSON 计划可能包含不存在的工具或参数名

**方案**：在 `_llm_decompose` 解析后加入 schema 校验

```python
def _validate_goal_tree(self, goal: Goal, available_tools: set[str],
                        tool_params: dict[str, set[str]]) -> list[str]:
    """递归校验目标树，返回所有违规项。"""
    errors = []
    if goal.assigned_action:
        tool_name = goal.assigned_action.tool_name
        if tool_name == "think":
            pass  # 内部步骤，无需校验
        elif tool_name not in available_tools:
            errors.append(
                f"目标 '{goal.description}' 使用了不存在的工具 '{tool_name}'"
            )
        else:
            # 校验参数名是否合法
            valid_params = tool_params.get(tool_name, set())
            for param in goal.assigned_action.parameters:
                if param not in valid_params:
                    errors.append(
                        f"目标 '{goal.description}' 的工具 '{tool_name}' "
                        f"使用了未知参数 '{param}'"
                    )
    for child in goal.children:
        errors.extend(self._validate_goal_tree(child, available_tools, tool_params))
    return errors
```

在 `_llm_decompose` 中，若校验失败则回退到模板分解，并将违规信息写入日志。

---

### 3.4 记忆系统使用真实 Embedding

**对应缺陷**: 6.2-8 — `_SimpleEmbedder` 使用 SHA256 哈希作为伪嵌入，语义质量极低

**方案**：`LongTermMemory` 已预留 `embedding_fn` 参数，只需在 `MemoryManager` 初始化时注入真实 embedding：

```python
# automind/memory/manager.py

def __init__(self, max_tokens=128000, persist_dir=".automind/chroma",
             project_root=".", llm_backend=None):
    ...
    # 使用真实的 LLM embedding 替代 SHA256 伪嵌入
    embedder = None
    if llm_backend and hasattr(llm_backend, 'embed'):
        async def _embed(texts):
            return await llm_backend.embed(texts)
        embedder = _embedder_wrapper(llm_backend)

    self.long_term = LongTermMemory(
        persist_dir=persist_dir,
        embedding_fn=embedder,  # 传入 OpenAI text-embedding-3-small
    )
```

`AgentConfig.memory` 中已有 `embedding_provider` 和 `embedding_model` 字段（`text-embedding-3-small`），连接即可。

---

### 3.5 引入 Hook 中间件

**对应缺陷**: 6.2-9 — 缺少插件/拦截器机制

**方案**：轻量级钩子系统

```python
# automind/core/hooks.py

from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from automind.core.types import AgentResult, HierarchicalPlan, InputMessage, ToolResult

@dataclass
class AgentHooks:
    """Agent 生命周期钩子。所有钩子可选，未设置时跳过。"""
    before_run: Callable[[str], Awaitable[None]] | None = None
    after_parse: Callable[[InputMessage], Awaitable[None]] | None = None
    before_plan: Callable[[str], Awaitable[None]] | None = None
    after_plan: Callable[[HierarchicalPlan], Awaitable[None]] | None = None
    before_tool: Callable[[str, dict], Awaitable[None]] | None = None
    after_tool: Callable[[str, ToolResult], Awaitable[None]] | None = None
    after_run: Callable[[AgentResult], Awaitable[None]] | None = None
    on_error: Callable[[Exception, str], Awaitable[None]] | None = None
```

在 `agent.py` 增加 `self.hooks = AgentHooks()`，在 `run()` 各阶段调用 `await self._invoke_hook(...)`。

---

### 3.6 多智能体工具能力

**对应缺陷**: 6.2-11 — 子智能体仅能进行纯文本推理，无法调用工具

**方案**：允许 orchestrator 中的子智能体使用只读工具

```python
# automind/multiagent/orchestrator.py

async def _run_role(self, role, subtask, task, scratch,
                    tool_registry=None, llm_backend=None):
    # 只暴露只读工具
    tool_schemas = None
    if tool_registry:
        read_only_tools = [
            t for t in tool_registry.list_all()
            if t.permission_tier.value == "safe"
        ]
        if read_only_tools:
            tool_schemas = [t.to_openai_schema() for t in read_only_tools]

    messages = [
        {"role": "system", "content": ROLE_PROMPTS.get(role, ROLE_PROMPTS["researcher"])},
        {"role": "user", "content": (
            f"团队总目标：{task}\n\n"
            f"白板内容：\n{scratch[-4000:]}\n\n"
            f"你的子任务：{subtask}\n"
            f'可用工具（只读）：{", ".join(t.name for t in read_only_tools) if tool_schemas else "无"}'
        )},
    ]
    resp = await llm_backend.generate(messages, tools=tool_schemas)
    # 如果 LLM 返回了工具调用，执行并追加结果
    ...
```

---

## 4. 7.3 低优先级重构（架构演进）

### 4.1 轻量级依赖注入

不引入重量级 DI 框架，而是在 `AgentConfig` 基础上增加工厂注册能力：

```python
class AgentConfig(BaseSettings):
    # 现有字段...

    # 模块工厂（可选覆盖，用于测试注入）
    planner_factory: str = "automind.planning.hierarchical_planner.HierarchicalPlanner"
    memory_factory: str = "automind.memory.manager.MemoryManager"
    tool_registry_factory: str = "automind.tools.base.ToolRegistry"
```

`AutoMindAgent.__init__` 通过 `importlib` 动态加载，测试时注入 mock 工厂。

### 4.2 配置源统一

废弃 YAML/JSON 自动发现的多重路径，所有持久化配置统一到 `.automind_config.json`：

- `AgentConfig.auto_load()` 改为只从 `.automind_config.json` 读取
- 环境变量仅作为初始默认值（已有 `model_post_init` 逻辑），不参与持久化
- YAML 加载能力保留但标记为 deprecated，仅通过 `--config` 显式指定时使用

### 4.3 OpenTelemetry 追踪

在关键路径插入 Span：

```python
# pip install opentelemetry-api opentelemetry-sdk
from opentelemetry import trace
tracer = trace.get_tracer(__name__)
```

覆盖的 Span：

| Span 名称 | 覆盖阶段 | 关键属性 |
|----------|---------|---------|
| `agent.run` | 整体任务执行 | task, mode, interaction |
| `llm.generate` | 每次 LLM 调用 | model, provider, messages_count, tokens |
| `tool.dispatch` | 每次工具调用 | tool_name, duration_ms, success |
| `planner.plan` | 规划生成 | task, goal_count |
| `reflexion.reflect` | 反思 | outcome, lessons_count |

### 4.4 Docker 沙箱隔离

当前的 `PythonSandboxTool` 使用受限 `exec()` 执行，仍有逃逸风险。生产环境应支持 Docker 容器隔离：

```python
class DockerSandboxTool(AbstractTool):
    """使用 Docker 容器隔离执行任意代码。"""
    name = "docker_sandbox"
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 55

    async def execute(self, code: str, image: str = "python:3.11-slim",
                      timeout: float = 30.0, **kwargs) -> ToolResult:
        process = await asyncio.create_subprocess_exec(
            "docker", "run", "--rm",
            "--network", "none",
            "--memory", "256m",
            "--cpus", "1",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            image,
            "python", "-c", code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        ...
```

### 4.5 Agent 作为独立库

将 `AutoMindAgent` 与 Web 层完全解耦，使其可被 `pip install automind` 后作为库使用：

- 移除 `agent.py` 中对 `_active`、`_load_api_keys` 等 Web 函数的引用
- `_rebuild_agent` 中的 Web 配置逻辑下沉为 `AgentConfig` 标准加载流程
- 发布到 PyPI

---

## 5. 缺陷覆盖矩阵

| 缺陷编号 | 缺陷描述 | 对应方案 | 优先级 |
|---------|---------|---------|-------|
| 6.1-1 | server.py 过于臃肿 (64KB) | 2.1 拆分 server.py | 高 |
| 6.1-2 | 全局状态过多 (8 个全局变量) | 2.2 消除全局状态 (AppContext) | 高 |
| 6.1-3 | 缺少依赖注入框架 | 4.1 轻量级 DI | 低 |
| 6.1-4 | 缺少真正的并行执行 | 2.4 实现并行执行 (asyncio.gather) | 高 |
| 6.1-5 | Agent 与 Web 层耦合 | 2.1 + 2.2 解耦 | 高 |
| 6.2-6 | Loop 模式防护不足 | 3.2 三级停止条件 | 中 |
| 6.2-7 | 规划质量依赖 LLM（无校验） | 3.3 结构化规划验证 | 中 |
| 6.2-8 | 记忆系统伪嵌入 | 3.4 真实 Embedding | 中 |
| 6.2-9 | 缺少中间件/插件体系 | 3.5 Hook 中间件 | 中 |
| 6.2-10 | 检查点恢复不完整 (TODO) | 2.5 完善检查点恢复 | 高 |
| 6.2-11 | 多智能体缺少工具能力 | 3.6 多智能体工具 | 中 |
| 6.3-12 | 测试覆盖严重不足 | 2.3 三层测试体系 | 高 |
| 6.3-13 | 类型标注不完整 (大量 Any) | 3.1 LLMBackendProtocol | 中 |
| 6.3-14 | 错误处理粒度不统一 | 随各模块重构时统一 | 中 |
| 6.3-15 | 配置管理分散 | 4.2 配置源统一 | 低 |
| **6.1-6** | **全局单例 Agent 导致不支持多用户并发** | **7 多用户支持方案** | **Phase 2** |

---

## 6. 执行路线图

```
第 1 周：地基
├── 创建 tests/ 目录结构 + conftest.py (pytest + pytest-asyncio)
├── 补第一层单元测试
│   ├── test_json_utils.py (10+ 用例)
│   ├── test_types.py (Goal 树操作)
│   ├── test_permissions.py (三级权限门控)
│   ├── test_dependency_graph.py (DAG 算法)
│   └── test_config.py (配置加载优先级)
├── 提取 config_store.py + chat_store.py
└── 创建 AppContext dataclass

第 2 周：拆分 server.py
├── 提取 scheduler.py / ws.py / routes/*.py
├── 全局变量 → AppContext 依赖注入迁移
├── 补 server 路由集成测试 (FastAPI TestClient)
└── 冒烟测试：全部 API 端点

第 3 周：加固核心
├── 第二层组件测试 (registry / memory / quality_assessor / plan_executor)
├── 统一 LLM 类型接口 (LLMBackendProtocol, 9 个文件)
├── 结构化规划验证 (_validate_goal_tree)
├── Hook 中间件 (AgentHooks)
└── 并行执行 (asyncio.gather)

第 4 周：能力提升 + 收尾
├── 检查点恢复完整实现
├── Loop 停止条件增强 (三级判断)
├── 记忆系统真实 Embedding 连接
├── 多智能体只读工具注入
├── 集成测试 + 文档更新
└── CHANGELOG / 迁移指南
```

---

## 7. 多用户支持分析与方案（Phase 2 追加）

### 7.1 当前架构的多用户缺陷（源码铁证）

当前系统**不支持多用户并发**，存在严重数据冲突。以下基于 `automind/server.py` 源码逐条举证：

#### 缺陷 1：全局单例 Agent — 所有请求共用同一个执行体

```python
# server.py 第 42 行
_agent: Any = None

# server.py 第 298-302 行 — 所有用户拿同一个实例
def get_agent():
    global _agent
    if _agent is None:
        _rebuild_agent()
    return _agent
```

User A 和 User B 调用 `agent.run()` 时操作的是**同一个对象**。当 A 的执行修改 `self._current_plan`，B 的执行会覆盖它。

#### 缺陷 2：对话历史全局共享

```python
# server.py 第 1204-1205 行 — Chat 模式
reply = await agent.chat(task, images=images)
_save_chat_history(agent._chat_history)    # 所有人写同一份

# server.py 第 1312 行 — 任意用户点击"新会话"
agent.reset_chat()     # 清掉所有人的历史
_save_chat_history([])
```

**场景**：用户 A 在对话中聊了 10 轮；用户 B 打开页面，看到的是 A 的聊天记录；B 点"新会话"→ A 的历史消失。

#### 缺陷 3：审批回调竞态

```python
# server.py 第 1424 行 — WebSocket 连接时注入
agent.approval_callback = _approval_cb   # 后连接者覆盖先连接者
```

**场景**：A 先连 WebSocket（设置回调），B 后连（覆盖）。当 A 的任务触发审批时，请求可能错误发到 B 的前端。

#### 缺陷 4：执行状态互斥

```python
# agent.py 第 165-171 行 — 单例的实例属性，全部共享
self._current_plan: HierarchicalPlan | None = None
self._agent_state = AgentState()
self._chat_history: list[dict[str, str]] = []

# agent.py 第 234-236 行 — run() 执行时会互相覆盖
plan, step_results = await self._run_plan_execute(user_input, context)
```

**场景**：A 提交"创建 FastAPI 项目"（耗时 30s），B 在 5s 后提交"修复 main.py bug"。B 覆盖了 A 的 `_current_plan`，A 的工具执行可能写入 B 的目标，两个任务的反思/质量评估互相污染。

#### 缺陷 5：`_active_sessions` 形同虚设

```python
# server.py 第 1194 行 — 按 session_id 记录元数据
_active_sessions[session_id] = {"task": task, "status": "running", ...}
```

只跟踪**元数据**（任务描述、状态标签），真正的执行体 `_agent` 仍然是单例。多个 session 在执行层面串行共享同一个对象。

### 7.2 影响范围矩阵

| 交互模式 | 单用户 | 多用户并发 | 冲突类型 |
|---------|:---:|:---:|---------|
| 对话 (Chat) | ✅ | ❌ | 共享 `_chat_history`，互相可见 |
| 工作 (Work) | ✅ | ❌ | 后提交任务覆盖先提交任务的 `_current_plan` |
| 编程 (Coding) | ✅ | ❌ | ReAct 状态（thoughts/actions）互相污染 |
| 协同 (Multi) | ✅ | ❌ | orchestrator 内部状态共享 |
| 循环 (Loop) | ✅ | ❌ | `_loop_verify` 和 `react_executor` 状态共享 |
| 定时任务 | ✅ | ❌ | 后台调度与前台用户共用同一个 agent |
| 配置切换 | ✅ | ❌ | 一个用户切换模型/API Key，其他用户也被切换 |

### 7.3 解决思路

**核心原则**：从"单例 Agent"升级为"每会话一个 Agent 实例"，最低成本隔离各用户的执行上下文。

架构对比：

```
当前架构（单例）:
  HTTP Request A ──┐
  HTTP Request B ──┤──→ _agent (singleton) ──→ 状态互相覆盖
  WebSocket C    ──┘

目标架构（会话隔离）:
  HTTP Request A ──→ SessionAgent A (独立 plan / chat_history / context_mgr)
  HTTP Request B ──→ SessionAgent B (独立 plan / chat_history / context_mgr)
  WebSocket C    ──→ SessionAgent C (独立 approval_callback)
```

### 7.4 实现方案（Session Agent Pool）

#### 步骤 1：Agent 工厂（创建独立实例）

```python
# automind/server/deps.py

@dataclass
class AppContext:
    # ... 现有字段 ...
    # Phase 2 新增：会话级 Agent 池
    session_agents: dict[str, Any] = field(default_factory=dict)
    session_agent_events: dict[str, asyncio.Event] = field(default_factory=dict)
    max_concurrent_agents: int = 10


async def create_session_agent(ctx: AppContext, session_id: str) -> Any:
    """为一个会话创建独立的 Agent 实例（复用 LLM 配置）。"""
    from automind.agent import AutoMindAgent
    from automind.core.config import AgentConfig
    from automind.core.types import ExecutionMode, InteractionMode

    # 从当前全局配置克隆 LLM/权限/执行参数
    base = ctx.agent if ctx.agent else _build_base_agent(ctx)
    config = AgentConfig(
        llm=base.config.llm.model_copy(deep=True),
        permissions=base.config.permissions.model_copy(deep=True),
        memory=base.config.memory.model_copy(deep=True),
        execution=base.config.execution.model_copy(deep=True),
        project_root=base.config.project_root,
    )
    agent = AutoMindAgent(config)
    agent._interaction = base._interaction
    agent._mode = base._mode

    # 会话隔离的对话历史
    agent._chat_history = _load_chat_history(session_id)

    ctx.session_agents[session_id] = agent
    return agent


async def destroy_session_agent(ctx: AppContext, session_id: str) -> None:
    """销毁会话 Agent 并释放资源。"""
    agent = ctx.session_agents.pop(session_id, None)
    if agent:
        try:
            await agent.close()
        except Exception:
            pass
```

#### 步骤 2：对话历史按 session 隔离

```python
# automind/server/chat_store.py

_CHAT_DIR = Path(".automind") / "chats"

def _load_chat_history(session_id: str) -> list[dict]:
    path = _CHAT_DIR / f"{session_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []

def _save_chat_history(session_id: str, history: list[dict]) -> None:
    _CHAT_DIR.mkdir(parents=True, exist_ok=True)
    path = _CHAT_DIR / f"{session_id}.json"
    path.write_text(
        json.dumps(history[-200:], ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def _delete_chat_history(session_id: str) -> None:
    path = _CHAT_DIR / f"{session_id}.json"
    path.unlink(missing_ok=True)
```

#### 步骤 3：WebSocket 审批回调绑定到 session agent

```python
# server/ws.py — WebSocket 连接处理

async def ws_run(ws: WebSocket, ctx: AppContext, data: dict):
    session_id = uuid.uuid4().hex[:12]
    agent = await create_session_agent(ctx, session_id)

    # 审批回调绑定到此会话的 agent（不污染全局）
    async def _approval_cb(tool_name, args, tier, reason):
        # ... 向此 ws 发送审批请求 ...

    agent.approval_callback = _approval_cb

    try:
        # ... 执行任务 ...
    finally:
        try:
            _save_chat_history(session_id, agent._chat_history)
        except Exception:
            pass
        await destroy_session_agent(ctx, session_id)
```

#### 步骤 4：并发 Agent 数量上限

```python
async def acquire_agent_slot(ctx: AppContext) -> str | None:
    """获取 Agent 槽位。若满则返回 None（前端提示排队或稍后重试）。"""
    if len(ctx.session_agents) >= ctx.max_concurrent_agents:
        return None
    return str(uuid.uuid4().hex[:12])
```

#### 步骤 5：资源安全释放

```python
@app.on_event("shutdown")
async def _shutdown_cleanup():
    ctx = get_app_context()
    for sid in list(ctx.session_agents.keys()):
        await destroy_session_agent(ctx, sid)
```

### 7.5 项目目录隔离

当前所有 session 共享 `config.project_root`，多用户若操作同一项目目录将产生**文件级冲突**（同时写入同一文件）。

**方案**：支持两种模式
- **共享模式**（默认）：所有 session 操作同一项目目录（适合团队协作场景，需前端显式提示）
- **隔离模式**：每个 session 可设置独立的工作目录（`/api/config/project` 改为 session 级配置）

### 7.6 对前端的影响

| 端点/行为 | 改动 |
|----------|------|
| `/api/chat/history` | 增加 `session_id` query 参数，返回该会话的独立历史 |
| `/api/chat/reset` | 增加 `session_id`，只清空该会话 |
| `/api/config` | provider/model 切换仅影响当前会话（或增加"全局切换"选项） |
| `/api/status` | 返回当前活跃 session 数 |
| WebSocket | 每条消息已带 `session_id`，无需改动 |
| 前端 | 首次访问时后端分配 `session_id`，前端存储在 `sessionStorage` 中 |

### 7.7 追加路线图

```
Phase 2：多用户支持（在 Phase 1 完成后启动，预估 2 周）

第 5 周：会话隔离基础
├── Agent 工厂（create_session_agent / destroy_session_agent）
├── 对话历史按 session 分文件存储
├── AppContext 增加 session_agents + 并发上限
└── 单元测试: session agent 创建/销毁/隔离

第 6 周：Web 层适配
├── /api/run 使用 session agent（非全局 _agent）
├── /api/chat 使用 session agent + 会话级历史
├── WebSocket 审批回调绑定到 session agent
├── /api/config 会话级配置 vs 全局配置分离
├── 前后端联调: session_id 生成与传递
└── 集成测试: 多用户并发场景（用 TestClient 模拟 3 个并发请求）
```

---

## 8. 功能优化建议

以下建议基于对现有功能完整度的审视，聚焦于**提升用户效率**和**扩展自动化能力**。

### 8.1 任务模板与预设提示词

**现状**：用户每次需要手动输入完整的任务描述，重复性高。

**建议**：

| 功能 | 说明 |
|------|------|
| **预设模板库** | 内置 20+ 常用模板（"初始化 Python 项目"、"修复 lint 错误"、"写单元测试"、"生成 API 文档"、"Docker 化部署"、"代码重构"等），点击即填充 |
| **自定义模板** | 用户可保存自己的常用提示词模板，本地持久化到 `.automind_config.json` |
| **模板变量** | 支持 `{{project_name}}`、`{{file_path}}` 等占位符，使用时弹窗填写 |
| **模式推荐** | 根据模板内容自动建议合适的交互模式（对话/工作/编程） |

**实现要点**：

```python
# 后端新增端点
GET  /api/templates          # 获取内置 + 自定义模板列表
POST /api/templates          # 保存自定义模板
DELETE /api/templates/{id}   # 删除模板
```

### 8.2 输出操作增强

**现状**：对话输出仅显示在聊天区，缺少便捷的二次利用能力。

**建议**：

| 功能 | 说明 |
|------|------|
| **一键复制** | 每条助手消息右上角显示复制按钮，点击复制完整 Markdown 原文 |
| **导出对话** | 支持导出当前对话为 Markdown / PDF / JSON 文件 |
| **代码块独立复制** | 每个 ` ``` ` 代码块右上角显示"复制代码"按钮 |
| **重新生成** | 对最后一条助手回复，提供"重新生成"按钮（用相同 prompt 再请求一次） |
| **编辑后重发** | 允许用户编辑已发送的消息后重新提交（分支对话） |

### 8.3 对话管理增强

**现状**：单会话模式，不支持多轮对话的管理和回溯。

**建议**：

| 功能 | 说明 |
|------|------|
| **对话分支** | 用户编辑历史消息后重新发送时，自动创建分支而非覆盖 |
| **对话书签** | 对重要对话节点打书签，方便快速跳转 |
| **历史搜索** | 在当前对话中搜索关键词，高亮并滚动到对应消息 |
| **消息引用** | 用户可以引用之前的某条回复作为后续问题的上下文 |
| **对话对比** | 并排显示两个分支的输出结果 |

### 8.4 文件与项目管理

**现状**：项目目录通过右上角浏览选择，工具执行结果在聊天区显示。

**建议**：

| 功能 | 说明 |
|------|------|
| **文件树视图** | 侧边栏增加"文件"标签，展示项目目录树，点击预览/编辑 |
| **文件变更感知** | 工具执行后自动刷新文件树，变更文件高亮标记 |
| **Diff 可视化** | `file_edit` 产生的 diff 在聊天区以绿色/红色标注渲染 |
| **最近文件** | 右侧面板增加"最近操作的文件"列表，点击快速打开 |
| **拖拽上传** | 输入区支持拖拽文件/图片（当前仅支持按钮选择图片） |

### 8.5 执行可视化与调试

**现状**：任务执行时右侧面板显示统计数据，但过程缺乏直观呈现。

**建议**：

| 功能 | 说明 |
|------|------|
| **步骤时间线** | 计划执行时显示 Gantt 风格步骤时间线，实时反映各 goal 状态 |
| **工具调用动画** | 工具执行时输入区上方显示动效提示（"正在执行 terminal..."） |
| **ReAct 思考链可视化** | 编程模式下，Thought → Action → Observation 以可折叠卡片展示 |
| **Token 消耗实时图** | 右侧面板增加 Token 消耗折线图（按时间/按步骤） |
| **错误诊断面板** | 执行失败时自动弹出错误分析摘要 + 建议操作 |

### 8.6 通知与告警

**现状**：任务完成后仅在聊天区显示结果，无主动通知。

**建议**：

| 功能 | 说明 |
|------|------|
| **浏览器通知** | 长时间任务完成后发送 Web Notification（需用户授权） |
| **声音提示** | 任务完成/失败时播放不同提示音（可关闭） |
| **定时任务失败告警** | 定时任务连续失败 N 次后，前端徽标变红 + 通知 |
| **Token 预算告警** | Token 累计用量达到配置阈值时提醒 |

### 8.7 多模态能力深化

**现状**：已支持图片输入（视觉模型）和语音输入（Web Speech API）。

**建议**：

| 功能 | 说明 |
|------|------|
| **截图粘贴** | 聊天区直接 Ctrl+V 粘贴剪贴板中的截图 |
| **语音输出 (TTS)** | 助手回复支持语音朗读（Web Speech Synthesis API） |
| **文件预览** | 拖入 PDF/Office 文件时自动转文本提取摘要 |
| **视频关键帧** | 上传视频文件时自动提取关键帧送给视觉模型 |

### 8.8 协作与分享

**现状**：无分享能力，仅在本地运行。

**建议**：

| 功能 | 说明 |
|------|------|
| **分享对话链接** | 将当前对话生成一个只读 HTML 页面，可分享给他人查看 |
| **导出为 Notebook** | 编程模式的完整对话导出为 Jupyter Notebook (.ipynb) |
| **Prompt 分享** | 一键将当前任务 prompt 复制为 curl 命令（含模型/参数） |
| **团队模板库** | 通过网络目录共享模板（企业场景） |

---

## 9. 界面优化建议

以下建议基于对当前 `static/index.html`（92KB 单文件 SPA）的 CSS/布局审查。

### 9.1 布局与响应式

**现状**：三段式布局（侧边栏 / 聊天区 / 右面板），宽度 < 1100px 时隐藏右面板，侧边栏压缩为仅图标。

**改进建议**：

| 问题 | 建议 |
|------|------|
| 右面板在小屏完全消失 | 改为可切换的覆盖式抽屉（toggle），不被屏幕尺寸硬隐藏 |
| 移动端不可用 | 增加移动端断点（< 768px），输入区固定在底部，消息列表全屏 |
| 侧边栏折叠时信息密度低 | 折叠态增加 tooltip 悬浮提示按钮功能 |
| 模式切换占用 header 空间 | 将模式切换并入输入区左侧的下拉菜单，释放 header 空间给系统状态 |

### 9.2 主题与外观

**现状**：单一暗色主题（CSS 变量驱动），视觉效果专业。

**改进建议**：

| 功能 | 说明 |
|------|------|
| **亮色主题** | 增加亮色配色方案（`prefers-color-scheme: light` 自动切换 + 手动切换按钮） |
| **字体大小调节** | 设置中增加"小/中/大"三档字体缩放（影响聊天区和代码块） |
| **代码高亮主题** | 代码块支持多种语法高亮主题（Monokai / GitHub / One Dark） |
| **消息密度** | 设置中增加"紧凑/舒适"切换，控制消息间距 |
| **自定义强调色** | 允许用户在设置中选择预设的主色调（蓝/紫/绿/橙） |

### 9.3 交互与快捷键

**现状**：仅支持 Enter 发送、Shift+Enter 换行。

**改进建议**：

| 快捷键 | 功能 |
|--------|------|
| `Ctrl+K` | 打开命令面板（切换模式、打开设置、清空对话等） |
| `Ctrl+L` | 清空当前对话 |
| `Ctrl+↑/↓` | 在历史消息间导航（编辑模式） |
| `Ctrl+B` | 切换侧边栏显示/隐藏 |
| `Ctrl+Shift+C` | 复制最后一条助手回复 |
| `Ctrl+.` | 中断当前任务 |
| `Esc` | 关闭所有弹窗/面板 |

在设置 Modal 的"通用"标签中增加"键盘快捷键"参考表（静态 HTML 表格）。

### 9.4 消息渲染优化

**现状**：Markdown 渲染已支持代码块、表格、图片。但存在可优化点：

| 问题 | 建议 |
|------|------|
| 长消息无折叠 | 超过 500 字的助手回复默认折叠，显示"展开全部"按钮 |
| 表格在窄屏溢出 | 表格容器增加 `overflow-x: auto` |
| 代码块无行号 | 代码块左侧增加行号显示（CSS counter 实现，零 JS 开销） |
| 流式输出闪烁 | 打字光标 `▍` 改为 CSS `@keyframes blink`，减少重排 |
| LaTeX 不渲染 | 增加 KaTeX/MathJax 支持（可选，CDN 按需加载） |

### 9.5 输入区增强

**现状**：基础 textarea + 发送/停止/附件/语音四个按钮。

**改进建议**：

| 功能 | 说明 |
|------|------|
| **输入历史** | 按 `Ctrl+↑/↓` 浏览历史输入（sessionStorage 存储最近 50 条） |
| **自动增高** | textarea 已有 `scrollHeight` 自适应，增加最大行数限制（当前 5 行合理） |
| **字符计数** | 输入区右下角显示当前字符数 / Token 估算 |
| **模式指示器** | 输入区左侧显示当前激活模式的图标和颜色指示 |
| **上下文提示** | 当有附加图片时，输入区上方显示缩略图 + 数量标签 |
| **Slash 命令** | 输入 `/` 弹出命令菜单（`/clear` `/mode chat` `/export` `/templates`） |

### 9.6 右侧面板优化

**现状**：显示任务统计、Token 累计、计划树、HTML 预览、安全审计。

**改进建议**：

| 问题 | 建议 |
|------|------|
| 面板内容静态堆叠 | 增加标签页切换（统计 / 计划 / 文件 / 审计），节省垂直空间 |
| 计划树只读 | 点击计划树中的 goal 节点跳转到对应执行步骤 |
| HTML 预览列表无预览图 | 对 HTML 文件路径增加缩略图生成（iframe snapshot，可选） |
| 安全审计缺少时间线 | 改为时间线布局，最新事件在上，支持按风险等级筛选 |

### 9.7 设置弹窗优化

**现状**：单一 Modal 包含三个标签（模型 / API Keys / 通用），功能完善。

**改进建议**：

| 功能 | 说明 |
|------|------|
| **配置导入导出** | "通用"标签增加"导出配置"和"导入配置"按钮（JSON 文件） |
| **连通性测试可视化** | 测试 API 时显示逐阶段进度条（解析 → 连接 → 认证 → 响应） |
| **模型对比** | 模型选择器旁增加"对比"按钮，同时测试两个模型的响应质量 |
| **自定义模型列表管理** | 当前已支持添加/删除自定义模型，增加拖拽排序 |

### 9.8 辅助功能 (Accessibility)

| 功能 | 说明 |
|------|------|
| **ARIA 标签** | 为侧边栏按钮、模式切换、输入区添加 `aria-label` |
| **焦点管理** | Modal 打开时自动聚焦第一个输入框，关闭时归还焦点 |
| **键盘导航** | Tab 键在消息列表间导航，Enter 展开折叠内容 |
| **屏幕阅读器** | 消息使用 `role="log"` + `aria-live="polite"` 自动播报新消息 |
| **对比度** | 确保文本/背景对比度 ≥ 4.5:1（WCAG AA） |

---

## 10. 功能与界面新增缺陷追踪

| 编号 | 建议 | 涉及层面 | 预估工时 | 优先级 |
|------|------|---------|:---:|:---:|
| F-01 | 任务模板与预设提示词 | 功能 | 1 周 | 高 |
| F-02 | 一键复制 + 代码块复制 + 导出对话 | 功能 | 3 天 | 高 |
| F-03 | 文件树视图 + Diff 可视化 | 功能 + 界面 | 1 周 | 高 |
| F-04 | 步骤时间线 + 工具调用动画 | 界面 | 1 周 | 中 |
| F-05 | 亮色主题 + 字体缩放 | 界面 | 3 天 | 中 |
| F-06 | 键盘快捷键体系 + 命令面板 | 界面 | 1 周 | 中 |
| F-07 | 浏览器通知 + Token 预算告警 | 功能 | 2 天 | 中 |
| F-08 | 对话分支 + 历史搜索 + 书签 | 功能 | 1 周 | 中 |
| F-09 | 消息折叠 + 代码行号 + LaTeX | 界面 | 3 天 | 低 |
| F-10 | Slash 命令 + 输入历史 + 字符计数 | 界面 | 1 周 | 低 |
| F-11 | 截图粘贴 + 语音输出 + 文件拖拽 | 功能 | 1 周 | 低 |
| F-12 | 分享对话 + 导出 Notebook | 功能 | 3 天 | 低 |
| F-13 | 配置导入导出 + 模型对比 | 界面 | 3 天 | 低 |
| F-14 | 无障碍 (ARIA + 键盘导航 + 对比度) | 界面 | 3 天 | 低 |
| **F-15** | **高级统计仪表盘（上下文/命中率/效率）** | **功能 + 界面** | **1 周** | **高** |
| **F-16** | **高端视觉体验升级（玻璃态/粒子/动效）** | **界面** | **1 周** | **高** |

---

## 11. 高级统计功能（上下文百分比 / 命中率 / 平均命中率）

### 11.1 设计目标

当前 `/api/stats` 端点返回任务数量、成功率、Token 消耗、工具使用排行等基础统计，但缺少以下关键指标：

| 指标 | 含义 | 当前状态 |
|------|------|:---:|
| **上下文使用率 (Context Usage %)** | 当前对话窗口 Token 用量占最大窗口的比例 | ❌ 未对外暴露 |
| **上下文压缩触发率** | 上下文压缩被触发的频率 | ❌ 未统计 |
| **工具命中率 (Tool Hit Rate)** | 工具执行成功次数 / 总调用次数 | ❌ 未独立统计 |
| **计划命中率 (Plan Hit Rate)** | 计划中目标完成数 / 总目标数 | ❌ 未独立统计 |
| **自我修正成功率** | 自动修正成功次数 / 修正尝试次数 | ❌ 未独立统计 |
| **记忆命中率 (Memory Hit Rate)** | 检索到相关记忆的任务数 / 总任务数 | ❌ 未统计 |
| **综合平均命中率** | 以上各项命中率的加权平均 | ❌ |
| **Token 效率** | 输出有效字符 / 消耗 Token 比值 | ❌ |

### 11.2 后端数据源

以下是系统中**已存在但未汇总**的数据，可直接利用：

```python
# 1. 上下文统计 — ContextManager.get_stats() 已返回
ctx_stats = agent.context_mgr.get_stats()
# → {"estimated_tokens": 12400, "max_tokens": 128000, "has_summary": True, ...}

# 2. 资源预算 — ResourceManager 已计算
res_stats = agent.resources.get_stats()
# → {"token_usage_pct": 62.3, "elapsed_seconds": 180.5, ...}

# 3. 工具执行记录 — _task_history 中每步含 steps/errors_corrected/backtracks
# 可通过 records 反向统计

# 4. 审计日志 — PermissionEngine.audit_log 已有每次工具调用的决策/等级
for entry in agent.permissions.audit_log:
    # entry.tool_name, entry.decision (allow/deny/ask_user), entry.tier, entry.risk_score

# 5. 计划执行进度 — HierarchicalPlanner.get_progress() 已计算
progress = agent.hierarchical_planner.get_progress(plan)
# → {"completed": 3, "total": 5, "failed": 1, "percent": 60.0, ...}
```

### 11.3 新增统计端点

#### 端点 1：`GET /api/stats/detail` — 详细仪表盘

```python
@app.get("/api/stats/detail")
async def api_stats_detail():
    """高级统计仪表盘 — 上下文、命中率、效率指标。"""
    agent = get_agent()
    hist = _task_history

    # ── 上下文使用率 ──
    ctx_stats = agent.context_mgr.get_stats()
    context_usage_pct = round(
        ctx_stats["estimated_tokens"] / max(ctx_stats["max_tokens"], 1) * 100, 1
    )
    context_compressed = ctx_stats["has_summary"]

    # ── 工具命中率（聚合所有历史任务） ──
    tool_total = 0
    tool_success = 0
    tool_errors_corrected = 0
    plan_goals_total = 0
    plan_goals_completed = 0
    self_correction_attempts = 0
    self_correction_success = 0

    for h in hist:
        steps = h.get("steps", 0)
        backtracks = h.get("backtracks", 0)
        errors = h.get("errors_corrected", 0)

        tool_total += steps + backtracks  # 总工具调用 ≈ 步骤 + 回溯重试
        tool_success += steps - backtracks  # 成功 ≈ 步骤 - 回溯
        tool_errors_corrected += errors
        self_correction_attempts += backtracks
        self_correction_success += errors

        if h.get("plan") and isinstance(h["plan"], dict):
            root = h["plan"].get("root_goal", {})
            all_goals = _count_goals(root)
            completed = _count_completed(root)
            plan_goals_total += all_goals
            plan_goals_completed += completed

    # ── 计算各项命中率 ──
    tool_hit_rate = round(tool_success / max(tool_total, 1) * 100, 1)
    plan_hit_rate = round(plan_goals_completed / max(plan_goals_total, 1) * 100, 1)
    correction_rate = round(
        self_correction_success / max(self_correction_attempts, 1) * 100, 1
    ) if self_correction_attempts > 0 else None

    # 任务成功率（已有）
    tasks_total = len(hist)
    tasks_success = sum(1 for h in hist if h.get("success"))
    task_success_rate = round(tasks_success / max(tasks_total, 1) * 100, 1)

    # ── 综合平均命中率（加权） ──
    rates = [tool_hit_rate, plan_hit_rate, task_success_rate]
    if correction_rate is not None:
        rates.append(correction_rate)
    avg_hit_rate = round(sum(rates) / len(rates), 1)

    # ── Token 效率 ──
    total_prompt = _token_totals.get("prompt", 1)
    total_completion = _token_totals.get("completion", 1)
    total_output_chars = sum(
        len(str(h.get("output", ""))) for h in hist
    )
    token_efficiency = round(total_output_chars / max(total_completion, 1), 1)

    return {
        "context": {
            "usage_pct": context_usage_pct,
            "estimated_tokens": ctx_stats["estimated_tokens"],
            "max_tokens": ctx_stats["max_tokens"],
            "compressed": context_compressed,
            "summary_length": ctx_stats.get("summary_length", 0),
        },
        "hit_rates": {
            "tool_hit_rate": tool_hit_rate,
            "plan_hit_rate": plan_hit_rate,
            "task_success_rate": task_success_rate,
            "self_correction_rate": correction_rate,
            "average_hit_rate": avg_hit_rate,
        },
        "efficiency": {
            "token_efficiency_chars_per_token": token_efficiency,
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_output_chars": total_output_chars,
        },
        "totals": {
            "tool_calls": tool_total,
            "tool_successes": tool_success,
            "tool_errors_corrected": tool_errors_corrected,
            "plan_goals_total": plan_goals_total,
            "plan_goals_completed": plan_goals_completed,
            "tasks_total": tasks_total,
            "tasks_success": tasks_success,
        },
        "memory": {
            "long_term_docs": agent.memory.long_term.count(),
            "short_term_msgs": len(agent.memory.short_term._messages),
            "kg_entities": agent.memory.knowledge_graph.entity_count,
            "kg_relations": agent.memory.knowledge_graph.relation_count,
        },
    }
```

#### 端点 2：`GET /api/stats/context` — 实时上下文快照

```python
@app.get("/api/stats/context")
async def api_stats_context():
    """当前会话上下文的实时快照。"""
    agent = get_agent()
    ctx = agent.context_mgr.get_stats()
    res = agent.resources.get_stats()

    return {
        "context_tokens": ctx["estimated_tokens"],
        "context_max": ctx["max_tokens"],
        "context_pct": round(ctx["estimated_tokens"] / max(ctx["max_tokens"], 1) * 100, 1),
        "compression_triggered": ctx["has_summary"],
        "summary_length": ctx["summary_length"],
        "message_count": ctx["message_count"],
        "token_budget_pct": res["token_usage_pct"],
        "token_budget_used": res["tokens_used"],
        "elapsed_seconds": res["elapsed_seconds"],
    }
```

#### 端点 3：`GET /api/stats/history` — 历史趋势（供图表）

```python
@app.get("/api/stats/history")
async def api_stats_history():
    """返回最近 N 次任务的各项命中率趋势（供前端折线图）。"""
    hist = _task_history[-50:]  # 最近 50 条
    points = []
    for h in hist:
        steps = h.get("steps", 0) or 0
        backtracks = h.get("backtracks", 0) or 0
        errors = h.get("errors_corrected", 0) or 0
        total = steps + backtracks
        points.append({
            "session_id": h.get("session_id", ""),
            "task": (h.get("task", "") or "")[:80],
            "interaction": h.get("interaction", ""),
            "success": h.get("success", False),
            "tool_hit_rate": round((steps - backtracks) / max(total, 1) * 100, 1),
            "tokens": h.get("tokens", 0),
            "duration_ms": h.get("duration_ms", 0),
            "self_corrected": errors > 0,
        })
    return {"points": points, "count": len(points)}
```

### 11.4 前端统计面板设计

在右侧面板新增"📊 高级统计"标签页，替代当前静态卡片布局：

```
┌────────────────────────────────────┐
│  📊 高级统计          [🔄 刷新]    │
├────────────────────────────────────┤
│  上下文使用率                      │
│  ████████████░░░░░░░░  62.4%       │
│  12,400 / 128,000 tokens           │
│  ⚠ 已触发压缩  摘要: 1,023 chars   │
├────────────────────────────────────┤
│  命中率仪表盘                      │
│  ┌──────────┬──────────┐          │
│  │ 工具命中  │ 计划命中  │          │
│  │  ████░░   │  █████░  │          │
│  │  78.5%    │  91.2%   │          │
│  ├──────────┼──────────┤          │
│  │ 任务成功  │ 自我修正  │          │
│  │  █████░   │  ███░░░  │          │
│  │  88.0%    │  60.0%   │          │
│  └──────────┴──────────┘          │
│  ★ 综合平均命中率: 79.4%          │
├────────────────────────────────────┤
│  效率指标                          │
│  Token 效率: 3.2 chars/token       │
│  总输出字符: 245,832               │
│  累计 Token: 76,481                │
├────────────────────────────────────┤
│  趋势图 (最近 20 次任务)           │
│  ┌────────────────────────────┐   │
│  │  📈 工具命中率趋势          │   │
│  │  ·──·──·╲  ·──·            │   │
│  │  ──────────────────→ 次数  │   │
│  └────────────────────────────┘   │
├────────────────────────────────────┤
│  记忆系统                          │
│  向量存储: 128 docs                │
│  知识图谱: 42 实体 / 15 关系       │
│  短期窗口: 18 msgs / 12.4K tokens  │
└────────────────────────────────────┘
```

---

## 12. 高端视觉体验升级方案

### 12.1 设计关键词

> **暗色基底 + 玻璃态 + 渐变光晕 + 微动效 + 专业排版**

### 12.2 全局视觉升级

#### 12.2.1 玻璃态毛玻璃效果（Glass Morphism）

将当前纯色 `var(--bg1)` 面板升级为半透明玻璃态：

```css
/* 当前 */
#sidebar { background: var(--bg1); }

/* 升级为 */
#sidebar {
  background: rgba(14, 18, 32, 0.72);
  backdrop-filter: blur(24px) saturate(140%);
  -webkit-backdrop-filter: blur(24px) saturate(140%);
  border-right: 1px solid rgba(108, 140, 255, 0.12);
}
```

应用于：侧边栏、右面板、Modal、消息气泡、设置面板。

#### 12.2.2 动态光晕背景

```css
body {
  /* 替换静态 radial-gradient */
  background: var(--bg0);
}

body::before {
  content: '';
  position: fixed; inset: 0; z-index: -1;
  background:
    radial-gradient(ellipse 60% 50% at 20% 50%, rgba(108, 140, 255, 0.06) 0%, transparent 70%),
    radial-gradient(ellipse 50% 60% at 80% 30%, rgba(157, 123, 255, 0.05) 0%, transparent 70%),
    radial-gradient(ellipse 40% 40% at 50% 80%, rgba(58, 209, 127, 0.04) 0%, transparent 70%);
  animation: ambientShift 20s ease-in-out infinite alternate;
}

@keyframes ambientShift {
  0%   { opacity: 0.7; transform: scale(1); }
  100% { opacity: 1;   transform: scale(1.05); }
}
```

#### 12.2.3 侧边栏 Logo 动态渐变

```css
#sidebar .logo > span:not(.dot):not(:last-child) {
  background: linear-gradient(
    135deg,
    var(--accent) 0%,
    var(--accent2) 30%,
    #6ce0ff 60%,
    var(--accent) 100%
  );
  background-size: 200% 200%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: logoShimmer 4s ease-in-out infinite;
}

@keyframes logoShimmer {
  0%, 100% { background-position: 0% 50%; }
  50%      { background-position: 100% 50%; }
}
```

#### 12.2.4 微粒子背景（Canvas 粒子网络）

在 `<body>` 底部添加 Canvas 粒子网络（低性能开销，60fps 减半至 30fps CSS animation 驱动）：

```javascript
// 50 个微粒子，连线距离 < 150px，透明度 0.03-0.08
// 仅在非移动端启用（window.innerWidth > 768px）
// 使用 requestAnimationFrame + 节流 30fps
```

#### 12.2.5 消息气泡升级

```css
/* 当前静态气泡 */
.bubble {
  background: var(--bg2);
  border-radius: var(--radius);
}

/* 升级：悬浮感 + 微光边 */
.bubble {
  background: rgba(22, 28, 46, 0.65);
  backdrop-filter: blur(12px);
  border: 1px solid rgba(108, 140, 255, 0.08);
  border-radius: 16px;
  box-shadow:
    0 4px 24px rgba(0, 0, 0, 0.25),
    inset 0 1px 0 rgba(255, 255, 255, 0.03);
  transition: box-shadow var(--transition), border-color var(--transition);
}

.bubble:hover {
  border-color: rgba(108, 140, 255, 0.18);
  box-shadow:
    0 6px 32px rgba(108, 140, 255, 0.08),
    inset 0 1px 0 rgba(255, 255, 255, 0.05);
}
```

#### 12.2.6 模式切换按钮动画

```css
.mode-switch button {
  /* 当前已有渐变背景 + 阴影 */
  /* 增加切换过渡动画 */
  transition: all 0.35s cubic-bezier(0.4, 0, 0.2, 1);
}

.mode-switch button.active {
  /* 增加脉冲光晕 */
  animation: modeGlow 3s ease-in-out infinite;
}

@keyframes modeGlow {
  0%, 100% { box-shadow: 0 3px 12px var(--accent-glow); }
  50%      { box-shadow: 0 3px 24px rgba(108, 140, 255, 0.45); }
}
```

### 12.3 统计面板升级为 Dashboard

右侧面板改造为标签式 Dashboard：

```
┌─────────────────────────────────────┐
│  [📊 仪表盘] [📋 计划] [📁 HTML] [🛡️ 审计]  │  ← 标签切换
├─────────────────────────────────────┤
│                                     │
│  ┌───────┐ ┌───────┐ ┌───────┐    │
│  │  78%  │ │  91%  │ │ 12.4K │    │  ← 圆环图卡片
│  │ 工具   │ │ 计划   │ │ Token  │    │
│  └───────┘ └───────┘ └───────┘    │
│                                     │
│  上下文使用率                        │
│  ████████████░░░░░░  62.4%  ← 发光进度条 │
│                                     │
│  📈 最近 20 次命中率趋势             │
│  ┌──────────────────────────┐      │
│  │  ·   ··    ·  ·          │      │  ← Canvas/SVG 迷你折线图
│  └──────────────────────────┘      │
│                                     │
│  🧠 记忆系统: 128 docs · 42 实体    │
│  ⏱ Token 效率: 3.2 chars/token     │
└─────────────────────────────────────┘
```

#### 圆环进度图（纯 CSS）

```css
.ring-chart {
  width: 80px; height: 80px;
  border-radius: 50%;
  background: conic-gradient(
    var(--accent) 0% var(--pct),
    var(--bg3) var(--pct) 100%
  );
  display: flex; align-items: center; justify-content: center;
  position: relative;
}
.ring-chart::after {
  content: '';
  width: 58px; height: 58px;
  border-radius: 50%;
  background: var(--bg1);
  position: absolute;
}
.ring-chart .value {
  position: relative; z-index: 1;
  font-weight: 700; font-size: 1.1em;
}
```

#### 发光进度条

```css
.glow-bar {
  height: 8px;
  border-radius: 4px;
  background: var(--bg3);
  overflow: hidden;
}
.glow-bar .fill {
  height: 100%;
  border-radius: 4px;
  background: var(--accent-grad);
  box-shadow: 0 0 12px var(--accent-glow);
  transition: width 0.8s cubic-bezier(0.4, 0, 0.2, 1);
}
```

### 12.4 微交互与动效规范

| 元素 | 动效 | 时长 | 缓动 |
|------|------|:---:|------|
| 页面加载 | 消息列表从下淡入 | 400ms | ease-out |
| 新消息 | 从下方滑入 + 渐显 | 300ms | `cubic-bezier(0.34, 1.56, 0.64, 1)` |
| 按钮悬停 | 微上浮 + 阴影增强 | 200ms | ease |
| Modal 打开 | 缩放 0.95→1 + 渐显 | 250ms | `cubic-bezier(0.4, 0, 0.2, 1)` |
| 侧边栏折叠 | 宽度过渡 | 350ms | `cubic-bezier(0.4, 0, 0.2, 1)` |
| 统计数字更新 | 数字递增动画 (countUp) | 600ms | ease-out |
| 进度条填充 | 宽度过渡 | 800ms | `cubic-bezier(0.4, 0, 0.2, 1)` |
| 审批弹窗 | 从右侧滑入 | 300ms | ease-out |
| 打字光标 | 闪烁 | 500ms | step-end |

### 12.5 字体与版式

```css
/* 引入专业等宽字体 */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root {
  --font: 'Inter', system-ui, -apple-system, sans-serif;
  --mono: 'Cascadia Code', 'JetBrains Mono', 'Fira Code', 'SF Mono', monospace;
  --font-size-sm: 0.82rem;
  --font-size-base: 0.92rem;
  --font-size-lg: 1.1rem;
  --font-size-xl: 1.35rem;
  --line-height: 1.6;
  --letter-spacing: -0.01em;
}
```

### 12.6 颜色升级

当前调色板已经很优秀，微调增强对比度：

```css
:root {
  /* 升级后的强调色 — 更鲜艳、更通透 */
  --accent: #7b9fff;
  --accent2: #b594ff;
  --accent-glow: rgba(123, 159, 255, 0.35);
  --accent-grad: linear-gradient(135deg, #7b9fff, #b594ff);

  /* 成功/危险色 — 更高饱和度 */
  --green: #3ce68a;
  --red: #ff6b6b;
  --yellow: #f5c542;

  /* 表面增加层次 */
  --bg0: #060913;
  --bg1: rgba(14, 18, 32, 0.72);
  --bg2: rgba(22, 28, 46, 0.65);
  --bg3: rgba(31, 39, 64, 0.8);
}
```

---

## 13. 新增到 §10 缺陷追踪

上述设计与统计方案对应的实施工单：

| 编号 | 建议 | 涉及层面 | 预估工时 | 优先级 |
|------|------|---------|:---:|:---:|
| F-15-1 | `/api/stats/detail` 端点实现 | 后端 | 1 天 | 高 |
| F-15-2 | `/api/stats/context` 实时快照 | 后端 | 0.5 天 | 高 |
| F-15-3 | `/api/stats/history` 趋势数据 | 后端 | 0.5 天 | 中 |
| F-15-4 | 前端统计仪表盘（标签+卡片+圆环+进度条） | 前端 | 2 天 | 高 |
| F-15-5 | 前端迷你折线图（Canvas/SVG） | 前端 | 1 天 | 中 |
| F-16-1 | 全局玻璃态 + 动态光晕背景 | CSS | 1 天 | 高 |
| F-16-2 | Logo 渐变 + 消息气泡悬浮效果 | CSS | 0.5 天 | 高 |
| F-16-3 | Canvas 粒子背景（可选） | JS | 1 天 | 低 |
| F-16-4 | 微交互动效规范统一 | CSS | 1 天 | 中 |
| F-16-5 | Inter 字体 + 版式升级 + 颜色微调 | CSS | 0.5 天 | 中 |
| F-16-6 | 圆环进度图 + 发光进度条组件 | CSS | 1 天 | 中 |
| F-16-7 | 数字递增动画 (countUp) | JS | 0.5 天 | 低 |

---

---

## 14. 工程化与基础设施改善（补充自实测评估）

以下建议基于对 v0.1.0 源码的全面实测评估，补充前述计划中**尚未覆盖**的工程化短板。按优先级排列。

### 14.1 版本号统一（P0）

**现状**：多文件版本号不一致。
- `automind/__init__.py` → `__version__ = "0.1.0"`
- `automind/server.py` → `app = FastAPI(title="AutoMind Agent", version="0.3.0")`
- `pyproject.toml` → `version = "0.1.0"`
- README 中各处硬编码 v0.1.0

**建议**：统一为单数据源。

```python
# automind/__init__.py
__version__ = "0.2.0"

# automind/server.py — 从包导入
from automind import __version__
app = FastAPI(title="AutoMind Agent", version=__version__)

# pyproject.toml — 通过 `tool.setuptools.dynamic` 或发布时同步
```

**操作项**：
| 步骤 | 内容 |
|------|------|
| 1 | 确定新版本号（建议 v0.2.0） |
| 2 | `__init__.py` 设为唯一来源 |
| 3 | `server.py` 改为 `from automind import __version__` |
| 4 | `pyproject.toml` 使用 `dynamic = ["version"]` 并配置 `tool.setuptools.dynamic` |
| 5 | README / AUTOMIND_REFACTOR_PLAN 头部版本号同步 |

---

### 14.2 CI/CD 持续集成（P1）

**现状**：无任何 CI 配置，无法自动验证 PR 质量。

**建议**：增加 GitHub Actions 工作流。

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: pip install -e ".[dev]"
      - run: ruff check .
      - run: ruff format --check .
      - run: mypy automind/
      - run: pytest tests/ --cov=automind --cov-report=term-missing
```

**检查点**：
| 步骤 | 内容 | 验证 |
|------|------|------|
| 1 | 创建 `.github/workflows/ci.yml` | GitHub Actions 界面显示绿色勾 |
| 2 | 添加 `pytest-cov` 到 `dev` 依赖 | `pip list` 显示 |
| 3 | 添加 `.coveragerc` 或 `pyproject.toml` 覆盖率配置 | 报告生成 |
| 4 | 可选：添加 `codecov` / `coveralls` 上传 | badge 显示 |

---

### 14.3 TUI / REPL 交互完善（P1）

**现状**：`automind/cli/tui.py`（3943 行）存在大量占位，REPL 模式 (`_run_repl`) 仅有基础能力：
- 未使用 Rich 等 TUI 库增强体验
- 无彩色输出、无进度指示
- 无历史命令补全
- 无上下文切换能力

**建议**：引入 Rich 库增强 REPL。

```python
# automind/cli/tui.py — 升级示例
from rich.console import Console
from rich.panel import Panel
from rich.live import Live
from rich.markdown import Markdown
from rich.prompt import Prompt

console = Console()

async def _run_repl_v2(agent):
    console.print(Panel.fit(
        "[bold cyan]AutoMind REPL v2[/] — 输入任务或 /help 查看命令",
        border_style="blue"
    ))
    while True:
        cmd = Prompt.ask("[bold]>[/]")
        if cmd in ("exit", "/exit", "/quit"):
            break
        elif cmd == "/help":
            _show_help()
        elif cmd.startswith("/mode"):
            _switch_mode(cmd)
        else:
            with Live(console=console, refresh_per_second=10) as live:
                async for chunk in agent.run_stream(cmd):
                    live.update(Markdown(chunk))
```

**操作项**：
| 步骤 | 内容 |
|------|------|
| 1 | 将 `rich` 从 `full` 依赖提到核心 `dependencies` |
| 2 | 重写 `_run_repl` 使用 Rich Console + Live |
| 3 | 增加 `/mode`、`/clear`、`/export` 等 slash 命令 |
| 4 | 增加命令历史（`readline` 或 `prompt_toolkit`） |
| 5 | 增加 Token 统计退出摘要 |

---

### 14.4 代码生成器技能完善（P1）

**现状**：`automind/skills/builtin/code_generator.py`（2544 行）功能基础，仅支持简单模板替换。

**建议**：升级为多语言代码生成器：

| 能力 | 说明 |
|------|------|
| **语言检测** | 根据项目已有文件自动推断语言（如检测到 `pyproject.toml` → Python） |
| **项目脚手架** | 内置 FastAPI / Flask / Django / React / Vue 项目模板 |
| **代码片段生成** | 支持根据注释生成函数体、根据类型签名生成实现 |
| **生成后验证** | 自动运行语法检查（`flake8` / `eslint` 等）并反馈 |
| **增量生成** | 已有文件时只追加/修改，不覆盖用户已有内容 |

**操作项**：
| 步骤 | 内容 |
|------|------|
| 1 | 定义 `CodeGenInput` 模型（language、style、existing_files、constraints） |
| 2 | 实现项目脚手架模板（Python/FastAPI 优先） |
| 3 | 实现语法验证集成（try/except 捕获编译错误） |
| 4 | 注册到 SkillRegistry 作为内置技能 |

---

### 14.5 英文文档（P1）

**现状**：README、AUTOMIND_REFACTOR_PLAN.md、所有代码注释均为中文，非中文开发者无法参与。

**建议**：
| 文件 | 内容 | 优先级 |
|------|------|:---:|
| `README.en.md` | 英文版 README（功能描述 + 快速开始 + API 说明） | 高 |
| 代码 docstring | 英文 docstring（Python 社区标准） | 中 |
| AUTOMIND_REFACTOR_PLAN.en.md | 英文版重构计划（可选） | 低 |

**原则**：代码内注释保持中文（现有团队习惯），公开文档（README、docstring）提供英文版本。

---

### 14.6 发布到 PyPI（P1）

**现状**：`automind.egg-info/` 存在但未发布到 PyPI，只能源码安装。

**建议**：完善打包配置并发布。

**前提条件**（发布前必须完成）：
1. 版本号统一（§14.1）
2. CI/CD 通过（§14.2）
3. 核心测试覆盖率 ≥ 60%（§2.3 推进中）
4. 确定 `web` / `full` / `dev` 依赖分组合理

**操作项**：
| 步骤 | 内容 |
|------|------|
| 1 | 确认 `pyproject.toml` 中 `[project.urls]` 填写正确 |
| 2 | 增加 `long_description = "file: README.md"` 和 `long_description_content_type = "text/markdown"` |
| 3 | 配置 PyPI 信任发布（`trusted-publishing`） |
| 4 | 在 CI 中增加 `publish.yml`（tag 触发） |
| 5 | `pip install automind` 测试安装 |

---

### 14.7 插件系统（P1 — 补充 §3.5）

**现状**：§3.5 定义的 `AgentHooks` 提供了生命周期钩子，但还未形成完整的**第三方插件体系**。

**建议**：在 Hook 之上增加插件发现与加载机制。

```python
# automind/core/plugin.py （新增）

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from automind.core.hooks import AgentHooks


@dataclass
class PluginMeta:
    """插件元信息。"""
    name: str
    version: str
    description: str
    author: str = ""
    entry_point: str = ""  # 例如 "my_plugin.hooks:MyHooks"


class PluginManager:
    """插件管理器 — 扫描、加载、卸载插件。"""

    def __init__(self, plugin_dirs: list[Path] | None = None):
        self.plugin_dirs = plugin_dirs or [Path("~/.automind/plugins").expanduser()]
        self._loaded: dict[str, AgentHooks] = {}

    def discover(self) -> list[PluginMeta]:
        """扫描插件目录，发现所有可用插件。"""
        ...

    def load(self, name: str) -> AgentHooks | None:
        """加载插件并返回其 hooks 实例。"""
        ...

    def unload(self, name: str) -> None:
        """卸载插件。"""
        ...

    def assemble_hooks(self) -> AgentHooks:
        """合并所有已加载插件的 hooks。"""
        ...
```

**插件目录结构**：

```
~/.automind/plugins/
├── plugin-a/
│   ├── plugin.json         # { "name": "plugin-a", "version": "0.1.0", ... }
│   └── hooks.py            # 实现 AgentHooks 子类
└── plugin-b/
    ├── plugin.json
    └── hooks.py
```

**Web 层支持**：
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/plugins` | GET | 列出已发现/已加载插件 |
| `/api/plugins/{name}/load` | POST | 加载插件 |
| `/api/plugins/{name}/unload` | POST | 卸载插件 |

---

### 14.8 更多内置技能（P2）

**现状**：只有 3 个内置技能（`code_generator`、`project_init`、`test_runner`）+ 23 个桌面 SKILL.md 导入技能。

**建议**：增加高频场景内置技能。

| 技能 | 说明 | 依赖 |
|------|------|------|
| **Docker 管理** | 构建镜像、启动容器、查看日志 | `docker` CLI |
| **数据库查询** | 连接 PostgreSQL/MySQL/SQLite 并执行 SQL | `psycopg2` / `mysql-connector` |
| **API 测试** | 发送 HTTP 请求、验证响应、生成测试报告 | `httpx` |
| **Git 工作流** | PR 创建、分支合并、冲突解决辅助 | `git` CLI |
| **日志分析** | 解析日志文件、提取模式、生成摘要 | 无 |
| **依赖审计** | 检查依赖版本、安全漏洞扫描 | `pip-audit` / `npm audit` |
| **文档生成** | 从代码注释生成 Markdown/HTML 文档 | 无 |
| **性能分析** | 运行 profiler、分析热点、提供优化建议 | `cProfile` |

**实现原则**：每个技能 ≤ 300 行，遵循 `AbstractSkill` 接口，可选依赖延迟导入。

---

### 14.9 教程与示例目录（P2）

**现状**：除 `demo/e2e_demo.py`（依赖内部模块，不适合新手）外无任何教程或示例。

**建议**：创建 `examples/` 目录，按场景组织独立可运行的示例。

```
examples/
├── 01-quick-start/
│   ├── README.md            # pip install → 启动 Web → 第一个任务
│   └── start.sh
├── 02-custom-model/
│   ├── README.md            # 配置 DeepSeek / Ollama 本地模型
│   └── config.yaml
├── 03-mcp-server/
│   ├── README.md            # 连接一个 MCP 服务器
│   ├── mcp-server.py
│   └── automind.yaml
├── 04-multi-agent/
│   ├── README.md            # 多智能体协同：研究报告生成
│   └── task.txt
├── 05-loop-engineering/
│   ├── README.md            # 循环模式：持续优化代码
│   └── task.txt
└── 06-skill-development/
    ├── README.md            # 如何编写一个 Python 技能
    └── my_skill.py
```

**每个示例包含**：
- 清晰的 README（含预期输出截图）
- 可直接运行的命令（`python -m automind.server ...`）
- 预期结果说明

---

### 14.10 Web UI 前端工程化（P2）

**现状**：`automind/static/index.html` 为 137KB 单文件 SPA（纯 HTML+CSS+JS），功能完善但维护困难。

**建议**：渐进式前端工程化——**不要求一步到位 React/Vue**，分阶段演进。

| 阶段 | 方案 | 收益 |
|:---:|------|------|
| 1 | 拆分单文件：`index.html` ← `styles.css` ← `app.js`，保持无构建步骤 | 易维护，零工具链 |
| 2 | 使用 ES Module 拆分 JS 模块（`chat.js`、`stats.js`、`settings.js`、`sidebar.js`） | 代码组织清晰 |
| 3 | 引入轻量组件库（如 Preact + HTM，零构建） | 组件化，仍是单 HTML |
| 4 | 正式前端工程化（Vite + React/Vue，构建产出自带版本 hash） | 完全工程化 |

**阶段 1 拆分示例**：

```
automind/static/
├── index.html              # 仅 HTML 骨架（~2KB）
├── css/
│   ├── variables.css        # CSS 变量与主题
│   ├── layout.css           # 布局（侧边栏/聊天区/右面板）
│   ├── components.css       # 组件（气泡/按钮/输入框/弹窗/卡片）
│   └── animations.css       # 动效与过渡
└── js/
    ├── app.js               # 入口 + 路由
    ├── chat.js              # 聊天核心（发送/接收/渲染/流式）
    ├── sidepanel.js         # 右侧面板管理
    ├── settings.js          # 设置弹窗
    ├── stats.js             # 统计仪表盘
    ├── security.js          # 安全审计
    ├── websocket.js         # WebSocket 管理
    └── utils.js             # 工具函数（格式化/日期/复制）
```

**操作项**：
| 步骤 | 内容 | 验证 |
|------|------|------|
| 1 | 创建 `css/` 和 `js/` 目录 | 目录存在 |
| 2 | 按模块拆分 CSS（首版保持所有样式不变） | 页面渲染与拆分前完全一致 |
| 3 | 按模块拆分 JS（首版仅拆分，不重构逻辑） | 所有交互功能零回归 |
| 4 | 更新加载路径，删除原单文件 | 全面冒烟测试 |
| 5 | 后续：阶段 2-4 按需推进 | |

---

### 14.11 安全性深度审计（P1 — 补充现有安全体系）

**现状**：已有完善的权限系统（三级审批 + 审计日志 + 危险命令检测），但在以下方面仍可加强：

**建议**：

| 安全增强 | 说明 | 实现方式 |
|----------|------|---------|
| **文件路径穿越防护** | `file_read`/`file_write` 路径参数必须解析到 `project_root` 内 | `os.path.realpath(path).startswith(project_root)` |
| **沙箱逃逸加固** | 当前 `PythonSandboxTool` 的 `SAFE_BUILTINS` 覆盖不全（缺 `compile`/`exec` 自身检查） | 增加 `__builtins__` 白名单 + 禁用 `__import__` |
| **输出过滤** | 工具返回结果中可能含 API Key / 密码等敏感信息 | 正则匹配常见密钥模式（`sk-...`、`AKIA...`）并自动打码 |
| **请求速率限制** | 防止滥用导致 Token 快速消耗 | `slowapi` 或自定义中间件限制 `/api/run` 每分钟调用次数 |
| **WebSocket 源验证** | 防止跨域 WS 劫持 | `Origin` header 校验 + 强制 WSS 在生产环境 |

**操作项**：
| 步骤 | 内容 | 优先级 |
|------|------|:---:|
| 1 | 文件工具增加路径解析防护 | 高 |
| 2 | Python 沙箱白名单加固 | 高 |
| 3 | 工具输出敏感信息过滤 | 中 |
| 4 | API 速率限制中间件 | 中 |
| 5 | WebSocket 源验证 | 中 |

---

### 14.12 缺陷追踪补充

| 编号 | 缺陷/建议 | 对应方案 | 优先级 |
|------|----------|---------|:---:|
| E-01 | 版本号不统一（4 处不一致） | §14.1 | P0 |
| E-02 | 无 CI/CD 配置 | §14.2 | P1 |
| E-03 | TUI/REPL 交互体验差 | §14.3 | P1 |
| E-04 | code_generator 技能过于基础 | §14.4 | P1 |
| E-05 | 无英文文档 | §14.5 | P1 |
| E-06 | 未发布到 PyPI | §14.6 | P1 |
| E-07 | 缺少完整插件系统（§3.5 需补充） | §14.7 | P1 |
| E-08 | 内置技能过少 | §14.8 | P2 |
| E-09 | 无教程/示例目录 | §14.9 | P2 |
| E-10 | Web UI 单文件 137KB 难以维护 | §14.10 | P2 |
| E-11 | 安全体系缺口（路径穿越/沙箱逃逸/速率限制） | §14.11 | P1 |

---

### 14.13 路线图补充

将上述工作整合进现有执行路线图：

```
现有第 1-6 周（不变）
 ...

第 7 周：工程化地基
├── 版本号统一（§14.1）
├── CI/CD 配置（§14.2）
├── 文件路径穿越防护 + 沙箱加固（§14.11）
└── 代码生成器技能升级（§14.4）

第 8 周：文档与发布
├── 英文 README（§14.5）
├── 教程/示例目录（§14.9）
├── PyPI 发布（§14.6）
└── 插件系统实现（§14.7）

第 9 周（按需）：体验提升
├── TUI 升级（§14.3）
├── 更多内置技能（§14.8）
└── Web UI 前端工程化阶段 1（§14.10）
```

---

> **文档维护**: 本方案随重构进度持续更新。每个阶段完成后勾选已完成项并记录实际耗时与偏差原因。
>
> **Phase 1**（第 1-4 周）保证单用户体验不变且工程基础稳固；**Phase 2**（第 5-6 周）增加多用户并发能力；**Phase 3**（按需启动）可根据 §8-§14 优先级选择性实施功能、界面、统计、工程化优化。
