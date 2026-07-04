# AutoMind — 通用自动化 Agent 框架

融合 Claude Code、OpenAI Codex 与 Reasonix 的核心能力，
支持 MCP 协议、Skill 技能系统、分层规划、符号推理与自我纠错。
内置 **Web 工作台**，开箱即用，可聊天、可工作、可编程。

## 五种交互模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| 💬 **对话** | 纯多轮对话，不调用工具，响应最快（支持图片输入/视觉模型） | 问答、咨询、头脑风暴 |
| ⚙️ **工作** | 分层规划 + 工具执行 + 符号验证 | 建项目、跑命令、改文件等自动化任务 |
| 💻 **编程** | ReAct 思考-行动循环，聚焦代码 | 阅读 / 编写 / 调试 / 重构 / 测试 |
| 🤝 **协同** | 多智能体分工协作并综合 | 复杂、跨角色的长任务 |
| 🔁 **循环** | Loop Engineering：自主"行动-观察-修正"闭环 | 需反复迭代直到达标的任务 |

## 工具审批模式（Approval）

顶部下拉可切换工具调用的审批策略（Reasonix 风格 `deny > ask > allow` 门控）：

- 🙋 **询问** — 除只读工具外，每次工具调用前弹窗请求人工批准。
- ⚡ **自动**（默认）— 自动批准普通/低风险工具，仅高危操作需确认。
- ✅ **全批准** — 跳过所有工具审批，完全自主运行（慎用）。

## 循环工程 / 定时任务 / 统计

- **循环模式**自带停止条件（任务完成 / 连续无进展 / 达到最大轮数）与人工中断，防止死循环。
- **⏰ 定时任务**：按固定间隔自动执行某个任务（任意模式），后台调度并记录结果。
- **📊 统计分析**：按模式聚合任务数、成功率、Token（输入/输出）、平均耗时与工具使用排行。

## 快速开始

```bash
# 安装（含 Web UI 依赖）
pip install -e ".[web]"        # 仅 Web + OpenAI 兼容
# 或安装全部后端
pip install -e ".[full]"

# 启动 Web 工作台（推荐）
python -m automind.server --port 8765
# 然后浏览器打开 http://localhost:8765

# Windows 一键启动
launch.bat

# CLI 交互模式
automind
automind "你的任务描述"
```

## 模型配置

打开 Web 工作台后，点击右上角 **🔑 API Keys**：

- 支持 OpenAI / Claude / DeepSeek / Kimi / 百炼 / 智谱 / 豆包 / Gemini / Grok / Ollama。
- **自定义 OpenAI 标准接口（中转代理）**：在底部填写 `api_base`（如 `https://api.your-proxy.com/v1`）、
  默认模型与 API Key，即可对接任意兼容 OpenAI `/v1/chat/completions` 的服务或中转站。
- 每个提供商均可自定义具体使用的模型名（模型名输入框可直接输入任意模型）。
- API Key 仅保存在本地 `.automind_config.json`，不会上传。
  也可通过环境变量配置（如 `OPENAI_API_KEY`、`DEEPSEEK_API_KEY`、`MOONSHOT_API_KEY` 等）。

## 项目目录 / 工具 / 技能 / MCP

- **本地项目目录**：右上角 **📁** 徽标或「设置 → 通用 → 浏览」可选择本地目录作为 Agent 工作根目录。
- **自定义模型**：模型配置中输入模型名后点「➕ 添加」即可持久化到下拉列表（按提供商区分，可删除）。
- **工具面板**（侧边栏 🔧）分三个标签：
  - **工具** — 内置工具列表（含权限等级与风险分）。
  - **技能** — 内置技能 + 「加载本地技能目录」（扫描含 `AbstractSkill` 子类的 `.py`）。
  - **MCP** — 添加并连接 MCP 服务器（stdio / sse），自动发现其工具（需 `pip install mcp`）。

## 对话历史

对话模式的多轮记录持久化到 `.automind/chat_history.json`，刷新页面自动恢复；
点击 **🔄 新会话** 可删除当前对话记录。任务历史（侧边栏 📜）支持单条删除与全部清空。

## 流式输出 · 任务中断

- **流式对话**：对话模式通过 WebSocket 逐字流式返回，带打字光标实时显示。
- **任务中断**：执行中点击 ■ 可真正取消后台任务（`asyncio.Task` 取消），对话/工作/编程均支持。
- WebSocket 不可用时自动回退到 REST 同步模式。

## 多模态 · 语音 · 预览 · Token 统计

- **🪙 Token 用量统计**：右栏实时显示每次任务的输入/输出 token 与累计总量、任务数，可一键重置。
- **🔍 HTML 预览**：模型输出的 ```html 代码块带「预览页面」按钮，在安全沙箱 iframe 中渲染；
  右栏「HTML 预览」列出项目目录中的 `.html` 文件，点击即可预览（`/api/preview/file`，限定项目目录防穿越）。
- **🎤 语音输入**：点击麦克风按钮用 Web Speech API 语音转文字（Chrome/Edge）。
- **📎 多模态**：可附加图片发送给视觉模型；输出中的图片/链接/表格自动渲染展示。

## 安全审计

侧边栏 **🛡️ 安全审计** 展示每次工具调用的风险评分与授权决策（放行 / 需确认 / 高危），
危险命令（`rm -rf`、`git push --force` 等）会被识别为高危并要求确认。

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
- **健康检查**：`GET /api/health`（无需鉴权）返回版本、运行任务数、并发上限、uptime，供探活/负载均衡。
- **资源保护**：并发任务超过 `AUTOMIND_MAX_CONCURRENT` 时返回 429。

> 注：当前对话模式为完整多用户隔离；工作/编程/协同等执行态任务仍共享单 Agent（详见
> [AUTOMIND_REFACTOR_PLAN.md](AUTOMIND_REFACTOR_PLAN.md) §7，完整 Session-Agent-Pool 为后续阶段）。

## 演示

```bash
python demo/e2e_demo.py
```
