"""Agent scheduler — runs agent prompts on cron schedules.

Reads schedule config from agents.yaml and fires agent sessions at the
specified times. Runs as an asyncio task alongside the Discord connector.

Config example in agents.yaml:

    nest:
      schedule:
        - cron: "0 23 */2 * *"
          prompt: "Scan Hemnet and Booli for new listings..."
          channel: "1479967736862867567"
          bot: house
        - cron: "0 21 * * *"
          prompt: "Run ClickUp cleanup..."
          channel: "1479967736862867567"
          bot: house
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _parse_cron_field(field_str: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron field into a set of matching values."""
    values: set[int] = set()
    for part in field_str.split(","):
        part = part.strip()
        # Handle */N (every N)
        if part.startswith("*/"):
            step = int(part[2:])
            values.update(range(min_val, max_val + 1, step))
        # Handle N-M
        elif "-" in part and "/" not in part:
            start, end = part.split("-", 1)
            values.update(range(int(start), int(end) + 1))
        # Handle N-M/S
        elif "-" in part and "/" in part:
            range_part, step_str = part.split("/", 1)
            start, end = range_part.split("-", 1)
            values.update(range(int(start), int(end) + 1, int(step_str)))
        # Handle *
        elif part == "*":
            values.update(range(min_val, max_val + 1))
        # Handle plain number
        else:
            values.add(int(part))
    return values


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """Check if a datetime matches a 5-field cron expression.

    Fields: minute hour day-of-month month day-of-week (0=Sun or 7=Sun)
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        logger.warning(f"Invalid cron expression: {cron_expr}")
        return False

    minute = _parse_cron_field(parts[0], 0, 59)
    hour = _parse_cron_field(parts[1], 0, 23)
    dom = _parse_cron_field(parts[2], 1, 31)
    month = _parse_cron_field(parts[3], 1, 12)
    dow = _parse_cron_field(parts[4], 0, 7)
    # Normalize Sunday: 7 → 0
    if 7 in dow:
        dow.add(0)

    return (
        dt.minute in minute
        and dt.hour in hour
        and dt.day in dom
        and dt.month in month
        and dt.weekday() in _isoweekday_to_cron(dow)
    )


def _isoweekday_to_cron(dow: set[int]) -> set[int]:
    """Convert cron day-of-week (0=Sun) to Python weekday (0=Mon).

    Cron: 0=Sun, 1=Mon, ..., 6=Sat
    Python: 0=Mon, 1=Tue, ..., 6=Sun
    """
    mapping = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
    return {mapping.get(d, d) for d in dow if d in mapping}


@dataclass
class ScheduledJob:
    """A scheduled job definition."""
    agent_id: str
    cron: str
    prompt: str
    channel: str
    bot: str | None = None
    last_fired: float = 0.0


class Scheduler:
    """Runs scheduled agent tasks on cron-like schedules.

    Checks every 30 seconds. When a cron expression matches the current
    minute (and hasn't fired this minute yet), triggers the agent via
    the agent manager's handle_message flow.
    """

    def __init__(self, agent_configs: dict[str, dict]):
        self.jobs: list[ScheduledJob] = []
        self._load_jobs(agent_configs)
        self._running = False

    def _load_jobs(self, agent_configs: dict[str, dict]) -> None:
        """Extract schedule entries from agent configs."""
        for agent_id, cfg in agent_configs.items():
            schedules = cfg.get("schedule", [])
            if not schedules:
                continue
            # Support single dict or list of dicts
            if isinstance(schedules, dict):
                schedules = [schedules]
            for sched in schedules:
                cron_expr = sched.get("cron")
                prompt = sched.get("prompt")
                channel = sched.get("channel")
                if not all([cron_expr, prompt, channel]):
                    logger.warning(
                        f"Agent {agent_id}: incomplete schedule entry "
                        f"(need cron, prompt, channel): {sched}"
                    )
                    continue
                job = ScheduledJob(
                    agent_id=agent_id,
                    cron=cron_expr,
                    prompt=prompt,
                    channel=str(channel),
                    bot=sched.get("bot"),
                )
                self.jobs.append(job)
                logger.info(
                    f"Scheduled: {agent_id} [{cron_expr}] → channel {channel}"
                )

    async def start(self, agent_manager: "AgentManager") -> None:
        """Run the scheduler loop. Call via asyncio.create_task()."""
        if not self.jobs:
            logger.info("Scheduler: no jobs configured")
            return

        self._running = True
        logger.info(f"Scheduler started with {len(self.jobs)} job(s)")

        while self._running:
            try:
                await self._tick(agent_manager)
            except Exception as e:
                logger.error(f"Scheduler tick error: {e}", exc_info=True)

            # Sleep 30 seconds between checks
            await asyncio.sleep(30)

    def stop(self) -> None:
        """Signal the scheduler to stop."""
        self._running = False

    async def _tick(self, agent_manager: "AgentManager") -> None:
        """Check all jobs against current time and fire matches."""
        now = datetime.now()
        now_ts = time.time()

        for job in self.jobs:
            if not cron_matches(job.cron, now):
                continue

            # Don't fire twice in the same minute
            last_fired_minute = datetime.fromtimestamp(job.last_fired).strftime(
                "%Y-%m-%d %H:%M"
            ) if job.last_fired else ""
            current_minute = now.strftime("%Y-%m-%d %H:%M")

            if last_fired_minute == current_minute:
                continue

            job.last_fired = now_ts
            logger.info(
                f"Scheduler firing: {job.agent_id} [{job.cron}] → {job.channel}"
            )

            # Fire in background — don't block the scheduler
            asyncio.create_task(
                self._fire_job(agent_manager, job),
                name=f"sched-{job.agent_id}-{now.strftime('%H%M')}",
            )

    async def _fire_job(
        self, agent_manager: "AgentManager", job: ScheduledJob
    ) -> None:
        """Fire a scheduled job by synthesizing an IncomingMessage."""
        from src.core.base import IncomingMessage

        message = IncomingMessage(
            content=job.prompt,
            user_id="scheduler",
            user_name="Scheduler",
            channel_id=job.channel,
            connector="discord",
            bot_account=job.bot,
        )

        try:
            await agent_manager.handle_message(job.agent_id, message)
            logger.info(f"Scheduler job completed: {job.agent_id}")
        except Exception as e:
            logger.error(
                f"Scheduler job failed: {job.agent_id}: {e}", exc_info=True
            )
