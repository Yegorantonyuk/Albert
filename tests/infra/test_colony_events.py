"""Journal privacy, bounded storage and real normalized CLI events."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ductor_bot.cli.stream_events import AssistantTextDelta, ResultEvent, ToolUseEvent
from ductor_bot.cli.types import AgentRequest
from ductor_bot.infra import colony_events
from ductor_bot.infra.colony_events import append_event
from tests.cli.test_service import _make_service


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_journal_scrubs_secrets_and_accepts_only_metadata(tmp_path):
    path = tmp_path / "colony_events.jsonl"
    secret = "sk-" + "S" * 48
    append_event(
        path,
        run_id="r",
        kind="task",
        event="started",
        name="Work " + secret,
        prompt="private prompt",
        arguments={"token": secret},
        result="private answer",
    )
    row = records(path)[0]
    assert secret not in path.read_text()
    assert "private" not in path.read_text()
    assert not {"prompt", "arguments", "result"}.intersection(row)
    assert row["v"] == 1
    assert row["at"]
    assert row["event_id"]


def test_rotation_is_bounded_and_parallel_appends_remain_json(tmp_path, monkeypatch):
    path = tmp_path / "colony_events.jsonl"
    monkeypatch.setattr(colony_events, "MAX_BYTES", 1500)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: append_event(path, run_id=str(i), event="started"), range(80)))
    assert path.stat().st_size <= 1500
    previous = path.with_name(path.name + ".1")
    assert previous.stat().st_size <= 1500
    assert records(path)
    assert records(previous)
    assert len(list(tmp_path.glob("*.jsonl*"))) == 2


def test_journal_failure_does_not_interrupt_work(tmp_path):
    parent = tmp_path / "file"
    parent.write_text("not directory")
    append_event(parent / "events.jsonl", event="started")


async def test_cli_stream_records_real_stages_once_without_contents(tmp_path):
    path = tmp_path / "colony_events.jsonl"
    service = _make_service()
    service.update_config(replace(service._config, event_log_path=str(path)))

    async def stream(**_kwargs):
        yield ToolUseEvent(type="assistant", tool_name="Read")
        yield ToolUseEvent(type="assistant", tool_name="Read")
        yield ToolUseEvent(type="assistant", tool_name="mcp__web__search")
        yield AssistantTextDelta(type="assistant", text="PRIVATE ANSWER")
        yield ResultEvent(type="result", session_id="real-session", result="PRIVATE ANSWER")

    cli = MagicMock()
    cli.send_streaming = stream
    with patch("ductor_bot.cli.service.create_cli", return_value=cli):
        answer = await service.execute_streaming(AgentRequest(prompt="PRIVATE PROMPT"))
    rows = records(path)
    assert answer.result == "PRIVATE ANSWER"
    assert [r["event"] for r in rows] == ["started", "stage", "stage", "stage", "completed"]
    assert [r["stage"] for r in rows if r["event"] == "stage"] == ["reading", "mcp", "replying"]
    assert len({r["run_id"] for r in rows}) == 1
    assert rows[-1]["session_id"] == "real-session"
    assert "PRIVATE" not in path.read_text()


async def test_cancelled_foreground_emits_cancelled(tmp_path):
    path = tmp_path / "colony_events.jsonl"
    service = _make_service()
    service.update_config(replace(service._config, event_log_path=str(path)))

    async def stream(**_kwargs):
        raise asyncio.CancelledError
        yield

    cli = MagicMock()
    cli.send_streaming = stream
    with (
        patch("ductor_bot.cli.service.create_cli", return_value=cli),
        pytest.raises(asyncio.CancelledError),
    ):
        await service.execute_streaming(AgentRequest(prompt="p"))
    assert [r["event"] for r in records(path)] == ["started", "cancelled"]
