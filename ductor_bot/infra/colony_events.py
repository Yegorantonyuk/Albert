"""Bounded metadata-only execution journal shared by Colony readers.

Append/rotation is process-safe. No prompts, response bodies, raw tool arguments,
chat IDs or exception messages are accepted as fields. Failure to write telemetry
never interrupts work. Consumers must treat missing events as unknown outcomes.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # Windows has no flock; do not break the provider runtime.
    fcntl = None  # type: ignore[assignment]

MAX_BYTES = 4 * 1024 * 1024
_ALLOWED = frozenset(
    {
        "run_id",
        "kind",
        "event",
        "provider",
        "model",
        "task_id",
        "parent_id",
        "cron_id",
        "session_id",
        "name",
        "stage",
        "tool",
        "result_ref",
    }
)
_SECRET = re.compile(
    r"(?:sk-[\w-]{12,}|gh[pousr]_[\w]{16,}|github_pat_[\w]{16,}|xox[baprs]-[\w-]{10,}|\b\d{8,10}:AA[\w-]{15,})"
)
_ASSIGNMENT = re.compile(
    r"(?i)\b([\w-]*(?:token|secret|password|api[_-]?key)[\w-]*\s*[:=]\s*)[^\s,;]+"
)
CURRENT_RUN: contextvars.ContextVar[ExecutionRun | None] = contextvars.ContextVar(
    "colony_run", default=None
)


def scrub(value: object, limit: int = 160, *, multiline: bool = False) -> str:
    text = _SECRET.sub("[redacted]", str(value or ""))
    text = _ASSIGNMENT.sub(r"\1[redacted]", text)
    text = re.sub(r"https?://[^\s/@]+:[^\s/@]+@", "https://[redacted]@", text)
    controls = (
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]"
        if multiline
        else r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]"
    )
    text = re.sub(controls, " ", text)
    return (
        (text if multiline else " ".join(text.split()))
        .encode("utf-8")[:limit]
        .decode("utf-8", errors="ignore")
    )


def event_path(home: Path) -> Path:
    if home.parent.name == "agents":
        home = home.parent.parent
    return home / "colony_events.jsonl"


def append_event(path: Path | None, **fields: object) -> None:
    if path is None:
        return
    record = {
        key: scrub(value) for key, value in fields.items() if key in _ALLOWED and value is not None
    }
    record.update(v=1, event_id=uuid.uuid4().hex, at=datetime.now(UTC).isoformat())
    payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with (path.with_suffix(".lock")).open("a", encoding="utf-8") as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if path.exists() and path.stat().st_size + len(payload) > MAX_BYTES:
                path.replace(path.with_name(path.name + ".1"))
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(descriptor, payload)
            finally:
                os.close(descriptor)
    except (OSError, ValueError):
        return


class ExecutionRun:
    def __init__(self, path: Path | None, *, run_id: str | None = None, **fields: Any) -> None:
        self.path = path
        self.run_id = run_id or uuid.uuid4().hex
        self.fields = {**fields, "run_id": self.run_id}
        self.finished = False
        self._last_stage: tuple[str, str] | None = None

    def emit(self, event: str, **fields: object) -> None:
        append_event(self.path, **{**self.fields, "event": event, **fields})

    def stage(self, stage: str, tool: str = "") -> None:
        if self.finished:
            return
        state = (scrub(stage), scrub(tool))
        if state != self._last_stage:
            self._last_stage = state
            self.emit("stage", stage=state[0], tool=state[1])

    def finish(self, event: str, **fields: object) -> None:
        if not self.finished:
            self.finished = True
            self.emit(event, **fields)


def current_stage(stage: str, tool: str = "") -> None:
    run = CURRENT_RUN.get()
    if run is not None:
        if tool:
            value = tool.lower()
            stage = (
                "mcp"
                if value.startswith("mcp")
                else "research"
                if "search" in value
                else "reading"
                if value in {"read", "glob", "grep"}
                else "building"
                if value in {"edit", "write", "apply_patch", "filechange"}
                else "working"
            )
        run.stage(stage.lower(), tool)


def record_cli_execution(method: Any) -> Any:
    """Wrap the existing CLI execution path; nested fallbacks share one run."""

    @functools.wraps(method)
    async def wrapped(service: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        existing = CURRENT_RUN.get()
        if existing is not None:
            return await method(service, request, *args, **kwargs)
        path = service._config.event_log_path
        provider, model = service.resolve_provider(request)
        run = ExecutionRun(
            Path(path) if path else None,
            kind="reply",
            provider=provider,
            model=model,
            name=service._config.agent_name + " reply",
            session_id=request.resume_session or "",
        )
        token = CURRENT_RUN.set(run)
        run.emit("started")
        try:
            response = await method(service, request, *args, **kwargs)
            aborted = (
                response.returncode in (-15, -9, 137, 143)
                or service._process_registry.was_aborted(request.chat_id)
                or service._process_registry.was_aborted_topic(request.chat_id, request.topic_id)
            )
            run.finish(
                "cancelled" if aborted else "failed" if response.is_error else "completed",
                session_id=response.session_id or "",
            )
        except asyncio.CancelledError:
            run.finish("cancelled")
            raise
        except Exception:
            run.finish("failed")
            raise
        else:
            return response
        finally:
            CURRENT_RUN.reset(token)

    return wrapped
