#!/usr/bin/env python3
"""T.A.R.S Interactive Setup Wizard.

Complete setup from clone to running agent — dependencies, overlay,
vault, Discord, agents, systemd, everything.

Usage: uv run python setup.py
"""

import getpass
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

try:
    import yaml
except ImportError:
    print("PyYAML not found. Run: uv sync")
    sys.exit(1)

try:
    from src.vault.fernet import FernetVault
except ImportError:
    print("T.A.R.S modules not found. Run: uv sync")
    sys.exit(1)


# --- Formatting ---

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"


def banner():
    print(f"""
{BOLD}{CYAN}╔══════════════════════════════════════╗
║          T.A.R.S Setup Wizard        ║
║    The Agent Routing System          ║
╚══════════════════════════════════════╝{RESET}
""")


def header(text: str):
    print(f"\n{BOLD}{CYAN}── {text} ──{RESET}\n")


def ok(text: str):
    print(f"  {GREEN}✓{RESET} {text}")


def warn(text: str):
    print(f"  {YELLOW}!{RESET} {text}")


def err(text: str):
    print(f"  {RED}✗{RESET} {text}")


def info(text: str):
    print(f"  {DIM}{text}{RESET}")


def cc_allow_for_tier(tier: str) -> list[str]:
    """Derive Claude Code settings.json allow list from agent tier."""
    base = ["Read(*)", "Glob(*)", "Grep(*)", "mcp__tars-tools__*"]
    if tier == "privileged":
        return ["Bash(*)", "Read(*)", "Glob(*)", "Grep(*)",
                "WebSearch(*)", "WebFetch(*)", "mcp__tars-tools__*"]
    if tier == "coordinator":
        return [*base, "Bash(uv run python:*)"]
    return base


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    result = input(f"  {prompt}{suffix}: ").strip()
    return result or default


def ask_yn(prompt: str, default: bool = True) -> bool:
    yn = "[Y/n]" if default else "[y/N]"
    result = input(f"  {prompt} {yn}: ").strip().lower()
    if not result:
        return default
    return result in ("y", "yes")


def ask_secret(prompt: str) -> str:
    return getpass.getpass(f"  {prompt}: ").strip()


def ask_choice(prompt: str, options: list[str], default: str = "") -> str:
    for i, opt in enumerate(options, 1):
        marker = " (default)" if opt == default else ""
        print(f"    {i}) {opt}{marker}")
    while True:
        result = input(f"  {prompt}: ").strip()
        if not result and default:
            return default
        if result in options:
            return result
        try:
            idx = int(result) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass
        print(f"  Please enter 1-{len(options)} or a valid option.")


# --- Discord API ---

