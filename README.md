```
╔══════════════════════════════════════════════════════════════════╗
║                                                                  ║
║   ████████╗ █████╗ ██████╗ ███████╗                              ║
║   ╚══██╔══╝██╔══██╗██╔══██╗██╔════╝                              ║
║      ██║   ███████║██████╔╝███████╗                              ║
║      ██║   ██╔══██║██╔══██╗╚════██║                              ║
║      ██║   ██║  ██║██║  ██║███████║                              ║
║      ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝                              ║
║                                                                  ║
║   TRUSTED  AGENT  RUNTIME  STACK          v2 . MIT . Python 3.12 ║
║                                                                  ║
║   > SYSTEM ONLINE                                                ║
║   > AGENTS LOADED .......... OK                                  ║
║   > VAULT SEALED ........... OK                                  ║
║   > MEMORY ACTIVE .......... OK                                  ║
║   > AWAITING INPUT _                                             ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
```

Single-process agent framework. Connect messaging platforms to persistent LLM sessions with tools, memory, and multi-agent coordination. No containers. No microservices. One process, full control.

```
INCOMING SIGNAL ──→ Router ──→ Agent Manager ──→ Claude Code CLI ──→ MCP Tools ──→ RESPONSE
```

## SYSTEM CAPABILITIES

- **Single process** — no Docker, no microservices, no infrastructure to manage
- **Multi-agent** — multiple bots with different personalities, tools, and permissions
- **Multi-bot** — each agent gets its own Discord bot identity
- **Persistent memory** — SQLite with FTS5 full-text search + semantic embeddings
- **Drop-in tools** — add a `@tool` decorated Python function, it's instantly available
- **Drop-in skills** — YAML prompt templates become Discord slash commands automatically
- **Human-in-the-loop** — sensitive tools require approval via Discord reactions
- **Encrypted vault** — Fernet-encrypted credentials, never in env vars or config files
- **Category routing** — route agents to Discord categories, not just channels
- **Per-agent tool access** — allowlists, denylists, and built-in tool blocking per agent
- **Media pipeline** — image/video analysis (Gemini), audio transcription (Groq)
- **Headless browser** — `browse_url` tool renders JS-heavy pages via Playwright + Chromium
- **Hot reload** — tools and skills update without restarting

## BOOT SEQUENCE

Only prerequisite is **Python 3.12+** and a **Linux VPS** (2 CPU, 4GB RAM). The setup wizard installs everything else (uv, jq, Claude Code CLI) and walks you through vault creation, Discord bot connection, team setup, agent configuration, HITL approval, systemd units, and optional headless browser.

```bash
git clone https://github.com/TARS-OTHS/tars.git /opt/tars
cd /opt/tars
uv run python setup.py
```

> **Note:** if you don't have `uv` yet, install it first: `curl -LsSf https://astral.sh/uv/install.sh | sh`

> **Browser tool:** the `browse_url` tool requires a Chromium binary (~170MB) that isn't installed by `uv sync`. The setup wizard offers to install it, or run manually: `uv run playwright install chromium`

Then start T.A.R.S:

```bash
# Development
uv run python -m src.main

# Production (setup.py installs the systemd unit)
sudo systemctl start tars.service
journalctl -u tars -f
```

### POST-INSTALL

Use `settings.py` to modify configuration without re-running setup:

```bash
uv run python scripts/settings.py
```

TUI menu for: LLM defaults, connectors, memory, HITL gates, rate limits, agents, timers, vault secrets, and more. See [SCRIPTS.md](SCRIPTS.md) for the full menu.

### UPDATING

```bash
cd /opt/tars
git pull
scripts/sync.sh                    # must run before restart — installs deps across all layers
sudo systemctl restart tars.service
```

> **Warning:** do not skip `scripts/sync.sh`. The service unit uses `uv run --no-sync`, so new dependencies won't be installed at runtime. Skipping sync after a pull can crash the service or silently run stale code.

### FILE OWNERSHIP

All files under the install directory are owned by `tars:tars` (the service user). If you edit files as root, `chown tars:tars` them back — root-owned files in the tree break `uv sync` and are not writable by the service.

```bash
# Check for root-owned files
find /opt/tars -not -user tars 2>/dev/null
```

### PROFILES

```bash
# Default profile
uv run python -m src.main

# Named profile (loads config.<profile>.yaml and agents.<profile>.yaml)
uv run python -m src.main --profile test
```

## WEAPON SYSTEMS (TOOLS)

