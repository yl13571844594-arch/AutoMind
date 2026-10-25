<div align="center">

# AutoMind

**General-Purpose Automation Agent Framework · Community Edition**

Chat, get work done, write code — a local agent workbench that plans, calls tools, and corrects itself.

[![CI](https://github.com/yl13571844594-arch/AutoMind/actions/workflows/ci.yml/badge.svg)](https://github.com/yl13571844594-arch/AutoMind/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/automind-agent?logo=pypi&logoColor=white)](https://pypi.org/project/automind-agent/)
[![Python](https://img.shields.io/pypi/pyversions/automind-agent?logo=python&logoColor=white)](https://pypi.org/project/automind-agent/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Downloads](https://static.pepy.tech/badge/automind-agent)](https://pepy.tech/project/automind-agent)
[![Release](https://img.shields.io/github/v/release/yl13571844594-arch/AutoMind?logo=github)](https://github.com/yl13571844594-arch/AutoMind/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)](https://github.com/yl13571844594-arch/AutoMind/releases/latest)

[中文](README.md) · [User Manual (zh)](使用手册.md) · [Changelog](CHANGELOG.md) · [Download installers](https://github.com/yl13571844594-arch/AutoMind/releases/latest)

</div>

![Chat workbench](docs/images/chat.png)

AutoMind combines the core capabilities of Claude Code, OpenAI Codex, and Reasonix:
MCP protocol support, a Skill system, hierarchical planning, symbolic reasoning, and
self-correction. It ships a built-in **Web workbench** — since v1.0 a modern
React 18 + TypeScript + Ant Design app (sources in `web/`, prebuilt assets bundled so
it works out of the box) — for chatting, working, and coding, plus a
**📚 RAG knowledge base**: upload documents and answers cite them automatically.

> **Your data stays on your machine.** Model API keys live in a local config file,
> task history in a local SQLite database, knowledge-base vectors in a local directory.
> Nothing is sent anywhere except the model provider you configured yourself.

## ✨ What's new in v1.11.18

- 🛡 **Unattended evaluation approvals**: the eval executor used to configure approval mode `auto`, so sensitive
  `terminal` calls were rejected in an eval process with **no approval channel** and the model simply retried —
  the standard-tools `smoke_run_command` case timed out at **237.2s** and burned **436,698** tokens. The eval
  executor now defaults to **`approve_all`** unattended approval (`--approval-mode` can override it and the
  choice is recorded in `report.notes`), and the executor enforces the per-task `timeout_seconds` **hard limit**,
  so a run can no longer hang for 237s.
- 🪙 **Chinese writes complete in one shot**: after a successful `file_write` the tool now **reads the file back
  from disk** and returns `size_bytes` / `chars` / `encoding` / `verified` / `readback_matches` — the model no
  longer needs a follow-up `file_read` or terminal check (the main cause of the `smoke_chinese_content`
  token overrun: 180,170 / 139,053 tokens). Tool descriptions and the coding prompt explicitly support
  Chinese file names / content (UTF-8) and add a "verify once, then finish" discipline for simple tasks.
- 🧰 **Multi-task & collaboration**: `pwsh` gains a native background channel (`background=true` returns a
  `task_id` immediately; poll / output / list / kill via `terminal_background`); `subagent_list` accepts an
  `agent_id` to return one sub-agent's **full output** (with an explicit truncation marker); the new
  **`team_task`** tool (list / summary / create / update / claim / comment) plugs into the team task board's
  SQLite store and broadcasts changes to the Web board in real time — agents can dedupe, claim, update and
  comment alongside humans.
- 🚀 **v1.11.18 release**: core / Pro extension / web / Windows installer / manual are unified at **1.11.18**.
  This is a patch release with no commercial-extension or frontend feature changes;
  `tests/test_version_consistency.py` keeps all six version declarations identical.

## Screenshots

<table>
<tr>
<td width="50%"><img src="docs/images/tools.png" alt="Tools panel"><br>
<b>🔧 Tools panel</b> — 41 built-in tools (including team tasks, background pwsh and write-readback verification) plus skills, MCP servers and plugins</td>
<td width="50%"><img src="docs/images/workflow.png" alt="Workflows"><br>
<b>🧩 Workflows</b> — every step pinned in YAML: reviewable, diffable and versionable for reproducible long-running work</td>
</tr>
<tr>
<td width="50%"><img src="docs/images/team.png" alt="Team board"><br>
<b>👥 Team board</b> — live members, shared tasks, comments and status flow; agents and humans work on one board</td>
<td width="50%"><img src="docs/images/kb.png" alt="Knowledge base"><br>
<b>📚 RAG knowledge base</b> — drop documents in, chat answers retrieve and cite them</td>
</tr>
</table>

## Editions: Community / Pro / Enterprise

This repository is the **Community Edition** (MIT, free). Commercial capabilities ship
as a separate closed-source `automind-pro` package that activates at runtime once a
license is configured — see [docs/EDITIONS.md](docs/EDITIONS.md).

| Capability | Community (free, open source) | Pro | Enterprise |
|------------|:---:|:---:|:---:|
| 💬 Chat / ⚙️ Work / 💻 Coding modes | ✅ | ✅ | ✅ |
| Tools / skills / MCP / plugin system | ✅ | ✅ | ✅ |
| Hierarchical planning · symbolic reasoning · self-correction · memory | ✅ | ✅ | ✅ |
| Web workbench + CLI + autonomous task loop | ✅ | ✅ | ✅ |
| Tool approval · security audit · auth / rate limiting / redaction | ✅ | ✅ | ✅ |
| Basic statistics (tasks / success rate / tokens / tool ranking) | ✅ | ✅ | ✅ |
| 📚 RAG knowledge base (chunking / embedding / auto-retrieval) | 5 docs · 10 MB | unlimited · 200 MB | unlimited |
| 📚 Multi-KB · reranker · citation tracing · scheduled re-embedding | — | ✅ | ✅ |
| 📚 Hybrid retrieval · usage analytics · retrieval audit · bulk import | — | — | ✅ |
| ⚡ Semantic cache (instant answers to similar questions, saves tokens) | — | ✅ basic | ✅ advanced |
| 🧭 Model routing (pick a model by task complexity) | — | ✅ 2 tiers | ✅ N tiers |
| 💰 Cost dashboard (per-model cost / cache savings) | — | — | ✅ |
| 📏 Daily tasks / workspaces | 100 · 3 | unlimited · 30 | unlimited |
| 🤝 Multi-agent mode | — | ✅ | ✅ |
| 🔁 Loop mode (Loop Engineering) | — | ✅ | ✅ |
| ⏰ Scheduled tasks | — | ✅ | ✅ |
| 📊 Advanced statistics dashboard (hit rate / efficiency / trends) | — | ✅ | ✅ |
| 📈 Observability (execution DAG + live board) | current task DAG (read-only) | + board / 200 runs / export | + per-session · 2000 runs |
| ⭐ Custom templates (reusable prompt assets) | — | ✅ | ✅ |
| 📄 Audit report export (PDF) | — | ✅ | ✅ |
| 🎓 Expert profiles | basic (3) | ✅ unlimited / share / import / stats | ✅ |
| 🏛️ Enterprise expert marketplace (approval workflow) | — | — | ✅ |
| 👥 Multi-user session pool (execution isolation) | — | — | ✅ |
| 🔐 SSO / LDAP integration | — | — | ✅ |
| 🧩 Fine-grained permissions (RBAC) | — | — | ✅ |
| 🚪 Private model gateway (egress control, model allowlist) | — | — | ✅ |
| Standalone analytics service | — | — | ✅ |

> Security features (auth, rate limiting, secret redaction, audit) **all stay in the
> Community Edition** — security is never behind a paywall.

> 💼 **Commercial activation (since v1.11.14; license v3 in v1.11.15)**: a Community → Pro/Enterprise
> entitlement is an **offline Ed25519 license** (`AMP2-…`). The client only embeds the verification
> public key, so it validates with no network and cannot be forged or edited (tier / expiry / seats are
> signed). v1.11.15 adds **serials, machine binding and not-before dates** (v3, backward compatible with
> v2) plus **batch issuance with a ledger, trial licenses (`--days`), offline CRL revocation, periodic
> re-checks with grace periods and smooth key rotation**. Given the vendor's one-file `.ampkg` upgrade
> (wheel + license), drag it into **Settings → ⭐ Upgrade Pro / Enterprise → ⚡ One-click upgrade** to
> install *and* activate — no pip, no env vars, no admin rights.
> **Upgrade with an `.ampkg` matching the core version (currently v1.11.18; this patch adds no commercial-extension changes, so a v1.11.17 package keeps working)** (the public key was not rotated, so existing
> licenses remain valid and old `.ampkg` files still activate; the current package also carries the v3
> license / machine-binding / CRL / re-check capabilities, and v1.11.17 fixes commercial feature route
> mounting for semantic cache / model routing / cost dashboard / Pro KB). On the release side, the 12-check
> `issue_license.py doctor` validates keys, wheel, versions, CRL, rotation and re-check configuration
> before a bundle ships. Full flow: [docs/COMMERCIAL_ACTIVATION.md](docs/COMMERCIAL_ACTIVATION.md);
> operations SOP: [docs/LICENSE_OPERATIONS.md](docs/LICENSE_OPERATIONS.md).

## Five Interaction Modes

| Mode | Description | Best for |
|------|-------------|----------|
| 💬 **Chat** | Pure multi-turn conversation, no tool calls, fastest (supports image input / vision models) | Q&A, consulting, brainstorming |
| ⚙️ **Work** | Hierarchical planning + tool execution + symbolic verification | Scaffolding projects, running commands, editing files |
| 💻 **Coding** | ReAct think-act loop focused on code | Reading / writing / debugging / refactoring / testing |
| 🤝 **Multi-agent** (Pro) | Several role agents collaborate, then synthesize | Complex, cross-role, long-running tasks |
| 🔁 **Loop** (Pro) | Loop Engineering: autonomous act–observe–correct cycle | Tasks needing iteration until a target is met |

## Tool Approval Modes

The dropdown at the top switches the approval policy for tool calls
(Reasonix-style `deny > ask > allow` gating):

- 🙋 **Ask** — every non-read-only tool call needs manual approval.
- ⚡ **Auto** (default) — low-risk tools are approved automatically; only dangerous
  operations need confirmation.
- ✅ **Approve all** — skip approval entirely and run fully autonomously (use with care).

## Loop Engineering / Scheduling / Statistics

- **🪙 Token-budget windows and multi-mode continuation (v1.11.16)**: the per-task token budget is a
  **window**; when it is exhausted the agent compacts context and opens the next one automatically.
  Multi-agent / loop / sub-agent runs no longer fail silently on exhaustion — they persist a resumable
  checkpoint and the UI offers "Continue multi-agent / Continue loop", picking up the unfinished parts.
- **Loop mode** (Pro) has built-in stop conditions (task complete / no progress for N
  rounds / max rounds) plus manual interruption, so it cannot spin forever.
- **⏰ Scheduled tasks** (Pro): run a task at a fixed interval in any mode, scheduled in
  the background with results recorded.
- **📊 Statistics**: the Community Edition provides basic aggregation (task count,
  success rate, tokens, duration, tool ranking); hit-rate donuts, context-usage and
  efficiency trends are Pro.
- **📈 Observability**: draws execution as a live DAG (task → plan step → tool call)
  with status colouring, durations and failure reasons, so you can see exactly where the
  agent is stuck. Community shows the **current task**'s live DAG (read-only); Pro adds a
  live board (success rate / P50·P95 / tool heat / failure attribution), run history and
  export; Enterprise adds per-session grouping.

## 🧭 Smart Routing & Cost (Pro and up; UI rebuilt in v1.11.15, stability fixes in v1.11.16, route-mounting fix in v1.11.17)

The sidebar's **🧭 Routing & Cost** page covers semantic caching, model routing and cost analysis;
v1.11.15 rebuilt the whole view:

- **Five KPI cards**: total cost / total tokens / cache hit rate / routing hits / 30-day average daily
  cost — cost and tokens use real 30-day series in mini bar charts; when cache or routing have no
  per-day data the page uses hit-rate bars and tier-distribution bars instead of **drawing fake curves**.
- **Semantic cache**: an SVG ring gauge for hit rate, a capacity bar and estimated savings (near-identical
  questions are answered instantly at 0 tokens).
- **Model routing**: a 0–100 complexity ladder with editable tiers (2 tiers in Pro, N in Enterprise); the
  "route preview" takes a task description and shows, via a complexity ring, which tier it will hit.
- **Enterprise cost dashboard**: 30-day bar charts in both amount and token views, per-model cost share,
  cache savings and methodology notes.
- **Methodology**: costs are estimated from the built-in price table (unknown models count as 0).
  `GET /api/costs` keeps every legacy field and adds `total_tokens` / `by_day` / `by_model[*].share` /
  `top_model` etc., so the UI and the API share one definition.
- **Permission states**: Community / Pro / Enterprise are rendered as capability cards; locked features
  show an upgrade entry instead of pretending to be zero data.
- **Stability and legacy-data compatibility (v1.11.16)**: an old `/api/cache` response without
  `threshold` used to crash `toFixed` and blank the page. `/cache`, `/router` and `/costs` responses are
  now sanitised (missing fields / nulls / string numbers / bad tiers) and numbers are formatted only
  after a valid check; page-level and five block-level error boundaries degrade just the affected card,
  and 403 / error payloads show an explicit error panel instead of silently displaying zeros.
- **Route-mounting fix (v1.11.17)**: activated Pro / Enterprise machines could still see
  "feature not enabled or version-gated (feature=semantic_cache)" because the community core's
  hand-maintained attach list omitted every commercial feature that ships its own REST routes — the real
  routes were never registered and requests hit the 403 fallback. The list is now derived from
  `COMMERCIAL_FEATURES`, and both startup and hot activation mount the real routes; if a 403 shows up
  again the message points at "restart / install the same-version extension package" instead.

## Quick Start

```bash
# Option A: download a desktop installer (recommended, no Python required)
#   https://github.com/yl13571844594-arch/AutoMind/releases/latest
#   Windows .exe (code-signed) · macOS .dmg (universal) · Linux .deb

# Option B: install from source
git clone https://github.com/yl13571844594-arch/AutoMind.git
cd AutoMind
pip install -e ".[web]"

# Option C: install from PyPI
pip install "automind-agent[web]==1.11.18"     # current version · Web + OpenAI-compatible backends
# Upgrade: pip install -U "automind-agent[web]==1.11.18"; use [full] for every backend
```

> ✅ **v1.11.18 is released**: PyPI is up to date, so `pip install -U "automind-agent[web]"` picks up
> exactly this release. Desktop installers live on
> [Releases](https://github.com/yl13571844594-arch/AutoMind/releases/latest)
> (Windows `.exe` code-signed · macOS `.dmg` universal · Linux `.deb`), and each release ships a
> `SHA256SUMS` file you can check with `sha256sum -c`.
>
> For air-gapped machines, run `pip download "automind-agent[web]==1.11.18"` on a connected
> host, copy the wheel directory over, and follow [docs/OFFLINE_OPS.md](docs/OFFLINE_OPS.md).

```bash
# Start the Web workbench (recommended)
python -m automind.server --port 8765
# then open http://localhost:8765

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

Open the Web workbench and click **⚙ Settings → 🔑 API Keys** (bottom left):

- Supports OpenAI / Claude / DeepSeek / Kimi (Moonshot) / Bailian (Qwen) / Zhipu (GLM) /
  Doubao / Gemini / Grok / Ollama.
- **Custom OpenAI-compatible endpoint (relay / proxy)**: fill in `api_base`
  (e.g. `https://api.your-proxy.com/v1`), a default model and an API key to use any
  service compatible with OpenAI's `/v1/chat/completions`.
- Every provider lets you type an arbitrary model name.
- **🧠 Model picker in the composer (v1.11.15)**: the dropdown next to the input switches models on the
  fly — default model / configured models grouped by provider (only providers with a usable key, plus
  local Ollama models; favourites pinned). The choice persists for subsequent messages and is honoured
  by resume/retry. If a manually selected model fails to initialise, the receipt explains why and the
  previous model is kept — **the app never silently swaps in a model you did not pick**; the
  `task_start` receipt reports the effective provider/model.
- **🗺 Automatic routing (Pro / Enterprise)**: switch the picker to "Automatic routing" and the server
  scores task complexity (0–100 heuristic), picks the routing tier and switches the model for this run;
  the `task_start` receipt reports tier, complexity and target provider/model. Community shows 🔒 with an
  upgrade prompt, and the server only replies "Pro/Enterprise required" when it receives `auto` — it does
  not quietly substitute a model. Priority: **explicit manual choice > automatic routing > global default**.
- API keys are stored only in the local `.automind_config.json` and are never uploaded.
  Environment variables work too (`OPENAI_API_KEY`, `DEEPSEEK_API_KEY`,
  `MOONSHOT_API_KEY`, …).

## Project Directory / Tools / Skills / MCP

- **Local project directory**: the 📁 badge at the top right (or Settings → General →
  Browse) picks a local directory as the agent's working root.
- **Custom models**: type a model name and click ➕ Add to persist it in the dropdown
  (per provider, removable).
- **Tools panel** (sidebar 🔧) has three tabs:
  - **Tools** — the built-in tool list with permission tier and risk score.
  - **Skills** — built-in skills plus "load a local skill directory" (scans `.py` files
    for `AbstractSkill` subclasses).
  - **MCP** — add and connect MCP servers (stdio / SSE) and auto-discover their tools
    (requires `pip install mcp`).

## Workspaces / Templates / Theme / Undo

- **🗂 Workspaces**: manage and switch between workspaces from the badge at the top
  right. Each workspace = its own directory + its own context, so tasks never pollute
  each other.
- **💬 Session manager (v1.11.17: group / sort / filter / search)**: a 🔍 quick search sits in the
  sidebar session list, and ⚙️ next to the "Sessions" header (or Settings → 💬 Session Manager) opens a
  full manager — scope current / all workspaces, live search over title / archived summary / session id
  (space-separated keywords, all must match), filters all / pinned / running / archived, grouping by
  none / time / status / workspace, sorting by last activity / created / title / turns with pinned always
  first. Open / rename / pin / archive / restore / delete in one place; the view settings persist.
  Archiving still writes the summary into the workspace long-term memory (see manual §8.7.1).
- **📚 Template library**: 10 built-in starter templates (website, scaffolding, bug fix,
  tests, data analysis, scraping, …), one click from the welcome screen or the 📚 button.
  A 4-step onboarding tour runs on first open (replay with ❓).
- **🌓 Light mode**: switch dark/light any time from ⚙ Settings; initialises from your
  system preference.
- **💰 Live cost**: the token panel on the right and every result show estimated spend
  (built-in pricing for mainstream models, customisable).
- **↩️ Undo / rollback**: files the agent changed are listed under "File changes" on the
  right; revert one file or roll back everything.
- **▶ Resume (four modes, v1.11.16)**: after an interruption, error or a spent token budget, **plan /
  ReAct / multi-agent / loop** runs all resume from a checkpoint (discoverable after a process restart):
  only the remaining steps / roles / iterations run, finished work is not redone, and the
  same-millisecond checkpoint-id collision that silently overwrote checkpoints is fixed.
- **📄 Code editor (Web IDE)**: the 📄 Code tab on the right = file tree + Monaco editor
  + change diff preview. Edit code straight from the browser (Ctrl+S to save, undoable)
  without disturbing the chat.
- **🎓 Expert marketplace**: 10 curated official experts installable in one click, plus
  your own (3 in Community). Once activated every task runs with that role's setup; Pro
  adds unlimited creation, sharing, import/export and statistics.
- **👥 Team collaboration**: v1.11.15 turns this into a full team workbench (live presence, four-column
  board, priority, due dates, comments, summaries). Workspaces, templates and experts are server-level
  storage, so one deployment is shared by the team — see the next section.
- **🔌 Agent integration**: a built-in OpenAI-compatible endpoint
  (`/v1/chat/completions`, SSE streaming). ⚙ Settings → 🔌 Agent integration generates
  ready-made **Continue.dev** (VS Code / JetBrains) and **Cline** configs; any other
  OpenAI client (Zed, …) works the same way.

## 👥 Team Collaboration Workbench (v1.11.15)

One deployment = one team. The sidebar's **👥 Team** view is now a complete workbench:

- **Live presence**: WebSocket connect / disconnect / rename events are broadcast in real time; the
  online-members strip shows avatar initials, online dots and task counts. Multiple tabs are aggregated
  per person; nicknames persist locally and are restored after a restart.
- **Richer tasks**: beyond title and assignee, tasks now carry **priority** (low / normal / high /
  urgent), **due date**, **tags**, **comments** and an **activity timeline**, plus a **blocked** status.
  Existing tasks get defaults on read — zero migration, ids preserved.
- **Four-column board**: To do / In progress / Blocked / Done, with priority colour bars, red overdue
  badges, assignee, comment count and tags; search plus filters by assignee, priority and status, and
  sort by due date.
- **Live updates and summaries**: create / update / comment / claim / move events broadcast to online
  members instantly; five stat cards summarise completion rate, overdue, today's and 7-day completions
  and recent activity, alongside per-person workload. The detail drawer edits fields, moves status,
  comments and confirms deletion.
- **Single-instance semantics**: "online members" reflects WebSocket connections **inside the current
  process**, not a cross-machine cluster; multiple instances each track their own presence.

## Chat History

Chat-mode conversations persist to `.automind/chat_history.json` and are restored on
reload; **🔄 New session** clears the current one. Task history (sidebar 📜) supports
deleting single entries or clearing everything.

## Streaming · Interruption · Mid-turn Interjection

- **Streaming chat**: chat mode streams token by token over WebSocket with a live cursor.
- **Interruption**: click ■ during execution to genuinely cancel the background task
  (`asyncio.Task` cancellation) — works in chat, work and coding modes.
- **💬 Mid-turn interjection (v1.7.2)**: noticed it is missing something halfway through
  an answer? **You no longer have to stop first** — just type the extra instruction and
  press `Ctrl/⌘+Enter` (or click "⤴ Insert"). It is folded into **the answer currently
  being written** and generation continues in the same bubble, so you read one continuous
  reply. Work/coding modes are covered too (the supplement reaches the model's context
  before the next step). Each bubble is flagged "pending → ✓ incorporated"; if the turn
  ended before your note arrived, the app **says so explicitly** instead of pretending it
  was received.
- Falls back to synchronous REST automatically when WebSocket is unavailable.

## Multimodal · Voice · Preview · Token Stats

- **🪙 Token usage**: the right panel shows input/output tokens per task plus running
  totals and task count, resettable in one click.
- **🔍 HTML preview**: ```html blocks in model output get a "Preview" button that renders
  them in a sandboxed iframe; the right panel lists `.html` files in the project directory
  (`/api/preview/file`, restricted to the project directory to prevent traversal).
- **🎤 Voice input**: the microphone button uses the Web Speech API (Chrome/Edge).
- **📎 Multimodal**: attach images for vision models; images, links and tables in output
  are rendered inline.

## 🤖 IM Bots (Feishu / WeCom / DingTalk)

Add a bot under **Settings → Bots**, fill in the platform credentials / webhook, then simply
message it from that chat — the agent runs locally and relays progress and results back to the
same conversation:

- **One chat = one session**: Feishu, WeCom, DingTalk and generic webhooks are supported;
  `/new` starts a fresh session, `/stop` cancels, `/mode` switches chat / work / coding, and
  `/apply` copies isolated-workspace artifacts back into the project.
- **Approvals inside the chat**: dangerous operations arrive as a numbered approval message —
  reply "approve / reject". A timeout counts as reject (fail-closed); nothing slips through.
- **Workspace isolation (v1.11.13)**: bot sessions use their own `bot-…` namespace and isolated
  working copy, invisible to the Web workspace and to other chats; relative paths are anchored
  to that copy.
- **Formatted per platform (v1.11.14)**: replies are converted to what each platform actually
  renders — tables degrade to field lists, WeCom code blocks become quotes, titles respect
  platform limits; long replies split at block boundaries and **never inside a code fence**,
  with "(continued N/M)" markers. See the "Markdown rendering rules per platform" section of
  [docs/IM_BOTS.md](docs/IM_BOTS.md) and [docs/FEISHU.md](docs/FEISHU.md).

## Security Audit

The sidebar's **🛡️ Security audit** shows the risk score and authorisation decision for
every tool call (allowed / needs confirmation / dangerous). Destructive commands
(`rm -rf`, `git push --force`, …) are classified as dangerous and require confirmation.

## Workflow Automation & Governance (v1.11.16)

- **🔀 Workflow automation**: step-level `if` / `unless`, failure fallback
  `on_failure: fallback(<step-id>)`, workflow-wide `timeout_seconds`, and per-step / whole-run token
  accounting in reports and the dashboard. Pro triggers add cron catch-up (`catch_up` /
  `max_delay_seconds`) and webhook replay protection (`dedupe_window`). Every new field is optional and
  defaults to the old behaviour; existing workflows keep working unchanged. See
  [docs/WORKFLOWS.md](docs/WORKFLOWS.md).
- **🛡 Execution governance**: **all 26 parameters** under **Settings → Execution Governance** now really
  take effect (7 classes of "saved but ignored" were fixed: trace switch/retention, terminal timeouts and
  background channel, write-conflict policy, approval timeout/action, task token-budget propagation, …).
  Each setting is labelled with its effective timing ("immediate / next task / next approval / next call /
  next write") and the API returns `applied / rejected / effective`. Defaults, read points, timing and
  tuning advice: [docs/GOVERNANCE.md](docs/GOVERNANCE.md).

## Production Deployment / Multi-user

- **Session isolation**: every browser gets its own `session_id`; chat histories are
  invisible to each other and never overwrite (persisted under `.automind/chats/`).
- **Access control** (optional, off by default): once a token is set, every `/api/*` and
  `/ws` request must carry it.

| Env variable | Effect |
|--------------|--------|
| `AUTOMIND_AUTH_TOKEN` | Require `Authorization: Bearer <token>` on all `/api/*` and `/ws` |
| `AUTOMIND_CORS_ORIGINS` | Restrict CORS origins (comma-separated) |
| `AUTOMIND_MAX_CONCURRENT` | Max concurrent tasks (default 8; returns 429 beyond) |
| `AUTOMIND_RATE_LIMIT` | Per-client per-minute limit for `/api/run` (0 = off) |
| `AUTOMIND_REDACT_SECRETS` | Redact API keys / tokens in task output |
| `AUTOMIND_ALLOWED_ORIGINS` | WebSocket `Origin` allowlist |
| `AUTOMIND_TRUSTED_HOSTS` | Extra `Host` names allowed to reach the service (comma-separated, `*.example.com` supported). Needed behind a reverse proxy / tunnel; loopback names always work |

```bash
export AUTOMIND_AUTH_TOKEN="your-secret-token"
export AUTOMIND_CORS_ORIGINS="https://your-domain.com"
export AUTOMIND_MAX_CONCURRENT="16"
python -m automind.server --host 0.0.0.0 --port 8765
```

The frontend sends `Authorization: Bearer <token>`; WebSocket uses
`ws://host/ws?token=<token>`.

- **Health check**: `GET /api/health` (no auth) returns version, running task count,
  concurrency limit and uptime — for liveness probes and load balancers.
- **Commercial activation & upgrades (v1.11.15)**: the same offline flow works for intranet /
  production deployments — drag the vendor's `.ampkg` into the in-app
  **⭐ Upgrade Pro / Enterprise** panel (no pip, no outbound network). One activation covers the
  whole host; confirm the tier via `edition` on `GET /api/health`
  (`community` / `pro` / `enterprise`). Activation and upgrades are admin actions: local requests
  work directly, remote ones need the admin token (`AUTOMIND_ADMIN_TOKEN`). See
  [docs/COMMERCIAL_ACTIVATION.md](docs/COMMERCIAL_ACTIVATION.md).
- **License operations (v1.11.15)**: v3 licenses support **serials, machine binding and not-before
  dates**. Vendors can batch-issue with `issue_license.py issue-batch` plus a 12-column ledger,
  revoke offline with `revoke` + CRL, issue trials with `--days`, and schedule activation with
  `--start`. Clients re-check periodically (`AUTOMIND_LICENSE_RECHECK`, default 3600s), honour a grace
  period (`AUTOMIND_LICENSE_GRACE_DAYS`, default 0), and downgrade when the system clock is rolled back
  by more than one day. See [docs/COMMERCIAL_ACTIVATION.md](docs/COMMERCIAL_ACTIVATION.md) and
  [docs/LICENSE_OPERATIONS.md](docs/LICENSE_OPERATIONS.md).

> Note: the Community Edition gives full multi-user isolation for chat mode; work/coding
> execution shares a single agent. **Enterprise** adds a session-agent pool so execution
> is isolated per user too, and can connect to a standalone analytics service. See
> [docs/EDITIONS.md](docs/EDITIONS.md).

## Architecture

```
automind/
├── core/            # types, config, LLM backends, hooks, plugins, logging, edition gating
├── agent.py         # AutoMindAgent — top-level orchestrator
├── planning/        # hierarchical planner, ReAct executor, dependency DAG
├── reflection/      # quality assessment, self-correction, retry / circuit breaker
├── memory/          # short/long-term memory, entity memory, knowledge graph
├── rag/             # knowledge base: parsing, chunking, embedding, retrieval
├── tools/           # terminal, file editing, sandbox, permissions, MCP, office/media/net
├── skills/          # skill system (built-ins + SKILL.md + entry points)
├── context/         # context window management, project indexing, code analysis
├── state/           # checkpoints, human-in-the-loop, resource budgets
├── server.py        # FastAPI web layer (REST + WebSocket)
├── cli/             # CLI + Rich REPL
└── static/dist/     # prebuilt React workbench (sources in web/)
```

## Plugin System

Drop a plugin under `~/.automind/plugins/<name>/`:

```
~/.automind/plugins/my-plugin/
├── plugin.json     # {"name": "my-plugin", "version": "1.0.0", "description": "..."}
└── hooks.py        # def get_hooks() -> AgentHooks
```

```python
from automind.core.hooks import AgentHooks

def get_hooks():
    async def before(task):
        print(f"task starting: {task}")
    return AgentHooks(before_run=before)
```

Four plugins ship built in (`cost_tracker`, `pii_guard`, `task_notify`, `hello_hooks`).
Manage them from the Web UI (Tools panel → 🧩 Plugins) or via `GET /api/plugins` and
`POST /api/plugins/{name}/load|unload`.

## Built-in Skills

12 built-in skills: `project_init`, `code_generator` (generation / completion with syntax
validation and a self-repair pass), `test_runner`, `log_analyzer`, `doc_generator`,
`dep_audit`, `data_insight`, `excel_report`, `doc_batch`, `article_writer`,
`web_research`, `env_doctor` — plus any `SKILL.md` skill packs or Python skills you load.

## Examples

See the [examples/](examples/) directory:

- `01-quick-start` — install, launch, first task
- `02-custom-model` — DeepSeek / Ollama / relay-proxy configuration
- `03-skill-development` — write your own Python skill
- `04-plugin-development` — write a lifecycle-hook plugin

## Demo

```bash
python demo/e2e_demo.py
```

## Development

```bash
pip install -e ".[dev]"
pytest tests/            # full test suite
ruff check .             # lint
cd web && pnpm install && pnpm dev    # frontend dev server
```

## Open Source

- **License**: the Community Edition (all code in this repository) is [MIT](LICENSE) —
  free to use, modify, distribute and sell, as long as the copyright and license notice
  are kept. Provided "as is", without warranty of any kind.
- **Edition boundary**: commercial features (multi-agent / loop / scheduling / advanced
  statistics / session pool) are provided by the closed-source `automind-pro` package and
  are **not in this repository**. The community core talks to it through the stable
  extension protocol in [`automind/core/edition.py`](automind/core/edition.py) and runs
  fully standalone when the extension is absent. Security features stay in Community for good.
- **Privacy**: AutoMind runs locally. API keys and chat history stay on your machine
  (`.automind_config.json` / `.automind/`). No telemetry is collected.
- **Contributing**: see [CONTRIBUTING.md](CONTRIBUTING.md); release history in
  [CHANGELOG.md](CHANGELOG.md).

## Community

| I want to… | Go here |
|---|---|
| 🐛 Report a bug | [New issue](https://github.com/yl13571844594-arch/AutoMind/issues/new?template=bug_report.yml) |
| ✨ Request a feature | [New issue](https://github.com/yl13571844594-arch/AutoMind/issues/new?template=feature_request.yml) |
| 💬 Ask how to use it / unsure if it's a bug | [Discussions](https://github.com/yl13571844594-arch/AutoMind/discussions) |
| 🔒 Report a security vulnerability | [Private channel](https://github.com/yl13571844594-arch/AutoMind/security/advisories/new) — please do not open a public issue |
| 🛠 Contribute code | [Contributing guide](CONTRIBUTING.md) · [Code of conduct](.github/CODE_OF_CONDUCT.md) |

The security policy and threat model (what counts as a vulnerability and what is
by design) is in [SECURITY.md](.github/SECURITY.md).
