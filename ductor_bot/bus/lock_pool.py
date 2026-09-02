"""Unified per-session lock pool shared by all transports and the message bus."""

from __future__ import annotations

import asyncio
import logging
from typing import TypeAlias

from ductor_bot.session.key import LockKey, SessionKey

logger = logging.getLogger(__name__)

_MAX_LOCKS = 1000

LegacyLockKey: TypeAlias = tuple[int, int | None]
"""Pre-transport Telegram lock identity accepted for compatibility."""

LockPoolInput: TypeAlias = LockKey | LegacyLockKey | SessionKey | int
"""Canonical and legacy identities accepted by :class:`LockPool`."""


class LockPool:
    """Single lock pool for all message sources.

    Replaces ``SequentialMiddleware._locks``, ``ApiServer._locks``, and
    the ad-hoc ``bot.sequential.get_lock()`` calls in result delivery.
    """

    def __init__(self, *, max_locks: int = _MAX_LOCKS) -> None:
        self._locks: dict[LockKey, asyncio.Lock] = {}
        self._max = max_locks

    def get(self, key: LockPoolInput) -> asyncio.Lock:
        """Return the lock for *key*, creating one if needed.

        Accepts a canonical ``(transport, chat_id, topic_id)`` key or a
        :class:`SessionKey`. Legacy ``(chat_id, topic_id)`` and plain
        ``chat_id`` inputs are normalized to Telegram (``"tg"``).

        Malformed inputs raise :class:`TypeError` rather than being coerced.
        """
        k = self._normalize(key)
        if k not in self._locks:
            self._evict_if_needed()
            self._locks[k] = asyncio.Lock()
        return self._locks[k]

    def is_locked(self, key: LockPoolInput) -> bool:
        """Return True if the lock for *key* is currently held."""
        lock = self._locks.get(self._normalize(key))
        return lock.locked() if lock else False

    def any_locked_for_chat(self, chat_id: int, *, transport: str = "tg") -> bool:
        """Return True if any lock for *transport* and *chat_id* is held."""
        return any(
            lock.locked()
            for (key_transport, cid, _), lock in self._locks.items()
            if key_transport == transport and cid == chat_id
        )

    def __len__(self) -> int:
        return len(self._locks)

    # -- Internal helpers ------------------------------------------------------

    @staticmethod
    def _normalize(key: LockPoolInput) -> LockKey:
        transport: object
        chat_id: object
        topic_id: object
        if isinstance(key, SessionKey):
            transport, chat_id, topic_id = key.lock_key
        elif isinstance(key, int) and not isinstance(key, bool):
            transport, chat_id, topic_id = "tg", key, None
        elif isinstance(key, tuple):
            tuple_key: tuple[object, ...] = key
            if len(tuple_key) == 3:
                transport, chat_id, topic_id = tuple_key
            elif len(tuple_key) == 2:
                chat_id, topic_id = tuple_key
                transport = "tg"
            else:
                msg = "Lock key tuples must have two legacy or three canonical parts"
                raise TypeError(msg)
        else:
            msg = "Lock key must be a SessionKey, integer, or tuple"
            raise TypeError(msg)

        if not isinstance(transport, str) or not transport:
            msg = "Lock key transport must be a non-empty string"
            raise TypeError(msg)
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            msg = "Lock key chat_id must be an integer"
            raise TypeError(msg)
        if topic_id is not None and (not isinstance(topic_id, int) or isinstance(topic_id, bool)):
            msg = "Lock key topic_id must be an integer or None"
            raise TypeError(msg)
        return (transport, chat_id, topic_id)

    def _evict_if_needed(self) -> None:
        if len(self._locks) < self._max:
            return
        idle = [k for k, v in self._locks.items() if not v.locked()]
        for k in idle[: max(1, len(idle) // 2)]:
            del self._locks[k]
