"""Status-line editor: a single live-updated message showing agent activity.

Used by ``streaming.mode = "status"``. Instead of streaming response text,
one small message is kept up to date with what the agent is doing::

    🤔 Thinking · 3s
    👀 Read ×2 · 1s
    👨‍💻 Shell · 2s

The message is deleted before the final answer is delivered, so the final
answer is always a single normal message (no duplication possible).

Flood-control lessons baked in (see modules/framework-dev.md history):
- ONE ``asyncio.Lock`` guards every send/edit/delete on the status message.
- Edits are throttled (>= ``_MIN_EDIT_INTERVAL`` apart, only when content
  changed) and the ticker slows down as the turn gets longer.
- ``TelegramRetryAfter`` pushes the next allowed edit past the cooldown
  instead of retrying.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter

from ductor_bot.text.response_format import normalize_tool_name

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

logger = logging.getLogger(__name__)

_MIN_EDIT_INTERVAL = 4.0
_MAX_LINES = 10

_TOOL_EMOJI = {
    "Shell": "👨‍💻",
    "Read": "👀",
    "Glob": "🔍",
    "Grep": "🔍",
    "LS": "📁",
    "Edit": "✏️",
    "Write": "✏️",
    "MultiEdit": "✏️",
    "NotebookEdit": "✏️",
    "WebSearch": "🌐",
    "WebFetch": "🌐",
    "Task": "🤖",
    "TodoWrite": "📋",
}
_DEFAULT_TOOL_EMOJI = "🔧"
_MCP_EMOJI = "🔌"
_INITIAL_TEXT = "🤔 Thinking…"


def _tool_display(tool_name: str) -> tuple[str, str]:
    """Return ``(emoji, label)`` for a raw tool name."""
    if tool_name.startswith("mcp__"):
        return _MCP_EMOJI, tool_name.split("__")[-1] or tool_name
    name = normalize_tool_name(tool_name)
    return _TOOL_EMOJI.get(name, _DEFAULT_TOOL_EMOJI), name


def _fmt_duration(seconds: float) -> str:
    secs = max(0, int(seconds))
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m {secs % 60}s"


@dataclass(slots=True)
class _Entry:
    emoji: str
    label: str
    started: float
    ended: float | None = None
    count: int = 1


@dataclass(slots=True)
class _EditorState:
    entries: list[_Entry] = field(default_factory=list)
    message_id: int | None = None
    last_text: str = ""
    next_edit_at: float = 0.0
    finalized: bool = False


class StatusLineEditor:
    """Maintains one Telegram message reflecting current agent activity."""

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        *,
        reply_to: Message | None = None,
        thread_id: int | None = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._reply_to = reply_to
        self._thread_id = thread_id
        self._lock = asyncio.Lock()
        self._state = _EditorState()
        self._started = time.monotonic()
        self._ticker: asyncio.Task[None] | None = None

    # -- Activity notes -------------------------------------------------------

    async def note_tool(self, tool_name: str) -> None:
        """Record a tool invocation (consecutive same tools aggregate as ×N)."""
        emoji, label = _tool_display(tool_name)
        await self._note(emoji, label, count_repeats=True)

    async def note_activity(self, emoji: str, label: str) -> None:
        """Record a non-tool activity (Thinking/Writing); repeats are no-ops."""
        await self._note(emoji, label, count_repeats=False)

    async def _note(self, emoji: str, label: str, *, count_repeats: bool) -> None:
        now = time.monotonic()
        async with self._lock:
            if self._state.finalized:
                return
            entries = self._state.entries
            current = entries[-1] if entries else None
            if current is not None and current.ended is None and current.label == label:
                if count_repeats:
                    current.count += 1
                else:
                    return
            else:
                if current is not None and current.ended is None:
                    current.ended = now
                entries.append(_Entry(emoji=emoji, label=label, started=now))
            await self._maybe_edit_locked()

    # -- Lifecycle ------------------------------------------------------------

    async def finalize(self) -> None:
        """Stop updates and remove the status message. Never raises."""
        ticker = self._ticker
        self._ticker = None
        if ticker is not None:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker
        async with self._lock:
            self._state.finalized = True
            message_id = self._state.message_id
            self._state.message_id = None
        if message_id is None:
            return
        try:
            await self._bot.delete_message(chat_id=self._chat_id, message_id=message_id)
        except TelegramAPIError:
            logger.debug("Status message delete failed, leaving it in place", exc_info=True)

    # -- Rendering / editing --------------------------------------------------

    def _render(self) -> str:
        now = time.monotonic()
        lines: list[str] = []
        for entry in self._state.entries[-_MAX_LINES:]:
            duration = (entry.ended if entry.ended is not None else now) - entry.started
            label = entry.label if entry.count == 1 else f"{entry.label} ×{entry.count}"
            lines.append(f"{entry.emoji} {label} · {_fmt_duration(duration)}")
        if len(self._state.entries) > _MAX_LINES:
            lines.insert(0, "…")
        return "\n".join(lines) if lines else _INITIAL_TEXT

    def _tick_interval(self) -> float:
        """Ticker slows down on long turns to stay clear of flood limits."""
        elapsed = time.monotonic() - self._started
        if elapsed > 300:
            return 20.0
        if elapsed > 120:
            return 10.0
        return 5.0

    async def _tick_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._tick_interval())
                async with self._lock:
                    if self._state.finalized:
                        return
                    await self._maybe_edit_locked()
        except asyncio.CancelledError:
            raise
        except TelegramAPIError:
            logger.debug("Status ticker stopped on Telegram error", exc_info=True)

    async def _maybe_edit_locked(self) -> None:
        """Create or edit the status message. Caller must hold the lock."""
        now = time.monotonic()
        state = self._state
        if state.message_id is None:
            await self._create_message_locked()
            return
        if now < state.next_edit_at:
            return
        text = self._render()
        if text == state.last_text:
            return
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=state.message_id,
                text=text,
                parse_mode=None,
            )
            state.last_text = text
            state.next_edit_at = now + _MIN_EDIT_INTERVAL
        except TelegramRetryAfter as exc:
            state.next_edit_at = now + exc.retry_after + 2.0
            logger.info("Status edit flood-limited, backing off %.0fs", exc.retry_after)
        except TelegramBadRequest:
            # "message is not modified" and similar — harmless.
            state.next_edit_at = now + _MIN_EDIT_INTERVAL
            logger.debug("Status edit rejected", exc_info=True)
        except TelegramAPIError:
            state.next_edit_at = now + _MIN_EDIT_INTERVAL
            logger.debug("Status edit failed", exc_info=True)

    async def _create_message_locked(self) -> None:
        state = self._state
        text = self._render()
        try:
            if self._reply_to is not None:
                msg = await self._reply_to.reply(text, parse_mode=None)
            else:
                msg = await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=text,
                    parse_mode=None,
                    message_thread_id=self._thread_id,
                )
        except TelegramAPIError:
            logger.debug("Status message create failed", exc_info=True)
            return
        state.message_id = msg.message_id
        state.last_text = text
        state.next_edit_at = time.monotonic() + _MIN_EDIT_INTERVAL
        if self._ticker is None:
            self._ticker = asyncio.create_task(self._tick_loop())
