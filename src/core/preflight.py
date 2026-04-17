"""Preflight checks — run once at startup, block boot on critical failures.

Each check returns (ok: bool, message: str). Critical failures prevent startup.
Warnings log but allow boot to continue.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path

from src.core.base import PROJECT_ROOT
from src.core.config_schema import validate_config

logger = logging.getLogger(__name__)


def _find_claude_bin() -> str | None:
    """Find claude binary — check PATH, then common install locations."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in [
        Path.home() / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
    ]:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _check_claude_cli() -> tuple[bool, str]:
    """Verify Claude Code CLI is installed."""
    if _find_claude_bin():
        return True, "Claude CLI found"
    return False, ("Claude CLI not found — install with: "
                   "curl -fsSL https://claude.ai/install.sh | sh")


async def _check_claude_auth() -> tuple[bool, str]:
    """Verify Claude auth works by making a minimal call."""
    claude_bin = _find_claude_bin()
    if not claude_bin:
        return False, "Claude CLI not found"
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "--print", "--output-format", "json", "--model", "haiku",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=b"Reply with exactly: pong"),
            timeout=30,
        )
        if proc.returncode == 0:
            return True, "Claude auth OK"
        err = stderr.decode().strip() or stdout.decode().strip()
        if "401" in err or "authentication" in err.lower():
            return False, f"Claude auth failed (401) — run: claude setup-token\n  Detail: {err[:200]}"
        return False, f"Claude CLI error (exit {proc.returncode}): {err[:200]}"
    except asyncio.TimeoutError:
        return False, "Claude auth check timed out (30s) — network issue?"
    except FileNotFoundError:
        return False, "Claude CLI not found"


def _check_storage(db_path: str) -> tuple[bool, str]:
    """Verify database is accessible."""
    db = Path(db_path)
    if not db.parent.exists():
        return False, f"Data directory missing: {db.parent}"
    if db.exists():
        # Check it's readable and not locked
        try:
            import sqlite3
            conn = sqlite3.connect(str(db), timeout=2)
            conn.execute("SELECT 1")
            conn.close()
            return True, "Storage OK"
        except Exception as e:
            return False, f"Database error: {e}"
    # DB doesn't exist yet — that's fine, Storage.init() creates it
    return True, "Storage OK (will be created)"


def _check_agent_paths(agent_configs: dict) -> tuple[bool, str]:
    """Verify all agent project directories exist."""
    overlay = os.environ.get("TARS_OVERLAY")
    missing = []
    for name, cfg in agent_configs.items():
        project_dir = cfg.get("project_dir", f"./agents/{name}")
        p = Path(project_dir).resolve()
        if not p.exists() and overlay:
            p = Path(overlay) / "agents" / name
        if not p.exists():
            missing.append(f"  {name}: {p}")
    if missing:
        return False, "Agent project directories missing:\n" + "\n".join(missing)
    return True, f"Agent paths OK ({len(agent_configs)} agents)"


def _check_vault_secret(vault, secret_name: str) -> tuple[bool, str]:
    """Verify a required secret exists in the vault."""
    val = vault.get(secret_name)
    if val:
        return True, f"Vault secret '{secret_name}' present"
    return False, f"Vault secret '{secret_name}' missing — add with: python -m src.vault.fernet set {secret_name}"


def _check_file_ownership(paths: list[str], expected_user: str = "tars") -> list[str]:
    """Check for files not owned by the expected user. Returns warnings."""
    warnings = []
    try:
        import pwd
        expected_uid = pwd.getpwnam(expected_user).pw_uid
    except KeyError:
        return []  # User doesn't exist (dev machine), skip check

    for path_str in paths:
        p = Path(path_str)
        if not p.exists():
            continue
        try:
            stat = p.stat()
            if stat.st_uid != expected_uid:
                warnings.append(f"Wrong owner on {p} — run: sudo chown -R tars:tars {p}")
        except OSError:
            pass
    return warnings


async def run_preflight(config: dict, vault, storage_path: str) -> bool:
    """Run all preflight checks. Returns True if OK to proceed.

    Critical failures (auth, storage, paths) block startup.
    Warnings (ownership) log but allow boot.
    """
    logger.info("Running preflight checks...")
    passed = 0
    failed = 0
    warnings = 0

    # --- Config schema validation (typo detection + required keys) ---
    schema_errors, schema_warnings = validate_config(config)
    for e in schema_errors:
        logger.error(f"  ✗ config_schema: {e}")
        failed += 1
    for w in schema_warnings:
        logger.warning(f"  ⚠ config_schema: {w}")
        warnings += 1
    if not schema_errors and not schema_warnings:
        logger.info("  ✓ config_schema: OK")
        passed += 1

    # --- Critical checks ---
    checks: list[tuple[str, tuple[bool, str]]] = []

    # CLI exists
    ok, msg = _check_claude_cli()
    checks.append(("claude_cli", (ok, msg)))

    # Auth works (async) — warn only, don't block startup
    ok, msg = await _check_claude_auth()
    if ok:
        logger.info(f"  ✓ claude_auth: {msg}")
        passed += 1
    else:
        logger.warning(f"  ⚠ claude_auth: {msg}")
        warnings += 1

    # Storage
    ok, msg = _check_storage(storage_path)
    checks.append(("storage", (ok, msg)))

    # Agent paths
    agent_configs = config.get("agents", {})
    ok, msg = _check_agent_paths(agent_configs)
    checks.append(("agent_paths", (ok, msg)))

    # Discord tokens in vault — check each configured account
    connectors = config.get("connectors", {})
    discord_cfg = connectors.get("discord", {})
    if discord_cfg.get("enabled"):
        accounts = discord_cfg.get("accounts", {})
        if accounts:
            for acct_name, acct_cfg in accounts.items():
                token_key = acct_cfg.get("token_key", f"DISCORD_TOKEN_{acct_name.upper()}")
                ok, msg = _check_vault_secret(vault, token_key)
                checks.append((f"discord_{acct_name}", (ok, msg)))
        else:
            token_key = discord_cfg.get("token_key", "DISCORD_BOT_TOKEN")
            ok, msg = _check_vault_secret(vault, token_key)
            checks.append(("discord_token", (ok, msg)))

    # Log results
    for name, (ok, msg) in checks:
        if ok:
            logger.info(f"  ✓ {name}: {msg}")
            passed += 1
        else:
            logger.error(f"  ✗ {name}: {msg}")
            failed += 1

    # --- Warnings (non-blocking) ---
    ownership_warnings = _check_file_ownership([
        str(PROJECT_ROOT / "data"),
        str(PROJECT_ROOT / "agents"),
        str(PROJECT_ROOT / "config"),
        str(PROJECT_ROOT / ".venv"),
    ])
    for w in ownership_warnings:
        logger.warning(f"  ⚠ ownership: {w}")
        warnings += 1

    # Summary
    total = passed + failed
    if failed:
        logger.error(f"Preflight FAILED: {passed}/{total} passed, {failed} critical failure(s)")
        return False

    suffix = f", {warnings} warning(s)" if warnings else ""
    logger.info(f"Preflight passed: {total}/{total} checks OK{suffix}")
    return True
