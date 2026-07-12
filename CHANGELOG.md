# 更新日志 / Changelog

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)。日期为发布日期。

## [0.9.0] - 2026-07-12

**Web IDE · 团队协作 · 专家系统三大能力落地**

- **📄 代码编辑器（Web IDE，社区版）**：右栏新增「📄 代码」标签 —— 项目文件树
  （排除依赖/缓存目录）+ **Monaco 编辑器**（jsDelivr 按需加载，CSP 定向放行，
  离线自动回退基础编辑器）+ **改动 Diff 预览**（前像 vs 当前，Monaco DiffEditor）；
  `Ctrl+S` 保存，前像记入改动日志可撤销；读写严格限定项目根（防穿越）；
  代码标签激活时右栏自动加宽，不影响聊天区（`/api/files/tree|read|write`、
  `/api/changes/diff`）。
- **🎓 专家系统与专家市场**：专家 = 可复用角色设定，激活后所有任务注入执行
  （`automind/core/experts.py`）。社区版：官方精选 10 个专家一键安装 +
  自建最多 3 个；专业版（experts_pro）：无限创建、团队分享、JSON 导入/导出、
  使用统计；企业版（expert_approval）：分享需管理员审批后全员可见
  （`/api/experts` 系列 + `/api/experts/pending|approve`）。
- **👥 团队协作（社区版）**：任务分配看板（待办/进行中/已完成，
  `/api/team/tasks`）；任务完成实时广播 `team_activity` —— 同事的 Agent
  改了文件会收到弹窗提醒与活动流记录；工作区/自定义模板/专家均为服务器级
  存储，同一部署天然团队共享。
- 前端新增 editor.js / experts.js / team.js 三个模块；侧边栏新增
  🎓 专家市场 与 👥 团队 视图；模式提示条显示激活专家徽标。

## [0.8.0] - 2026-07-09

**商业功能扩容（社区核心仅新增特性注册表与两个稳定契约键，功能不减）**

- 专业版 ⭐ **自定义模板**：把常用任务保存为模板（名称/图标/模式/提示词，
  上限 100 个），模板库弹窗管理并一键填入；持久化于
  `.automind/custom_templates.json`（`/api/templates/custom`）。
- 专业版 📄 **审计报告导出（PDF）**：安全审计日志一键导出正式报告——装有
  reportlab 时输出真 PDF（内置中文 CID 字体），否则输出可打印保存为 PDF 的
  自包含 HTML（`/api/audit/export`）。
- 企业版 🔐 **SSO / LDAP 单点登录**：LDAP bind（ldap3）或内置用户目录校验，
  签发短期会话令牌并经新扩展点 `register_token_validator` 接入既有鉴权中间件，
  HTTP 与 WebSocket 通用（`/api/auth/login|session|logout`）。
- 企业版 🧩 **细粒度权限（RBAC）**：角色 × 动作矩阵（`tool:*`/`api:*` 通配），
  内置 admin/developer/viewer 三角色，可自定义角色（`/api/rbac`）。
- 企业版 🚪 **私有模型网关**：全部提供商 api_base 一键收敛到企业网关
  （原地址自动备份、停用即恢复），模型白名单校验（`/api/gateway`）。
- 社区核心：`edition.py` 特性注册表 +5；server_ctx 追加 `register_token_validator`
  与 `rebuild_agent` 两个契约键（向后兼容）；社区版访问新端点返回 403 升级提示。
- **🔌 Agent 集成（社区版）**：OpenAI 兼容 API —— `POST /v1/chat/completions`
  （SSE 流式 + 多模态文本分片拍平 + 用量统计）与 `GET /v1/models`；
  「⚙ 设置 → 🔌 集成」标签页一键生成 **Continue.dev**（VS Code/JetBrains
  侧边面板）即贴即用配置，任何 OpenAI 客户端（Cline/Zed/脚本）均可接入；
  `/v1/*` 与 `/api/*` 同等鉴权（静态令牌 / SSO 会话令牌）并纳入限流。

## [0.7.1] - 2026-07-09

- **文档**：使用手册（md + 内置 HTML 手册）新增三种安装方式说明 —— PyPI
  （`pip install "automind-agent[web]"`）、GitHub 源码克隆、本地源码目录，
  含升级命令与国内镜像提速提示。
- **编程准确度**：TDD 内环语法自检扩展到 **YAML / TOML**（覆盖 Python/JSON/
  YAML/TOML 四类结构化文件）；`file_read` 支持 **offset/limit 按行分段读取**，
  超大文件自动截断并提示分段读（防止上下文被撑爆导致后续编辑失准）；
  `file_edit` 拒绝 old_string 与 new_string 相同的无效编辑并给出明确提示。
- **对话展示美化**：气泡内 Markdown 完整渲染 —— 标题（#~####）、有序/无序
  列表、**表格**（圆角边框 + 悬停高亮 + 横向滚动）、引用块（侧边条）、
  分隔线、删除线、斜体；深浅两种主题均适配；XSS 防线不变（内容先转义
  再做结构化包装，已验证 `<script>` 注入仅按纯文本显示）。

