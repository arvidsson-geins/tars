"""T.A.R.S entry point — load config, init registry, start connectors, run."""

import asyncio
import fcntl
import logging
import os
import signal
import sys
from pathlib import Path

import yaml

from src.core.registry import Registry
from src.core.router import Router
from src.core.agent_manager import AgentManager
from src.core.storage import Storage
from src.core.skills import get_all_skills
from src.core.preflight import run_preflight
from src.core.base import resolve_vault_key_file
from src.vault.fernet import FernetVault

logger = logging.getLogger("tars")


def _resolve_config_dirs() -> list[Path]:
    """Return config directories in priority order (highest-priority first).

    Resolution order:
      1. $TARS_OVERLAY/config/  — client overlay (wins on conflict)
      2. $TARS_OTHS/config/     — OTHS proprietary layer
      3. ./config/              — Core defaults / examples
    """
    dirs: list[Path] = []
    overlay = os.environ.get("TARS_OVERLAY")
    if overlay:
        p = Path(overlay) / "config"
        if p.is_dir():
            dirs.append(p)
    oths_raw = os.environ.get("TARS_OTHS", "")
    for oths in oths_raw.split(":"):
        if not oths.strip():
            continue
        p = Path(oths.strip()) / "config"
        if p.is_dir():
            dirs.append(p)
    dirs.append(Path("config"))
    return dirs


def _find_config_file(name: str, config_dirs: list[Path]) -> Path | None:
    """Find first matching config file across config dirs (highest-priority first)."""
    for d in config_dirs:
        f = d / name
        if f.exists():
            return f
    return None


def load_config(profile: str | None = None) -> dict:
    """Load all YAML config files.

    If profile is set (e.g. 'test'), loads config.test.yaml and agents.test.yaml
    instead of the defaults. Falls back to default files if profile files don't exist.

    Config resolution walks: $TARS_OVERLAY/config → $TARS_OTHS/config → ./config
    (first file found wins).
    """
    config_dirs = _resolve_config_dirs()
    suffix = f".{profile}" if profile else ""

    main_file = _find_config_file(f"config{suffix}.yaml", config_dirs)
    if not main_file:
        main_file = _find_config_file("config.yaml", config_dirs)
    main_config = {}
    if main_file:
        with open(main_file) as f:
            main_config = yaml.safe_load(f) or {}
        logger.info(f"Config loaded from {main_file}")

    agents_file = _find_config_file(f"agents{suffix}.yaml", config_dirs)
    if not agents_file:
        agents_file = _find_config_file("agents.yaml", config_dirs)
    agents_config = {}
    if agents_file:
        with open(agents_file) as f:
            agents_config = yaml.safe_load(f) or {}
        logger.info(f"Agents loaded from {agents_file}")

    return {
        **main_config,
        "agents": agents_config.get("agents", {}),
    }


