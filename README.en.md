# AutoMind — General-Purpose Automation Agent Framework (Community Edition)

> [中文文档 / Chinese README](README.md)

AutoMind combines the core capabilities of Claude Code, OpenAI Codex, and Reasonix:
MCP protocol support, a Skill system, hierarchical planning, symbolic reasoning,
and self-correction — with a built-in **Web workbench** that works out of the box
for chatting, working, and coding.

## Editions: Community / Pro / Enterprise

This repository is the **Community Edition** (MIT, free forever). Commercial
capabilities ship as a separate closed-source `automind-pro` package that
activates at runtime once a license is configured — see
[docs/EDITIONS.md](docs/EDITIONS.md).

| Capability | Community | Pro | Enterprise |
|------------|:---:|:---:|:---:|
| Chat / Work / Coding modes, tools, skills, MCP, plugins | ✅ | ✅ | ✅ |
| Planning, symbolic reasoning, self-correction, memory | ✅ | ✅ | ✅ |
| Web workbench + CLI, approval gating, security audit | ✅ | ✅ | ✅ |
| Auth token / rate limiting / secret redaction | ✅ | ✅ | ✅ |
| Basic statistics | ✅ | ✅ | ✅ |
| 🤝 Multi-Agent mode | — | ✅ | ✅ |
| 🔁 Loop Engineering mode | — | ✅ | ✅ |
| ⏰ Scheduled tasks | — | ✅ | ✅ |
| 📊 Advanced statistics dashboard | — | ✅ | ✅ |
| ⭐ Custom task templates | — | ✅ | ✅ |
| 📄 Audit report export (PDF) | — | ✅ | ✅ |
| 👥 Session agent pool (multi-user execution isolation) | — | — | ✅ |
| 🔐 SSO / LDAP integration | — | — | ✅ |
| 🧩 Fine-grained permissions (RBAC) | — | — | ✅ |
| 🚪 Private model gateway (egress control, model allowlist) | — | — | ✅ |

> Security features are **never** paywalled — they all stay in Community.

## Five Interaction Modes

| Mode | Description | Best For |
|------|-------------|----------|
| 💬 **Chat** | Pure multi-turn conversation, no tools, fastest (supports image input / vision models) | Q&A, consulting, brainstorming |
| ⚙️ **Work** | Hierarchical planning + tool execution + symbolic verification | Scaffolding projects, running commands, editing files |
| 💻 **Coding** | ReAct think-act loop focused on code | Reading / writing / debugging / refactoring / testing |
| 🤝 **Multi-Agent** (Pro) | Multiple role agents collaborate and synthesize | Complex, cross-role long tasks |
| 🔁 **Loop** (Pro) | Loop Engineering: autonomous act-observe-correct cycle | Tasks that need iteration until a target is met |

## Tool Approval Modes

A dropdown at the top switches the approval policy for tool calls
(Reasonix-style `deny > ask > allow` gating):

- 🙋 **Ask** — every non-read-only tool call requires manual approval.
- ⚡ **Auto** (default) — low-risk tools auto-approved; only dangerous operations need confirmation.
- ✅ **Approve All** — skip all approval and run fully autonomously (use with care).

## Quick Start

```bash
# Option A: install from PyPI (recommended)
pip install "automind-agent[web]"     # Web + OpenAI-compatible backends
# Upgrade: pip install -U "automind-agent[web]"; use [full] for every backend

# Option B: install from source (after git clone, inside the repo)
pip install -e ".[web]"

# Start the Web workbench (recommended)
python -m automind.server --port 8765
# Then open http://localhost:8765

# Windows one-click launcher
launch.bat

# CLI interactive mode (Rich REPL)
automind
automind "your task description"
automind --version
```

### Docker

```bash
docker compose up --build
# Web UI at http://localhost:8765
```

## Model Configuration

Open the Web workbench and click **🔑 API Keys** (top right):

- Supports OpenAI / Claude / DeepSeek / Kimi (Moonshot) / Bailian (Qwen) / Zhipu (GLM) /
  Doubao / Gemini / Grok / Ollama.
