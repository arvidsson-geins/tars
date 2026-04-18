"""Claude Code CLI as LLM provider.

Spawns Claude Code CLI sessions per agent in their project directory.
Uses Max subscription — no API key needed. Claude Code reads CLAUDE.md
automatically and is sandboxed to the project dir.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from src.core.base import LLMProvider, LLMResponse, Message, MessageRole, ToolDef

logger = logging.getLogger(__name__)

MODEL_MAP = {
    "opus": "opus",
    "sonnet": "sonnet",
    "haiku": "haiku",
    "claude-opus-4-6": "opus",
    "claude-sonnet-4-6": "sonnet",
    "claude-haiku-4-5": "haiku",
}


class ClaudeCodeProvider(LLMProvider):
    """LLM provider that uses Claude Code CLI.

    Each call spawns `claude --print` in the agent's project directory.
    Claude Code reads CLAUDE.md automatically for identity/instructions.
    """
    name = "claude_code"

    def __init__(self, config: dict):
        super().__init__(config)
        self._claude_bin = config.get("claude_bin", "claude")
        self._default_model = config.get("model", "sonnet")
        self._timeout = config.get("timeout", 3600)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        stream: bool = False,
        **kwargs,
    ) -> LLMResponse:
        """Send messages to Claude Code CLI and return the response.

        kwargs:
            project_dir: str — working directory for the CLI session
            model: str — model override for this call
            session_id: str — resume a previous session
            allowed_tools: list[str] — Claude Code tools to allow
        """
        project_dir = kwargs.get("project_dir", ".")
        model = kwargs.get("model", self._default_model)
        session_id = kwargs.get("session_id")
        allowed_tools = kwargs.get("allowed_tools")
        disallowed_tools = kwargs.get("disallowed_tools")
        effort = kwargs.get("effort")
        agent_id = kwargs.get("agent_id")
        tag = f"[{agent_id}] " if agent_id else ""

        # Resolve cwd up-front so we can probe the session file before building the prompt
        cwd = Path(project_dir).resolve()
        if not cwd.exists():
            cwd.mkdir(parents=True, exist_ok=True)

        # Pre-flight probe: if the CLI has no local state for this session, skip --resume.
        # Stale session_ids that still exist in storage but not on disk cause silent hangs
        # when the CLI tries to resume them.
        if session_id and not self._session_file_exists(cwd, session_id):
            logger.info(
                f"{tag}Session file missing for {session_id} — starting fresh"
            )
            session_id = None

        # Build the prompt from messages
        resuming = session_id is not None
        prompt = self._build_prompt(messages, resuming=resuming)

        # Build CLI args
        args = [self._claude_bin, "--print", "--output-format", "json"]

        # Model
        resolved_model = MODEL_MAP.get(model, model)
        args.extend(["--model", resolved_model])

        # Effort (thinking level) — accepted values: low, medium, high, xhigh, max
        if effort:
            args.extend(["--effort", effort])

        # Session management
        if session_id:
            args.extend(["--resume", session_id])
            # Don't inject system prompt on resume — session already has it
        else:
            # System prompt — only inject if there's an explicit system message
            # (Claude Code reads CLAUDE.md automatically from project_dir)
            system_msgs = [m for m in messages if m.role == MessageRole.SYSTEM and m.content]
            if system_msgs:
                args.extend(["--system-prompt", system_msgs[0].content])

        # MCP config — use explicit path if set, otherwise auto-discover from cwd
        mcp_config = kwargs.get("mcp_config")
        if mcp_config:
            args.extend(["--mcp-config", str(mcp_config)])

        # Allowed tools
        if allowed_tools:
            args.extend(["--allowedTools"] + allowed_tools)

        # Disallowed tools — hide and block these entirely
        if disallowed_tools:
            args.extend(["--disallowedTools"] + disallowed_tools)

        logger.debug(f"{tag}Claude Code: cwd={cwd} model={resolved_model} prompt_len={len(prompt)}")

        was_resuming = "--resume" in args
        max_attempts = 4  # refresh+resume, resume-dropped fresh, backoff retry, give up
        proc = None
        refreshed_token = False

        for attempt in range(1, max_attempts + 1):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(cwd),
                    env=self._build_env(),
                )

                # Notify caller of running process (for /stop support)
                proc_callback = kwargs.get("proc_callback")
                if proc_callback:
                    proc_callback(proc)

                # Cap resume attempts at 5 min to catch hang-on-stale-resume quickly.
                # Fresh sessions get the full timeout for legitimate long-running calls.
                timeout = kwargs.get("timeout", self._timeout)
                if "--resume" in args:
                    timeout = min(timeout, 300)
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=prompt.encode()),
                    timeout=timeout,
                )

                if proc.returncode != 0:
                    stderr_text = stderr.decode().strip() if stderr else ""
                    stdout_text = stdout.decode().strip() if stdout else ""

                    # Try to extract error from JSON stdout (Claude Code sometimes returns errors there)
                    error_detail = ""
                    if stdout_text:
                        try:
                            data = json.loads(stdout_text)
                            if data.get("is_error"):
                                error_detail = data.get("result", "")
                        except json.JSONDecodeError:
                            error_detail = stdout_text[:500]

                    error_msg = stderr_text or error_detail or f"Exit code {proc.returncode}"
                    combined = f"{stderr_text} {error_detail}".lower()
                    is_auth_error = "401" in combined or "authentication" in combined or "not logged in" in combined
                    is_dead_session = "no conversation found" in combined

                    # Dead session — drop --resume immediately, no point retrying
                    if is_dead_session and "--resume" in args:
                        resume_idx = args.index("--resume")
                        dead_id = args[resume_idx + 1] if resume_idx + 1 < len(args) else "unknown"
                        args = [a for i, a in enumerate(args)
                                if i != resume_idx and i != resume_idx + 1]
                        logger.warning(
                            f"{tag}Dead CLI session {dead_id} — dropping --resume, starting fresh"
                        )
                        continue

                    if is_auth_error:
                        # Stage 1: Force token refresh on first auth error
                        if not refreshed_token:
                            logger.warning(
                                f"{tag}Auth error (attempt {attempt}/{max_attempts}) — "
                                f"forcing token refresh, retrying..."
                            )
                            await self._force_token_refresh()
                            refreshed_token = True
                            continue

                        # Stage 2: Token refresh didn't help — session is stale, drop --resume
                        if "--resume" in args:
                            resume_idx = args.index("--resume")
                            stale_id = args[resume_idx + 1] if resume_idx + 1 < len(args) else "unknown"
                            args = [a for i, a in enumerate(args)
                                    if i != resume_idx and i != resume_idx + 1]
                            logger.warning(
                                f"{tag}Auth error persists after token refresh — "
                                f"dropping stale session {stale_id}, retrying fresh"
                            )
                            continue

                        # Stage 3: No resume involved — genuine auth error, backoff and retry
                        if attempt < max_attempts:
                            logger.warning(
                                f"{tag}Claude auth error (attempt {attempt}/{max_attempts}) — "
                                f"retrying in 5s..."
                            )
                            await asyncio.sleep(5)
                            continue

                        # Stage 4: All retries exhausted
                        logger.critical(
                            f"{tag}Claude auth failed after all retries — token may be expired. "
                            "Fix: run 'claude setup-token' as the tars user, then restart."
                        )
                        return LLMResponse(
                            content="I'm having trouble connecting right now — my authentication needs attention. Try again in a few minutes, and let Peter know if it keeps happening.",
                            stop_reason="error",
                        )

                    # Non-auth CLI error — retry with backoff
                    logger.error(
                        f"{tag}Claude Code failed (attempt {attempt}/{max_attempts}, "
                        f"exit={proc.returncode}): {error_msg}"
                        + (f" | stdout: {stdout_text[:300]}" if stdout_text and not error_detail else "")
                    )
                    if attempt < max_attempts:
                        await asyncio.sleep(2 * attempt)
                        continue

                    return LLMResponse(
                        content="Sorry, something went wrong on my end. Try sending that again — if it keeps failing, let Peter know.",
                        stop_reason="error",
                    )

                response = self._parse_response(stdout.decode())

                # If we dropped --resume to recover, log the transition
                if was_resuming and "--resume" not in args:
                    logger.info(f"{tag}Recovered from stale session — new session started")

                return response

            except FileNotFoundError:
                logger.error(f"{tag}Claude Code CLI not found at '{self._claude_bin}'")
                return LLMResponse(
                    content="I can't start up right now — there's a system issue. Let Peter know.",
                    stop_reason="error",
                )
            except asyncio.TimeoutError:
                if proc:
                    proc.kill()
                logger.error(
                    f"{tag}Claude Code timed out (attempt {attempt}/{max_attempts}) "
                    f"after {timeout}s"
                )
                # Hang on --resume is the usual cause. Drop the stale session and retry
                # fresh rather than looping on the same doomed args.
                if "--resume" in args:
                    resume_idx = args.index("--resume")
                    hung_id = args[resume_idx + 1] if resume_idx + 1 < len(args) else "unknown"
                    args = [a for i, a in enumerate(args)
                            if i != resume_idx and i != resume_idx + 1]
                    logger.warning(
                        f"{tag}Session --resume {hung_id} hung — dropping, retrying fresh"
                    )
                    continue
                if attempt < max_attempts:
                    continue
                return LLMResponse(
                    content="That took too long and I had to stop. Try a shorter question, or break it into parts.",
                    stop_reason="timeout",
                )

    def _session_file_exists(self, cwd: Path, session_id: str) -> bool:
        """Check whether Claude CLI has local state for this session.

        Claude Code stores sessions at
        ``$CLAUDE_CONFIG_DIR/projects/<cwd-slugged>/<session_id>.jsonl`` where the
        slug is the cwd with ``/`` replaced by ``-``. Missing file → nothing to
        resume, so we skip ``--resume`` to avoid silent hangs.
        """
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
        slug = str(cwd).replace("/", "-")
        return (Path(config_dir) / "projects" / slug / f"{session_id}.jsonl").exists()

    async def _force_token_refresh(self) -> None:
        """Force the CLI to refresh its OAuth access token.

        Running 'claude auth status' triggers the CLI's internal token
        refresh if the access token is expired but the refresh token is valid.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self._claude_bin, "auth", "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env(),
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                logger.info("Token refresh triggered via 'claude auth status'")
            else:
                logger.warning("Token refresh attempt returned non-zero — may still work")
        except (asyncio.TimeoutError, FileNotFoundError) as e:
            logger.warning(f"Token refresh attempt failed: {e}")

    def _build_prompt(self, messages: list[Message], resuming: bool = False) -> str:
        """Build a prompt string from messages.

        For Claude Code CLI, we send the user messages as the prompt.
        System messages are handled via --system-prompt flag.

        When resuming a session, only send the latest user message —
        Claude Code already has the conversation history internally.
        """
        if resuming:
            # Only the latest user message — CLI has the rest
            for msg in reversed(messages):
                if msg.role == MessageRole.USER:
                    return msg.content
            return ""

        parts = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                continue  # handled via --system-prompt
            elif msg.role == MessageRole.USER:
                parts.append(msg.content)
            elif msg.role == MessageRole.ASSISTANT:
                parts.append(f"[Previous response]: {msg.content}")
            elif msg.role == MessageRole.TOOL:
                parts.append(f"[Tool result ({msg.name})]: {msg.content}")
        return "\n\n".join(parts)

    def _parse_response(self, output: str) -> LLMResponse:
        """Parse Claude Code JSON output into LLMResponse."""
        output = output.strip()
        if not output:
            return LLMResponse(content="(empty response)", stop_reason="error")

        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            # Not JSON — treat as plain text (shouldn't happen with --output-format json)
            return LLMResponse(content=output, stop_reason="end_turn")

        if data.get("is_error"):
            return LLMResponse(
                content=data.get("result", "Unknown error"),
                stop_reason="error",
            )

        # Extract usage info
        usage = data.get("usage", {})
        total_tokens = (
            usage.get("input_tokens", 0) +
            usage.get("output_tokens", 0) +
            usage.get("cache_read_input_tokens", 0)
        )

        return LLMResponse(
            content=data.get("result", ""),
            tokens_used=total_tokens,
            model=data.get("model"),
            stop_reason=data.get("stop_reason", "end_turn"),
            session_id=data.get("session_id"),
        )

    # Env vars safe to pass to Claude Code subprocess.
    # Everything else is stripped to prevent secret leakage.
    _ENV_ALLOWLIST = {
        "PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
        "TERM", "COLORTERM", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "NODE_PATH", "EDITOR",
        "SSH_AUTH_SOCK", "CLAUDE_CONFIG_DIR",
    }

    def _build_env(self) -> dict[str, str]:
        """Build environment for the CLI subprocess.

        Only passes allowlisted env vars — prevents vault secrets,
        API keys, or other sensitive values from leaking into the subprocess.
        """
        return {k: v for k, v in os.environ.items() if k in self._ENV_ALLOWLIST}
