"""Append-only audit log for gateway activity.

Every authenticated action and every rejected attempt lands here as one JSON
object per line.  The file is opened in append mode and never rewritten, so a
compromised session can add entries but cannot quietly erase its own tracks.

This log doubles as the data source for the app's Activity feed: the security
requirement and the product feature are satisfied by the same records.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

Outcome = Literal["allowed", "denied", "error"]

# Values that must never reach disk even if a caller passes them in details.
_REDACTED_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "private_key",
        "secret",
        "signature",
        "token",
    }
)
_REDACTED = "[redacted]"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One recorded action."""

    timestamp: str
    action: str
    outcome: Outcome
    device_id: str | None = None
    remote_ip: str | None = None
    agent_id: str | None = None
    detail: dict[str, Any] | None = None


def _redact(details: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip credential-shaped values, at any nesting depth."""
    if not details:
        return None
    cleaned: dict[str, Any] = {}
    for key, value in details.items():
        if key.lower() in _REDACTED_KEYS:
            cleaned[key] = _REDACTED
        elif isinstance(value, dict):
            cleaned[key] = _redact(value)
        else:
            cleaned[key] = value
    return cleaned


class AuditLog:
    """Line-delimited JSON audit sink.

    Writes are serialized by a lock and flushed immediately: an audit record
    that is still sitting in a buffer when the process dies is worthless.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(  # noqa: PLR0913
        self,
        action: str,
        outcome: Outcome,
        *,
        device_id: str | None = None,
        remote_ip: str | None = None,
        agent_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append one event.  Never raises: auditing must not break a request."""
        event = AuditEvent(
            timestamp=datetime.now(UTC).isoformat(),
            action=action,
            outcome=outcome,
            device_id=device_id,
            remote_ip=remote_ip,
            agent_id=agent_id,
            detail=_redact(detail),
        )
        line = json.dumps(asdict(event), ensure_ascii=False, separators=(",", ":"))
        try:
            with self._lock, self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        except OSError:
            logger.exception("Failed to write audit event %r", action)

    def tail(self, limit: int = 100) -> list[AuditEvent]:
        """Return the most recent events, oldest first.

        Malformed lines are skipped rather than raising -- a corrupted tail must
        not make the whole Activity feed unreadable.
        """
        if limit <= 0 or not self._path.exists():
            return []
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                lines = handle.readlines()[-limit:]
        except OSError:
            logger.exception("Failed to read audit log")
            return []
        return list(self._parse(lines))

    @staticmethod
    def _parse(lines: list[str]) -> Iterator[AuditEvent]:
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw = json.loads(stripped)
                yield AuditEvent(**raw)
            except (json.JSONDecodeError, TypeError):
                continue
