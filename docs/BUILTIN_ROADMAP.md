# AutoMind 内置能力规划清单（工具 / 技能 / MCP / 插件）

> 整理日期：2026-08-12 ｜ 基线版本：1.5.2
> 目的：汇总"开箱即用能力"的缺口分析与新增建议，供按优先级实施。

---

## 一、现状基线（已有能力，避免重复建设）

### 已有工具（20 个）
| 类别 | 工具 |
|------|------|
| 文件类 | `file_read` `file_write` `file_edit` `file_search` `archive` |
| 办公类 | `excel_tool` `word_tool` `pdf_tool` `calendar` |
| 网络类 | `http_request` `web_fetch` `web_search` `browser` |
| 执行类 | `terminal` `python_sandbox` `code_generate` `db_query` |
| 通信类 | `email_tool` `im_integration` `notify` |

### 已有技能（6 个，全部为软件工程向）
`project_init` `code_generator` `test_runner` `log_analyzer` `doc_generator` `dep_audit`

### MCP / 插件
- MCP：机制完备（支持 `stdio` / `sse` 传输，含 SSRF 检查、审批门控），但 `mcp_servers` 配置为空，无任何内置适配或推荐清单。
- 插件：PluginManager + 生命周期钩子（`before_run` / `on_error` / `after_run` 等）机制完整，但零内置插件、无示例模板。

---

## 二、建议新增 · 工具（本地原子能力）

> 原则：本地原子操作做工具；外部服务集成走 MCP，避免重复。

### 🥇 第一优先（高频闭环缺口）
| 工具 | 场景 | 实现成本 |
|------|------|----------|
| `screenshot_tool` | 全屏/区域/窗口截屏 —— "看屏幕报错"类任务刚需 | 低 |
| `ocr_tool` | 图片/截图文字提取，与 screenshot 组成"看→读"链路 | 低 |
| `ppt_tool` | PowerPoint 生成/编辑，补齐 word/excel/pdf 办公闭环 | 中（复用 zip/XML 技术栈） |
| `git_tool` | 结构化 git 操作（status/commit/push/branch），带安全门控 | 低（subprocess 封装） |

### 🥈 第二优先（增强已有能力）
| 工具 | 场景 | 说明 |
|------|------|------|
| `image_tool` | 缩放/裁剪/格式转换/水印 | 办公素材处理 |
| `csv_tool` | CSV/JSON 结构化读写合并 | 数据交换通用格式 |
| `clipboard_tool` | 读写剪贴板 | "帮我复制这段"刚需 |
| `chart_tool` | 数据 → 图表文件 | 接在 excel_tool 后形成"分析→出图" |

### 🥉 第三优先（扩展边界，视路线取舍）
| 工具 | 场景 | 注意 |
|------|------|------|
| `audio_tool` | 录音/语音转写 | 依赖语音模型，成本较高 |
| `video_tool` | 视频信息提取/截图 | 依赖 ffmpeg，体积大 |
| `process_tool` | 进程/服务/端口管理 | 与 terminal 重叠，仅结构化查询有价值 |
| ~~`sql_tool`~~ | ~~PostgreSQL/MySQL~~ | ❌ 不建议内置 —— 走 MCP 接入 |

---

## 三、建议新增 · 技能（场景化工作流）

> 原则：纯编排现有工具，无外部依赖，直接强化"装完能干什么"。

| 技能 | 场景 | 复用能力 |
|------|------|----------|
| `excel_report` | 数据汇总 → 周报/统计表 | excel_tool + code_generate |
| `doc_batch` | 批量 Word/PDF 转换、合并、重命名 | word_tool + pdf_tool |
| `web_research` | 多来源搜索 → 带引用的调研报告 | web_search + web_fetch |
| `data_insight` | CSV/Excel 分析 → 结论 + 图表代码 | excel_tool + python_sandbox |
| `article_writer` | 公众号/小红书风格写作（含审校循环） | 质量评估 + code_generator 模式 |
| `env_doctor` | 环境诊断（版本/依赖/端口冲突） | terminal + dep_audit |

---

## 四、建议新增 · MCP（外部服务接入）

> 原则：只接入本地工具替代不了的外部服务。落地分两档：A 档做官方内置适配，B 档做推荐清单 + `mcp add` 一键配置。

### A 档 · 官方适配（4-5 个）
| MCP | 理由（本地替代不了） |
|-----|---------------------|
| **GitHub** | issues/PR/CI 需 OAuth + API，terminal 无法安全操作 |
| **PostgreSQL / MySQL** | `db_query` 仅支持 SQLite |
| **Google Drive / OneDrive** | 云盘读写，本地无对应能力 |
| **飞书 / 钉钉 / Slack** | `im_integration` 仅单向 webhook，无法收消息/建文档 |
| **Playwright 浏览器** | 复杂交互/登录态场景，`browser` 工具不够 |

### B 档 · 推荐清单（8-10 个，配 `automind mcp add` 命令）
Notion、Todoist、Jira、Gmail、Figma、S3、Kubernetes、MongoDB、GitLab、PagerDuty

---

## 五、建议新增 · 插件（生命周期扩展）

> 原则：先出"看得见价值"的样板，激活生态。

| 插件 | 挂接钩子 | 价值 |
|------|----------|------|
| `cost_tracker` | after_run + usage 事件 | token/费用按任务入账 CSV（衔接 1.5.2 实时用量统计） |
| `pii_guard` | on_output | 输出自动脱敏（复用 `core/redact.py`） |
| `task_notify` | after_run | 任务完成推送 IM/邮箱（复用 im_integration + email_tool） |
| `hello_hooks` | 全钩子 | 示例插件（完整 manifest + 注释），供第三方抄模板 |

---

## 六、实施顺序建议

```
第一批（最快见效）：
  工具：screenshot_tool + ocr_tool（打通"看屏幕"闭环）
  技能：excel_report + web_research（办公 + 研究高频场景）

第二批（补齐闭环）：
  工具：ppt_tool + git_tool
  技能：data_insight + env_doctor

第三批（生态与连接）：
  MCP：docs/MCP_GUIDE.md 推荐清单 + `mcp add` 命令 + GitHub/Postgres/飞书 精选适配
  插件：cost_tracker + pii_guard + task_notify + hello_hooks 示例

第四批（扩展边界，按需）：
  工具：image_tool / csv_tool / clipboard_tool / chart_tool / audio / video
  技能：doc_batch / article_writer
```

> 核心逻辑：**工具**强化原子能力 → **技能**强化"开箱即用"（留存）→ **MCP** 扩展"连接能力"（深度）→ **插件**激活生态（长期）。社区版保持轻量，重能力全部走"一键安装"。
