"""Device registry: pairing, lookup, and revocation, persisted to devices.json."""

from __future__ import annotations

import ipaddress
import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ductor_bot.gateway.models import Device, PairingCode, Platform
from ductor_bot.infra.json_store import atomic_json_save, load_json

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Networks a device may pair from.  Pairing hands out long-lived credentials,
# so it is restricted to networks that already imply physical or VPN trust:
# loopback, RFC1918 LAN, link-local, and Tailscale's CGNAT range (100.64/10).
# Everyday use has no such restriction -- this gate applies to enrolment only,
# which is what lets the app work from anywhere afterwards.
_TRUSTED_PAIRING_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("100.64.0.0/10"),
)


class PairingError(Exception):
    """Raised when a pairing attempt is rejected."""


class UntrustedProxyError(Exception):
    """Raised when the real client address cannot be established with confidence."""


def resolve_client_ip(
    peer_ip: str,
    forwarded_for: str | None,
    trusted_proxies: tuple[str, ...],
) -> str:
    """Determine the real client address behind an optional reverse proxy.

    This exists because ``request.remote`` is the *proxy's* address once a
    reverse proxy is in front of the gateway.  Naively trusting it would make
    every internet request look like it came from loopback, silently turning
    the trusted-network gate on pairing into no gate at all.

    The rule is fail-closed: an ``X-Forwarded-For`` header is only honoured when
    the immediate peer is a configured trusted proxy.  If the header is present
    but no proxy is configured, the deployment is ambiguous and this raises
    rather than guessing -- an unconfigured proxy must break pairing loudly, not
    silently accept the world.
    """
    if not forwarded_for:
        return peer_ip

    if not trusted_proxies:
        raise UntrustedProxyError(
            "X-Forwarded-For present but no trusted proxies configured"
        )

    if not _ip_in_any(peer_ip, trusted_proxies):
        raise UntrustedProxyError("X-Forwarded-For from an untrusted peer")

    # Walk right to left, discarding hops we control; the first address we do
    # not control is the closest thing to the real client. Entries further left
    # are attacker-controlled and must never be trusted.
    for candidate in reversed([part.strip() for part in forwarded_for.split(",")]):
        if candidate and not _ip_in_any(candidate, trusted_proxies):
            return candidate
    return peer_ip


def _ip_in_any(candidate: str, networks: tuple[str, ...]) -> bool:
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    for entry in networks:
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def is_trusted_pairing_source(remote_ip: str) -> bool:
    """Return True when *remote_ip* sits in a network allowed to pair."""
    try:
        address = ipaddress.ip_address(remote_ip.strip())
    except ValueError:
        return False
    if address.is_loopback:
        return True
    return any(address in network for network in _TRUSTED_PAIRING_NETWORKS)


class DeviceRegistry:
    """Thread-safe registry of paired devices backed by an atomic JSON file.

    Pairing codes live in memory only: they are short-lived by design, and a
    restart invalidating outstanding codes is the safe failure direction.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._devices: dict[str, Device] = {}
        self._pending: dict[str, PairingCode] = {}
        self._load()

    # -- persistence --

    def _load(self) -> None:
        raw = load_json(self._path)
        if not raw:
            return
        entries = raw.get("devices", []) if isinstance(raw, dict) else []
        for entry in entries:
            try:
                device = Device.from_dict(entry)
            except (KeyError, TypeError):
                logger.warning("Skipping malformed device entry in %s", self._path)
                continue
            self._devices[device.id] = device

    def _save(self) -> None:
        payload = {"devices": [d.to_dict() for d in self._devices.values()]}
        atomic_json_save(self._path, payload)

    # -- pairing --

    def issue_pairing_code(self, *, label: str = "") -> PairingCode:
        """Create a single-use enrolment code.

        Any previously outstanding codes are dropped: only one enrolment can be
        pending at a time, which keeps a forgotten code from lingering.
        """
        with self._lock:
            self._pending.clear()
            code = PairingCode.generate(label=label)
            self._pending[code.code] = code
            logger.info("Issued pairing code (expires %s)", code.expires_at.isoformat())
            return code

    def redeem_pairing_code(
        self,
        candidate: str,
        *,
        name: str,
        platform: Platform,
        public_key: str,
        remote_ip: str,
    ) -> Device:
        """Exchange a valid code for a registered device.

        Raises ``PairingError`` when the source network is untrusted or the code
        is unknown, expired, or already spent.
        """
        if not is_trusted_pairing_source(remote_ip):
            logger.warning("Rejected pairing attempt from untrusted source %s", remote_ip)
            raise PairingError("pairing is only allowed from a trusted network")

        with self._lock:
            match = next(
                (c for c in self._pending.values() if c.is_valid() and c.matches(candidate)),
                None,
            )
            if match is None:
                logger.warning("Rejected pairing attempt with invalid code from %s", remote_ip)
                raise PairingError("invalid or expired pairing code")

            match.consume()
            self._pending.pop(match.code, None)

            device = Device.create(name=name, platform=platform, public_key=public_key)
            self._devices[device.id] = device
            self._save()
            logger.info("Paired new device %s (%s, %s)", device.id, device.name, device.platform)
            return device

    # -- lookup and lifecycle --

    def get(self, device_id: str) -> Device | None:
        """Return the device if it exists and has not been revoked."""
        with self._lock:
            device = self._devices.get(device_id)
            if device is None or not device.is_active:
                return None
            return device

    def list_devices(self, *, include_revoked: bool = False) -> list[Device]:
        with self._lock:
            return [d for d in self._devices.values() if include_revoked or d.is_active]

    def touch(self, device_id: str) -> None:
        """Record last-seen time.  Best-effort: never fails a live request."""
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                return
            device.last_seen_at = datetime.now(UTC).isoformat()
            try:
                self._save()
            except OSError:
                logger.warning("Could not persist last-seen for %s", device_id)

    def revoke(self, device_id: str) -> bool:
        """Revoke one device.  Returns False when it was unknown or already revoked."""
        with self._lock:
            device = self._devices.get(device_id)
            if device is None or not device.is_active:
                return False
            device.revoked_at = datetime.now(UTC).isoformat()
            self._save()
            logger.warning("Revoked device %s (%s)", device_id, device.name)
            return True

    def revoke_all(self) -> int:
        """Kill switch: revoke every active device.  Returns how many were revoked."""
        with self._lock:
            now = datetime.now(UTC).isoformat()
            active = [d for d in self._devices.values() if d.is_active]
            for device in active:
                device.revoked_at = now
            if active:
                self._save()
            logger.warning("Kill switch: revoked %d device(s)", len(active))
            return len(active)
