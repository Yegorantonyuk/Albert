"""Tests for MemoryFlusher (#77 — pre-compaction silent flush)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import MemoryFlushConfig
from ductor_bot.orchestrator.memory_flush import MemoryFlusher
from ductor_bot.session import SessionKey
from ductor_bot.session.manager import ProviderSessionData, SessionData


def _session_with_id(session_id: str) -> SessionData:
    s = SessionData(chat_id=101, provider="claude", model="opus")
    s.provider_sessions["claude"] = ProviderSessionData(
        session_id=session_id, message_count=3, total_cost_usd=0.01, total_tokens=100
    )
    return s


async def test_memory_flusher_fires_silent_turn_after_boundary() -> None:
    """mark_boundary + maybe_flush triggers a silent cli.execute with flush prompt."""
    cli = AsyncMock()
    cli.execute = AsyncMock(return_value=AgentResponse(result="FLUSH_NOOP"))
    cfg = MemoryFlushConfig()
    flusher = MemoryFlusher(cfg, cli)

    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")
    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 1
    request = cli.execute.await_args[0][0]
    assert request.prompt == cfg.flush_prompt
    assert request.resume_session == "sess-abc"
    assert request.chat_id == 101
    assert request.process_label == "memory_flush"


async def test_memory_flusher_dedup_within_window() -> None:
    """Two boundaries within dedup_seconds cause only one flush."""
    cli = AsyncMock()
    cli.execute = AsyncMock(return_value=AgentResponse(result=""))
    cfg = MemoryFlushConfig(dedup_seconds=300)
    flusher = MemoryFlusher(cfg, cli)

    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")

    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)
    flusher.mark_boundary(key)  # second boundary inside window
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 1


async def test_memory_flusher_skips_when_no_session_id() -> None:
    """Flush is a no-op when the session has no resume session_id yet."""
    cli = AsyncMock()
    cli.execute = AsyncMock(return_value=AgentResponse(result=""))
    flusher = MemoryFlusher(MemoryFlushConfig(), cli)

    key = SessionKey(chat_id=101)
    session = _session_with_id("")  # empty session_id
    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 0
