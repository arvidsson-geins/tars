# Migrating from OpenClaw to T.A.R.S

T.A.R.S replaces the entire OpenClaw stack — gateway, auth proxy, Docker sandboxes, plugin system — with a single Python process. Same agents, same memories, same Discord channels. Migration takes about 10 minutes.

## What You're Replacing

```
OpenClaw stack (multiple services)        T.A.R.S (one process)
──────────────────────────────────        ─────────────────────
openclaw-gateway.service (Node.js)   →    tars.service (Python)
openclaw-proxy.service               →    (removed — not needed)
openclaw-dashboard.service           →    (removed — not needed)
auth-proxy (credentials, HITL)       →    Built-in Fernet vault + HITL middleware
memory-api (separate service)        →    Built-in SQLite + FTS5 + embeddings
embedding-service                    →    Built-in ONNX model (BGE-small-en-v1.5)
mcp-gateway / MetaMCP               →    Built-in MCP tool server
Docker agent sandboxes               →    In-process agents (Claude Code CLI)
OpenClaw plugins (TypeScript)        →    @tool decorator (Python)
Plugin SDK + npm                     →    Drop a .py file in src/tools/
```

**What stays the same:** Discord bot tokens, channel routing, team members, agent personalities, skill definitions (converted to YAML).

## Prerequisites

- Python 3.12+
- Linux VPS (2 CPU, 4GB RAM minimum)
- Your existing Discord bot token(s)
- Your existing API keys (Tavily, Google, Trello, etc.)

## Migration Steps

### 1. Install T.A.R.S (2 minutes)

```bash
git clone https://github.com/TARS-OTHS/tars.git /opt/tars
cd /opt/tars
```

### 2. Run Setup Wizard (5 minutes)

The wizard handles everything — dependencies, vault, Discord connection, agents, team, HITL gates, systemd units:

```bash
uv run python setup.py
```

> If you don't have `uv`: `curl -LsSf https://astral.sh/uv/install.sh | sh`

The wizard will ask for:
- **Discord bot token** — reuse your existing one
- **Team members** — re-enter your owner/admin/staff users
- **Agent config** — name, model, personality, channel routing
- **HITL gates** — which tools need approval before running
- **API keys** — stored in the encrypted Fernet vault

### 3. Migrate Your Data

#### Agent identities

Copy your agent personality/instructions into the agent's `CLAUDE.md`:

```
# OpenClaw                          # T.A.R.S
agents/<name>/workspace/SOUL.md  →  agents/<name>/CLAUDE.md
```

The setup wizard creates this file for you. Paste your existing personality content in.

#### Team members

Re-enter via the setup wizard (step 7) or edit `config/team.json` directly. Same format — Discord user IDs, names, roles, tiers.

#### Skills

Convert skill definitions from your existing format to YAML:

```yaml
# skills/my_skill.yaml
name: daily_report
description: Generate a daily report
parameters:
  - name: focus
    type: string
    choices: [sales, marketing]
prompt: |
  Generate a {focus} report for today.
tools:
  - web_search
  - memory_search
```

Drop YAML files in `skills/` — auto-discovered, no registry to update.

#### Memories

T.A.R.S uses its own SQLite database with FTS5 full-text search and semantic embeddings. Memories from OpenClaw's memory API are not automatically migrated.

**Options:**
- **Start fresh** — agents rebuild memory naturally through conversation
- **Manual import** — export from your memory API and use `memory_store` tool calls to re-import key facts
- **Pin critical facts** — store essential business knowledge in `codex/` markdown files instead of memory (always available, never decays)

#### API keys and secrets

Re-enter during setup wizard, or add later:

```bash
uv run python vault-manage.py
```

### 4. Stop OpenClaw, Start T.A.R.S (1 minute)

```bash
# Stop old services
systemctl stop openclaw-gateway.service
systemctl stop openclaw-proxy.service
systemctl stop openclaw-dashboard.service
# Stop any other OpenClaw services (auth-proxy, memory-api, etc.)

# Start T.A.R.S
sudo systemctl start tars.service
journalctl -u tars -f
```

Agents should respond in Discord within seconds.

### 5. Verify

- Message your bot in a Discord channel — it should respond
- Try a slash command (e.g., `/daily-review`)
- Check tool access — memory search, web search, etc.
- Verify HITL gates — gated tools should trigger approval requests

### 6. Clean Up (optional)

Once T.A.R.S is stable:

```bash
# Disable old services permanently
systemctl disable openclaw-gateway.service
systemctl disable openclaw-proxy.service
systemctl disable openclaw-dashboard.service

# Archive old config (don't delete yet)
mv /path/to/openclaw /path/to/openclaw-archive
```

## Rollback

If something goes wrong, rollback takes 30 seconds:

```bash
systemctl stop tars.service
systemctl start openclaw-gateway.service
# Start any other OpenClaw services you stopped
```

T.A.R.S doesn't modify OpenClaw's data. Both can coexist on the same box (use different bot tokens or different Discord channels for testing).

## What You Gain

| Feature | OpenClaw | T.A.R.S |
|---------|----------|---------|
| Architecture | Multiple services, Docker containers | Single process |
| Language | TypeScript + Python | Python only |
| Secrets | External vault / env vars | Fernet-encrypted vault (AES-128-CBC) |
| Memory | Separate API service | Built-in SQLite + FTS5 + semantic embeddings |
| Tools | Plugin SDK (npm) | `@tool` decorator (drop a .py file) |
| Skills | Registry + config | YAML files (auto-discovered) |
| HITL | External proxy | Built-in Discord reaction flow |
| Multi-agent | Docker sandbox per agent | In-process, shared runtime |
| Hot reload | Restart required | Tools and skills reload automatically |
| LLM engine | LLM gateway API | Claude Code CLI (no per-token API cost) |

## What You Lose

- **Per-agent Docker isolation** — agents share a process. For hard isolation, run multiple T.A.R.S instances with separate service units.
- **OpenClaw plugin ecosystem** — replaced by Python `@tool` decorator. Simpler, but existing TypeScript plugins need rewriting.
- **Network-layer credential injection** — replaced by in-process Fernet vault. Same trust model, different implementation.

## Parallel Testing

Want to test before cutting over? Run both systems simultaneously:

1. Create a test bot at [Discord Developer Portal](https://discord.com/developers/applications)
2. Invite it to your server
3. Configure T.A.R.S to use the test bot token and route to a `#tars-test` channel
4. Run T.A.R.S alongside OpenClaw — they don't interfere
5. When satisfied, swap bot tokens and cut over

## Need Help?

- [README.md](README.md) — full feature overview and boot sequence
- [ARCHITECTURE.md](ARCHITECTURE.md) — system architecture and operations reference
- [SCRIPTS.md](SCRIPTS.md) — all scripts with usage
- [GitHub Issues](https://github.com/TARS-OTHS/tars/issues) — bug reports and questions
