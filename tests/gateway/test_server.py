"""End-to-end gateway flow: pairing, signed requests, tokens, and revocation."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip("nacl", reason="PyNaCl not installed (optional: pip install albert[gateway])")
pytest.importorskip("jwt", reason="PyJWT not installed (optional: pip install albert[gateway])")
pytest.importorskip(
    "cryptography", reason="cryptography not installed (optional: pip install albert[gateway])"
)

from nacl.signing import SigningKey

from ductor_bot.gateway.audit import AuditLog
from ductor_bot.gateway.ca import ClientCertificateAuthority
from ductor_bot.gateway.devices import DeviceRegistry
from ductor_bot.gateway.server import GatewayServer
from ductor_bot.gateway.signing import canonical_string
from ductor_bot.gateway.tokens import TokenIssuer

if TYPE_CHECKING:
    from pathlib import Path

    from aiohttp.test_utils import TestClient


@pytest.fixture
def registry(tmp_path: Path) -> DeviceRegistry:
    return DeviceRegistry(tmp_path / "devices.json")


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def server(tmp_path: Path, registry: DeviceRegistry, audit: AuditLog) -> GatewayServer:
    return GatewayServer(
        devices=registry,
        tokens=TokenIssuer("test-secret-not-for-production-32b+"),
        audit=audit,
        ca=ClientCertificateAuthority(tmp_path / "ca"),
    )


@pytest.fixture
async def client(aiohttp_client: Any, server: GatewayServer) -> TestClient:
    return await aiohttp_client(server.build_app())


def sign_headers(
    key: SigningKey,
    device_id: str,
    *,
    method: str,
    path: str,
    body: bytes = b"",
    nonce: str = "n1",
    timestamp: int | None = None,
) -> dict[str, str]:
    import time

    stamp = int(time.time()) if timestamp is None else timestamp
    message = canonical_string(
        method=method, path=path, timestamp=stamp, nonce=nonce, body=body
    )
    return {
        "X-Albert-Device": device_id,
        "X-Albert-Timestamp": str(stamp),
        "X-Albert-Nonce": nonce,
        "X-Albert-Signature": base64.b64encode(key.sign(message).signature).decode(),
    }


async def pair_device(client: TestClient, server: GatewayServer) -> tuple[str, SigningKey, dict]:
    """Run a full pairing and return (device_id, signing key, response body)."""
    key = SigningKey.generate()
    code = server._devices.issue_pairing_code()
    response = await client.post(
        "/auth/pair",
        json={
            "code": code.code,
            "name": "Test laptop",
            "platform": "desktop",
            "public_key": base64.b64encode(bytes(key.verify_key)).decode(),
        },
    )
    assert response.status == 201
    body = await response.json()
    return body["device_id"], key, body


async def test_health_needs_no_authentication(client: TestClient) -> None:
    response = await client.get("/health")
    assert response.status == 200
    assert (await response.json())["status"] == "ok"


async def test_pairing_issues_a_usable_credential(
    client: TestClient, server: GatewayServer
) -> None:
    _, _, body = await pair_device(client, server)
    assert "BEGIN CERTIFICATE" in body["client_certificate"]
    assert "BEGIN PRIVATE KEY" in body["client_private_key"]
    assert "BEGIN CERTIFICATE" in body["ca_certificate"]
    assert body["token"]


async def test_paired_device_can_make_a_signed_request(
    client: TestClient, server: GatewayServer
) -> None:
    device_id, key, _ = await pair_device(client, server)
    response = await client.get(
        "/devices", headers=sign_headers(key, device_id, method="GET", path="/devices")
    )
    assert response.status == 200
    devices = (await response.json())["devices"]
    assert len(devices) == 1
    assert devices[0]["is_current"] is True


async def test_unsigned_request_is_rejected(client: TestClient) -> None:
    response = await client.get("/devices")
    assert response.status == 401


async def test_signature_from_unknown_device_is_rejected(client: TestClient) -> None:
    key = SigningKey.generate()
    response = await client.get(
        "/devices", headers=sign_headers(key, "dev_nonexistent", method="GET", path="/devices")
    )
    assert response.status == 401


async def test_failure_responses_do_not_leak_the_reason(
    client: TestClient, server: GatewayServer
) -> None:
    """Unknown device and bad signature must be indistinguishable to the caller."""
    device_id, _, _ = await pair_device(client, server)
    wrong_key = SigningKey.generate()

    unknown = await client.get(
        "/devices",
        headers=sign_headers(wrong_key, "dev_nope", method="GET", path="/devices", nonce="a"),
    )
    bad_signature = await client.get(
        "/devices",
        headers=sign_headers(wrong_key, device_id, method="GET", path="/devices", nonce="b"),
    )
    assert unknown.status == bad_signature.status == 401
    assert await unknown.json() == await bad_signature.json()


async def test_replayed_request_is_rejected(client: TestClient, server: GatewayServer) -> None:
    device_id, key, _ = await pair_device(client, server)
    headers = sign_headers(key, device_id, method="GET", path="/devices", nonce="replay-me")

    assert (await client.get("/devices", headers=headers)).status == 200
    assert (await client.get("/devices", headers=headers)).status == 401


async def test_token_exchange_then_bearer_access(
    client: TestClient, server: GatewayServer
) -> None:
    device_id, key, _ = await pair_device(client, server)
    minted = await client.post(
        "/auth/token", headers=sign_headers(key, device_id, method="POST", path="/auth/token")
    )
    assert minted.status == 200
    token = (await minted.json())["token"]

    response = await client.get("/devices", headers={"Authorization": f"Bearer {token}"})
    assert response.status == 200


async def test_revoked_device_loses_access_immediately(
    client: TestClient, server: GatewayServer
) -> None:
    """A live token must stop working the moment its device is revoked."""
    device_id, key, _ = await pair_device(client, server)
    minted = await client.post(
        "/auth/token", headers=sign_headers(key, device_id, method="POST", path="/auth/token")
    )
    token = (await minted.json())["token"]
    assert (await client.get("/devices", headers={"Authorization": f"Bearer {token}"})).status == 200

    server._devices.revoke(device_id)

    assert (await client.get("/devices", headers={"Authorization": f"Bearer {token}"})).status == 401


async def test_kill_switch_revokes_every_device(
    client: TestClient, server: GatewayServer
) -> None:
    device_id, key, _ = await pair_device(client, server)
    await pair_device(client, server)

    response = await client.post(
        "/devices/revoke-all",
        headers=sign_headers(key, device_id, method="POST", path="/devices/revoke-all"),
    )
    assert response.status == 200
    assert (await response.json())["revoked"] == 2
    assert server._devices.list_devices() == []


async def test_pairing_with_a_bad_code_fails(client: TestClient, server: GatewayServer) -> None:
    server._devices.issue_pairing_code()
    key = SigningKey.generate()
    response = await client.post(
        "/auth/pair",
        json={
            "code": "WRONGCOD",
            "name": "Impostor",
            "platform": "desktop",
            "public_key": base64.b64encode(bytes(key.verify_key)).decode(),
        },
    )
    assert response.status == 403


async def test_pairing_requires_all_fields(client: TestClient, server: GatewayServer) -> None:
    code = server._devices.issue_pairing_code()
    response = await client.post("/auth/pair", json={"code": code.code})
    assert response.status == 400


async def test_activity_records_both_outcomes(
    client: TestClient, server: GatewayServer
) -> None:
    device_id, key, _ = await pair_device(client, server)
    await client.get("/devices")  # denied, unsigned

    response = await client.get(
        "/activity", headers=sign_headers(key, device_id, method="GET", path="/activity")
    )
    assert response.status == 200
    actions = {e["action"] for e in (await response.json())["events"]}
    assert "auth.pair" in actions
    assert "auth.signature" in actions


async def test_audit_log_is_append_only_jsonl(
    client: TestClient, server: GatewayServer, tmp_path: Path
) -> None:
    await pair_device(client, server)
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) >= 1
    for line in lines:
        assert json.loads(line)["timestamp"]


async def test_rate_limit_returns_429(client: TestClient, server: GatewayServer) -> None:
    server._rate = type(server._rate)(limit=3, window=60)
    for _ in range(3):
        await client.get("/health")
    assert (await client.get("/health")).status == 429
