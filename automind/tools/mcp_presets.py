"""内置 MCP 官方适配预设（§v1.6.0）。

把社区最常用的外部服务做成「一键添加」预设：前端 MCP 面板列出这些卡片，
用户点一下即写入 ``mcp_servers`` 配置并连接，无需手抄 npx 命令。

设计约定：
- ``env_hints`` 是**需要用户自行设置的环境变量提示**（npx 启动的子进程会继承
  AutoMind 进程环境，密钥不放 UI、不进配置，只做提醒）；
- sse 类（飞书 / 钉钉）需要用户填自己的开放平台 MCP 端点，``url`` 留空表示
  「添加时需补填」；
- 这些预设只做「配置 + 连接」，不内置任何密钥。
"""

from __future__ import annotations

#: 每个预设：id / name / emoji / category / description / transport /
#: command / args / url / env_hints([{key, note}]) / docs
MCP_PRESETS: list[dict] = [
    {
        "id": "github",
        "name": "GitHub",
        "emoji": "🐙",
        "category": "代码托管",
        "description": "Issues / PR / 仓库 / Actions 的查询与操作",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "url": "",
        "env_hints": [{"key": "GITHUB_PERSONAL_ACCESS_TOKEN",
                       "note": "GitHub Personal Access Token"}],
        "docs": "https://github.com/github/github-mcp-server",
    },
    {
        "id": "postgres",
        "name": "PostgreSQL",
        "emoji": "🐘",
        "category": "数据库",
        "description": "查询 / 管理 PostgreSQL（补齐内置 db_query 仅 SQLite 的缺口）",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres"],
        "url": "",
        "env_hints": [{"key": "DATABASE_URL",
                       "note": "postgresql://user:pass@host:port/db"}],
        "docs": "https://github.com/modelcontextprotocol/servers",
    },
    {
        "id": "mysql",
        "name": "MySQL",
        "emoji": "🐬",
        "category": "数据库",
        "description": "查询 / 管理 MySQL 数据库",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-mysql"],
        "url": "",
        "env_hints": [
            {"key": "MYSQL_HOST", "note": "主机"},
            {"key": "MYSQL_PORT", "note": "端口（默认 3306）"},
            {"key": "MYSQL_USER", "note": "用户名"},
            {"key": "MYSQL_PASS", "note": "密码"},
            {"key": "MYSQL_DB", "note": "数据库名"},
        ],
        "docs": "https://github.com/modelcontextprotocol/servers",
    },
    {
        "id": "slack",
        "name": "Slack",
        "emoji": "💬",
        "category": "即时通讯",
        "description": "读取频道 / 发送消息 / 管理线程",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-slack"],
        "url": "",
        "env_hints": [
            {"key": "SLACK_BOT_TOKEN", "note": "Slack Bot User OAuth Token"},
            {"key": "SLACK_TEAM_ID", "note": "工作区 Team ID"},
        ],
        "docs": "https://github.com/modelcontextprotocol/servers",
    },
    {
        "id": "feishu",
        "name": "飞书（Lark）",
        "emoji": "🕊️",
        "category": "即时通讯",
        "description": "飞书消息 / 文档 / 表格（需在开放平台创建应用并填写 MCP 端点）",
        "transport": "sse",
        "command": "",
        "args": [],
        "url": "",
        "env_hints": [],
        "docs": "https://open.feishu.cn/",
    },
    {
        "id": "dingtalk",
        "name": "钉钉",
        "emoji": "📌",
        "category": "即时通讯",
        "description": "钉钉消息 / 审批 / 通讯录（需在开放平台创建应用并填写 MCP 端点）",
        "transport": "sse",
        "command": "",
        "args": [],
        "url": "",
        "env_hints": [],
        "docs": "https://open.dingtalk.com/",
    },
    {
        "id": "playwright",
        "name": "Playwright 浏览器",
        "emoji": "🎭",
        "category": "浏览器",
        "description": "复杂网页交互 / 登录态 / 自动化测试（比内置 browser 工具更强）",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest"],
        "url": "",
        "env_hints": [],
        "docs": "https://github.com/microsoft/playwright-mcp",
    },
]
