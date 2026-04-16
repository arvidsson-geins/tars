# T.A.R.S

## What Is This?

T.A.R.S is a lightweight, LLM-agnostic agent system that connects messaging apps (Discord, with more on the roadmap) to persistent LLM sessions running inside project contexts. No OpenClaw dependency.

## Design Philosophy

- **Lightweight over feature-rich** — minimal dependencies, minimal config, minimal moving parts
- **Zero maintenance** — SQLite for everything, no external DBs, no Docker required, no cron jobs to manage
- **LLM agnostic** — Claude API, Groq, Ollama, any OpenAI-compatible endpoint
- **Project-scoped sessions** — each agent runs inside a project directory with relevant files as context
- **Easy to extend** — adding tools, APIs, skills = adding a decorated Python function
- **Simple security** — encrypted credential vault (Fernet), no proxy chains, no containers needed
- **Inter-agent communication** — agents can message each other through a simple message bus
- **Easy migration** — TARS/OpenClaw users can migrate with one command

## Architecture — Everything Is a Module

Single async Python process. Everything is a pluggable module with auto-discovery.
Drop a file in the right folder → reference it in config → it works.

```
Registry (auto-discovers all modules)
    │
    ├── Connectors   src/connectors/    discord, telegram, slack, http...
    ├── LLM          src/llm/           claude, groq, ollama, openai...
    ├── Memory       src/memory/        sqlite, postgres, redis...
    ├── Vault        src/vault/         fernet (default)
    ├── Tools        src/tools/         @tool decorated functions
    ├── Skills       skills/            YAML (prompt + tool list)
    ├── MCP          config/mcp.yaml    auto-surfaces as native tools
    └── APIs         src/apis/          inbound webhooks
```

An agent is a project folder + config that picks modules. See [ARCHITECTURE.md](ARCHITECTURE.md) for full system design.

## Key Directories

```
src/
  core/          — registry, agent manager, router, middleware, base interfaces
  connectors/    — one file per platform (discord.py, telegram.py, etc.)
  llm/           — one file per provider (claude_code.py, etc.)
  memory/        — one file per backend (sqlite.py, etc.)
  vault/         — credential encryption (fernet.py)
  tools/         — @tool decorated functions, auto-discovered
  lib/           — shared libraries (compressor.py)
  auth/          — OAuth2 token refresh, etc.
  apis/          — @api decorated inbound webhooks
config/          — YAML: config.yaml, agents.yaml, mcp.yaml
agents/          — one folder per agent (SOUL.md + workspace + local data)
skills/          — YAML skill definitions (prompt + tool list)
data/            — SQLite databases, audit logs, state (gitignored)
```

## Tech Stack

- **Python 3.12+** with asyncio
- **SQLite** (WAL mode) for all persistence
- **Claude Code CLI** for primary LLM engine (Max subscription)
- Direct HTTP client for alternative LLM providers (roadmap — any OpenAI-compatible endpoint)
- **discord.py** for Discord
# - **python-telegram-bot** for Telegram (roadmap)
- **cryptography** (Fernet) for credential vault
# - **FastAPI** for HTTP API / webhooks (roadmap)

## Development

```bash
scripts/sync.sh            # install deps (all layers)
uv run python -m src.main  # run locally
```

## Deployment