def validate_discord_token(token: str) -> dict | None:
    """Validate a Discord bot token by calling the API. Returns bot info or None."""
    try:
        req = urllib.request.Request(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None


# ==========================================================================
# Steps
# ==========================================================================

def step_dependencies(state: dict):
    header("Step 1: Dependencies")

    # Python version
    v = sys.version_info
    if v.major >= 3 and v.minor >= 12:
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        err(f"Python {v.major}.{v.minor} — 3.12+ required")
        sys.exit(1)

    # uv
    if shutil.which("uv"):
        ok("uv")
    else:
        info("uv not found — installing...")
        try:
            subprocess.run(
                ["bash", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
                check=True,
            )
            ok("uv installed")
        except subprocess.CalledProcessError:
            err("Failed to install uv. Install manually: https://docs.astral.sh/uv/")
            sys.exit(1)

    # jq (optional but useful)
    if shutil.which("jq"):
        ok("jq")
    else:
        warn("jq not found")
        if ask_yn("Install now?"):
            try:
                subprocess.run(["sudo", "apt-get", "install", "-y", "jq"], check=True)
                ok("jq installed")
            except subprocess.CalledProcessError:
                warn("Failed to install jq — install manually: sudo apt install jq")

    # Claude Code CLI
    if shutil.which("claude"):
        ok("Claude Code CLI")
    else:
        warn("Claude Code CLI not found")
        if ask_yn("Install now?"):
            try:
                subprocess.run(
                    ["bash", "-c", "curl -fsSL https://claude.ai/install.sh | sh"],
                    check=True,
                )
                if shutil.which("claude"):
                    ok("Claude Code CLI installed")
                else:
                    warn("Installed but not on PATH — restart your shell")
            except subprocess.CalledProcessError:
                err("Installation failed")
                info("Install manually: https://docs.anthropic.com/en/docs/claude-code")
        else:
            info("Install later: curl -fsSL https://claude.ai/install.sh | sh")

    # Sync Python deps
    info("Installing Python dependencies...")
    sync_script = PROJECT_ROOT / "scripts" / "sync.sh"
    if sync_script.exists():
        subprocess.run([str(sync_script)], cwd=str(PROJECT_ROOT), check=False)
    else:
        subprocess.run(["uv", "sync"], cwd=str(PROJECT_ROOT), check=False)
    ok("Dependencies installed")


def step_overlay(state: dict):
    header("Step 2: Deployment Overlay")
    info("The overlay holds your config, agent identities, service files,")
    info("and generated files — separate from the engine code.")
    info("This keeps Core clean for updates.")
    print()

    # Default: sibling directory named <base>-overlay
    parent = PROJECT_ROOT.parent
    base = PROJECT_ROOT.name
    # Strip version suffixes (tars-v2 -> tars)
    for suffix in ["-v2", "-v3", "-v4"]:
        base = base.replace(suffix, "")
    default_overlay = str(parent / f"{base}-overlay")

    overlay_input = ask("Overlay directory", default_overlay)
    overlay = Path(overlay_input).resolve()

    # Create directory structure
    for d in ["config", "agents", "systemd", "tmp/media", "tmp/docs", "tmp/scratch"]:
        (overlay / d).mkdir(parents=True, exist_ok=True)

    # .gitignore
    gitignore = overlay / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "# Runtime data\n"
            "agents/*/data/\n"
            "**/MEMORY_CONTEXT.md\n"
            "\n"
            "# Agent-generated files\n"
            "tmp/\n"
            "\n"
            "# Python\n"
            "__pycache__/\n"
            "*.pyc\n"
            "\n"
            "# Claude Code session state\n"
            "/.claude/\n"
            "\n"
            "# Secrets\n"
            "config/secrets.enc\n"
            "config/secrets.salt\n"
        )

    # Add TARS env vars to shell profile so interactive tools (settings.py etc.) work
    bashrc = Path.home() / ".bashrc"
    if bashrc.exists():
        content = bashrc.read_text()
        if "TARS_OVERLAY" not in content:
            env_block = f"\n# T.A.R.S environment\nexport TARS_OVERLAY={overlay}\n"
            if state.get("tars_oths"):
                env_block += f"export TARS_OTHS={state['tars_oths']}\n"
            with open(bashrc, "a") as f:
                f.write(env_block)
            ok("Added T.A.R.S env vars to ~/.bashrc")
        else:
            info("TARS_OVERLAY already in ~/.bashrc")

    ok(f"Overlay: {overlay}")
    state["overlay"] = overlay


def step_hooks(state: dict):
    header("Step 3: Git Hooks")

    hooks_src = PROJECT_ROOT / "hooks"
    hooks_dst = PROJECT_ROOT / ".git" / "hooks"

    if not hooks_src.exists() or not hooks_dst.exists():
        info("No hooks to install — skipping")
        return

    installed = 0
    for hook in sorted(hooks_src.iterdir()):
        if not hook.is_file():
            continue
        target = hooks_dst / hook.name
        if target.exists():
            if hook.read_text() != target.read_text():
                if ask_yn(f"Hook '{hook.name}' differs from shipped version. Update?"):
                    shutil.copy2(hook, target)
                    target.chmod(target.stat().st_mode | stat.S_IEXEC)
                    ok(f"Updated hook: {hook.name}")
                    installed += 1
                else:
                    info(f"Hook kept: {hook.name}")
            else:
                info(f"Hook up to date: {hook.name}")
        else:
            shutil.copy2(hook, target)
            target.chmod(target.stat().st_mode | stat.S_IEXEC)
            ok(f"Installed hook: {hook.name}")
            installed += 1

    if installed == 0:
        ok("All hooks up to date")


def step_modules(state: dict):
    header("Step 4: Deployment Pattern & Extension Modules")

    info("T.A.R.S supports two deployment patterns:")
    print()
    print(f"  {BOLD}[1] 2-layer{RESET} — Core + Overlay  (single deployment, recommended)")
    info("      All tools/skills live in Core. Overlay holds your config, agents, data.")
    print()
    print(f"  {BOLD}[2] 3-layer{RESET} — Core + Extension Modules + Overlay")
    info("      Use when one extension module set feeds multiple overlays")
    info("      (e.g. shared domain tools across staging/prod or client installs).")
    print()

    # Show core tools
    print(f"  {BOLD}Core tools (always included):{RESET}")
    tools_dir = PROJECT_ROOT / "src" / "tools"
    if tools_dir.exists():
        for py in sorted(tools_dir.glob("*.py")):
            if py.name.startswith("__"):
                continue
            print(f"    {GREEN}✓{RESET} {py.stem}")

    # Show core skills
    skills_dir = PROJECT_ROOT / "skills"
    core_skills = []
    if skills_dir.exists():
        for yml in sorted(list(skills_dir.glob("*.yaml")) + list(skills_dir.glob("*.yml"))):
            core_skills.append(yml.stem)
    if core_skills:
        print(f"\n  {BOLD}Core skills (always included):{RESET}")
        for s in core_skills:
            print(f"    {GREEN}✓{RESET} {s}")

    # Scan for Layer 2 modules up front so we can default the pattern intelligently
    oths_root = None
    for candidate in [PROJECT_ROOT.parent / "tars-oths", PROJECT_ROOT.parent / "oths"]:
        if candidate.is_dir():
            oths_root = candidate
            break

    # Default to 2-layer unless extension modules are present on disk
    default_pattern = "2" if oths_root is None else "3"
    print()
    pattern = ask(f"Deployment pattern [1=2-layer, 2=3-layer]", default_pattern)
    state["deployment_pattern"] = "2-layer" if pattern.strip() in ("1", "2-layer") else "3-layer"

    if state["deployment_pattern"] == "2-layer":
        print()
        ok("2-layer deployment: Core + Overlay only")
        state["tars_oths"] = ""
        state["selected_modules"] = []
        return

    if not oths_root:
        print()
        oths_input = ask("Path to extension modules (leave blank to skip)")
        if oths_input:
            p = Path(oths_input).resolve()
            if p.is_dir():
                oths_root = p

    if not oths_root or not oths_root.is_dir():
        print()
        warn("3-layer selected but no extension module directory found — falling back to 2-layer")
        state["deployment_pattern"] = "2-layer"
        state["tars_oths"] = ""
        state["selected_modules"] = []
        return

    # List available modules
    available = []
    print(f"\n  {BOLD}Available extension modules:{RESET}")
    idx = 0
    for mod_dir in sorted(oths_root.iterdir()):
        if not mod_dir.is_dir():
            continue
        idx += 1

        mod_tools = []
        if (mod_dir / "tools").exists():
            mod_tools = [p.stem for p in (mod_dir / "tools").glob("*.py") if not p.name.startswith("__")]

        mod_skills = []
        skills_p = mod_dir / "skills"
        if skills_p.exists():
            mod_skills = [p.stem for p in sorted(list(skills_p.glob("*.yaml")) + list(skills_p.glob("*.yml")))]

        available.append(mod_dir.name)
        print(f"\n    {BOLD}[{idx}] {mod_dir.name}{RESET}")
        if mod_tools:
            print(f"        Tools:  {' '.join(mod_tools)}")
        if mod_skills:
            print(f"        Skills: {' '.join(mod_skills)}")

    if not available:
        ok("No extension modules found")
        state["tars_oths"] = ""
        state["selected_modules"] = []
        return

    print()
    info("Enter module numbers (comma-separated), 'all', or leave blank to skip.")
    choice = ask("Modules", "")

    selected = []
    if choice.lower() == "all":
        selected = available[:]
    elif choice:
        for c in choice.split(","):
            c = c.strip()
            try:
                i = int(c) - 1
                if 0 <= i < len(available):
                    selected.append(available[i])
            except ValueError:
                pass

    if selected:
        oths_paths = [str(oths_root / mod) for mod in selected]
        state["tars_oths"] = ":".join(oths_paths)
        state["tars_oths_root"] = str(oths_root)
        state["selected_modules"] = selected
        print()
        for mod in selected:
            ok(f"Module: {mod}")
    else:
        state["tars_oths"] = ""
        state["selected_modules"] = []
        ok("No extension modules selected (core tools only)")


def step_vault(state: dict):
    header("Step 5: Vault Setup")
    info("The vault encrypts your API tokens and secrets at rest.")
    info("You'll set a passphrase that's needed to unlock the vault at startup.")

    overlay: Path = state["overlay"]
    vault_path = overlay / "config" / "secrets.enc"
    key_file = Path.home() / ".config" / "tars-vault-key"
    vault = FernetVault(str(vault_path))

    if vault_path.exists():
        info("Existing vault found.")
        # Try key file first
        if key_file.exists():
            try:
                passphrase = key_file.read_text().strip()
                vault.unlock(passphrase)
                ok(f"Vault unlocked ({len(vault.list_keys())} secrets)")
                state["vault"] = vault
                state["vault_existed"] = True
                return
            except ValueError:
                warn("Key file passphrase didn't work.")

        for attempt in range(3):
            passphrase = ask_secret("Enter vault passphrase")
            try:
                vault.unlock(passphrase)
                ok(f"Vault unlocked ({len(vault.list_keys())} secrets)")
                if ask_yn("Save passphrase to key file for auto-unlock?"):
                    key_file.parent.mkdir(parents=True, exist_ok=True)
                    key_file.write_text(passphrase + "\n")
                    key_file.chmod(0o600)
                    ok(f"Key file saved to {key_file}")
                state["vault"] = vault
                state["vault_existed"] = True
                return
            except ValueError:
                err("Wrong passphrase.")

        err("Failed to unlock vault after 3 attempts.")
        sys.exit(1)

    else:
        while True:
            passphrase = ask_secret("Create a vault passphrase")
            if len(passphrase) < 4:
                err("Passphrase must be at least 4 characters.")
                continue
            confirm = ask_secret("Confirm passphrase")
            if passphrase != confirm:
                err("Passphrases don't match.")
                continue
            break

        vault.unlock(passphrase)
        # Persist empty vault to create the file
        vault.set("_setup", "true")
        vault.delete("_setup")
        ok("Vault created")

        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(passphrase + "\n")
        key_file.chmod(0o600)
        ok(f"Key file saved to {key_file}")

        state["vault"] = vault
        state["vault_existed"] = False


def step_discord(state: dict):
    header("Step 6: Discord Bot")
    info("T.A.R.S connects to Discord via a bot account.")
    info("Create one at: https://discord.com/developers/applications")
    info("Required intents: Message Content, Server Members, Guild Messages")
    print()

    if not ask_yn("Do you have a Discord bot token ready?"):
        warn("Skipped — you'll need to add the token later via vault-manage.py")
        state["discord_skip"] = True
        return

    vault: FernetVault = state["vault"]

    # Token
    while True:
        token = ask_secret("Bot token")
        if not token:
            warn("Skipped Discord setup.")
            state["discord_skip"] = True
            return

        info("Validating token...")
        bot_info = validate_discord_token(token)
        if bot_info:
            ok(f"Bot verified: {bot_info.get('username', '?')}#{bot_info.get('discriminator', '0')}")
            break
        else:
            if ask_yn("Token validation failed. Use it anyway?", default=False):
                break
            continue

    vault.set("discord-token", token)
    ok("Token stored in vault")

    # Guild ID
    guild_id = ask("Server (guild) ID")
    state["guild_id"] = guild_id

    # User ID
    user_id = ask("Your Discord user ID (for admin access)")
    state["owner_discord_id"] = user_id
    state["discord_skip"] = False

    # Bot account name
    bot_name = ask("Internal name for this bot account", "main")
    state["bot_name"] = bot_name
    state["bot_token_key"] = "discord-token"


def step_team(state: dict):
    header("Step 7: Team — Owner Profile")
    info("Set up your profile so the system knows who you are.")
    print()

    name = ask("Your name")
    role = ask("Your role", "Founder")
    timezone = ask("Timezone", "UTC")
    context = ask("Short context (what you do)")

    discord_id = state.get("owner_discord_id", "")
    if not discord_id:
        discord_id = ask("Discord user ID")

    state["owner"] = {
        "id": name.lower().replace(" ", "-"),
        "name": name,
        "type": "human",
        "access": "owner",
        "role": role,
        "responsibilities": ["Everything"],
        "context": context,
        "contact": {"discord": discord_id},
        "preferences": {"timezone": timezone},
    }
    state["team_members"] = [state["owner"]]
    ok(f"Owner profile: {name} ({role})")


def step_agent(state: dict):
    header("Step 8: First Agent")
    info("Configure your primary AI agent.")
    print()

    agent_name = ask("Agent internal name (lowercase, no spaces)", "main")
    display_name = ask("Display name (shown in Discord)", agent_name.upper())
    description = ask("One-line description", "Primary agent")
    model = ask_choice("LLM model", ["sonnet", "opus"], default="sonnet")

    state["agent"] = {
        "name": agent_name,
        "display_name": display_name,
        "description": description,
        "model": model,
    }

    # Agent personality
    print()
    info("Optionally set a personality for the agent's CLAUDE.md.")
    personality = ask("Personality (e.g., 'concise and direct', 'friendly and detailed')", "concise and direct")
    state["agent"]["personality"] = personality

    # Routing
    print()
    bot_name = state.get("bot_name", "main")
    state["agent"]["routing"] = _ask_routing(bot_name)

    ok(f"Agent: {display_name} ({model})")


def step_hitl(state: dict):
    header("Step 9: Human-in-the-Loop Approval")
    info("Some tools require human approval before executing.")
    info("Approvals are sent to a Discord channel as reaction prompts.")
    print()

    if state.get("discord_skip"):
        warn("Discord not configured — using defaults for HITL.")
        state["hitl"] = {
            "channel": "",
            "approvers": [],
            "gated_tools": ["send_email", "install_mcp"],
        }
        return

    channel_id = ask("Discord channel ID for approvals (ops/alerts channel)")
    approvers = [state.get("owner_discord_id", "")]
    approvers = [a for a in approvers if a]

    more = ask_yn("Add more approvers?", default=False)
    while more:
        uid = ask("Discord user ID")
        if uid:
            approvers.append(uid)
        more = ask_yn("Add another?", default=False)

    print()
    info("Default gated tools: send_email, install_mcp")
    gated = ["send_email", "install_mcp"]
    if ask_yn("Add more gated tools?", default=False):
        while True:
            tool = ask("Tool name (empty to stop)")
            if not tool:
                break
            gated.append(tool)

    state["hitl"] = {
        "channel": channel_id,
        "approvers": approvers,
        "gated_tools": gated,
    }
    ok(f"HITL: {len(approvers)} approver(s), {len(gated)} gated tool(s)")


def step_compression(state: dict):
    header("Step 10: Context Compression (optional)")
    info("T.A.R.S can compress verbose context files (codex docs, skill prompts)")
    info("to reduce input tokens per agent message. Rule-based, no LLM calls.")
    info("CLAUDE.md files are excluded — they're carefully tuned prompts.")
    print()

    compression = {"enabled": False, "level": "standard"}

    if ask_yn("Enable context compression?", default=False):
        compression["enabled"] = True
        compression["level"] = ask_choice(
            "Compression level",
            ["lite", "standard"],
            default="standard",
        )
        ok(f"Context compression enabled ({compression['level']})")

        print()
        info("Memory recall compression strips filler from memories before")
        info("injecting them into agent context. Same rules, applied at runtime.")
        print()
        if ask_yn("Enable memory recall compression?", default=False):
            compression["memory_recall"] = True
            ok("Memory recall compression enabled")
        else:
            compression["memory_recall"] = False
    else:
        info("Skipped — can be enabled later in config.yaml")

    state["compression"] = compression


def step_generate(state: dict):
    header("Step 11: Generating Config Files")

    overlay: Path = state["overlay"]
    agent = state["agent"]
    owner = state["owner"]
    hitl = state["hitl"]
    bot_name = state.get("bot_name", "main")
    guild_id = state.get("guild_id", "YOUR_GUILD_ID")
    owner_discord = owner["contact"]["discord"] or "YOUR_DISCORD_USER_ID"
    agent_dir = overlay / "agents" / agent["name"]

    # --- config.yaml ---
    config = {
        "tars": {"name": "T.A.R.S", "log_level": "info", "data_dir": "./data"},
        "connectors": {
            "discord": {
                "enabled": True,
                "accounts": {
                    bot_name: {"token_key": state.get("bot_token_key", "discord-token")},
                },
            },
            "telegram": {"enabled": False},
            "http": {"enabled": False, "port": 8080},
        },
        "defaults": {
            "llm": {"provider": "claude_code", "model": agent["model"], "max_tokens": 4096},
            "session": {"max_history": 50, "summarize_after": 30},
            "memory": {"backend": "sqlite", "semantic_search": False, "decay_enabled": False, "max_results": 10},
        },
        "security": {
            "hitl": {
                "connector": "discord",
                "channel": hitl["channel"],
                "approvers": hitl["approvers"],
                "timeout": 1800,
                "fail_mode": "closed",
                "poll_interval": 3,
                "gated_tools": hitl["gated_tools"],
            },
            "rate_limits": {"mode": "log", "defaults": {"max_per_hour": 100}},
            "compression": state.get("compression", {"enabled": False, "level": "standard"}),
        },
        "admin_users": {"discord": [owner_discord]},
    }
    _write_yaml(overlay / "config" / "config.yaml", config, state)

    # --- agents.yaml ---
    agents = {
        "agents": {
            agent["name"]: {
                "display_name": agent["display_name"],
                "description": agent["description"],
                "project_dir": str(agent_dir),
                "llm": {"provider": "claude_code", "model": agent["model"]},
                "tools": "all",
                "skills": "all",
                "disallow_builtins": ["Edit", "Write", "Bash", "MultiEdit"],
                "routing": agent["routing"],
            }
        }
    }
    _write_yaml(overlay / "config" / "agents.yaml", agents, state)

    # --- team.json ---
    team = {"humans": state["team_members"], "agents": []}
    _write_json(overlay / "config" / "team.json", team, state)

    # --- mcp.yaml ---
    mcp_yaml_path = overlay / "config" / "mcp.yaml"
    if not mcp_yaml_path.exists():
        mcp_config = {
            "servers": {
                "tars-tools": {
                    "transport": "stdio",
                    "command": str(PROJECT_ROOT / ".venv" / "bin" / "python3"),
                    "args": ["-m", "src.mcp_server"],
                    "cwd": str(PROJECT_ROOT),
                }
            }
        }
        mcp_yaml_path.write_text(yaml.dump(mcp_config, default_flow_style=False, sort_keys=False))
        ok(f"Created {_display(mcp_yaml_path)}")

    # --- Agent directory ---
    agent_dir.mkdir(parents=True, exist_ok=True)

    # CLAUDE.md
    claude_md = f"""# {agent['display_name']}

## Identity

You are **{agent['display_name']}**. {agent['description']}.

## Guidelines

- Be {agent['personality']}
- Search memory before asking the user for context you might already have
- Remember important things from conversations by storing them to memory
- When handling tasks, break them down and track progress

## File System

You run inside a sandboxed T.A.R.S instance. Your project directory is in the **overlay**, separate from the engine code.

- **Your directory:** `{agent_dir}/` — your CLAUDE.md, config, and data live here
- **Generated files:** use `$TARS_TMP` (media, docs, scratch) for any files you create
- **NEVER** `git add`, `git commit`, or `git push` in the T.A.R.S core engine directory — it is the framework, not your workspace. Agent configs, custom files, and deployment data belong in the overlay.

## Memory System

Use your MCP tools for memory — do NOT use curl or HTTP calls.

- `memory_search` — keyword/FTS5 search
- `memory_semantic_search` — embedding-based conceptual search
- `memory_store` — save important information
- `memory_forget` — remove a memory by ID

## Team

The team roster is at `config/team.json`. User context is injected before each message so you know who you're talking to.
"""
    _write_file(agent_dir / "CLAUDE.md", claude_md, state)

    # .mcp.json
    mcp_env = {
        "TARS_PROFILE": "${TARS_PROFILE:-}",
        "TARS_PROJECT_DIR": str(agent_dir),
        "TARS_OVERLAY": str(overlay),
    }
    if state.get("tars_oths"):
        mcp_env["TARS_OTHS"] = state["tars_oths"]
    mcp_json = {
        "mcpServers": {
            "tars-tools": {
                "command": str(PROJECT_ROOT / ".venv" / "bin" / "python3"),
                "args": ["-m", "src.mcp_server"],
                "cwd": str(PROJECT_ROOT),
                "env": mcp_env,
            }
        }
    }
    mcp_json_path = agent_dir / ".mcp.json"
    if not mcp_json_path.exists():
        mcp_json_path.write_text(json.dumps(mcp_json, indent=2) + "\n")
        ok(f"Created {_display(mcp_json_path)}")

    # .claude/settings.json
    claude_dir = agent_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    allow_list = cc_allow_for_tier("coordinator")
    settings = {
        "permissions": {
            "allow": list(allow_list),
            "deny": [],
        },
        "env": {
            "PATH": f"{PROJECT_ROOT}/.venv/bin:{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        },
    }
    settings_path = claude_dir / "settings.json"
    if not settings_path.exists():
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        ok(f"Created {_display(settings_path)}")

    ok(f"Agent directory: {_display(agent_dir)}")

    # Neutralise Core remote
    _neutralise_core_remote()

    # Neutralise Core remote — prevent accidental pushes to the upstream repo
    _neutralise_core_remote(project_root)


def step_ops_instance(state: dict):
    header("Step 12: Privileged Ops Instance (optional)")
    info("T.A.R.S supports a dual-instance deployment pattern:")
    info("  Main instance  — sandboxed, runs your user-facing agents")
    info("  Ops instance   — unsandboxed, single privileged agent for dev/ops")
    info("")
    info("The ops agent can edit code, restart services, and run deploys.")
    info("Only the system owner should have access to it.")
    info("See ARCHITECTURE.md for full details.")
    print()

    if not ask_yn("Set up a privileged ops instance?", default=False):
        info("Skipped — you can set this up later via scripts/settings.py")
        return

    overlay: Path = state["overlay"]
    vault: FernetVault = state["vault"]

    agent_name = ask("Ops agent internal name", "engineer")
    display_name = ask("Display name", agent_name.capitalize() + " Bot")
    description = ask("Description", "Privileged ops agent — unsandboxed, owner-only")
    model = ask_choice("Model", ["sonnet", "opus"], default="opus")

    # Bot account
    print()
    info("The ops agent needs its own Discord bot account.")
    bot_name = ask("Bot account name", agent_name)
    token = ask_secret(f"Discord bot token for '{bot_name}' (empty to skip)")

    if token:
        info("Validating token...")
        bot_info = validate_discord_token(token)
        if bot_info:
            ok(f"Bot verified: {bot_info.get('username', '?')}")
        else:
            if not ask_yn("Validation failed. Store anyway?", default=False):
                token = None

    if token:
        vault_key = f"discord-{bot_name}"
        vault.set(vault_key, token)
        ok(f"Token stored as '{vault_key}'")
        state.setdefault("extra_bots", {})[bot_name] = vault_key
    else:
        warn("No token — add it later via vault-manage.py")

    # Routing
    routing = _ask_routing(bot_name, default_mentions=True)

    agent_dir = overlay / "agents" / agent_name

    # Write agents.rescue.yaml
    rescue_agents = {
        "agents": {
            agent_name: {
                "display_name": display_name,
                "description": description,
                "project_dir": str(agent_dir),
                "privileged": True,
                "llm": {"provider": "claude_code", "model": model},
                "tools": "all",
                "skills": "all",
                "routing": routing,
            }
        }
    }
    rescue_path = overlay / "config" / "agents.rescue.yaml"
    rescue_path.write_text(yaml.dump(rescue_agents, default_flow_style=False, sort_keys=False))
    ok(f"Created {_display(rescue_path)}")

    # Add bot account to main config.yaml
    if token:
        config_path = overlay / "config" / "config.yaml"
        if config_path.exists():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            cfg.setdefault("connectors", {}).setdefault("discord", {}).setdefault("accounts", {})[bot_name] = {"token_key": f"discord-{bot_name}"}
            config_path.write_text(yaml.dump(cfg, default_flow_style=False, sort_keys=False))
            ok(f"Added bot '{bot_name}' to config.yaml")

    # Create agent directory
    agent_dir.mkdir(parents=True, exist_ok=True)

    template_path = PROJECT_ROOT / "config" / "templates" / "rescue-claude.md"
    if template_path.exists():
        claude_md = template_path.read_text().format(display_name=display_name, agent_dir=agent_dir)
    else:
        claude_md = f"# {display_name}\n\nYou are {display_name} — the unsandboxed ops and dev agent.\n"
        warn("Template not found at config/templates/rescue-claude.md — using minimal fallback")
    claude_md_path = agent_dir / "CLAUDE.md"
    if not claude_md_path.exists():
        claude_md_path.write_text(claude_md)
        ok(f"Created {_display(claude_md_path)}")

    # .mcp.json
    mcp_env = {
        "TARS_PROFILE": "rescue",
        "TARS_PROJECT_DIR": str(agent_dir),
        "TARS_OVERLAY": str(overlay),
    }
    if state.get("tars_oths"):
        mcp_env["TARS_OTHS"] = state["tars_oths"]
    mcp_json = {
        "mcpServers": {
            "tars-tools": {
                "command": str(PROJECT_ROOT / ".venv" / "bin" / "python3"),
                "args": ["-m", "src.mcp_server"],
                "cwd": str(PROJECT_ROOT),
                "env": mcp_env,
            }
        }
    }
    mcp_path = agent_dir / ".mcp.json"
    if not mcp_path.exists():
        mcp_path.write_text(json.dumps(mcp_json, indent=2) + "\n")
        ok(f"Created {_display(mcp_path)}")

    # .claude/settings.json — ops agents get privileged access
    claude_dir = agent_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    if not settings_path.exists():
        settings = {
            "permissions": {
                "allow": cc_allow_for_tier("privileged"),
                "deny": [],
            },
            "env": {
                "PATH": f"{PROJECT_ROOT}/.venv/bin:{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin",
            },
        }
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        ok(f"Created {_display(settings_path)}")

    print()
    ok(f"Ops instance configured — agent: {display_name}")
    info("Service setup will be offered in a later step.")


def step_extras(state: dict):
    header("Step 13: Additional Setup (optional)")

    while True:
        print()
        print("  What would you like to add?")
        print("    1) Another team member")
        print("    2) Another agent")
        print("    3) Another Discord bot")
        print("    4) Done — finish setup")
        print()
        choice = ask("Choice", "4")

        if choice in ("4", "done", "d", ""):
            break
        elif choice in ("1", "team"):
            _add_team_member(state)
        elif choice in ("2", "agent"):
            _add_agent(state)
        elif choice in ("3", "bot"):
            _add_bot(state)


def step_systemd(state: dict):
    header("Step 14: Systemd Services")

    if not shutil.which("systemctl"):
        info("systemd not available — skipping")
        return

    overlay: Path = state["overlay"]
    has_sudo = _check_sudo()

    if not has_sudo:
        warn("sudo not available — service files will be generated but not installed")
        info("You'll need to symlink them to /etc/systemd/system/ manually")
        print()

    # --- Generate timer unit files (required for memory decay, health, integrity) ---
    timers_dir = PROJECT_ROOT / "config" / "timers"
    if timers_dir.exists():
        info("Generating maintenance timer units...")
        for f in sorted(timers_dir.iterdir()):
            if not f.is_file():
                continue

            # Templates use @TARS_HOME@ as placeholder for the install path.
            # Fall back to the legacy /opt/tars path regex for any template
            # that hasn't been migrated yet (harmless on modern templates).
            raw = f.read_text()
            content = raw.replace("@TARS_HOME@", str(PROJECT_ROOT))
            content = re.sub(r"/opt/tars(?=/|$)", str(PROJECT_ROOT), content)

            # Inject TARS_OVERLAY into service files (after TARS_HOME line)
            if f.name.endswith(".service"):
                lines = content.split("\n")
                new_lines = []
                for line in lines:
                    new_lines.append(line)
                    if line.startswith("Environment=TARS_HOME="):
                        new_lines.append(f"Environment=TARS_OVERLAY={overlay}")
                content = "\n".join(new_lines)

            out_path = overlay / "systemd" / f.name
            out_path.write_text(content)

        ok(f"Timer units written to {_display(overlay / 'systemd')}")

    # --- Generate main service unit file ---
    install_service = False
    print()
    if ask_yn("Install systemd service? (auto-start on boot)"):
        install_service = True
        uv_path = shutil.which("uv") or f"{Path.home()}/.local/bin/uv"
        template_path = PROJECT_ROOT / "config" / "tars.service"

        if not template_path.exists():
            err(f"Service template not found: {template_path}")
            return

        service = template_path.read_text()

        # Templates use @TARS_HOME@; legacy /opt/tars regex kept as fallback.
        service = service.replace("@TARS_HOME@", str(PROJECT_ROOT))
        service = re.sub(r"/opt/tars(?=/|$)", str(PROJECT_ROOT), service)
        service = service.replace(
            "ExecStart=/usr/local/bin/uv",
            f"ExecStart={uv_path}",
        )

        # Inject environment variables after the PATH line
        env_lines = [f"Environment=TARS_OVERLAY={overlay}"]
        if state.get("tars_oths"):
            env_lines.append(f"Environment=TARS_OTHS={state['tars_oths']}")

        lines = service.split("\n")
        new_lines = []
        tars_home = Path.home()
        for line in lines:
            if line.startswith("Environment=PATH="):
                new_lines.append(line)
                new_lines.extend(env_lines)
            elif line.startswith("ReadWritePaths="):
                # Replace with overlay-aware paths regardless of template formatting
                new_lines.append(
                    f"ReadWritePaths={PROJECT_ROOT}/data {overlay}/agents {overlay}/config "
                    f"{overlay}/data {overlay}/tmp /tmp {tars_home}/.cache {tars_home}/.claude"
                )
            elif line.startswith("ReadOnlyPaths="):
                new_lines.append(f"ReadOnlyPaths={PROJECT_ROOT} {overlay}")
            else:
                new_lines.append(line)
        service = "\n".join(new_lines)

        out_path = overlay / "systemd" / "tars.service"
        out_path.write_text(service)
        ok(f"Service file: {_display(out_path)}")

    # --- Install into systemd (bash script handles symlinks, reload, enable) ---
    install_script = PROJECT_ROOT / "scripts" / "install-systemd.sh"
    if has_sudo and install_script.exists():
        info("Installing units into systemd...")
        cmd = ["sudo", "-n", "bash", str(install_script), str(overlay)]
        if install_service:
            cmd.append("--enable-service")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            if result.stdout.strip():
                for line in result.stdout.strip().split("\n"):
                    info(line.strip())
            ok("Systemd units installed and timers enabled")
            if install_service:
                ok("Service enabled — start with: sudo systemctl start tars")
        else:
            err("Systemd installation failed:")
            if result.stderr.strip():
                for line in result.stderr.strip().split("\n"):
                    err(f"  {line}")
    elif not has_sudo:
        warn("No sudo — unit files generated but not installed")
        info(f"Install manually: sudo bash {install_script} {overlay}")
    else:
        warn(f"Install script not found: {install_script}")


def step_browser(state: dict):
    header("Step 15: Browser Tool (optional)")
    info("The browse_url tool uses a headless Chromium browser via Playwright")
    info("to fetch JavaScript-rendered pages. The Python package is already")
    info("installed; Chromium itself is a separate ~170MB download.")
    print()

    if not ask_yn("Install Chromium for the browse_url tool now?", default=True):
        warn("Skipped. browse_url will return an error until you run:")
        info("    uv run playwright install chromium")
        return

    cmd = ["uv", "run", "playwright", "install", "chromium"]
    try:
        print()
        info("Running: " + " ".join(cmd))
        result = subprocess.run(cmd, check=False)
        if result.returncode == 0:
            ok("Chromium installed — browse_url tool is ready.")
        else:
            err("Chromium install failed (exit " + str(result.returncode) + ").")
            info("Run manually later: uv run playwright install chromium")
    except FileNotFoundError:
        err("'uv' not found on PATH.")
        info("Run manually later: uv run playwright install chromium")


def step_final_sync(state: dict):
    """Re-run sync now that TARS_OTHS and TARS_OVERLAY are configured."""
    tars_oths = state.get("tars_oths", "")
    overlay = state.get("overlay")
    if not overlay and not tars_oths:
        return  # nothing new to sync

    header("Final Dependency Sync")
    info("Re-syncing with module and overlay configuration...")

    env = {**os.environ}
    if tars_oths:
        env["TARS_OTHS"] = tars_oths
    if overlay:
        env["TARS_OVERLAY"] = str(overlay)

    sync_script = PROJECT_ROOT / "scripts" / "sync.sh"
    if sync_script.exists():
        subprocess.run([str(sync_script)], cwd=str(PROJECT_ROOT), env=env, check=False)
    ok("Dependencies synced with module configuration")


def step_summary(state: dict):
    header("Setup Complete")

    overlay: Path = state["overlay"]
    agent = state["agent"]
    vault: FernetVault = state["vault"]

    print(f"  {BOLD}Core:{RESET}       {PROJECT_ROOT}")
    print(f"  {BOLD}Overlay:{RESET}    {overlay}")
    print(f"  {BOLD}Vault:{RESET}      {len(vault.list_keys())} secret(s)")
    print(f"  {BOLD}Team:{RESET}       {len(state['team_members'])} member(s)")
    print(f"  {BOLD}Agent:{RESET}      {agent['display_name']} ({agent['model']})")

    if state.get("selected_modules"):
        print(f"  {BOLD}Modules:{RESET}    {', '.join(state['selected_modules'])}")

    if state.get("discord_skip"):
        print(f"  {BOLD}Discord:{RESET}    {YELLOW}not configured{RESET} — add token via vault-manage.py")
    else:
        print(f"  {BOLD}Discord:{RESET}    connected (guild: {state.get('guild_id', '?')})")

    agent_dir = overlay / "agents" / agent["name"]
    print(f"""
  {BOLD}Generated files:{RESET}
    {overlay}/config/config.yaml
    {overlay}/config/agents.yaml
    {overlay}/config/team.json
    {agent_dir}/CLAUDE.md
    {agent_dir}/.mcp.json

  {BOLD}Next steps:{RESET}
    1. Review and customise {agent_dir}/CLAUDE.md
    2. Start T.A.R.S:  {CYAN}uv run python -m src.main{RESET}
    3. Or:             {CYAN}sudo systemctl start tars{RESET}
    4. Manage secrets:  {CYAN}uv run python vault-manage.py{RESET}
    5. Settings:        {CYAN}uv run python scripts/settings.py{RESET}

  {DIM}Config files are in the overlay — the engine stays clean for updates.{RESET}
""")


# ==========================================================================
# Private helpers
# ==========================================================================

def _add_team_member(state: dict):
    overlay: Path = state["overlay"]
    name = ask("Name")
    role = ask("Role")
    discord_id = ask("Discord user ID")
    access = ask_choice("Access level", ["owner", "admin", "staff", "viewer"], default="staff")

    member = {
        "id": name.lower().replace(" ", "-"),
        "name": name,
        "type": "human",
        "access": access,
        "role": role,
        "responsibilities": [],
        "context": "",
        "contact": {"discord": discord_id},
        "preferences": {"timezone": "UTC"},
    }
    state["team_members"].append(member)

    # Update team.json
    team = {"humans": state["team_members"], "agents": []}
    team_path = overlay / "config" / "team.json"
    team_path.write_text(json.dumps(team, indent=2) + "\n")
    ok(f"Added {name} ({role}, {access})")


def _add_agent(state: dict):
    overlay: Path = state["overlay"]

    agent_name = ask("Agent internal name")
    display_name = ask("Display name", agent_name.upper())
    description = ask("Description")
    model = ask_choice("Model", ["sonnet", "opus"], default="sonnet")
    bot_account = ask("Bot account name (from existing bots)", state.get("bot_name", "main"))

    # Tier selection drives permissions
    print()
    info("Tier determines default permissions:")
    info("  privileged — full file/shell access (ops/dev agents)")
    info("  coordinator — read builtins + restricted shell + MCP tools")
    info("  assistant — read builtins + MCP tools only")
    tier = ask_choice("Tier", ["privileged", "coordinator", "assistant"], default="assistant")

    privileged = tier == "privileged"
    disallow = [] if privileged else ["Edit", "Write", "Bash", "MultiEdit"]

    # Load current agents.yaml and add
    agents_path = overlay / "config" / "agents.yaml"
    with open(agents_path) as f:
        agents_cfg = yaml.safe_load(f) or {}

    agent_dir = overlay / "agents" / agent_name

    # Routing
    routing = _ask_routing(bot_account)

    agent_entry = {
        "display_name": display_name,
        "description": description,
        "project_dir": str(agent_dir),
        "llm": {"provider": "claude_code", "model": model},
        "tools": "all",
        "skills": "all",
        "routing": routing,
    }
    if disallow:
        agent_entry["disallow_builtins"] = disallow
    if privileged:
        agent_entry["privileged"] = True

    agents_cfg.setdefault("agents", {})[agent_name] = agent_entry

    agents_path.write_text(yaml.dump(agents_cfg, default_flow_style=False, sort_keys=False))

    # Create agent directory
    agent_dir.mkdir(parents=True, exist_ok=True)

    claude_md = f"""# {display_name}

## Identity

You are **{display_name}**. {description}.

## Guidelines

- Search memory before asking the user for context you might already have
- Remember important things from conversations by storing them to memory
- When handling tasks, break them down and track progress

## File System

You run inside a sandboxed T.A.R.S instance. Your project directory is in the **overlay**, separate from the engine code.

- **Your directory:** `{agent_dir}/` — your CLAUDE.md, config, and data live here
- **Generated files:** use `$TARS_TMP` (media, docs, scratch) for any files you create
- **NEVER** `git add`, `git commit`, or `git push` in the T.A.R.S core engine directory — it is the framework, not your workspace. Agent configs, custom files, and deployment data belong in the overlay.

## Memory System

Use your MCP tools for memory — do NOT use curl or HTTP calls.

- `memory_search` — keyword/FTS5 search
- `memory_semantic_search` — embedding-based conceptual search
- `memory_store` — save important information
- `memory_forget` — remove a memory by ID
"""
    claude_md_path = agent_dir / "CLAUDE.md"
    if not claude_md_path.exists():
        claude_md_path.write_text(claude_md)
        ok(f"Created {_display(claude_md_path, overlay)}")
    else:
        info(f"CLAUDE.md already exists: {_display(claude_md_path, overlay)} (skipped)")

    # .mcp.json
    mcp_env = {
        "TARS_PROFILE": "${TARS_PROFILE:-}",
        "TARS_PROJECT_DIR": str(agent_dir),
        "TARS_OVERLAY": str(overlay),
    }
    if state.get("tars_oths"):
        mcp_env["TARS_OTHS"] = state["tars_oths"]
    mcp_json = {
        "mcpServers": {
            "tars-tools": {
                "command": str(PROJECT_ROOT / ".venv" / "bin" / "python3"),
                "args": ["-m", "src.mcp_server"],
                "cwd": str(PROJECT_ROOT),
                "env": mcp_env,
            }
        }
    }
    mcp_json_path = agent_dir / ".mcp.json"
    if not mcp_json_path.exists():
        mcp_json_path.write_text(json.dumps(mcp_json, indent=2) + "\n")
        ok(f"Created {_display(mcp_json_path, overlay)}")
    else:
        info(f".mcp.json already exists (skipped)")

    claude_dir = agent_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    allow_list = cc_allow_for_tier(tier)
    settings = {"permissions": {"allow": list(allow_list), "deny": []}}
    settings_path = claude_dir / "settings.json"
    if not settings_path.exists():
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        ok(f"Created {_display(settings_path, overlay)}")
    else:
        info(f"settings.json already exists (skipped)")

    ok(f"Agent '{display_name}' added")


def _add_bot(state: dict):
    overlay: Path = state["overlay"]
    vault: FernetVault = state["vault"]
    bot_name = ask("Bot account name (internal)")
    token = ask_secret("Bot token")

    vault_key = f"discord-{bot_name}"
    vault.set(vault_key, token)
    ok(f"Token stored as '{vault_key}'")

    # Add to config.yaml
    config_path = overlay / "config" / "config.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    cfg["connectors"]["discord"]["accounts"][bot_name] = {"token_key": vault_key}
    config_path.write_text(yaml.dump(cfg, default_flow_style=False, sort_keys=False))
    ok(f"Bot '{bot_name}' added to config")


# ==========================================================================
# File writers
# ==========================================================================

def _ask_routing(bot_account: str, default_mentions: bool = True) -> dict:
    """Prompt for agent routing config and return the routing dict."""
    mentions_only = ask_yn("Only respond when @mentioned?", default=default_mentions)

    print()
    info("How should this agent listen for messages?")
    print(f"    1) All channels {DIM}(wildcard — responds everywhere){RESET}")
    print(f"    2) Specific channels {DIM}(by channel ID){RESET}")
    print(f"    3) By category {DIM}(all channels in a Discord category){RESET}")
    print(f"    4) Specific channels + category")
    scope = ask_choice("Scope", ["1", "2", "3", "4"], default="1")

    channels = []
    categories = []
    if scope in ("2", "4"):
        info("Enter channel IDs (one per line, empty to stop):")
        while True:
            ch = ask("Channel ID (empty to stop)")
            if not ch:
                break
            channels.append(ch)
    if scope in ("3", "4"):
        info("Enter category IDs (one per line, empty to stop):")
        while True:
            cat = ask("Category ID (empty to stop)")
            if not cat:
                break
            categories.append(cat)

    guilds = []
    if ask_yn("Restrict to specific server/guild?", default=False):
        while True:
            g = ask("Guild ID (empty to stop)")
            if not g:
                break
            guilds.append(g)

    routing = {
        "discord": {
            "account": bot_account,
            "channels": channels,
            "mentions": mentions_only,
        }
    }
    if categories:
        routing["discord"]["categories"] = categories
    if guilds:
        routing["discord"]["guilds"] = guilds

    return routing


def _write_yaml(path: Path, data: dict, state: dict):
    if path.exists() and state.get("vault_existed"):
        if not ask_yn(f"  {_display(path)} exists. Overwrite?", default=False):
            warn(f"Skipped {_display(path)}")
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    ok(f"Created {_display(path)}")


def _write_json(path: Path, data: dict, state: dict):
    if path.exists() and state.get("vault_existed"):
        if not ask_yn(f"  {_display(path)} exists. Overwrite?", default=False):
            warn(f"Skipped {_display(path)}")
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    ok(f"Created {_display(path)}")


def _neutralise_core_remote(project_root: Path):
    """Rename origin → upstream and block push to prevent accidental pushes to the Core repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "remote", "get-url", "origin"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return  # No origin remote — nothing to do

        subprocess.run(
            ["git", "-C", str(project_root), "remote", "rename", "origin", "upstream"],
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(project_root), "remote", "set-url", "--push", "upstream", "no-push"],
            capture_output=True,
        )
        ok("Core remote: origin → upstream (push disabled)")
        info("Pull updates:  git pull upstream main")
        info("Maintainers:   git remote rename upstream origin")
    except FileNotFoundError:
        pass  # git not installed — skip silently


def _write_file(path: Path, content: str, state: dict):
    if path.exists() and state.get("vault_existed"):
        if not ask_yn(f"  {_display(path)} exists. Overwrite?", default=False):
            warn(f"Skipped {_display(path)}")
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    ok(f"Created {_display(path)}")


def _display(path: Path, overlay: Path | None = None) -> str:
    """Show path relative to PROJECT_ROOT or overlay for readability."""
    if overlay:
        try:
            return f"<overlay>/{path.relative_to(overlay)}"
        except ValueError:
            pass
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _neutralise_core_remote():
    """Rename origin -> upstream and block push to prevent accidental pushes to the Core repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "remote", "get-url", "origin"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return  # No origin remote

        subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "remote", "rename", "origin", "upstream"],
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "remote", "set-url", "--push", "upstream", "no-push"],
            capture_output=True,
        )
        ok("Core remote: origin → upstream (push disabled)")
        info("Pull updates:  git pull upstream main")
        info("Maintainers:   git remote rename upstream origin")
    except FileNotFoundError:
        pass  # git not installed


def _check_sudo() -> bool:
    """Check if passwordless sudo is available."""
    try:
        result = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True, timeout=2,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ==========================================================================
# Main
# ==========================================================================

def main():
    os.chdir(PROJECT_ROOT)
    banner()

    state: dict = {}

    steps = [
        step_dependencies,
        step_overlay,
        step_hooks,
        step_modules,
        step_vault,
        step_discord,
        step_team,
        step_agent,
        step_hitl,
        step_compression,
        step_generate,
        step_ops_instance,
        step_extras,
        step_systemd,
        step_browser,
        step_final_sync,
        step_summary,
    ]

    for step in steps:
        step(state)

    print(f"  {GREEN}{BOLD}T.A.R.S is ready.{RESET}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n  {YELLOW}Setup interrupted. Files written so far are preserved.{RESET}\n")
        sys.exit(0)
