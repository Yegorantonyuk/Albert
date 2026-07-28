"""Token issuance, expiry, and algorithm pinning."""

from __future__ import annotations

import datetime as dt

import pytest

pytest.importorskip("jwt", reason="PyJWT not installed (optional: pip install albert[gateway])")

import jwt

from ductor_bot.gateway.tokens import (
    MIN_SECRET_BYTES,
    TokenError,
    TokenIssuer,
    load_or_create_secret,
)

SECRET = "a" * MIN_SECRET_BYTES


def test_round_trip() -> None:
    issuer = TokenIssuer(SECRET)
    assert issuer.verify(issuer.issue("dev_1")) == "dev_1"


def test_short_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="at least"):
        TokenIssuer("too-short")


def test_expired_token_is_refused() -> None:
    issuer = TokenIssuer(SECRET, ttl_seconds=-1)
    with pytest.raises(TokenError, match="expired"):
        issuer.verify(issuer.issue("dev_1"))


def test_token_from_another_secret_is_refused() -> None:
    other = TokenIssuer("b" * MIN_SECRET_BYTES)
    with pytest.raises(TokenError):
        TokenIssuer(SECRET).verify(other.issue("dev_1"))


def test_alg_none_token_is_refused() -> None:
    """The classic JWT bypass: a token asking to be verified with no algorithm."""
    forged = jwt.encode({"sub": "dev_1", "iss": "albert-gateway"}, key="", algorithm="none")
    with pytest.raises(TokenError):
        TokenIssuer(SECRET).verify(forged)


def test_token_without_expiry_is_refused() -> None:
    forged = jwt.encode({"sub": "dev_1", "iss": "albert-gateway"}, SECRET, algorithm="HS256")
    with pytest.raises(TokenError):
        TokenIssuer(SECRET).verify(forged)


def test_token_from_another_issuer_is_refused() -> None:
    now = dt.datetime.now(dt.UTC)
    forged = jwt.encode(
        {
            "sub": "dev_1",
            "iss": "somebody-else",
            "iat": now,
            "exp": now + dt.timedelta(minutes=5),
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        TokenIssuer(SECRET).verify(forged)


def test_garbage_is_refused() -> None:
    with pytest.raises(TokenError):
        TokenIssuer(SECRET).verify("not-a-token")


def test_secret_is_generated_once_and_reused(tmp_path) -> None:
    path = tmp_path / "token.secret"
    first = load_or_create_secret(path)
    assert load_or_create_secret(path) == first
    assert len(first.encode()) >= MIN_SECRET_BYTES


def test_secret_file_is_owner_only(tmp_path) -> None:
    path = tmp_path / "token.secret"
    load_or_create_secret(path)
    assert path.stat().st_mode & 0o777 == 0o600
