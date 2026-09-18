"""Origin transport payload tests for bundled async-work tools."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.request import Request

import pytest

_TOOLS_DIR = (
    Path(__file__).resolve().parents[2]
    / "ductor_bot"
    / "_home_defaults"
    / "workspace"
    / "tools"
)


def _load_tool(name: str, relative_path: str) -> ModuleType:
    path = _TOOLS_DIR / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("transport", ["mx", "tg"])
def test_create_task_payload_carries_environment_transport(
    monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    module = _load_tool(
        "_ductor_test_create_task_transport",
        "task_tools/create_task.py",
    )
    captured: dict[str, Any] = {}

    def post_json(url: str, body: dict[str, object], *, timeout: int) -> dict[str, object]:
        captured.update(url=url, body=body, timeout=timeout)
        return {"success": True, "task_id": "task1"}

    module._load_shared = lambda: (lambda path: f"http://internal{path}", post_json, lambda: "main")
    monkeypatch.setattr(sys, "argv", ["create_task.py", "do work"])
    monkeypatch.setenv("DUCTOR_TRANSPORT", transport)
    monkeypatch.setenv("DUCTOR_CHAT_ID", "123")
    monkeypatch.setenv("DUCTOR_TOPIC_ID", "7")

    module.main()

    assert captured["body"] == {
        "from": "main",
        "prompt": "do work",
        "transport": transport,
        "chat_id": 123,
        "topic_id": 7,
    }


class _Response:
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return b'{"success": true, "task_id": "ia1"}'


@pytest.mark.parametrize("transport", ["dc", "tg"])
def test_ask_agent_async_payload_carries_environment_transport(
    monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    module = _load_tool(
        "_ductor_test_ask_agent_async_transport",
        "agent_tools/ask_agent_async.py",
    )
    captured: dict[str, object] = {}

    def urlopen(request: Request, *, timeout: int) -> _Response:
        captured.update(request=request, timeout=timeout)
        return _Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sys, "argv", ["ask_agent_async.py", "worker", "do work"])
    monkeypatch.setenv("DUCTOR_AGENT_NAME", "main")
    monkeypatch.setenv("DUCTOR_TRANSPORT", transport)
    monkeypatch.setenv("DUCTOR_CHAT_ID", "123")
    monkeypatch.setenv("DUCTOR_TOPIC_ID", "7")

    module.main()

    request = captured["request"]
    assert isinstance(request, Request)
    assert json.loads(request.data or b"{}") == {
        "from": "main",
        "to": "worker",
        "message": "do work",
        "transport": transport,
        "chat_id": 123,
        "topic_id": 7,
    }