When updating a running install (`git pull` + restart), follow the ritual in [ARCHITECTURE.md → Operations → Updating a Running Install](ARCHITECTURE.md#updating-a-running-install). The `git pull` → `systemctl restart` shortcut is **wrong** — service units use `uv run --no-sync`, so you must run `sudo -u tars scripts/sync.sh` between the pull and the restart or the service will crash on a new dep or silently run stale code.

## File Ownership

All files under the install directory are owned by `tars:tars` (the service user). The main service and timers run as `tars`, so root-owned files inside the tree are latent breakage — readable but not writable/deletable by the service, and `uv sync` / `uv run` will fail on any root-owned file in `.venv/`.

**When editing files in this repo as root** (e.g. from a Claude Code session running as root), `chown tars:tars <file>` back after every save. Most editors — including the `Edit`/`Write` tools — rewrite files and the new file inherits the editing user's ownership, not the original's. After a batch of edits, verify with:

```bash
find "$TARS_HOME" -not -user tars 2>/dev/null
```

Zero output = clean. Any output = run `sudo chown -R tars:tars <paths>` on the listed files.

## Three-Layer Architecture

This repo is **Layer 1 (Core)** — the open-source engine. It is loaded by all deployments.

```
Layer 1: <org>/tars          (this repo, public)  — Core engine + generic tools
Layer 2: (private repo)                           — Domain-specific tools/skills
Layer 3: (private repo)                           — Per-deployment config, agents, data
```

The engine discovers Layer 2 and 3 via env vars:
- `TARS_OTHS` — colon-separated paths to Layer 2 module directories
- `TARS_OVERLAY` — path to the Layer 3 overlay directory

### What belongs in Core (this repo)

- Framework code: `src/core/`, `src/connectors/`, `src/llm/`, `src/memory/`, `src/vault/`, `src/auth/`
- Generic tools usable by any deployment: memory, team, web, browser, google, trello, notion, cloudflare, discord, gemini, audio, video, tmux, ingest, compress
- Generic skills, MCP server, setup wizard, scripts, timers
- Example configs only (`config/*.example`, `agents/rescue/CLAUDE.md.example`)

### What does NOT belong in Core

- Client agent identities (CLAUDE.md files for specific named agents)
- Real config files (config.yaml, agents.yaml, team.json with actual data)
- Domain-specific tools — these go in Layer 2
- Personal names, Discord IDs, API keys, or any identifying information
- Client-specific skills, timers, scripts, ETL pipelines

### Development workflow

Direct commits to `main` are blocked by a pre-commit hook. All changes go through feature branches:

```bash
git checkout -b fix/description        # create branch
# make changes, test with: sudo systemctl restart tars
git add <files>
git commit -m "fix: description"
git push --no-verify -u origin fix/description
gh pr create                           # PR for cross-review
# other install reviews and approves
# merge via GitHub, then both installs: git checkout main && git pull
```

### Commit rules

- All changes require a **PR with cross-review** from maintainers
- A pre-commit hook blocks direct commits to `main` — do not bypass with `--no-verify`
- Before committing, verify no personal names, company names, or numeric IDs in code
- Never force-push to `main` without explicit approval

## Key Conventions

- **Modules are files** — drop a .py in the right folder, it's available. No imports to add, no registry to update.
- **Agents are config** — an agent is a YAML block that picks: an LLM, a memory backend, tools, skills, routing.
- **Tools are decorated functions** — `@tool` on an async function. Schema auto-generated from type hints.
- **Skills are YAML** — a prompt + a list of tools. No code needed.
- **MCP auto-surfaces** — connect an MCP server, its tools appear as native tools. LLM doesn't know the difference.
- **Secrets in vault** — encrypted at rest, passphrase entered at startup, never in env vars or config files.
- **No ORMs** — raw SQL with simple helper functions
- **Type hints everywhere**, minimal abstractions

## Docs

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — Full system architecture and operations reference
- **[MIGRATION.md](MIGRATION.md)** — Migration guide from OpenClaw to T.A.R.S
- **[SCRIPTS.md](SCRIPTS.md)** — All scripts with usage
- **[skills/README.md](skills/README.md)** — Skill format reference

## Migration from legacy TARS

T.A.R.S replaces the OpenClaw dependency. See [MIGRATION.md](MIGRATION.md) for the generic migration guide.

**Design rule**: if a feature can't be migrated easily, redesign the feature, not the migration.

## Lessons from Prior Projects

Learned from building TARS (full platform) and Claude Commander (minimal bot):
- TARS: great memory system (SQLite+FTS5+embeddings) but too many services (9 Docker containers, 26 scripts)
- Commander: great simplicity (3 files) but no memory, no multi-agent, single-user only
- T.A.R.S targets the sweet spot: persistent memory + multi-agent + multi-connector in one process