## [0.7.0] - 2026-07-07

**体验与安全感升级（后端功能不变，前端架构模块化增强）**

- 🌓 **浅色模式**：右上角一键切换深/浅主题，随系统偏好自动初始化，持久化记忆。
- 💰 **实时 Token 成本显示**：Token 面板与每条结果显示预估花费（内置主流模型
  单价表，点击成本数字可自定义单价）。
- 🗂 **工作区管理**：每个工作区 = 独立目录 + 独立上下文；切换后 Agent 在新目录
  下重建（记忆/索引/权限根随之切换），各工作区会话互不可见、任务互不污染
  （新增 `/api/workspaces` 系列端点）。
- 📜 **任务历史可回溯**：历史持久化到 `.automind/task_history.json`（关浏览器/
  重启服务不丢）；支持查看完整产出、一键复制、一键重跑。
- ▶ **任务中断继续**：任务中断/出错后提供「继续此任务」按钮，带着"不重做已完成
  部分"的指令续跑，避免 Token 浪费在重做上。
- ↩️ **撤销/回滚**：全局文件改动日志记录 Agent 每次写入/编辑的前像，右栏
  「文件改动」面板可单文件撤销或全部回滚（新建文件删除、修改文件恢复原内容，
  新增 `/api/changes` 端点）。
- 🧭 **新手引导**：首次打开自动弹出 4 步引导（配 Key → 选模式 → 用模板 → 安全
  机制），❓ 按钮可随时重看。
- 📚 **模板库**：社区版内置 10 个基础模板（个人主页/项目脚手架/修 Bug/补测试/
  数据分析/爬虫/周报/翻译…），欢迎页快速开始 + 📚 弹窗一键填入。
- 前端架构：新增 theme.js / workspace.js / templates.js 三个特性模块，
  静态资源测试同步更新。
- **打包修复**：wheel 此前完全不含 Web 界面静态资源（0.4.0 起即存在，
  pip 安装后界面退化为兜底页）——补充 `[tool.setuptools.package-data]`，
  现随包分发全部 16 个静态文件。

## [0.6.0] - 2026-07-06

**版本体系（开源核心模式）— 社区版 / 商业版分离**

- 新增 `automind/core/edition.py`：社区/专业/企业三版本特性门控与**稳定扩展协议 v1**；
  社区核心零商业代码，商业能力由独立分发的 `automind-pro` 包运行时注入。
- 迁入商业包：🤝 协同模式（多智能体）、🔁 循环模式（Loop Engineering）、
  ⏰ 定时任务、📊 高级统计仪表盘（专业版）；👥 会话级 Agent 池（企业版）。
- 社区版访问商业端点返回 `403 + 升级提示`；界面显示版本徽标、商业入口显示 🔒；
  `/api/health`、`/api/status` 返回 `edition` 与 `features`。
- 离线许可证（`AUTOMIND_LICENSE` / `.automind_license`，HMAC 签名），
  无效或过期自动降级社区版，服务照常启动。
- 安全能力（鉴权/限流/密钥脱敏/审计）**保留在社区版**。
- 共享角色提示词迁至 `automind/core/prompts.py`。
- 打包：社区包显式 `include = ["automind", "automind.*"]`，构建脚本
  `scripts/build_community.py` 自动审计产物中不含商业代码。
- 修复：子进程输出显式 UTF-8 解码（中文 Windows 默认 GBK 导致环境探测/
  测试技能崩溃）；新增 `/manual` 内置手册路由与界面 📖 入口。
- **编程准确度增强**：`file_edit` 精确匹配失败时回显文件中最接近的片段
  （带行号），模型可按原文立即纠正缩进/空白差异；ReAct TDD 内环语法自检
  升级——使用工具解析后的绝对路径、覆盖 `file_multi_edit` 的每个文件、
  新增 JSON 校验。
- 代码质量基线：`ruff check .` 全仓通过（修复 80+ 项，存量风格豁免见
  `pyproject.toml [tool.ruff.lint]`），CI 强制 lint + 双 Python 版本测试。

## [0.5.0]

- **执行态多用户隔离（Session-Agent-Pool）**：多会话并发执行互不干扰
  （0.6.0 起归入企业版）。
- **长对话性能**：消息列表虚拟滚动（`content-visibility`）、流式输出节流增量渲染。
- **可观测性**：向量库异常转为结构化日志，不再静默吞没。

## [0.4.0]

- 修复多处前端 XSS、WebSocket 断线指数退避重连、消息渲染内存泄漏、
  任务出错/取消时流式气泡清理、localStorage 容量上限。
- 后端安全响应头（CSP 等）、真实语义嵌入、CLI `--restore` 检查点恢复、
  生命周期迁移到 FastAPI lifespan。
