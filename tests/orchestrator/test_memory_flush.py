"""Tests for MemoryFlusher (#77 flush + #80 compaction)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import MemoryCompactionConfig, MemoryFlushConfig
from ductor_bot.orchestrator.memory_flush import MemoryFlusher
from ductor_bot.session import SessionKey
from ductor_bot.session.manager import ProviderSessionData, SessionData
from ductor_bot.workspace.paths import DuctorPaths


def _session_with_id(session_id: str) -> SessionData:
    s = SessionData(chat_id=101, provider="claude", model="opus")
    s.provider_sessions["claude"] = ProviderSessionData(
        session_id=session_id, message_count=3, total_cost_usd=0.01, total_tokens=100
    )
    return s


def _make_paths(tmp_path: Path, mainmemory_lines: int = 0) -> DuctorPaths:
    paths = DuctorPaths(ductor_home=tmp_path)
    paths.mainmemory_path.parent.mkdir(parents=True, exist_ok=True)
    if mainmemory_lines > 0:
        content = "\n".join(f"- entry {i}" for i in range(mainmemory_lines))
        paths.mainmemory_path.write_text(content, encoding="utf-8")
    else:
        paths.mainmemory_path.write_text("", encoding="utf-8")
    return paths


def _make_flusher(
    tmp_path: Path,
    *,
    mainmemory_lines: int = 0,
    flush_cfg: MemoryFlushConfig | None = None,
    compact_cfg: MemoryCompactionConfig | None = None,
) -> tuple[MemoryFlusher, AsyncMock]:
    cli = AsyncMock()
    cli.execute = AsyncMock(return_value=AgentResponse(result=""))
    paths = _make_paths(tmp_path, mainmemory_lines=mainmemory_lines)
    flusher = MemoryFlusher(
        flush_cfg or MemoryFlushConfig(),
        cli,
        compact_cfg or MemoryCompactionConfig(),
        paths,
    )
    return flusher, cli


# ---------------------------------------------------------------------------
# #77 -- pre-compaction silent flush
# ---------------------------------------------------------------------------


async def test_memory_flusher_fires_silent_turn_after_boundary(tmp_path: Path) -> None:
    """mark_boundary + maybe_flush triggers a silent cli.execute with flush prompt."""
    # Disable compaction so this test isolates flush behavior.
    flusher, cli = _make_flusher(tmp_path, compact_cfg=MemoryCompactionConfig(enabled=False))
    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")
    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 1
    request = cli.execute.await_args[0][0]
    assert request.prompt == MemoryFlushConfig().flush_prompt
    assert request.resume_session == "sess-abc"
    assert request.chat_id == 101
    assert request.process_label == "memory_flush"


async def test_memory_flusher_dedup_within_window(tmp_path: Path) -> None:
    """Two boundaries within dedup_seconds cause only one flush."""
    flusher, cli = _make_flusher(
        tmp_path,
        flush_cfg=MemoryFlushConfig(dedup_seconds=300),
        compact_cfg=MemoryCompactionConfig(enabled=False),
    )
    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")

    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)
    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 1


async def test_memory_flusher_skips_when_no_session_id(tmp_path: Path) -> None:
    """Flush is a no-op when the session has no resume session_id yet."""
    flusher, cli = _make_flusher(tmp_path, compact_cfg=MemoryCompactionConfig(enabled=False))
    key = SessionKey(chat_id=101)
    session = _session_with_id("")
    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 0


# ---------------------------------------------------------------------------
# #80 -- LLM-driven compaction
# ---------------------------------------------------------------------------


async def test_memory_flusher_runs_compaction_when_file_exceeds_threshold(
    tmp_path: Path,
) -> None:
    """MAINMEMORY.md >= trigger_lines -> flush THEN compaction fire."""
    flusher, cli = _make_flusher(
        tmp_path,
        mainmemory_lines=80,
        compact_cfg=MemoryCompactionConfig(trigger_lines=70, target_lines=40),
    )
    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")

    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 2
    flush_call = cli.execute.await_args_list[0][0][0]
    compact_call = cli.execute.await_args_list[1][0][0]
    assert flush_call.process_label == "memory_flush"
    assert compact_call.process_label == "memory_compact"
    assert "MEMORY COMPACTION" in compact_call.prompt
    assert "40" in compact_call.prompt
    assert compact_call.resume_session == "sess-abc"


async def test_memory_flusher_skips_compaction_when_file_under_threshold(
    tmp_path: Path,
) -> None:
    """Small MAINMEMORY.md -> only flush fires, no compaction."""
    flusher, cli = _make_flusher(
        tmp_path,
        mainmemory_lines=10,
        compact_cfg=MemoryCompactionConfig(trigger_lines=70),
    )
    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")

    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 1
    assert cli.execute.await_args[0][0].process_label == "memory_flush"


async def test_memory_flusher_skips_compaction_when_disabled(tmp_path: Path) -> None:
    """memory_compaction.enabled=False -> no compaction regardless of size."""
    flusher, cli = _make_flusher(
        tmp_path,
        mainmemory_lines=200,
        compact_cfg=MemoryCompactionConfig(enabled=False, trigger_lines=70),
    )
    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")

    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 1
    assert cli.execute.await_args[0][0].process_label == "memory_flush"
