// 内置工具/技能的中文展示层。
//
// 工具的 `description` 是**写给模型看的提示词**，必须保持英文且措辞精确 ——
// 那是它选对工具的依据，不能为了界面好看去改。但整个界面是中文的，工具面板
// 里却排着 31 段英文说明，读起来非常割裂，中文用户基本不会去逐条读。
//
// 所以这里只做展示层：给内置能力配中文名与一句话说明，界面优先显示中文，
// 查不到的（MCP / 插件注册进来的工具）自动回落到原始英文 description。
// 后端与模型侧一个字都不用改。

export interface ToolMeta {
  /** 中文名 */
  cn: string;
  /** 一句话说明：它能干什么、什么时候用得上 */
  desc: string;
}

export const TOOL_META: Record<string, ToolMeta> = {
  // ── 基础执行 ──
  terminal: { cn: '终端命令', desc: '执行 shell 命令并返回退出码与输出，用于跑 CLI、脚本与包管理器。' },
  python_sandbox: { cn: 'Python 沙箱', desc: '在隔离子进程中运行纯计算的 Python 代码，禁用文件、网络与进程接口。' },
  code_generate: { cn: '代码生成', desc: '按需求生成或补全代码并写入文件，自带语法校验与自动修复。' },

  // ── 文件 ──
  file_read: { cn: '读取文件', desc: '读取文件内容；大文件可用 offset/limit 按行分段读取。' },
  file_write: { cn: '写入文件', desc: '写入文件，不存在则创建、已存在则覆盖。' },
  file_edit: { cn: '编辑文件', desc: '把文件中的指定片段替换为新内容，要求原文逐字符匹配。' },
  file_search: { cn: '搜索文件', desc: '按文件名或内容在项目里查找，支持正则与通配符。' },
  archive: { cn: '压缩包', desc: '创建与解开 zip / tar 压缩包，查看包内文件列表。' },

  // ── 网络 ──
  browser: { cn: '浏览器', desc: '用 Playwright 操作真实浏览器：导航、点击、输入、截图与取文本。' },
  web_fetch: { cn: '网页抓取', desc: '抓取网页并提取正文与链接，适合读文章 / 文档 / API 返回。' },
  web_search: { cn: '联网搜索', desc: '搜索互联网并返回结果摘要与链接。' },
  http_request: { cn: 'HTTP 请求', desc: '发起任意 HTTP 请求，用于调用 REST API。' },

  // ── 办公文档 ──
  excel_tool: { cn: 'Excel 表格', desc: '读写 .xlsx：取值、写入、公式、格式与多工作表。' },
  word_tool: { cn: 'Word 文档', desc: '读写 .docx：段落、标题、表格与样式。' },
  pdf_tool: { cn: 'PDF 文档', desc: '解析 PDF 文本与表格，合并、拆分与提取页面。' },
  ppt_tool: { cn: 'PPT 演示', desc: '生成、读取与追加 PowerPoint 幻灯片。' },
  csv_tool: { cn: 'CSV 表格', desc: '读写 CSV，与 JSON 互转，多文件合并；纯标准库实现。' },
  email_tool: { cn: '邮件', desc: '通过 SMTP/IMAP 发送与读取邮件。' },
  calendar: { cn: '日历', desc: '读写 ICS 日历事件；Windows 上可对接 Outlook。' },
  db_query: { cn: '数据库查询', desc: '对 SQLite 执行 SQL 查询（PostgreSQL / MySQL 可用 MCP 接入）。' },

  // ── 多媒体 ──
  screenshot_tool: { cn: '屏幕截图', desc: '截取整屏或指定区域，可直接返回图片给模型识图。' },
  ocr_tool: { cn: 'OCR 文字识别', desc: '从图片或屏幕截图中识别文字，支持中英文。' },
  image_tool: { cn: '图像处理', desc: '缩放、裁剪、格式转换、加水印与读取图片信息。' },
  chart_tool: { cn: '图表绘制', desc: '生成折线图、柱状图、饼图与散点图并导出 PNG。' },
  audio_tool: { cn: '音频信息', desc: '读取音频文件的时长、比特率与标签等元信息。' },
  video_tool: { cn: '视频处理', desc: '读取视频元信息，按时间点抽取关键帧（需 ffmpeg）。' },

  // ── 系统 ──
  git_tool: { cn: 'Git 版本控制', desc: '查看状态与提交历史、暂存、提交、切分支与推拉代码。' },
  process_tool: { cn: '进程与端口', desc: '查看进程占用、端口监听情况，必要时结束指定进程。' },
  clipboard_tool: { cn: '剪贴板', desc: '读取与写入系统剪贴板文本。' },
  notify: { cn: '系统通知', desc: '发送桌面通知提醒任务进展。' },
  im_integration: { cn: '即时通讯推送', desc: '向企业微信 / 钉钉 / 飞书 / Slack 的机器人推送消息。' },
};

export const SKILL_META: Record<string, ToolMeta> = {
  project_init: { cn: '项目初始化', desc: '按技术栈生成项目骨架、配置与依赖清单。' },
  code_generator: { cn: '代码生成', desc: '从需求描述生成可运行代码，含语法校验与自修复。' },
  test_runner: { cn: '测试执行', desc: '自动发现并运行测试，汇总失败原因。' },
  log_analyzer: { cn: '日志分析', desc: '从日志里定位报错模式与异常时间段。' },
  doc_generator: { cn: '文档生成', desc: '根据代码与注释生成 README / API 文档。' },
  dep_audit: { cn: '依赖审计', desc: '检查依赖的版本、许可证与已知风险。' },
  excel_report: { cn: 'Excel 报表', desc: '把数据整理成带公式与图表的 Excel 报表。' },
  web_research: { cn: '网络调研', desc: '多轮检索与交叉验证，产出带来源的调研结论。' },
  data_insight: { cn: '数据洞察', desc: '对表格数据做统计分析并给出可视化与结论。' },
  doc_batch: { cn: '文档批处理', desc: '批量转换、抽取与汇总一整个目录的文档。' },
  article_writer: { cn: '文章撰写', desc: '按主题与风格产出结构完整的长文初稿。' },
  env_doctor: { cn: '环境体检', desc: '检查运行环境、依赖与配置，给出可执行的修复建议。' },
};

/** 中文名（查不到就用原名）。 */
export const toolLabel = (name: string, meta = TOOL_META): string => meta[name]?.cn || name;

/** 中文说明（查不到就回落到后端给的英文 description）。 */
export const toolDesc = (name: string, fallback = '', meta = TOOL_META): string =>
  meta[name]?.desc || fallback;