```python
# src/tools/my_tool.py
from src.core.base import ToolContext
from src.core.tools import tool

@tool(name="check_weather", description="Get weather for a city")
async def check_weather(ctx: ToolContext, city: str) -> str:
    # ctx.vault gives you encrypted API keys
    api_key = ctx.vault.get("weather-api-key")
    return f"Weather in {city}: sunny, 25C"
```

Drop it in `src/tools/`, it's auto-discovered and available to all agents via MCP. No imports to add, no registry to update.

## MISSION BRIEFINGS (SKILLS)

```yaml
# skills/my_skill.yaml
name: daily_report
description: Generate a daily business report
parameters:
  - name: focus
    type: string
    choices: [sales, marketing, operations]
prompt: |
  Generate a {focus} report for today. Check recent data and summarize key metrics.
tools:
  - web_search
  - memory_search
```

Skills can also run shell commands directly without an LLM session:

```yaml
name: system_audit
description: Run a full system health audit
command: scripts/health-audit.sh --report
```

See [skills/README.md](skills/README.md) for the full format reference including direct command execution.

## INTEL DATABASE (CODEX)

The `codex/` directory holds stable business knowledge that agents can't get from an API — brand voice, company profile, supplier contacts, processes, strategy docs.

```
codex/
├── _index.md          <- Master index (agents read this first)
├── business/          <- Brand voice, compliance
├── products/          <- Product info, guidelines
├── strategy/          <- Playbooks, competitor analysis
└── processes/         <- SOPs, workflows
```

Agents reference the codex via their CLAUDE.md. The `_index.md` tells agents what's in the codex vs. what to query from tools, so they don't use stale docs when live data is available.

See [codex/README.md](codex/README.md) for the full guide.

## ARSENAL

| Category | File | Tools | Requires |
|----------|------|-------|----------|
| **Memory** | memory.py | store, search, semantic_search, forget | Built-in |
| **Team** | team.py | list, get, add, update, remove | Built-in |
| **Web** | web_search.py | search | Tavily API key |
| **Discord** | discord_tools.py | read_channel, read_message, search, send_file | Discord bot token |
| **Google Workspace** | google.py | Gmail, Calendar, Drive (13 tools) | Google OAuth2 credentials |
| **Trello** | trello.py | boards, lists, cards, create_card, activity | Trello API key + token |
| **Media** | gemini.py, audio.py, video.py | image/video analysis, transcription | Gemini + Groq API keys |
| **Cloudflare** | cloudflare.py | zones, dns_list, dns_update | Cloudflare API token |
| **Notion** | notion.py | search, read, create | Notion API key |
| **Browser** | ingest.py | read_url, browse_url (headless Chromium via Playwright) | `playwright install chromium` |
| **System** | ingest.py, tmux.py, builtin.py | skill creation, tmux, inter-agent messaging | Built-in |

Remove a file = remove those tools. Add your own integrations (Shopify, Stripe, GitHub, Slack, etc.) by dropping a `@tool` decorated Python file into `src/tools/`.

## MAINFRAME ARCHITECTURE

```
╔══════════════════════════════════════════════════════╗
║                   T.A.R.S  PROCESS                   ║
║                                                      ║
║   CONNECTOR ─── Discord (Telegram, Slack: roadmap)   ║
║       │                                              ║
║       ├── ROUTER ──── channel/category → agent       ║
║       ├── ACCESS ──── 3-layer auth gate              ║
║       ├── AGENTS ──── context, memory, sessions      ║
║       ├── LLM ─────── Claude Code CLI subprocess     ║
║       └── MCP ─────── tools + middleware             ║
║              ├── Rate limit                          ║
║              ├── HITL gate                           ║
║              ├── Execute tool                        ║
║              └── Audit log                           ║
║                                                      ║
║   ▓ VAULT (Fernet)  ▓ MEMORY (SQLite)  ▓ HOT RELOAD ║
╚══════════════════════════════════════════════════════╝
```

Everything is a pluggable module with auto-discovery. Drop a file in the right folder, reference it in config, it works.

## MULTI-AGENT DEPLOYMENT

Each agent gets its own bot identity, tool access, and channel routing:

```yaml
# config/agents.yaml
agents:
  main:
    display_name: "My Agent"
    llm:
      provider: claude_code
      model: opus
    tools: all                    # Full MCP tool access
    disallow_builtins:            # Block file editing and shell access
      - Edit
      - Write
      - Bash
      - MultiEdit
    routing:
      discord:
        account: main             # Uses the 'main' bot
        channels: []              # All channels (wildcard)
        mentions: true

  assistant:
    display_name: "Helper"
    llm:
      provider: claude_code
      model: sonnet
      timeout: 1800
    tools:                        # Restricted tool list
      - memory_search
      - web_search
      - trello_boards
      - trello_cards
    routing:
      discord:
        account: helper           # Separate bot identity
        categories: ["123456"]    # Only responds in this category
        mentions: true
```