def setup_logging(level: str = "info") -> None:
    """Configure structured logging."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def acquire_lock(profile: str | None = None) -> int:
    """Acquire an exclusive lock file. Exits if another instance is running."""
    suffix = f"-{profile}" if profile else ""
    lock_path = Path(f"data/tars{suffix}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("Another T.A.R.S instance is already running. Exiting.")
        sys.exit(1)
    # Write our PID for visibility
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd  # Keep fd open — lock released when process exits


async def main() -> None:
    # Parse --profile early so lock file is profile-aware
    profile = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--profile" and i < len(sys.argv):
            profile = sys.argv[i + 1] if i + 1 <= len(sys.argv) else None
        elif arg.startswith("--profile="):
            profile = arg.split("=", 1)[1]

    lock_fd = acquire_lock(profile)

    if profile:
        os.environ["TARS_PROFILE"] = profile
    config = load_config(profile=profile)

    log_level = config.get("tars", {}).get("log_level", "info")
    setup_logging(log_level)

    logger.info("Starting T.A.R.S...")

    # --- Vault ---
    # Resolve secrets.enc from config dirs (overlay → OTHS → core)
    config_dirs = _resolve_config_dirs()
    vault_file = _find_config_file("secrets.enc", config_dirs)
    vault_path = vault_file if vault_file else Path("config/secrets.enc")
    vault = FernetVault(vault_path=str(vault_path))
    if vault_path.exists():
        # Try key file first (for systemd), then interactive prompt
        key_file = resolve_vault_key_file()
        if key_file.exists():
            passphrase = key_file.read_text().strip()
        elif not sys.stdin.isatty():
            passphrase = sys.stdin.readline().strip()
        else:
            import getpass
            passphrase = getpass.getpass("Vault passphrase: ")
        try:
            vault.unlock(passphrase)
            del passphrase  # Don't keep in memory
        except ValueError as e:
            logger.error(f"Vault unlock failed: {e}")
            sys.exit(1)
    else:
        # Dev mode — load from .env
        vault.unlock_from_env()

    # Export GitHub token from vault so agents that shell out to `gh` or `git`
    # pick it up via the standard env vars. Passed through to Claude Code
    # subprocesses via the env allowlist in src/llm/claude_code.py.
    gh_token = vault.get("github-token")
    if gh_token:
        os.environ["GH_TOKEN"] = gh_token
        os.environ["GITHUB_TOKEN"] = gh_token
        logger.info("GitHub token loaded from vault")

    # --- Preflight checks ---
    data_dir = config.get("tars", {}).get("data_dir", "./data")
    preflight_ok = await run_preflight(config, vault, f"{data_dir}/tars.db")
    if not preflight_ok:
        logger.critical("Preflight failed — fix errors above and restart")
        sys.exit(1)

    # --- Storage ---
    storage = Storage(db_path=f"{data_dir}/tars.db")
    await storage.init()

    # Prune stale sessions on startup (keeps DB bounded across long-running installs).
    # Configurable via tars.session_retention_days, default 30. Set to 0 to disable.
    retention_days = config.get("tars", {}).get("session_retention_days", 30)
    if retention_days > 0:
        try:
            pruned = await storage.prune_stale_sessions(max_age_days=retention_days)
            if pruned:
                logger.info(f"Startup prune: removed {pruned} sessions older than {retention_days}d")
        except Exception as e:
            logger.error(f"Startup prune failed: {e}")

    # --- Auto-discover modules ---
    registry = Registry()
    registry.discover()

    # --- LLM providers ---
    defaults = config.get("defaults", {})
    llm_providers = {}
    for provider_name, provider_cls in registry.llm_providers.items():
        provider_config = defaults.get("llm", {})
        try:
            llm_providers[provider_name] = provider_cls(config=provider_config)
            logger.info(f"LLM provider: {provider_name}")
        except Exception as e:
            logger.error(f"Failed to init LLM provider {provider_name}: {e}")

    # --- Memory backends ---
    memory_backends = {}
    for backend_name, backend_cls in registry.memory_backends.items():
        mem_config = defaults.get("memory", {})
        try:
            memory_backends[backend_name] = backend_cls(config=mem_config)
            logger.info(f"Memory backend: {backend_name}")
        except Exception as e:
            logger.error(f"Failed to init memory backend {backend_name}: {e}")

    # Note: team module reads from team.json directly, no memory backend needed

    # --- Security: HITL, rate limiting, audit, content safety ---
    from src.core.hitl import HITLGate
    from src.core.rate_limiter import RateLimiter
    from src.core.audit import AuditLog
    from src.core.content_safety import BehaviorMonitor

    security_cfg = config.get("security", {})

    # HITL
    hitl = None
    hitl_cfg = security_cfg.get("hitl", {})
    if hitl_cfg.get("channel"):
        hitl = HITLGate(hitl_cfg, storage._db)
        await hitl.init_schema()
        recovered = await hitl.recover_pending()
        if recovered:
            logger.info(f"HITL: recovered {recovered} expired pending requests")
        logger.info(f"HITL gates: {len(hitl_cfg.get('gated_tools', []))} tools gated")

    # Rate limiter
    rate_limiter = RateLimiter(security_cfg.get("rate_limits", {}))
    logger.info("Rate limiter initialized")

    # Audit log
    audit = AuditLog(f"{data_dir}/audit.jsonl")
    audit.log_auth("startup", "T.A.R.S starting")
    logger.info(f"Audit log: {data_dir}/audit.jsonl")

    # Behavior monitor
    behavior_monitor = BehaviorMonitor()

    # Security alerter — sends alerts to configured Discord channel
    from src.core.alerts import AlertSender
    alerter = AlertSender(config, vault)
    if alerter.enabled:
        logger.info(f"Security alerts: channel {alerter.channel_id}")
    else:
        logger.info("Security alerts: logger only (no alert_channel configured)")

    # Access control
    from src.core.access_control import AccessControl
    ac_cfg = security_cfg.get("access_control", {})
    hitl_gated = security_cfg.get("hitl", {}).get("gated_tools", [])
    access_control = AccessControl(ac_cfg, hitl_gated_tools=hitl_gated) if ac_cfg else None
    if access_control:
        logger.info(f"Access control: {len(ac_cfg.get('safe_tools', []))} safe tools, "
                     f"isolated agents: {ac_cfg.get('isolated_agents', [])}")

    # --- Agent manager ---
    agent_configs = config.get("agents", {})
    agent_manager = AgentManager(
        agent_configs=agent_configs,
        connectors={},
        llm_providers=llm_providers,
        memory_backends=memory_backends,
        vault=vault,
        defaults=defaults,
        storage=storage,
        hitl=hitl,
        rate_limiter=rate_limiter,
        audit=audit,
        behavior_monitor=behavior_monitor,
        access_control=access_control,
        alerter=alerter,
    )

    # --- Connectors ---
    connectors_config = config.get("connectors", {})
    active_connectors: dict[str, object] = {}

    for conn_name, conn_cfg in connectors_config.items():
        if not conn_cfg or not conn_cfg.get("enabled", False):
            continue

        if conn_name not in registry.connectors:
            logger.warning(f"Connector '{conn_name}' enabled but not found in registry")
            continue

        try:
            connector = registry.create_connector(
                conn_name,
                config={**conn_cfg, "admin_users": config.get("admin_users", {}).get(conn_name, [])},
                vault=vault,
            )

            if hasattr(connector, "set_agent_configs"):
                connector.set_agent_configs(agent_configs)
            if hasattr(connector, "set_skills"):
                connector.set_skills(get_all_skills())
            # Give connector a reference to agent manager for /status etc.
            connector._agent_manager = agent_manager

            active_connectors[conn_name] = connector
            logger.info(f"Connector: {conn_name}")
        except Exception as e:
            logger.error(f"Failed to create connector {conn_name}: {e}")

    agent_manager.connectors = active_connectors

    # Wire HITL to Discord connector for approval messages and reactions
    if hitl and "discord" in active_connectors:
        hitl.connector = active_connectors["discord"]
        active_connectors["discord"]._hitl = hitl

    # --- Router ---
    router = Router(agent_manager)
    for connector in active_connectors.values():
        connector.on_message = router.route

    # --- Start connectors ---
    for conn_name, connector in active_connectors.items():
        try:
            await connector.start()
            logger.info(f"Started: {conn_name}")
        except Exception as e:
            logger.error(f"Failed to start {conn_name}: {e}")

    if not active_connectors:
        logger.error("No connectors started. Nothing to do.")
        await storage.close()
        return

    logger.info(
        f"T.A.R.S running — {len(active_connectors)} connector(s), "
        f"{len(agent_configs)} agent(s), {len(llm_providers)} LLM provider(s)"
    )

    # --- Start hot-reload watcher ---
    from src.core.digest import Digest
    digest = Digest(check_interval=5.0)

    async def on_digest_reload(changes):
        # Update skills reference on connectors when skills change
        if "skills" in changes:
            skills = get_all_skills()
            for conn in active_connectors.values():
                if hasattr(conn, "set_skills"):
                    conn.set_skills(skills)
            logger.info(f"Skills updated on connectors: {list(skills.keys())}")

    asyncio.create_task(digest.start(on_reload=on_digest_reload), name="digest")
    logger.info("Hot-reload watcher started")

    # --- Run until interrupted ---
    stop_event = asyncio.Event()

    def handle_signal():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    await stop_event.wait()

    # --- Graceful shutdown ---
    logger.info("Shutting down...")
    for conn_name, connector in active_connectors.items():
        try:
            await connector.stop()
        except Exception as e:
            logger.error(f"Error stopping {conn_name}: {e}")

    audit.log_auth("shutdown", "T.A.R.S stopping")
    audit.close()
    await storage.close()
    logger.info("T.A.R.S stopped.")


if __name__ == "__main__":
    asyncio.run(main())