- **Custom OpenAI-compatible endpoint (relay/proxy)**: fill in `api_base`
  (e.g. `https://api.your-proxy.com/v1`), a default model, and an API key to use any
  service compatible with the OpenAI `/v1/chat/completions` API.
- API keys are stored locally in `.automind_config.json` and never uploaded.
  Environment variables also work (`OPENAI_API_KEY`, `DEEPSEEK_API_KEY`,
  `MOONSHOT_API_KEY`, etc.).

## Production Hardening (opt-in, off by default)

| Env Variable | Effect |
|--------------|--------|
| `AUTOMIND_AUTH_TOKEN` | Require `Authorization: Bearer <token>` on all `/api/*` and `/ws` |
| `AUTOMIND_CORS_ORIGINS` | Restrict CORS origins (comma-separated) |
| `AUTOMIND_MAX_CONCURRENT` | Max concurrent tasks (default 8, returns 429 beyond) |
| `AUTOMIND_RATE_LIMIT` | Per-client per-minute limit for `/api/run` (0 = off) |
| `AUTOMIND_REDACT_SECRETS` | Redact API keys / tokens in task outputs |
| `AUTOMIND_ALLOWED_ORIGINS` | WebSocket `Origin` allowlist |

## Architecture

```
automind/
├── core/         # types, config, LLM backends, hooks, plugins, logging
├── agent.py      # AutoMindAgent — top-level orchestrator
├── planning/     # hierarchical planner, ReAct executor, dependency DAG
├── reflection/   # quality assessment, self-correction, retry/circuit breaker
├── memory/       # short/long-term memory (ChromaDB), knowledge graph
├── tools/        # terminal, file editing, sandbox, permissions, MCP
├── skills/       # skill system (built-ins + SKILL.md + entry points)
├── context/      # context window management, project indexing
├── core/edition.py  # edition gating + stable extension protocol (v1)
├── state/        # checkpoints, human-in-the-loop, resource budgets
├── server.py     # FastAPI Web layer (REST + WebSocket)
├── cli/          # CLI + Rich REPL
└── static/       # Web UI (HTML skeleton + css/ + js/ modules)
```

## Plugin System

Drop a plugin under `~/.automind/plugins/<name>/`:

```
~/.automind/plugins/my-plugin/
├── plugin.json     # {"name": "my-plugin", "version": "1.0.0", "description": "..."}
└── hooks.py        # def get_hooks() -> AgentHooks
```

`hooks.py` example:

```python
from automind.core.hooks import AgentHooks

def get_hooks():
    async def before(task):
        print(f"task starting: {task}")
    return AgentHooks(before_run=before)
```

Manage plugins from the Web UI (Tools panel → 🧩 Plugins) or via
`GET /api/plugins`, `POST /api/plugins/{name}/load|unload`.

## Built-in Skills

`project_init`, `code_generator` (generation / completion / scaffolding with syntax
validation and self-repair), `test_runner`, `log_analyzer`, `doc_generator`,
`dep_audit` — plus any `SKILL.md` skill packs or Python skills you import.

## Examples

See the [examples/](examples/) directory:

- `01-quick-start` — install, launch, first task
- `02-custom-model` — DeepSeek / Ollama / relay-proxy configuration
- `03-skill-development` — write your own Python skill
- `04-plugin-development` — write a lifecycle-hook plugin

## Development

```bash
pip install -e ".[dev]"
pytest tests/            # full test suite
ruff check .             # lint
```

## License

Community Edition (this repository's `automind/` package): MIT.
The `automind-pro` extension package is commercial, closed-source software
(see `pro/LICENSE-COMMERCIAL.md`, not included in open-source releases).

## Open Source

- Install from PyPI: `pip install automind-agent` (extras: `[web]`, `[full]`).
- Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md); release history in [CHANGELOG.md](CHANGELOG.md).
- AutoMind runs entirely locally: API keys and chat history stay on your machine; no telemetry.
- Please report security vulnerabilities privately via GitHub Security Advisories.
