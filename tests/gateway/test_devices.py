"""Device pairing, network gating, persistence, and revocation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from ductor_bot.gateway.devices import DeviceRegistry, PairingError, is_trusted_pairing_source
from ductor_bot.gateway.models import PairingCode

if TYPE_CHECKING:
    from pathlib import Path

PUBKEY = "3jN9V6mM6i0YtQd2c1YQ5qV8xW0kZ7pL9sT2uR4nB1E="


@pytest.fixture
def registry(tmp_path: Path) -> DeviceRegistry:
    return DeviceRegistry(tmp_path / "devices.json")


def pair(registry: DeviceRegistry, code: str, *, ip: str = "100.64.0.5") -> object:
    return registry.redeem_pairing_code(
        code, name="Test device", platform="desktop", public_key=PUBKEY, remote_ip=ip
    )


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "::1", "192.168.1.20", "10.0.0.3", "172.16.5.5", "100.64.0.1"],
)
def test_trusted_sources_accepted(ip: str) -> None:
    assert is_trusted_pairing_source(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "203.0.113.7", "2606:4700::1", "", "not-an-ip"])
def test_untrusted_sources_rejected(ip: str) -> None:
    assert not is_trusted_pairing_source(ip)


def test_pairing_from_public_internet_is_refused(registry: DeviceRegistry) -> None:
    code = registry.issue_pairing_code()
    with pytest.raises(PairingError, match="trusted network"):
        pair(registry, code.code, ip="203.0.113.7")


def test_successful_pairing_registers_device(registry: DeviceRegistry) -> None:
    code = registry.issue_pairing_code()
    device = pair(registry, code.code)
    assert registry.get(device.id) is not None
    assert len(registry.list_devices()) == 1


def test_pairing_code_is_single_use(registry: DeviceRegistry) -> None:
    code = registry.issue_pairing_code()
    pair(registry, code.code)
    with pytest.raises(PairingError, match="invalid or expired"):
        pair(registry, code.code)


def test_pairing_code_is_case_insensitive(registry: DeviceRegistry) -> None:
    code = registry.issue_pairing_code()
    device = pair(registry, code.code.lower())
    assert registry.get(device.id) is not None


def test_wrong_code_is_refused(registry: DeviceRegistry) -> None:
    registry.issue_pairing_code()
    with pytest.raises(PairingError, match="invalid or expired"):
        pair(registry, "AAAAAAAA")


def test_issuing_a_new_code_invalidates_the_previous_one(registry: DeviceRegistry) -> None:
    first = registry.issue_pairing_code()
    registry.issue_pairing_code()
    with pytest.raises(PairingError, match="invalid or expired"):
        pair(registry, first.code)


def test_expired_code_is_refused() -> None:
    code = PairingCode.generate()
    future = datetime.now(UTC) + timedelta(minutes=10)
    assert code.is_expired(at=future)
    assert not code.is_valid(at=future)


def test_devices_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    registry = DeviceRegistry(path)
    code = registry.issue_pairing_code()
    device = pair(registry, code.code)

    reloaded = DeviceRegistry(path)
    assert reloaded.get(device.id) is not None


def test_pending_codes_do_not_survive_restart(tmp_path: Path) -> None:
    """Outstanding codes must die with the process -- fail safe, not open."""
    path = tmp_path / "devices.json"
    registry = DeviceRegistry(path)
    code = registry.issue_pairing_code()

    reloaded = DeviceRegistry(path)
    with pytest.raises(PairingError):
        pair(reloaded, code.code)


def test_revoked_device_is_not_returned(registry: DeviceRegistry) -> None:
    code = registry.issue_pairing_code()
    device = pair(registry, code.code)

    assert registry.revoke(device.id)
    assert registry.get(device.id) is None
    assert registry.list_devices() == []
    assert len(registry.list_devices(include_revoked=True)) == 1


def test_revoking_twice_reports_no_change(registry: DeviceRegistry) -> None:
    code = registry.issue_pairing_code()
    device = pair(registry, code.code)
    assert registry.revoke(device.id)
    assert not registry.revoke(device.id)


def test_revocation_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    registry = DeviceRegistry(path)
    code = registry.issue_pairing_code()
    device = pair(registry, code.code)
    registry.revoke(device.id)

    assert DeviceRegistry(path).get(device.id) is None


def test_kill_switch_revokes_everything(registry: DeviceRegistry) -> None:
    for _ in range(3):
        code = registry.issue_pairing_code()
        pair(registry, code.code)

    assert registry.revoke_all() == 3
    assert registry.list_devices() == []
    assert registry.revoke_all() == 0


def test_corrupt_registry_file_does_not_crash(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    path.write_text("{ not json", encoding="utf-8")
    assert DeviceRegistry(path).list_devices() == []
