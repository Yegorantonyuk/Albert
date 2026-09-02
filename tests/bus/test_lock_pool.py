"""Tests for the LockPool."""

from __future__ import annotations

import asyncio

import pytest

from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.session.key import SessionKey


def test_get_creates_lock() -> None:
    pool = LockPool()
    lock = pool.get((1, None))
    assert isinstance(lock, asyncio.Lock)
    assert len(pool) == 1


def test_get_returns_same_lock() -> None:
    pool = LockPool()
    a = pool.get((1, None))
    b = pool.get((1, None))
    assert a is b


def test_get_with_plain_int() -> None:
    pool = LockPool()
    a = pool.get(42)
    b = pool.get((42, None))
    assert a is b is pool.get(("tg", 42, None))


def test_get_different_topics() -> None:
    pool = LockPool()
    a = pool.get((1, None))
    b = pool.get((1, 5))
    assert a is not b
    assert len(pool) == 2


def test_legacy_inputs_normalize_to_telegram() -> None:
    pool = LockPool()
    assert pool.get(42) is pool.get((42, None))
    assert pool.get((42, 7)) is pool.get(("tg", 42, 7))
    assert pool.get(42) is pool.get(SessionKey("tg", 42))


def test_canonical_transport_keys_are_independent() -> None:
    pool = LockPool()
    assert pool.get(("tg", 42, None)) is not pool.get(("web", 42, None))
    assert pool.get(("tg", 42, None)) is not pool.get(("tg", 42, 7))


@pytest.mark.parametrize(
    "key",
    [(), (42,), ("tg", 42), ("tg", 42, None, "extra"), ("tg", "42", None), True],
)
def test_malformed_inputs_fail_clearly(key: object) -> None:
    with pytest.raises(TypeError, match="Lock key"):
        LockPool().get(key)  # type: ignore[arg-type]


def test_is_locked_false_when_no_lock() -> None:
    pool = LockPool()
    assert pool.is_locked(99) is False


async def test_is_locked_true_when_held() -> None:
    pool = LockPool()
    lock = pool.get(1)
    assert pool.is_locked(1) is False
    await lock.acquire()
    try:
        assert pool.is_locked(1) is True
    finally:
        lock.release()
    assert pool.is_locked(1) is False


async def test_any_locked_for_chat() -> None:
    pool = LockPool()
    lock = pool.get((10, 3))
    assert pool.any_locked_for_chat(10) is False
    await lock.acquire()
    try:
        assert pool.any_locked_for_chat(10) is True
        assert pool.any_locked_for_chat(99) is False
    finally:
        lock.release()


async def test_transport_aware_chat_query_preserves_telegram_default() -> None:
    pool = LockPool()
    web = pool.get(("web", 42, None))
    await web.acquire()
    try:
        assert pool.any_locked_for_chat(42) is False
        assert pool.any_locked_for_chat(42, transport="web") is True
    finally:
        web.release()


def test_eviction_on_overflow() -> None:
    pool = LockPool(max_locks=5)
    for i in range(5):
        pool.get((i, None))
    assert len(pool) == 5
    # Adding one more triggers eviction of idle locks
    pool.get((99, None))
    assert len(pool) <= 5


async def test_eviction_preserves_locked() -> None:
    pool = LockPool(max_locks=3)
    held = pool.get((1, None))
    pool.get((2, None))
    pool.get((3, None))
    await held.acquire()
    try:
        # Trigger eviction — lock for (1, None) must survive
        pool.get((99, None))
        assert pool.is_locked((1, None)) is True
    finally:
        held.release()


@pytest.mark.parametrize(
    "key",
    [42, (42, None), (42, 7), ("web", 42, None), SessionKey("web", 42)],
    ids=["int", "tuple_none", "tuple_topic", "canonical", "session_key"],
)
def test_normalize_variants(key: object) -> None:
    pool = LockPool()
    lock = pool.get(key)  # type: ignore[arg-type]
    assert isinstance(lock, asyncio.Lock)
