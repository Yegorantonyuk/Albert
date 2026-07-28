"""Short-lived session tokens bound to a paired device.

A token is only ever handed out in exchange for a valid Ed25519 signature, so
possessing one proves the device authenticated recently.  Lifetime is
deliberately short: a token that leaks through a log, a crash report, or a
screenshot expires before it is useful, and refreshing it costs the client one
signature it can always produce.

Tokens are symmetric (HS256).  Only the gateway issues and verifies them, so
asymmetric signing would add key distribution for no benefit.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from typing import TYPE_CHECKING, Any

import jwt

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_ALGORITHM = "HS256"
_ISSUER = "albert-gateway"
DEFAULT_TTL_SECONDS = 15 * 60

# The signing secret is as powerful as every device credential combined.
_SECRET_MODE = 0o600
_SECRET_BYTES = 32

# Minimum accepted secret length, per RFC 7518 section 3.2 for HS256.
MIN_SECRET_BYTES = 32


class TokenError(Exception):
    """Raised when a token is absent, malformed, expired, or not ours."""


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def load_or_create_secret(path: Path) -> str:
    """Read the gateway signing secret, generating it on first use.

    Regenerating the secret invalidates every outstanding token, which is the
    correct behaviour after a suspected compromise: delete the file and restart.
    """
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret + "\n", encoding="utf-8")
    path.chmod(_SECRET_MODE)
    logger.info("Generated new gateway token secret at %s", path)
    return secret


class TokenIssuer:
    """Issues and validates device-scoped session tokens."""

    __slots__ = ("_secret", "_ttl")

    def __init__(self, secret: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        # RFC 7518 3.2 requires an HMAC key at least as long as the hash output.
        # Rejecting a short secret at construction beats discovering it in a
        # library warning after the gateway is already accepting traffic.
        if len(secret.encode()) < MIN_SECRET_BYTES:
            raise ValueError(
                f"gateway token secret must be at least {MIN_SECRET_BYTES} bytes"
            )
        self._secret = secret
        self._ttl = ttl_seconds

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def issue(self, device_id: str) -> str:
        """Mint a token for *device_id*."""
        now = _utcnow()
        payload: dict[str, Any] = {
            "sub": device_id,
            "iss": _ISSUER,
            "iat": now,
            "exp": now + dt.timedelta(seconds=self._ttl),
            # A per-token id makes individual revocation possible later without
            # changing the wire format.
            "jti": secrets.token_hex(8),
        }
        return jwt.encode(payload, self._secret, algorithm=_ALGORITHM)

    def verify(self, token: str) -> str:
        """Return the device id carried by *token*, or raise ``TokenError``.

        The algorithm is pinned to a single value: accepting whatever the token
        header requests is how ``alg: none`` and HMAC-vs-RSA confusion attacks
        get in.
        """
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[_ALGORITHM],
                issuer=_ISSUER,
                options={"require": ["exp", "iat", "sub", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenError("token expired") from exc
        except jwt.InvalidTokenError as exc:
            raise TokenError("invalid token") from exc

        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject:
            raise TokenError("token has no subject")
        return subject
