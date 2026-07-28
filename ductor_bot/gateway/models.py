"""Data models for gateway device pairing and authentication."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

Platform = Literal["desktop", "ios", "android", "web", "unknown"]

# Pairing codes are short enough to type from a screen but drawn from a
# 32-symbol alphabet, so an 8-char code carries 40 bits of entropy.  Combined
# with the 5-minute TTL and single-use semantics this is far beyond brute-force
# reach for a network attacker.
_PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1 lookalikes
_PAIRING_CODE_LEN = 8
_PAIRING_TTL = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


@dataclass(slots=True)
class Device:
    """A paired client device.

    ``public_key`` is the base64-encoded Ed25519 verify key.  The matching
    private key is generated on the device inside the Secure Enclave / Keystore
    and is never transmitted, so the server can authenticate the device but can
    never impersonate it.
    """

    id: str
    name: str
    platform: Platform
    public_key: str
    created_at: str
    cert_serial: str | None = None
    last_seen_at: str | None = None
    revoked_at: str | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "platform": self.platform,
            "public_key": self.public_key,
            "created_at": self.created_at,
            "cert_serial": self.cert_serial,
            "last_seen_at": self.last_seen_at,
            "revoked_at": self.revoked_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Device:
        return cls(
            id=str(raw["id"]),
            name=str(raw.get("name", "unnamed")),
            platform=raw.get("platform", "unknown"),
            public_key=str(raw["public_key"]),
            created_at=str(raw.get("created_at", _iso(_now()))),
            cert_serial=raw.get("cert_serial"),
            last_seen_at=raw.get("last_seen_at"),
            revoked_at=raw.get("revoked_at"),
        )

    @classmethod
    def create(cls, *, name: str, platform: Platform, public_key: str) -> Device:
        return cls(
            id=f"dev_{secrets.token_hex(8)}",
            name=name,
            platform=platform,
            public_key=public_key,
            created_at=_iso(_now()),
        )


@dataclass(slots=True)
class PairingCode:
    """A single-use, short-lived code that authorizes one device enrolment.

    Codes are only ever displayed locally (CLI / server console) and may only
    be redeemed from a trusted network.  Both constraints are enforced by the
    caller; this object only owns generation and expiry.
    """

    code: str
    expires_at: datetime
    label: str = ""
    used_at: datetime | None = field(default=None)

    @classmethod
    def generate(cls, *, label: str = "", ttl: timedelta = _PAIRING_TTL) -> PairingCode:
        code = "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(_PAIRING_CODE_LEN))
        return cls(code=code, expires_at=_now() + ttl, label=label)

    @property
    def is_used(self) -> bool:
        return self.used_at is not None

    def is_expired(self, *, at: datetime | None = None) -> bool:
        return (at or _now()) >= self.expires_at

    def is_valid(self, *, at: datetime | None = None) -> bool:
        return not self.is_used and not self.is_expired(at=at)

    def matches(self, candidate: str) -> bool:
        """Compare in constant time, case-insensitively.

        Constant-time comparison keeps the redemption endpoint from leaking a
        correct prefix through response timing.
        """
        return secrets.compare_digest(self.code, candidate.strip().upper())

    def consume(self) -> None:
        self.used_at = _now()
