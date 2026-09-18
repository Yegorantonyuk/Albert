"""Per-command continuation identity and atomic result attribution."""

import asyncio
import json
from dataclasses import replace

import pytest

from ductor_bot.tasks.registry import TaskRegistry
from tests.tasks.test_colony_tasks import make_hub
from tests.tasks.test_hub import _make_cli_service, _submit


async def test_continuation_idempotency_survives_multiple_runs_and_registry_reload(tmp_path):
    cli = _make_cli_service(result="first")
    hub, paths, handler = make_hub(tmp_path, cli)
    task_id = hub.submit(replace(_submit(), chat_id=0, transport="api", request_id="create-id-01"))
    await hub._in_flight[task_id].asyncio_task
    entry = hub.registry.get(task_id)
    assert entry.request_id == entry.last_request_id == entry.result_request_id == "create-id-01"
    first = json.loads((hub.registry.task_folder(task_id) / "RESULT.json").read_text())
    assert first["request_id"] == "create-id-01"
    assert first["result"] == "first"
    cli.execute.return_value.result = "second"
    hub.resume(
        task_id, "followup", parent_agent="main", transport="api", request_id="message-id-01"
    )
    running_handle = hub._in_flight[task_id].asyncio_task
    assert (
        hub.resume(
            task_id, "followup", parent_agent="main", transport="api", request_id="message-id-01"
        )
        == task_id
    )
    assert hub._in_flight[task_id].asyncio_task is running_handle
    await running_handle
    second = json.loads((hub.registry.task_folder(task_id) / "RESULT.json").read_text())
    assert second["request_id"] == "message-id-01"
    assert second["result"] == "second"
    assert entry.request_id == "create-id-01"
    assert entry.result_request_id == "message-id-01"
    with pytest.raises(ValueError, match="another task continuation"):
        hub.resume(
            task_id, "different", parent_agent="main", transport="api", request_id="message-id-01"
        )
    with pytest.raises(ValueError, match="authorized"):
        hub.resume(
            task_id,
            "followup",
            parent_agent="intruder",
            transport="api",
            request_id="message-id-01",
        )
    hub.resume(task_id, "third", parent_agent="main", transport="api", request_id="message-id-02")
    await hub._in_flight[task_id].asyncio_task
    assert (
        hub.resume(
            task_id, "followup", parent_agent="main", transport="api", request_id="message-id-01"
        )
        == task_id
    )
    reloaded = TaskRegistry(paths.tasks_registry_path, paths.tasks_dir)
    assert reloaded.get(task_id).resume_requests == entry.resume_requests
    assert reloaded.get(task_id).last_request_id == "message-id-02"
    handler.assert_not_awaited()


@pytest.mark.parametrize("failure", ["exception", "cancel", "waiting"])
async def test_non_success_outcomes_are_bound_to_the_current_command(tmp_path, failure):
    cli = _make_cli_service(result="partial")
    hub, _, handler = make_hub(tmp_path, cli)
    gate = asyncio.Event()
    response = cli.execute.return_value

    async def execute(request):
        if failure == "exception":
            raise RuntimeError("private internal details")
        if failure == "cancel":
            gate.set()
            await asyncio.Event().wait()
        task_id = request.process_label.split(":", 1)[1]
        await hub.forward_question(task_id, "Which option?")
        return response

    cli.execute.side_effect = execute
    task_id = hub.submit(
        replace(_submit(), chat_id=0, transport="api", request_id="current-command-1")
    )
    handle = hub._in_flight[task_id].asyncio_task
    if failure == "cancel":
        await gate.wait()
        await hub.cancel(task_id)
    else:
        await handle
    entry = hub.registry.get(task_id)
    envelope = json.loads((hub.registry.task_folder(task_id) / "RESULT.json").read_text())
    assert envelope["request_id"] == entry.result_request_id == "current-command-1"
    assert (
        envelope["status"]
        == {"exception": "failed", "cancel": "cancelled", "waiting": "waiting"}[failure]
    )
    assert "private internal details" not in json.dumps(envelope)
    if failure == "waiting":
        assert envelope["question"] == "Which option?"
    handler.assert_not_awaited()
