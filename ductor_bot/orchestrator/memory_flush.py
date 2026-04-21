"""Pre-compaction silent memory flush + LLM-driven compaction (#77, #80).

When the CLI emits ``CompactBoundaryEvent`` mid-stream, this helper runs a
silent follow-up turn that instructs the agent to APPEND durable facts to
``memory_system/MAINMEMORY.md`` so the post-compaction context retains what
the user just told us (#77). When the resulting file grows past
``trigger_lines``, a second silent turn chains in to rewrite the file
densely -- preserving recent entries verbatim and compressing older
clusters into one dense semantic entry each (#80).

Design notes:
- Boundary detection is additive: unsubscribed callers see no change.
- Dedup is in-memory (``dict[SessionKey, float]`` of monotonic timestamps).
  Process restart is a natural reset; a duplicate flush would cost at most
  one extra CLI call, never corrupt memory.
- Errors during flush or compaction are logged at WARNING and swallowed.
  Memory maintenance must never delay the user turn.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from ductor_bot.cli.types import AgentRequest
from ductor_bot.errors import CLIError
from ductor_bot.workspace.loader import read_mainmemory

if TYPE_CHECKING:
    from ductor_bot.cli.service import CLIService
    from ductor_bot.config import MemoryCompactionConfig, MemoryFlushConfig
    from ductor_bot.session import SessionKey
    from ductor_bot.session.manager import SessionData
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)


class MemoryFlusher:
    """Tracks pre-compaction boundary events and runs silent flush + compact turns."""

    def __init__(
        self,
        config: MemoryFlushConfig,
        cli_service: CLIService,
        compaction_config: MemoryCompactionConfig,
        paths: DuctorPaths,
    ) -> None:
        self._config = config
        self._cli = cli_service
        self._compaction = compaction_config
        self._paths = paths
        self._boundary_seen: set[SessionKey] = set()
        self._last_flushed: dict[SessionKey, float] = {}

    def mark_boundary(self, key: SessionKey) -> None:
        """Record that a CompactBoundaryEvent was seen for this session."""
        self._boundary_seen.add(key)
        logger.debug("Memory flush: boundary marked chat=%d", key.chat_id)

    def should_flush(self, key: SessionKey) -> bool:
        """True when a flush is due for this session key."""
        if key not in self._boundary_seen:
            return False
        last = self._last_flushed.get(key)
        if last is None:
            return True
        return (time.monotonic() - last) > self._config.dedup_seconds

    def should_compact(self) -> bool:
        """True when compaction is enabled and MAINMEMORY.md exceeds threshold."""
        if not self._compaction.enabled:
            return False
        content = read_mainmemory(self._paths)
        line_count = len(content.splitlines())
        return line_count >= self._compaction.trigger_lines

    async def maybe_flush(self, key: SessionKey, session: SessionData) -> None:
        """Run the silent flush turn if due, and compaction if file is large."""
        if not self.should_flush(key):
            return
        await self.flush(key, session)
        if self.should_compact():
            await self.compact(key, session)

    async def flush(self, key: SessionKey, session: SessionData) -> None:
        """Run a silent flush turn resuming the current session."""
        session_id = session.session_id
        if not session_id:
            logger.debug("Memory flush skipped chat=%d: no resume session_id", key.chat_id)
            self._boundary_seen.discard(key)
            return

        request = AgentRequest(
            prompt=self._config.flush_prompt,
            chat_id=key.chat_id,
            topic_id=key.topic_id,
            resume_session=session_id,
            process_label="memory_flush",
        )
        logger.info("Memory flush firing chat=%d session=%s", key.chat_id, session_id[:8])
        try:
            await self._cli.execute(request)
        except (CLIError, RuntimeError, OSError) as exc:
            logger.warning("Memory flush failed chat=%d: %s", key.chat_id, exc)
        finally:
            self._last_flushed[key] = time.monotonic()
            self._boundary_seen.discard(key)

    async def compact(self, key: SessionKey, session: SessionData) -> None:
        """Run a silent compaction turn resuming the current session."""
        session_id = session.session_id
        if not session_id:
            logger.debug(
                "Memory compaction skipped chat=%d: no resume session_id",
                key.chat_id,
            )
            return

        prompt = self._compaction.prompt.format(
            target_lines=self._compaction.target_lines,
            preserve_days=self._compaction.preserve_recency_days,
        )
        request = AgentRequest(
            prompt=prompt,
            chat_id=key.chat_id,
            topic_id=key.topic_id,
            resume_session=session_id,
            process_label="memory_compact",
        )
        logger.info(
            "Memory compaction firing chat=%d session=%s",
            key.chat_id,
            session_id[:8],
        )
        try:
            await self._cli.execute(request)
        except (CLIError, RuntimeError, OSError) as exc:
            logger.warning("Memory compaction failed chat=%d: %s", key.chat_id, exc)
