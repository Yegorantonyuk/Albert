"""Transport identity regression tests for concrete bot callbacks."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ductor_bot.messenger.discord.bot import DiscordBot
from ductor_bot.messenger.matrix.bot import MatrixBot
from ductor_bot.messenger.telegram.app import TelegramBot
from ductor_bot.multiagent.bus import AsyncInterAgentResult
from ductor_bot.tasks.models import TaskResult


def _interagent_result() -> AsyncInterAgentResult:
    return AsyncInterAgentResult(
        task_id="ia1",
        sender="main",
        recipient="worker",
        message_preview="work",
        result_text="done",
        chat_id=100,
        topic_id=7,
    )


def _task_result() -> TaskResult:
    return TaskResult(
        task_id="task1",
        chat_id=100,
        parent_agent="main",
        name="work",
        prompt_preview="work",
        result_text="done",
        status="done",
        elapsed_seconds=1,
        provider="codex",
        model="model",
        thread_id=7,
    )


@pytest.mark.parametrize(
    ("bot_type", "transport"),
    [(TelegramBot, "tg"), (MatrixBot, "mx"), (DiscordBot, "dc")],
)
async def test_concrete_callbacks_submit_transport_qualified_envelopes(
    bot_type: type[TelegramBot | MatrixBot | DiscordBot],
    transport: str,
) -> None:
    bot = bot_type.__new__(bot_type)
    submit = AsyncMock()
    bot._bus = SimpleNamespace(submit=submit)
    bot._default_chat_id = lambda: 100
    bot._default_channel_id = lambda: 100

    await bot.on_async_interagent_result(_interagent_result())
    interagent = submit.await_args.args[0]
    assert interagent.transport == transport
    assert interagent.lock_key == (transport, 100, 7)

    await bot.on_task_result(_task_result())
    task_result = submit.await_args.args[0]
    assert task_result.transport == transport
    assert task_result.lock_key == (transport, 100, 7)

    await bot.on_task_question("task1", "question?", "question", 100, 7)
    task_question = submit.await_args.args[0]
    assert task_question.transport == transport
    expected_topic = 7 if transport == "tg" else None
    assert task_question.lock_key == (transport, 100, expected_topic)
