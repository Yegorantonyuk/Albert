"""Output parsing for the Antigravity CLI (agy).

agy ``--print`` returns the final answer as plain text (occasionally wrapped
in a small JSON envelope), so only batch extraction is needed.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def parse_antigravity_json(raw: str) -> str:
    """Extract result text from Antigravity CLI ``--print`` output.

    Tries to parse as JSON; falls back to raw text bounded to 128 KiB.
    """
    if not raw:
        return ""
    raw = raw.strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            # "response" is the key used by `agy --output-format json`'s
            # print-mode envelope (confirmed via live run); the rest are
            # kept as fallbacks for other JSON shapes agy may emit.
            for key in ("response", "content", "result", "text", "message"):
                val = parsed.get(key)
                if isinstance(val, str) and val:
                    return val
            return str(parsed)
        return str(parsed)
    except json.JSONDecodeError:
        return raw.encode("utf-8")[:128 * 1024].decode("utf-8", errors="ignore")


def parse_antigravity_session_id(raw: str) -> str | None:
    """Preserve an explicit ID if the CLI emits one; never infer an ID."""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    for key in ("conversation_id", "session_id"):
        identity = value.get(key)
        if isinstance(identity, str) and identity and len(identity) <= 128:
            return identity
    return None
