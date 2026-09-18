"""Colony tasks retain answers/questions without messenger side effects."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from ductor_bot.tasks.hub import TaskHub
from ductor_bot.tasks.registry import TaskRegistry
from tests.cron.test_observer import _make_paths
from tests.tasks.test_hub import _make_cli_service, _make_config, _submit


def make_hub(tmp_path, cli=None):
    paths = _make_paths(tmp_path)
    registry = TaskRegistry(paths.tasks_registry_path, paths.tasks_dir)
    hub = TaskHub(registry, paths, cli_service=cli or _make_cli_service(), config=_make_config())
    hub.set_agent_chat_id("main", 777)
    handler = AsyncMock()
    hub.set_result_handler("main", handler)
    return hub, paths, handler


async def test_api_task_preserves_zero_chat_and_full_result_without_delivery(tmp_path):
    full = "# Result\n\n" + "done " * 300 + "\nAPI_KEY=abcdefghijklmno"
    hub, paths, handler = make_hub(tmp_path, _make_cli_service(result=full))
    task_id = hub.submit(replace(_submit(), chat_id=0, transport="api"))
    await hub._in_flight[task_id].asyncio_task
    entry = hub.registry.get(task_id)
    assert entry.chat_id == 0
    assert entry.transport == "api"
    assert entry.status == "done"
    result = (hub.registry.task_folder(task_id) / "RESULT.md").read_text()
    assert result.startswith("# Result\n\n")
    assert len(result) > 1000
    assert "abcdefghijklmno" not in result
    handler.assert_not_awaited()
    rows = [
        json.loads(line)
        for line in (paths.ductor_home / "colony_events.jsonl").read_text().splitlines()
    ]
    assert [r["event"] for r in rows] == ["started", "completed"]
    assert all(r["task_id"] == task_id for r in rows)


async def test_question_without_messenger_handler_persists_and_waits(tmp_path):
    cli = _make_cli_service()
    hub, paths, handler = make_hub(tmp_path, cli)
    response = cli.execute.return_value

    async def execute(request):
        task_id = request.process_label.split(":", 1)[1]
        answer = await hub.forward_question(task_id, "Which color? API_KEY=abcdefghijklmnop")
        assert answer.startswith("Question saved")
        return response

    cli.execute.side_effect = execute
    task_id = hub.submit(replace(_submit(), chat_id=0, transport="api"))
    await hub._in_flight[task_id].asyncio_task
    entry = hub.registry.get(task_id)
    assert entry.status == "waiting"
    assert "Which color?" in entry.last_question
    assert "abcdefghijklmnop" not in entry.last_question
    assert entry.question_count == 1
    handler.assert_not_awaited()
    assert '"event":"waiting"' in (paths.ductor_home / "colony_events.jsonl").read_text()


async def test_submission_idempotency_survives_registry_reload(tmp_path):
    hub, paths, _ = make_hub(tmp_path)
    submit = replace(_submit(), chat_id=0, transport="api", request_id="request-unique")
    first = hub.submit(submit)
    await hub._in_flight[first].asyncio_task
    assert hub.submit(submit) == first
    assert len(hub.registry.list_all()) == 1
    reloaded = TaskRegistry(paths.tasks_registry_path, paths.tasks_dir)
    assert reloaded.get(first).request_id == "request-unique"
    with pytest.raises(ValueError, match="another task"):
        hub.submit(replace(submit, prompt="different"))


async def test_resume_into_colony_suppresses_original_transport(tmp_path):
    hub, _, handler = make_hub(tmp_path)
    first = hub.submit(_submit())
    await hub._in_flight[first].asyncio_task
    handler.reset_mock()
    hub.resume(first, "followup", parent_agent="main", transport="api")
    await hub._in_flight[first].asyncio_task
    assert hub.registry.get(first).transport == "api"
    assert hub.registry.get(first).chat_id == 0
    handler.assert_not_awaited()


async def test_false_parent_id_rejected_before_creating_task(tmp_path):
    hub, _, _ = make_hub(tmp_path)
    with pytest.raises(ValueError, match="Unknown parent"):
        hub.submit(replace(_submit(), parent_id="does-not-exist"))
    assert hub.registry.list_all() == []


async def test_real_cli_service_task_stream_keeps_one_task_run(tmp_path):
    from unittest.mock import MagicMock, patch

    from ductor_bot.cli.stream_events import AssistantTextDelta, ResultEvent, ToolUseEvent
    from tests.cli.test_service import _make_service

    service = _make_service()
    hub, paths, handler = make_hub(tmp_path, service)

    async def stream(**_kwargs):
        yield ToolUseEvent(type="assistant", tool_name="Read")
        yield AssistantTextDelta(type="assistant", text="answer")
        yield ResultEvent(type="result", session_id="s1", result="answer")

    cli = MagicMock()
    cli.send_streaming = stream
    with patch("ductor_bot.cli.service.create_cli", return_value=cli):
        task_id = hub.submit(replace(_submit(), chat_id=0, transport="api"))
        await hub._in_flight[task_id].asyncio_task
    events = [
        json.loads(line)
        for line in (paths.ductor_home / "colony_events.jsonl").read_text().splitlines()
    ]
    assert len({event["run_id"] for event in events}) == 1
    assert {event["kind"] for event in events} == {"task"}
    assert {event.get("stage") for event in events} >= {"reading", "replying"}
    assert events[-1]["event"] == "completed"
    handler.assert_not_awaited()
