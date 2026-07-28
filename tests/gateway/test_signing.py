"""Signature verification: freshness, replay protection, and tamper detection."""

from __future__ import annotations

import base64

import pytest

pytest.importorskip("nacl", reason="PyNaCl not installed (optional: pip install albert[api])")

from nacl.signing import SigningKey

from ductor_bot.gateway.signing import (
    MAX_SKEW_SECONDS,
    NonceCache,
    SignatureError,
    SignedRequest,
    canonical_string,
    verify_signature,
)

NOW = 1_800_000_000.0


@pytest.fixture
def signing_key() -> SigningKey:
    return SigningKey.generate()


@pytest.fixture
def public_key_b64(signing_key: SigningKey) -> str:
    return base64.b64encode(bytes(signing_key.verify_key)).decode()


def sign(
    signing_key: SigningKey,
    *,
    method: str = "POST",
    path: str = "/chats",
    timestamp: int = int(NOW),
    nonce: str = "nonce-1",
    body: bytes = b"",
) -> SignedRequest:
    message = canonical_string(
        method=method, path=path, timestamp=timestamp, nonce=nonce, body=body
    )
    signature = base64.b64encode(signing_key.sign(message).signature).decode()
    return SignedRequest(
        device_id="dev_test", timestamp=timestamp, nonce=nonce, signature=signature
    )


def test_valid_signature_passes(signing_key: SigningKey, public_key_b64: str) -> None:
    signed = sign(signing_key, body=b'{"text":"hi"}')
    verify_signature(
        signed,
        public_key_b64=public_key_b64,
        method="POST",
        path="/chats",
        body=b'{"text":"hi"}',
        nonce_cache=NonceCache(),
        now=NOW,
    )


def test_tampered_body_is_rejected(signing_key: SigningKey, public_key_b64: str) -> None:
    signed = sign(signing_key, body=b'{"amount":1}')
    with pytest.raises(SignatureError):
        verify_signature(
            signed,
            public_key_b64=public_key_b64,
            method="POST",
            path="/chats",
            body=b'{"amount":1000}',
            nonce_cache=NonceCache(),
            now=NOW,
        )


def test_path_swap_is_rejected(signing_key: SigningKey, public_key_b64: str) -> None:
    """A signature for one endpoint must not authorize another."""
    signed = sign(signing_key, path="/chats")
    with pytest.raises(SignatureError):
        verify_signature(
            signed,
            public_key_b64=public_key_b64,
            method="POST",
            path="/auth/revoke",
            body=b"",
            nonce_cache=NonceCache(),
            now=NOW,
        )


def test_replayed_nonce_is_rejected(signing_key: SigningKey, public_key_b64: str) -> None:
    signed = sign(signing_key)
    cache = NonceCache()
    kwargs = {
        "public_key_b64": public_key_b64,
        "method": "POST",
        "path": "/chats",
        "body": b"",
        "nonce_cache": cache,
        "now": NOW,
    }
    verify_signature(signed, **kwargs)
    with pytest.raises(SignatureError, match="nonce"):
        verify_signature(signed, **kwargs)


def test_stale_timestamp_is_rejected(signing_key: SigningKey, public_key_b64: str) -> None:
    stale = int(NOW) - MAX_SKEW_SECONDS - 1
    signed = sign(signing_key, timestamp=stale)
    with pytest.raises(SignatureError, match="skew"):
        verify_signature(
            signed,
            public_key_b64=public_key_b64,
            method="POST",
            path="/chats",
            body=b"",
            nonce_cache=NonceCache(),
            now=NOW,
        )


def test_future_timestamp_is_rejected(signing_key: SigningKey, public_key_b64: str) -> None:
    """Clock skew is bounded in both directions, not just the past."""
    future = int(NOW) + MAX_SKEW_SECONDS + 1
    signed = sign(signing_key, timestamp=future)
    with pytest.raises(SignatureError, match="skew"):
        verify_signature(
            signed,
            public_key_b64=public_key_b64,
            method="POST",
            path="/chats",
            body=b"",
            nonce_cache=NonceCache(),
            now=NOW,
        )


def test_signature_from_another_device_is_rejected(public_key_b64: str) -> None:
    """A validly-formed signature by the wrong key must not pass."""
    attacker = SigningKey.generate()
    signed = sign(attacker)
    with pytest.raises(SignatureError):
        verify_signature(
            signed,
            public_key_b64=public_key_b64,
            method="POST",
            path="/chats",
            body=b"",
            nonce_cache=NonceCache(),
            now=NOW,
        )


def test_missing_headers_raise() -> None:
    with pytest.raises(SignatureError, match="missing"):
        SignedRequest.from_headers({"X-Albert-Device": "dev_test"})


def test_headers_are_case_insensitive() -> None:
    parsed = SignedRequest.from_headers(
        {
            "x-albert-device": "dev_test",
            "x-albert-timestamp": "1800000000",
            "x-albert-nonce": "n",
            "x-albert-signature": "s",
        }
    )
    assert parsed.device_id == "dev_test"


def test_nonce_cache_expires_old_entries() -> None:
    cache = NonceCache(ttl=10)
    assert cache.check_and_add("a", now=100.0)
    assert not cache.check_and_add("a", now=105.0)
    # Past the TTL the entry is pruned, so the key is free again -- harmless,
    # because a request that old fails the skew check first.
    assert cache.check_and_add("a", now=120.0)


def test_nonces_are_scoped_per_device(signing_key: SigningKey, public_key_b64: str) -> None:
    """One device must not be able to burn another device's nonce."""
    cache = NonceCache()
    first = sign(signing_key, nonce="shared")
    verify_signature(
        first,
        public_key_b64=public_key_b64,
        method="POST",
        path="/chats",
        body=b"",
        nonce_cache=cache,
        now=NOW,
    )
    other_key = SigningKey.generate()
    message = canonical_string(
        method="POST", path="/chats", timestamp=int(NOW), nonce="shared", body=b""
    )
    second = SignedRequest(
        device_id="dev_other",
        timestamp=int(NOW),
        nonce="shared",
        signature=base64.b64encode(other_key.sign(message).signature).decode(),
    )
    verify_signature(
        second,
        public_key_b64=base64.b64encode(bytes(other_key.verify_key)).decode(),
        method="POST",
        path="/chats",
        body=b"",
        nonce_cache=cache,
        now=NOW,
    )
