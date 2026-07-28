r"""Ed25519 request signing: proves a request is live, intact, and device-bound.

Every authenticated request carries four headers::

    X-Albert-Device      device id issued at pairing
    X-Albert-Timestamp   unix seconds, must be within +/- MAX_SKEW of server time
    X-Albert-Nonce       random 128-bit hex, must not have been seen before
    X-Albert-Signature   base64 Ed25519 signature over the canonical string

The canonical string binds method, path, time, nonce, and body together::

    METHOD \\n PATH \\n TIMESTAMP \\n NONCE \\n sha256_hex(body)

Signing the body hash means a proxy cannot alter the payload; including the
timestamp and nonce means a captured request cannot be replayed once the skew
window closes.  Because the signing key lives in device hardware, a stolen
bearer token alone is useless to an attacker.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

HEADER_DEVICE = "X-Albert-Device"
HEADER_TIMESTAMP = "X-Albert-Timestamp"
HEADER_NONCE = "X-Albert-Nonce"
HEADER_SIGNATURE = "X-Albert-Signature"

# A request may be at most this many seconds away from server time in either
# direction.  Wide enough to tolerate ordinary clock drift on a phone, narrow
# enough that the replay cache stays small.
MAX_SKEW_SECONDS = 60

# Nonces are remembered for twice the skew window: a request that old is
# already rejected on timestamp grounds, so it can be safely forgotten.
NONCE_TTL_SECONDS = MAX_SKEW_SECONDS * 2

_MAX_NONCE_ENTRIES = 20_000


class SignatureError(Exception):
    """Raised when a request signature is missing, malformed, or invalid.

    The message is intentionally coarse.  Callers must not relay it to the
    client verbatim -- distinguishing "unknown device" from "bad signature"
    would let an attacker enumerate valid device ids.
    """


@dataclass(frozen=True, slots=True)
class SignedRequest:
    """The signature material extracted from request headers."""

    device_id: str
    timestamp: int
    nonce: str
    signature: str

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> SignedRequest:
        lookup = {k.lower(): v for k, v in headers.items()}
        try:
            raw_ts = lookup[HEADER_TIMESTAMP.lower()]
            parsed = cls(
                device_id=lookup[HEADER_DEVICE.lower()],
                timestamp=int(raw_ts),
                nonce=lookup[HEADER_NONCE.lower()],
                signature=lookup[HEADER_SIGNATURE.lower()],
            )
        except KeyError as exc:
            raise SignatureError("missing signature headers") from exc
        except ValueError as exc:
            raise SignatureError("malformed timestamp") from exc
        if not parsed.device_id or not parsed.nonce or not parsed.signature:
            raise SignatureError("empty signature headers")
        return parsed


def canonical_string(
    *,
    method: str,
    path: str,
    timestamp: int,
    nonce: str,
    body: bytes,
) -> bytes:
    """Build the exact byte string that the device signs.

    Clients must reproduce this byte-for-byte, so the format is deliberately
    rigid: uppercase method, path without query rewriting, and a lowercase hex
    body digest (the digest of the empty string when there is no body).
    """
    body_hash = hashlib.sha256(body).hexdigest()
    parts = (method.upper(), path, str(timestamp), nonce, body_hash)
    return "\n".join(parts).encode()


class NonceCache:
    """Bounded, time-ordered replay cache.

    Entries expire after ``NONCE_TTL_SECONDS``.  The cache is also hard-capped
    so that a flood of unauthenticated requests cannot grow it without bound --
    under pressure the oldest entries are dropped, and any request old enough to
    be affected is already outside the skew window.
    """

    __slots__ = ("_entries", "_ttl")

    def __init__(self, *, ttl: int = NONCE_TTL_SECONDS) -> None:
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._ttl = ttl

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        while self._entries:
            seen_at = next(iter(self._entries.values()))
            if seen_at > cutoff:
                break
            self._entries.popitem(last=False)
        while len(self._entries) > _MAX_NONCE_ENTRIES:
            self._entries.popitem(last=False)

    def check_and_add(self, key: str, *, now: float | None = None) -> bool:
        """Record *key*; return False if it was already present (a replay)."""
        moment = time.time() if now is None else now
        self._prune(moment)
        if key in self._entries:
            return False
        self._entries[key] = moment
        return True

    def __len__(self) -> int:
        return len(self._entries)


def verify_signature(  # noqa: PLR0913
    signed: SignedRequest,
    *,
    public_key_b64: str,
    method: str,
    path: str,
    body: bytes,
    nonce_cache: NonceCache,
    now: float | None = None,
) -> None:
    """Validate freshness, uniqueness, and the Ed25519 signature.

    Raises ``SignatureError`` on any failure.  Checks run cheapest-first so a
    flood of junk requests costs little before being dropped.
    """
    moment = time.time() if now is None else now

    if abs(moment - signed.timestamp) > MAX_SKEW_SECONDS:
        raise SignatureError("timestamp outside allowed skew")

    # Scope the nonce to the device so one device cannot burn another's nonce
    # space, and so revoking a device cleanly discards its replay history.
    if not nonce_cache.check_and_add(f"{signed.device_id}:{signed.nonce}", now=moment):
        raise SignatureError("nonce already used")

    try:
        verify_key = VerifyKey(base64.b64decode(public_key_b64, validate=True))
        raw_signature = base64.b64decode(signed.signature, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignatureError("malformed key or signature encoding") from exc

    message = canonical_string(
        method=method,
        path=path,
        timestamp=signed.timestamp,
        nonce=signed.nonce,
        body=body,
    )
    try:
        verify_key.verify(message, raw_signature)
    except BadSignatureError as exc:
        raise SignatureError("signature verification failed") from exc
