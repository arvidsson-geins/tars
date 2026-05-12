"""Agent manager — loads agents from config, manages sessions, dispatches to LLM."""

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from src.core.base import (
    Attachment, Connector, IncomingMessage, LLMProvider, LLMResponse, MemoryBackend,
    Message, MessageRole, ToolContext, ToolDef, VaultBackend,
)
from src.core.tools import get_tools_for_agent, get_tool
from src.core.skills import get_skill, render_skill_prompt

logger = logging.getLogger(__name__)


class SessionState(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"


@dataclass
class Session:
    """Lightweight session handle. No conversation content stored here."""
    id: str
    agent_id: str
    channel_id: str
    user_id: str | None
    state: SessionState = SessionState.ACTIVE
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    message_count: int = 0
    cli_session_id: str | None = None  # Claude Code CLI session for --resume


class AgentManager:
    """Manages agent instances, sessions, and message dispatch."""

    def __init__(
        self,
        agent_configs: dict[str, dict],
        connectors: dict[str, Connector],
        llm_providers: dict[str, LLMProvider],
        memory_backends: dict[str, MemoryBackend],
        vault: VaultBackend | None = None,
        defaults: dict | None = None,
        storage: "Storage | None" = None,
        hitl: "HITLGate | None" = None,
        rate_limiter: "RateLimiter | None" = None,
        audit: "AuditLog | None" = None,
        behavior_monitor: "BehaviorMonitor | None" = None,
        access_control: "AccessControl | None" = None,
        alerter: "AlertSender | None" = None,
    ):
        self.agent_configs = agent_configs
        self.connectors = connectors
        self.llm_providers = llm_providers
        self.memory_backends = memory_backends
        self.vault = vault
        self.defaults = defaults or {}
        self.storage = storage
        self.hitl = hitl
        self.rate_limiter = rate_limiter
        self.audit = audit
        self.behavior_monitor = behavior_monitor
        self.access_control = access_control
        self.alerter = alerter

        # Validate agent isolation before anything else
        self._validate_project_dirs()

        # Session map — bounded by max_sessions (LRU)
        max_sessions = self.defaults.get("session", {}).get("max_sessions", 100)
        self.sessions: OrderedDict[str, Session] = OrderedDict()
        self._max_sessions = max_sessions
        self._max_history = self.defaults.get("session", {}).get("max_history", 50)
        self._max_tool_rounds = 10  # prevent infinite tool loops
        # Track running LLM processes per agent for /stop
        self._running_procs: dict[str, asyncio.subprocess.Process] = {}

    def _validate_project_dirs(self) -> None:
        """Validate that no non-privileged agent's project_dir is an ancestor of another's.

        Privileged agents (e.g. rescue bot) are exempt — they intentionally
        run in the project root with full access, gated by HITL.
        """
        resolved: dict[str, Path] = {}
        privileged: set[str] = set()
        overlay = os.environ.get("TARS_OVERLAY")
        for agent_id, cfg in self.agent_configs.items():
            project_dir = cfg.get("project_dir", f"./agents/{agent_id}")
            path = Path(project_dir).resolve()
            # In three-layer deployments, agents live in the overlay
            if not path.is_dir() and overlay:
                overlay_dir = Path(overlay) / "agents" / agent_id
                if overlay_dir.is_dir():
                    path = overlay_dir.resolve()
            resolved[agent_id] = path
            if cfg.get("privileged", False):
                privileged.add(agent_id)

        if privileged:
            logger.info(f"Privileged agents (bypass isolation): {privileged}")

        for agent_a, path_a in resolved.items():
            # Privileged agents can be ancestors — that's the point
            if agent_a in privileged:
                continue
            for agent_b, path_b in resolved.items():
                if agent_a == agent_b:
                    continue
                try:
                    path_b.relative_to(path_a)
                    raise ValueError(
                        f"Agent isolation violation: '{agent_a}' project_dir ({path_a}) "
                        f"is an ancestor of '{agent_b}' project_dir ({path_b}). "
                        f"Each agent must have its own isolated directory. "
                        f"Set 'privileged: true' if this is intentional (e.g. rescue agent)."
                    )
                except ValueError as e:
                    if "Agent isolation violation" in str(e):
                        raise
                    # Not a parent — this is what we want

    def _session_key(self, agent_id: str, channel_id: str) -> str:
        return f"{agent_id}:{channel_id}"

    def _get_or_create_session(self, agent_id: str, channel_id: str,
                                user_id: str | None = None) -> Session:
        """Get existing session or create a new one. LRU eviction if over cap."""
        key = self._session_key(agent_id, channel_id)

        if key in self.sessions:
            session = self.sessions[key]
            session.last_active = time.time()
            session.state = SessionState.ACTIVE
            # Move to end (most recently used)
            self.sessions.move_to_end(key)
            return session

        # Evict LRU if at capacity
        while len(self.sessions) >= self._max_sessions:
            evicted_key, evicted = self.sessions.popitem(last=False)
            logger.info(f"Evicted session {evicted_key} (LRU)")

        session = Session(
            id=key,
            agent_id=agent_id,
            channel_id=channel_id,
            user_id=user_id,
        )
        self.sessions[key] = session
        return session

    def _get_agent_llm(self, agent_id: str) -> LLMProvider:
        """Get the LLM provider for an agent."""
        agent_cfg = self.agent_configs[agent_id]
        llm_cfg = agent_cfg.get("llm", self.defaults.get("llm", {}))
        provider_name = llm_cfg.get("provider", "anthropic")

        if provider_name not in self.llm_providers:
            raise ValueError(
                f"Agent {agent_id} wants LLM provider '{provider_name}' "
                f"but available: {list(self.llm_providers.keys())}"
            )
        return self.llm_providers[provider_name]

    def _get_agent_memory(self, agent_id: str) -> MemoryBackend | None:
        """Get the memory backend for an agent."""
        agent_cfg = self.agent_configs[agent_id]
        mem_cfg = agent_cfg.get("memory", self.defaults.get("memory", {}))
        backend_name = mem_cfg.get("backend")
        if not backend_name:
            return None
        return self.memory_backends.get(backend_name)

    def _get_agent_tools(self, agent_id: str) -> list[ToolDef]:
        """Get the tool definitions an agent is allowed to use."""
        agent_cfg = self.agent_configs[agent_id]
        tool_names = agent_cfg.get("tools", [])
        return get_tools_for_agent(tool_names)

    def _get_project_dir(self, agent_id: str) -> str:
        """Get the project directory for an agent.

        Resolution: if the configured path exists, use it directly.
        Otherwise, check $TARS_OVERLAY/agents/<id> as a fallback
        (agents live in the overlay in three-layer deployments).
        """
        agent_cfg = self.agent_configs[agent_id]
        project_dir = agent_cfg.get("project_dir", f"./agents/{agent_id}")
        resolved = Path(project_dir).resolve()
        if resolved.is_dir():
            return project_dir
        # Try overlay
        overlay = os.environ.get("TARS_OVERLAY")
        if overlay:
            overlay_dir = Path(overlay) / "agents" / agent_id
            if overlay_dir.is_dir():
                return str(overlay_dir)
        return project_dir  # Return original (may fail, but logs will show why)

    def _build_system_prompt(self, agent_id: str) -> str:
        """Build the system prompt for an agent.

        When using Claude Code as the LLM provider, CLAUDE.md in the project
        directory is loaded automatically by the CLI — no injection needed.
        For other providers, we read CLAUDE.md and inject it as a system message.
        """
        agent_cfg = self.agent_configs[agent_id]

        # Check if using Claude Code — it reads CLAUDE.md automatically
        llm_cfg = agent_cfg.get("llm", self.defaults.get("llm", {}))
        if llm_cfg.get("provider") == "claude_code":
            return ""  # Claude Code handles this via CLAUDE.md

        # For other providers: read CLAUDE.md from project dir
        project_dir = self._get_project_dir(agent_id)
        claude_md = Path(project_dir) / "CLAUDE.md"
        if claude_md.exists():
            return claude_md.read_text()

        # Fallback: inline system_prompt from config
        return agent_cfg.get("system_prompt", "")

    async def _auto_recall(
        self, agent_id: str, user_message: str, context_blocks: list[str]
    ) -> None:
        """Auto-recall relevant memories and inject them as context.

        Searches the memory backend directly for memories related to the user's message.
        Keeps it lightweight — max 5 results, only if relevant.
        """
        if not user_message or len(user_message) < 5:
            return

        memory = self._get_agent_memory(agent_id)
        if not memory:
            return

        query = user_message[:200]  # truncate long messages

        try:
            results = await memory.search(query=query, agent_id=agent_id, limit=5)
            if not results:
                return

            lines = ["<auto-recalled-memories>"]
            for mem in results:
                content = mem.get("content", "")
                category = mem.get("category", "")
                if content:
                    lines.append(f"[{category}] {content}")
            lines.append("</auto-recalled-memories>")

            context_blocks.append("\n".join(lines))
            logger.debug(f"Auto-recalled {len(results)} memories for {agent_id}")

        except Exception as e:
            logger.debug(f"Memory auto-recall failed: {e}")

    async def handle_message(self, agent_id: str, message: IncomingMessage) -> None:
        """Handle an incoming message for a specific agent.

        Full flow: build context → call LLM → dispatch tools → re-call → send response.
        """
        agent_cfg = self.agent_configs.get(agent_id)
        if not agent_cfg:
            logger.error(f"Unknown agent: {agent_id}")
            return

        # --- Layer 1: Can this sender talk to this agent? ---
        # Scheduler-originated messages bypass access control
        is_scheduled = message.user_id == "scheduler"
        if self.access_control and not is_scheduled:
            is_bot = bool(
                message.raw
                and hasattr(message.raw, "author")
                and getattr(message.raw.author, "bot", False)
            )
            if not self.access_control.can_message(message.user_id, agent_id, is_bot):
                tier = self.access_control.resolve_tier(message.user_id, is_bot)
                logger.info(
                    f"Access control: sender={message.user_id} tier={tier} "
                    f"blocked from messaging agent={agent_id}"
                )
                return  # Silently ignore

        session = self._get_or_create_session(
            agent_id, message.channel_id, message.user_id
        )
        session.message_count += 1

        connector = self.connectors.get(message.connector)
        if not connector:
            logger.error(f"Unknown connector: {message.connector}")
            return

        # --- Build user content with auto-injected context ---
        user_content = message.content
        context_blocks = []

        # Inject channel context
        channel_context = f"<channel id=\"{message.channel_id}\" connector=\"{message.connector}\""
        if message.bot_account:
            channel_context += f" bot=\"{message.bot_account}\""
        channel_context += " />"
        context_blocks.append(channel_context)

        # Inject attachment info so the LLM knows about uploaded files
        # Include attachments from the current message AND any referenced (replied-to) message
        all_attachments = list(message.attachments)
        if message.raw and hasattr(message.raw, 'reference') and message.raw.reference:
            ref_msg = message.raw.reference.resolved
            if ref_msg and hasattr(ref_msg, 'attachments'):
                for a in ref_msg.attachments:
                    all_attachments.append(Attachment(
                        filename=a.filename, url=a.url,
                        content_type=a.content_type, size=a.size,
                    ))

        if all_attachments:
            att_lines = ["<attachments>"]
            for att in all_attachments:
                att_lines.append(
                    f'  <file name="{att.filename}" url="{att.url}" '
                    f'type="{att.content_type or "unknown"}" '
                    f'size="{att.size or 0}" />'
                )
            att_lines.append("</attachments>")
            att_lines.append(
                "Note: Use download_file to save attachments locally, then "
                "Read to view images/PDFs. For audio, use transcribe_audio."
            )
            context_blocks.append("\n".join(att_lines))

        # Inject user context from team system
        try:
            from src.tools.team import build_user_context
            user_context = build_user_context(message.user_id)
            if user_context:
                context_blocks.append(user_context)
        except ImportError:
            pass

        # Startup context: inject team roster, pinned memories, codex index
        # on first message of a session (subsequent messages inherit via --resume)
        if session.message_count == 1 or not session.cli_session_id:
            try:
                from src.core.startup_context import build_startup_context
                memory = self._get_agent_memory(agent_id)
                startup_ctx = await build_startup_context(agent_id, memory)
                if startup_ctx:
                    context_blocks.append(startup_ctx)
            except Exception as e:
                logger.debug(f"Startup context injection failed: {e}")

        # Auto-recall: search memory for relevant context
        try:
            await self._auto_recall(agent_id, message.content, context_blocks)
        except Exception as e:
            logger.debug(f"Auto-recall failed: {e}")

        if context_blocks:
            injected = "\n\n".join(context_blocks)
            user_content = f"{injected}\n\n{user_content}"

        # Skill invocation — render skill prompt
        # Skills start fresh (no --resume) to avoid stale context from prior conversations.
        if message.skill:
            skill = get_skill(message.skill)
            if skill:
                skill_prompt = render_skill_prompt(skill, message.skill_params or {})
                user_content = f"[Skill: {skill.name}]\n{skill_prompt}"
                if message.content:
                    user_content += f"\n\nUser message: {message.content}"
                # Force fresh CLI session — skills are self-contained and should not
                # carry context from previous conversations in the channel.
                session.cli_session_id = None
            else:
                await connector.send(
                    message.channel_id, f"Unknown skill: {message.skill}",
                    ephemeral=True, raw=message.raw, agent_id=agent_id,
                )
                return

        # --- Load session from storage (restore cli_session_id across restarts) ---
        if self.storage:
            stored = await self.storage.get_or_create_session(
                session.id, agent_id, message.channel_id, message.user_id
            )
            # Don't restore CLI session for skill invocations — they run fresh
            if stored.get("cli_session_id") and not session.cli_session_id and not message.skill:
                session.cli_session_id = stored["cli_session_id"]
                logger.debug(f"Restored CLI session {session.cli_session_id} from storage")

        # --- Build message context ---
        # Always load SQLite history into `messages`. When the CLI resume succeeds,
        # claude_code.py's _build_prompt(resuming=True) discards it and sends only the
        # latest user message. When --resume is dropped (stale/hung/auth), the provider
        # rebuilds the prompt with resuming=False and the history is what preserves
        # transcript continuity across the fallback.
        system_prompt = self._build_system_prompt(agent_id)
        messages = []
        if system_prompt:
            messages.append(Message(role=MessageRole.SYSTEM, content=system_prompt))

        if self.storage:
            if stored.get("summary"):
                messages.append(Message(
                    role=MessageRole.SYSTEM,
                    content=f"[Conversation summary]: {stored['summary']}"
                ))
                history = await self.storage.load_history(session.id, 5)
            else:
                history = await self.storage.load_history(session.id, self._max_history)
            messages.extend(history)

        # Add current user message
        messages.append(Message(role=MessageRole.USER, content=user_content))

        # Persist user message
        if self.storage:
            await self.storage.save_message(session.id, "user", user_content)

        # --- Get tools ---
        tools = self._get_agent_tools(agent_id)
        if message.skill:
            skill = get_skill(message.skill)
            if skill and skill.tools:
                skill_tools = get_tools_for_agent(skill.tools)
                existing_names = {t.name for t in tools}
                for st in skill_tools:
                    if st.name not in existing_names:
                        tools.append(st)

        # --- LLM call + tool dispatch loop ---
        llm = self._get_agent_llm(agent_id)
        project_dir = self._get_project_dir(agent_id)
        llm_cfg = agent_cfg.get("llm", {})
        llm_model = llm_cfg.get("model",
                     self.defaults.get("llm", {}).get("model", "opus"))
        mcp_config = llm_cfg.get("mcp_config") or self.defaults.get("llm", {}).get("mcp_config")
        llm_effort = agent_cfg.get("effort")
        # Runtime overrides (Discord /model, /effort) win over agents.yaml.
        if self.storage:
            overrides = await self.storage.get_agent_overrides(agent_id)
            llm_model = overrides.get("model", llm_model)
            llm_effort = overrides.get("effort", llm_effort)

        # Build allowed/disallowed tools for Claude Code CLI
        # allowed_tools: auto-approve these (no permission prompt)
        # disallowed_tools: block these entirely (LLM can't see or call them)
        agent_tool_names = agent_cfg.get("tools", [])
        if agent_tool_names and agent_tool_names != "all":
            allowed_tools = [f"mcp__tars-tools__{t}" for t in agent_tool_names]
            # Compute disallowed: all registered tools minus allowed
            from src.core.tools import get_all_tools
            all_tool_names = [f"mcp__tars-tools__{t}" for t in get_all_tools().keys()]
            disallowed_tools = [t for t in all_tool_names if t not in allowed_tools]
        else:
            allowed_tools = None
            disallowed_tools = None

        # Block Claude Code built-in tools if configured
        # disallow_builtins: ["Edit", "Write", "Bash", "MultiEdit"] etc.
        blocked_builtins = agent_cfg.get("disallow_builtins", [])
        if blocked_builtins:
            if disallowed_tools is None:
                disallowed_tools = []
            disallowed_tools.extend(blocked_builtins)

        # --- Per-sender access control: block MCP tools based on who sent the message ---
        if self.access_control:
            is_bot = bool(
                message.raw
                and hasattr(message.raw, "author")
                and getattr(message.raw.author, "bot", False)
            )
            from src.core.tools import get_all_tools
            all_mcp_names = list(get_all_tools().keys())
            sender_blocked = self.access_control.disallowed_tools_for_sender(
                message.user_id, all_mcp_names,
                is_bot=is_bot, agent_id=agent_id,
            )
            if sender_blocked:
                if disallowed_tools is None:
                    disallowed_tools = []
                prefixed = [f"mcp__tars-tools__{t}" for t in sender_blocked]
                disallowed_tools.extend(prefixed)

            # Also block CLI builtins for non-owner/admin senders
            sender_blocked_builtins = self.access_control.disallowed_builtins_for_sender(
                message.user_id, is_bot=is_bot,
            )
            if sender_blocked_builtins:
                if disallowed_tools is None:
                    disallowed_tools = []
                disallowed_tools.extend(sender_blocked_builtins)

            if sender_blocked or sender_blocked_builtins:
                tier = self.access_control.resolve_tier(message.user_id, is_bot)
                logger.info(
                    f"Access control: sender={message.user_id} tier={tier} "
                    f"agent={agent_id} blocked={len(sender_blocked)} MCP tools, "
                    f"{len(sender_blocked_builtins)} builtins"
                )

        async with connector.typing(message.channel_id, bot_account=message.bot_account):
            response = None
            for round_num in range(self._max_tool_rounds):
                try:
                    response = await llm.complete(
                        messages,
                        tools=tools if tools else None,
                        project_dir=project_dir,
                        model=llm_model,
                        effort=llm_effort,
                        session_id=session.cli_session_id,
                        allowed_tools=allowed_tools,
                        disallowed_tools=disallowed_tools,
                        mcp_config=mcp_config,
                        agent_id=agent_id,
                        proc_callback=lambda p: self._running_procs.__setitem__(agent_id, p),
                    )
                except Exception as e:
                    logger.error(f"LLM error for {agent_id}: {e}", exc_info=True)
                    await connector.send(message.channel_id, f"Error: {e}", agent_id=agent_id)
                    return
                finally:
                    self._running_procs.pop(agent_id, None)

                # No tool calls — we're done
                if not response.tool_calls:
                    break

                # Dispatch tool calls
                tool_results = await self._dispatch_tools(
                    agent_id, session.id, response.tool_calls, connector, message,
                )

                # Add assistant message + tool results to context
                messages.append(Message(
                    role=MessageRole.ASSISTANT,
                    content=response.content or "",
                    tool_calls=response.tool_calls,
                ))
                for tr in tool_results:
                    messages.append(Message(
                        role=MessageRole.TOOL,
                        content=tr["content"],
                        name=tr["name"],
                    ))

            else:
                logger.warning(f"Agent {agent_id} hit max tool rounds ({self._max_tool_rounds})")

        # --- Save CLI session ID for --resume on next message ---
        if response and response.session_id:
            session.cli_session_id = response.session_id
            if self.storage:
                await self.storage.save_cli_session_id(session.id, response.session_id)
            logger.debug(f"Saved CLI session {response.session_id} for {session.id}")

        # If resume failed (error response), clear the stale session and retry would
        # happen on the next message with fresh context from SQLite
        if response and response.stop_reason == "error" and session.cli_session_id:
            logger.warning(f"CLI session may be stale, clearing for {session.id}")
            session.cli_session_id = None
            if self.storage:
                await self.storage.save_cli_session_id(session.id, "")

        # --- Send final response ---
        if response and response.content:
            await connector.send(
                message.channel_id, response.content, reply_to=message.raw,
                bot_account=message.bot_account, agent_id=agent_id,
            )
            # Persist assistant response
            if self.storage:
                await self.storage.save_message(
                    session.id, "assistant", response.content,
                    tokens_used=response.tokens_used,
                )

        # --- Auto-summarize if conversation is getting long ---
        summarize_after = self.defaults.get("session", {}).get("summarize_after", 30)
        if self.storage and session.message_count > 0 and session.message_count % summarize_after == 0:
            import asyncio
            asyncio.create_task(
                self._auto_summarize(agent_id, session),
                name=f"summarize-{session.id}",
            )

    async def handle_internal_message(
        self, agent_id: str, message: IncomingMessage,
    ) -> str | None:
        """Handle an inter-agent message and return the text response.

        Unlike handle_message(), this does NOT send via connector. It runs the
        full LLM + tool-dispatch loop and returns the final response text.
        Used by ask_agent for synchronous inter-agent communication.
        """
        agent_cfg = self.agent_configs.get(agent_id)
        if not agent_cfg:
            return f"Unknown agent: {agent_id}"

        # Use a synthetic channel for inter-agent comms
        channel_id = f"internal:{message.user_id}:{agent_id}"
        session = self._get_or_create_session(agent_id, channel_id, message.user_id)
        session.message_count += 1

        # Build message context
        system_prompt = self._build_system_prompt(agent_id)
        messages = []
        if system_prompt:
            messages.append(Message(role=MessageRole.SYSTEM, content=system_prompt))

        messages.append(Message(role=MessageRole.USER, content=message.content))

        # Get tools
        tools = self._get_agent_tools(agent_id)

        # LLM call + tool dispatch loop
        llm = self._get_agent_llm(agent_id)
        project_dir = self._get_project_dir(agent_id)
        llm_cfg = agent_cfg.get("llm", {})
        llm_model = llm_cfg.get("model",
                     self.defaults.get("llm", {}).get("model", "opus"))
        mcp_config = llm_cfg.get("mcp_config") or self.defaults.get("llm", {}).get("mcp_config")
        llm_effort = agent_cfg.get("effort")
        if self.storage:
            overrides = await self.storage.get_agent_overrides(agent_id)
            llm_model = overrides.get("model", llm_model)
            llm_effort = overrides.get("effort", llm_effort)

        response = None
        for round_num in range(self._max_tool_rounds):
            try:
                response = await llm.complete(
                    messages,
                    tools=tools if tools else None,
                    project_dir=project_dir,
                    model=llm_model,
                    effort=llm_effort,
                    session_id=session.cli_session_id,
                    mcp_config=mcp_config,
                    agent_id=agent_id,
                )
            except Exception as e:
                logger.error(f"Internal LLM error for {agent_id}: {e}", exc_info=True)
                return f"Error from {agent_id}: {e}"

            if not response.tool_calls:
                break

            # Build a dummy IncomingMessage for tool dispatch context
            tool_results = await self._dispatch_tools(
                agent_id, session.id, response.tool_calls, None, message,
            )

            messages.append(Message(
                role=MessageRole.ASSISTANT,
                content=response.content or "",
                tool_calls=response.tool_calls,
            ))
            for tr in tool_results:
                messages.append(Message(
                    role=MessageRole.TOOL,
                    content=tr["content"],
                    name=tr["name"],
                ))
        else:
            logger.warning(f"Internal agent {agent_id} hit max tool rounds")

        if response and response.session_id:
            session.cli_session_id = response.session_id

        return response.content if response else None

    async def _dispatch_tools(
        self, agent_id: str, session_id: str,
        tool_calls: list[dict], connector: Connector,
        message: IncomingMessage,
    ) -> list[dict]:
        """Execute tool calls and return results.

        Each tool gets a fresh ToolContext. Results are returned as
        dicts with 'name' and 'content' keys.
        """
        results = []

        for tc in tool_calls:
            tool_name = tc.get("name", "")
            args_raw = tc.get("arguments", "{}")

            # Parse arguments
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
            else:
                args = args_raw

            # Look up the tool
            tool_def = get_tool(tool_name)
            if not tool_def:
                results.append({
                    "name": tool_name,
                    "content": f"Unknown tool: {tool_name}",
                })
                continue

            # Check tool is in agent's allowlist
            agent_tools = self.agent_configs[agent_id].get("tools", [])
            if tool_name not in agent_tools:
                logger.warning(f"Agent {agent_id} tried to use non-allowed tool: {tool_name}")
                results.append({
                    "name": tool_name,
                    "content": f"Tool '{tool_name}' is not available to this agent.",
                })
                continue

            # --- Rate limiting ---
            if self.rate_limiter:
                rl_check = self.rate_limiter.check(agent_id, tool_name)
                if not rl_check["allowed"]:
                    if self.audit:
                        self.audit.log_rate_limit(
                            agent_id, tool_name,
                            rl_check.get("count", 0), rl_check.get("limit", 0),
                            rl_check.get("window", ""),
                        )
                    results.append({
                        "name": tool_name,
                        "content": rl_check["reason"],
                    })
                    continue

            # --- Access control ---
            hitl_forced = False
            if self.access_control:
                is_bot = bool(
                    message.raw
                    and hasattr(message.raw, "author")
                    and getattr(message.raw.author, "bot", False)
                )
                ac_check = self.access_control.check(
                    message.user_id, tool_name,
                    is_bot=is_bot, agent_id=agent_id,
                )
                if not ac_check["allowed"]:
                    if ac_check.get("gate") == "hitl" and self.hitl:
                        # Route to HITL even if tool isn't in gated_tools
                        hitl_forced = True
                        logger.info(
                            f"Access control: '{tool_name}' from "
                            f"{'bot' if is_bot else 'user'} {message.user_id} "
                            f"→ HITL gate"
                        )
                    else:
                        logger.warning(
                            f"Access denied: {message.user_id} → {tool_name} "
                            f"({ac_check['reason']})"
                        )
                        results.append({
                            "name": tool_name,
                            "content": f"Access denied: {ac_check['reason']}",
                        })
                        continue

            # --- HITL gate ---
            if self.hitl and (self.hitl.is_gated(tool_name) or tool_def.hitl or hitl_forced):
                from src.core.hitl import build_hitl_description
                desc = build_hitl_description(tool_name, args)
                approval = await self.hitl.request_approval(agent_id, tool_name, args, desc)
                if self.audit:
                    self.audit.log_hitl(
                        approval.get("hitl_id", ""), agent_id, tool_name,
                        approval["status"], approval.get("approver"),
                    )
                if approval["status"] != "approved":
                    results.append({
                        "name": tool_name,
                        "content": f"HITL: Tool '{tool_name}' was {approval['status']}.",
                    })
                    continue

            # Build context
            memory = self._get_agent_memory(agent_id)
            ctx = ToolContext(
                agent_id=agent_id,
                session_id=session_id,
                channel_id=message.channel_id,
                user_id=message.user_id,
                memory=memory,
                vault=self.vault,
                registry=None,
                connector_send=(lambda ch, content, _aid=agent_id: connector.send(ch, content, agent_id=_aid)) if connector else None,
                agent_manager=self,
                inter_agent_depth=getattr(message, '_inter_agent_depth', 0),
            )

            # Record for rate limiting BEFORE execution (prevents TOCTOU race)
            if self.rate_limiter:
                self.rate_limiter.record(agent_id, tool_name)

            # Execute
            start_time = time.time()
            try:
                result = await tool_def.func(ctx, **args)
                duration_ms = int((time.time() - start_time) * 1000)
                logger.info(f"Tool {tool_name} completed in {duration_ms}ms")
                if self.behavior_monitor:
                    alerts = self.behavior_monitor.record_tool_call(agent_id, tool_name)
                    for alert in alerts:
                        logger.warning(f"Behavior alert: {alert}")
                        if self.audit:
                            self.audit.log_content_safety(
                                agent_id, 0, alert["type"],
                                [alert.get("tool", ""), alert.get("severity", "")],
                            )
                        if self.alerter:
                            self.alerter.send_bg(
                                f"\u26a0\ufe0f **Behavior Alert** [{alert.get('severity', 'MEDIUM')}]\n"
                                f"Agent: `{agent_id}`\n"
                                f"Type: `{alert['type']}`\n"
                                f"Tool: `{alert.get('tool', tool_name)}`"
                            )

                if self.storage:
                    await self.storage.log_tool_call(
                        agent_id, session_id, tool_name, args,
                        str(result)[:1000], True, duration_ms,
                    )
                if self.audit:
                    self.audit.log_tool(
                        agent_id, tool_name, args,
                        str(result)[:200], True, duration_ms,
                    )

                results.append({
                    "name": tool_name,
                    "content": str(result) if result else "(no output)",
                })

            except Exception as e:
                duration_ms = int((time.time() - start_time) * 1000)
                logger.error(f"Tool {tool_name} failed: {e}", exc_info=True)

                if self.storage:
                    await self.storage.log_tool_call(
                        agent_id, session_id, tool_name, args,
                        str(e), False, duration_ms,
                    )
                if self.audit:
                    self.audit.log_tool(
                        agent_id, tool_name, args,
                        str(e), False, duration_ms,
                    )

                results.append({
                    "name": tool_name,
                    "content": f"Tool error: {e}",
                })

        return results

    async def _auto_summarize(self, agent_id: str, session: Session) -> None:
        """Generate a conversation summary in the background.

        Called when message count hits summarize_after threshold.
        Uses a lightweight LLM call (haiku) to summarize, then stores it.
        On future context loads without a CLI session, the summary replaces
        bulk message history — keeping token usage bounded.
        """
        if not self.storage:
            return

        try:
            # Load recent history to summarize
            history = await self.storage.load_history(session.id, self._max_history)
            if len(history) < 10:
                return  # not enough to summarize

            # Build the conversation text
            convo_lines = []
            for msg in history:
                prefix = "User" if msg.role == MessageRole.USER else "Assistant"
                content = msg.content[:500]  # truncate long messages for the summary prompt
                convo_lines.append(f"{prefix}: {content}")
            convo_text = "\n".join(convo_lines)

            # Use the agent's LLM to generate a summary (override to haiku for speed/cost)
            llm = self._get_agent_llm(agent_id)
            summary_prompt = (
                "Summarize this conversation concisely. Capture: key topics discussed, "
                "decisions made, important facts shared, any pending tasks or questions. "
                "Be specific — names, numbers, dates, technical details matter. "
                "Keep it under 500 words.\n\n"
                f"Conversation ({len(history)} messages):\n{convo_text}"
            )

            response = await llm.complete(
                [Message(role=MessageRole.USER, content=summary_prompt)],
                project_dir=self._get_project_dir(agent_id),
                model="haiku",  # cheap and fast for summaries
                agent_id=agent_id,
            )

            if response.content and response.stop_reason != "error":
                await self.storage.save_summary(session.id, response.content)
                logger.info(
                    f"Auto-summarized session {session.id} "
                    f"({len(history)} messages → {len(response.content)} chars)"
                )
            else:
                logger.warning(f"Summary generation failed for {session.id}")

        except Exception as e:
            logger.error(f"Auto-summarize error for {session.id}: {e}", exc_info=True)

    async def hold_agent(self, agent_id: str) -> dict:
        """Hold (interrupt) an agent — stop current task but keep session intact.

        Unlike stop_agent, this preserves the CLI session ID so the next message
        resumes the conversation with full context. The user can interject and
        the agent will see both its previous work and the interjection.

        Returns {"held": True/False, "reason": str}.
        """
        proc = self._running_procs.get(agent_id)
        if not proc:
            return {"held": False, "reason": "No running process for this agent."}

        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
            self._running_procs.pop(agent_id, None)
            # NOTE: we do NOT clear the session or cli_session_id —
            # the next message will --resume with the agent's full context
            return {"held": True, "reason": f"Held {agent_id}. Send your message — the agent will resume with full context."}
        except ProcessLookupError:
            self._running_procs.pop(agent_id, None)
            return {"held": False, "reason": "Process already finished."}

    async def stop_agent(self, agent_id: str) -> dict:
        """Stop the running LLM process for an agent.

        Returns {"stopped": True/False, "reason": str}.
        """
        proc = self._running_procs.get(agent_id)
        if not proc:
            return {"stopped": False, "reason": "No running process for this agent."}

        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
            self._running_procs.pop(agent_id, None)
            return {"stopped": True, "reason": f"Stopped {agent_id}."}
        except ProcessLookupError:
            self._running_procs.pop(agent_id, None)
            return {"stopped": False, "reason": "Process already finished."}

    async def reset_session(self, agent_id: str, channel_id: str) -> dict:
        """Reset the session for an agent in a channel.

        Clears the CLI session ID so next message starts fresh.
        """
        session_key = f"{agent_id}:{channel_id}"
        # Find matching session
        for key, session in list(self.sessions.items()):
            if session.agent_id == agent_id and session.channel_id == channel_id:
                old_session_id = session.cli_session_id
                session.cli_session_id = None
                session.message_count = 0
                if self.storage:
                    await self.storage.save_cli_session_id(session.id, "")
                return {
                    "reset": True,
                    "old_session": old_session_id or "none",
                }
        return {"reset": False, "reason": "No session found."}

    async def get_agent_status(self, agent_id: str) -> dict:
        """Get status info for an agent."""
        agent_cfg = self.agent_configs.get(agent_id)
        if not agent_cfg:
            return {"error": f"Unknown agent: {agent_id}"}

        active_sessions = [
            s for s in self.sessions.values()
            if s.agent_id == agent_id
        ]

        return {
            "agent_id": agent_id,
            "display_name": agent_cfg.get("display_name", agent_id),
            "llm_provider": agent_cfg.get("llm", {}).get("provider", "unknown"),
            "llm_model": agent_cfg.get("llm", {}).get("model", "unknown"),
            "active_sessions": len(active_sessions),
            "total_messages": sum(s.message_count for s in active_sessions),
            "tools": agent_cfg.get("tools", []),
        }