Agents with specific channel/category routing take priority over wildcard agents.

## ACCESS CONTROL

```
AUTHORIZATION MATRIX ACTIVE
```

Three-layer permission system:

| Layer | Controls | Configured in |
|-------|----------|---------------|
| **Can they talk?** | Sender tier x agent tier — who can message which agent | `config/team.json` |
| **What tools?** | Per-sender tool restrictions computed per message | `config.yaml` |
| **Agent ceiling** | Static per-agent tool allowlist/denylist | `agents.yaml` |

**Clearance levels:** owner (full access) → admin (safe tools + HITL) → staff (assistant only) → unknown (denied)

## LLM ENGINE

T.A.R.S uses **Claude Code CLI** as its LLM engine via a Claude Max subscription (no per-token API costs). Each agent spawns a Claude Code subprocess with:

- Full tool access via MCP (Model Context Protocol)
- Session resume across conversations
- Per-agent model selection (opus, sonnet, haiku)
- Per-agent configurable timeout

Alternative LLM providers (OpenAI-compatible endpoints, Ollama, Groq) are on the roadmap.

## MEMORY CORE

```
MEMORY SUBSYSTEM ACTIVE
├── Storage ......... SQLite (WAL mode)
├── Text search ..... FTS5 full-text index
├── Semantic ........ BGE-small-en-v1.5 (384-dim, ONNX)
├── Scoping ......... per-agent isolation + shared scope
├── Persistence ..... auto-recall at session start
└── Lifecycle ....... decay → archive → purge (pinnable)
```

## CONFIGURATION

The setup wizard (`uv run python setup.py`) generates all config files interactively. Post-install, use `uv run python scripts/settings.py` to modify any setting. Or copy the examples manually:

```bash
cp config/config.yaml.example config/config.yaml
cp config/agents.yaml.example config/agents.yaml
cp config/team.json.example config/team.json
```

All config files are gitignored — your deployment details stay private.

## OPERATIONS

```bash
# Run
uv run python -m src.main

# Run with profile
uv run python -m src.main --profile test

# Setup wizard
uv run python setup.py

# Settings manager (post-install)
uv run python scripts/settings.py

# Vault management
uv run python vault-manage.py

# Update a running install
git pull && scripts/sync.sh && sudo systemctl restart tars.service

# Run tool tests
uv run python scripts/test-tools.py
```

## SECURITY PROTOCOLS

```
DEFCON STATUS: LOCKED DOWN
```

- **Fernet vault** — AES-128-CBC encrypted at rest, PBKDF2 key derivation (100k iterations)
- **HITL gates** — configurable per-tool, Discord reaction approval with timeout
- **Three-layer access control** — sender tier × agent tier, per-message tool filtering, static agent ceiling
- **Rate limiting** — per-tool per-agent sliding window
- **Audit log** — every tool call, HITL decision, auth event (JSONL)
- **Bot loop detection** — sliding window prevents runaway agent-to-agent ping-pong
- **Message dedup** — same content to same channel within 120s is dropped
- **Agent-scoped memory** — agents only see their own memories + shared scope

## MIGRATING FROM OPENCLAW

Coming from OpenClaw? T.A.R.S replaces the entire stack — gateway, auth proxy, Docker sandboxes, plugins — with one Python process. Same agents, same channels, 10-minute migration.

```bash
# Stop OpenClaw, install T.A.R.S, run setup wizard
systemctl stop openclaw-gateway.service
git clone https://github.com/TARS-OTHS/tars.git /opt/tars
cd /opt/tars && uv run python setup.py
```

See [MIGRATION.md](MIGRATION.md) for the full step-by-step guide.

## DOCUMENTATION INDEX

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Full system architecture and operations reference |
| [MIGRATION.md](MIGRATION.md) | Migration guide from OpenClaw |
| [SCRIPTS.md](SCRIPTS.md) | All scripts with usage |
| [skills/README.md](skills/README.md) | Skill format reference |
| [codex/README.md](codex/README.md) | Business knowledge guide |

## License

MIT — do whatever you want with it.

```
> A STRANGE GAME.
> THE ONLY WINNING MOVE IS TO BUILD YOUR OWN AGENTS.
> HOW ABOUT A NICE GAME OF CHESS?
```
