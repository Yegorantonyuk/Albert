"""Manual commands reuse scheduling lifecycle without duplicate work or delivery."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.infra.task_runner import TaskResult
from ductor_bot.multiagent.internal_api import InternalAgentAPI
from tests.cron.test_observer import _make_job, _make_manager, _make_observer, _make_paths


async def test_run_once_paused_cron_preserves_schedule_and_no_delivery(tmp_path):
    paths = _make_paths(tmp_path)
    manager = _make_manager(paths)
    manager.add_job(_make_job(enabled=False))
    (paths.cron_tasks_dir / "daily").mkdir()
    observer = _make_observer(paths, manager)
    handler = AsyncMock()
    observer.set_result_handler(handler)
    execute = AsyncMock(return_value=TaskResult("success", "# Result\n\nDone", None))
    with patch("ductor_bot.cron.observer.execute_in_task_folder", execute):
        run_id = observer.run_once("daily", "request-one")
        assert observer.run_once("daily", "request-one") == run_id
        with pytest.raises(ValueError, match="already running"):
            observer.run_once("daily", "request-other")
        await observer._manual["daily"]
    assert execute.await_count == 1
    job = manager.get_job("daily")
    assert job.schedule == "0 9 * * *"
    assert job.enabled is False
    assert job.last_run_status == "success"
    assert job.last_delivery_status == "skipped"
    handler.assert_not_awaited()
    assert (paths.cron_tasks_dir / "daily" / "RESULT.md").read_text() == "# Result\n\nDone"
    events = [
        json.loads(line)
        for line in (paths.ductor_home / "colony_events.jsonl").read_text().splitlines()
    ]
    assert [event["event"] for event in events] == ["started", "completed"]
    assert all(event["cron_id"] == "daily" and event["run_id"] == run_id for event in events)


async def test_scheduled_overlap_cannot_start_second_process(tmp_path):
    paths = _make_paths(tmp_path)
    manager = _make_manager(paths)
    manager.add_job(_make_job())
    observer = _make_observer(paths, manager)
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return "success"

    with patch.object(observer, "_execute_job_inner", side_effect=execute) as run:
        observer.run_once("daily", "request-1")
        await entered.wait()
        await observer._execute_job("daily", "instruction", "daily")
        assert run.await_count == 1
        release.set()
        await observer._manual["daily"]


async def test_http_commands_reject_missing_runtime_and_validate_requests(aiohttp_client, tmp_path):
    api = InternalAgentAPI()
    client = await aiohttp_client(api._app)
    response = await client.get("/colony/crons")
    payload = await response.json()
    assert payload["runtime"] is False
    assert not any(p["available"] for p in payload["providers"])
    response = await client.post(
        "/colony/cron", json={"action": "run", "cron_id": "x", "request_id": "request1"}
    )
    assert response.status == 503
    paths = _make_paths(tmp_path)
    manager = _make_manager(paths)
    manager.add_job(_make_job())
    observer = _make_observer(paths, manager)
    api.set_colony_runtime(observer, None)
    response = await client.post(
        "/colony/cron", json={"action": "pause", "cron_id": "daily", "request_id": "request1"}
    )
    assert response.status == 200
    assert manager.get_job("daily").enabled is False
    response = await client.post(
        "/colony/cron", json={"action": "pause", "cron_id": "missing", "request_id": "request2"}
    )
    assert response.status == 404
    response = await client.post("/colony/cron", json=["run"])
    assert response.status == 400
