"""Advertised resume/model capabilities require actual provider metadata."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from ductor_bot.cli.antigravity_discovery import _parse_models
from ductor_bot.cli.antigravity_events import parse_antigravity_json, parse_antigravity_session_id
from tests.cli.test_antigravity_provider import _make_cli


def test_plain_output_never_invents_a_resumable_session():
    assert parse_antigravity_session_id("plain answer") is None
    assert parse_antigravity_session_id('{"id":"unrelated","response":"answer"}') is None
    assert (
        parse_antigravity_session_id('{"conversation_id":"real-conversation"}')
        == "real-conversation"
    )
    assert parse_antigravity_session_id('{"session_id":"real-session"}') == "real-session"


async def test_provider_preserves_returned_conversation_id():
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (b'{"conversation_id":"real","response":"answer"}', b"")
    with patch(
        "ductor_bot.cli.antigravity_provider.asyncio.create_subprocess_exec", return_value=process
    ):
        response = await _make_cli().send("question")
    assert response.session_id == "real"
    assert response.result == "answer"


def test_model_discovery_uses_launchable_slug_from_tsv():
    assert _parse_models("gemini-high\tGemini (High)\nClaude Opus\n") == (
        "gemini-high",
        "Claude Opus",
    )


def test_plain_response_keeps_long_results_with_bounded_storage():
    value = "Result\n" + "я" * 100000
    result = parse_antigravity_json(value)
    assert len(result) > 2000
    assert len(result.encode("utf-8")) <= 128 * 1024
