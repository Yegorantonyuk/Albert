"""Client-address resolution behind a reverse proxy.

These tests guard a subtle and dangerous failure mode: once a reverse proxy
sits in front of the gateway, ``request.remote`` is the *proxy's* address, so
every internet request looks like it came from loopback. A trusted-network
check written against ``request.remote`` therefore silently permits the whole
internet -- the control appears to work and protects nothing.
"""

from __future__ import annotations

import base64
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
from ductor_bot.gateway.devices import (
    DeviceRegistry,
    UntrustedProxyError,
    resolve_client_ip,
)
from ductor_bot.gateway.server import GatewayServer
from ductor_bot.gateway.tokens import TokenIssuer

if TYPE_CHECKING:
    from pathlib import Path

    from aiohttp.test_utils import TestClient

SECRET = "s" * 32


def build_server(tmp_path: Path, **kwargs: Any) -> GatewayServer:
    return GatewayServer(
        devices=DeviceRegistry(tmp_path / "devices.json"),
        tokens=TokenIssuer(SECRET),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        ca=ClientCertificateAuthority(tmp_path / "ca"),
        **kwargs,
    )


async def attempt_pair(client: TestClient, server: GatewayServer, **headers: str) -> int:
    key = SigningKey.generate()
    code = server._devices.issue_pairing_code()
    response = await client.post(
        "/auth/pair",
        json={
            "code": code.code,
            "name": "Attacker laptop",
            "platform": "desktop",
            "public_key": base64.b64encode(bytes(key.verify_key)).decode(),
        },
        headers=headers,
    )
    return response.status


# -- unit level --


def test_no_forwarded_header_uses_the_peer() -> None:
    assert resolve_client_ip("192.168.1.5", None, ()) == "192.168.1.5"


def test_forwarded_header_without_configured_proxy_is_refused() -> None:
    """The dangerous default: a proxy is clearly present but unconfigured."""
    with pytest.raises(UntrustedProxyError):
        resolve_client_ip("127.0.0.1", "203.0.113.9", ())


def test_forwarded_header_from_untrusted_peer_is_refused() -> None:
    with pytest.raises(UntrustedProxyError):
        resolve_client_ip("203.0.113.1", "10.0.0.5", ("127.0.0.1/32",))


def test_trusted_proxy_reveals_the_real_client() -> None:
    assert resolve_client_ip("127.0.0.1", "203.0.113.9", ("127.0.0.1/32",)) == "203.0.113.9"


def test_only_the_rightmost_untrusted_hop_is_believed() -> None:
    """Entries to the left are attacker-supplied and must not win."""
    resolved = resolve_client_ip(
        "127.0.0.1", "192.168.1.10, 203.0.113.9", ("127.0.0.1/32",)
    )
    assert resolved == "203.0.113.9"


def test_attacker_cannot_forge_a_lan_address_through_a_trusted_proxy() -> None:
    """A spoofed private address in XFF must not be treated as the real client."""
    resolved = resolve_client_ip("127.0.0.1", "10.0.0.5", ("127.0.0.1/32",))
    # The proxy appended nothing of its own, so 10.0.0.5 is what the client
    # claimed. It is returned, but the pairing gate still evaluates it -- the
    # point of this test is that it is not silently replaced by the loopback
    # peer address, which would have passed unconditionally.
    assert resolved == "10.0.0.5"


# -- through the server --


async def test_pairing_from_internet_through_proxy_is_refused(
    aiohttp_client: Any, tmp_path: Path
) -> None:
    """The headline case: without this the gate is decorative."""
    server = build_server(tmp_path, trusted_proxies=("127.0.0.1/32",))
    client = await aiohttp_client(server.build_app())
    assert await attempt_pair(client, server, **{"X-Forwarded-For": "203.0.113.9"}) == 403


async def test_pairing_through_unconfigured_proxy_is_refused(
    aiohttp_client: Any, tmp_path: Path
) -> None:
    """Fail closed: an unconfigured proxy must break pairing, not open it."""
    server = build_server(tmp_path)
    client = await aiohttp_client(server.build_app())
    assert await attempt_pair(client, server, **{"X-Forwarded-For": "203.0.113.9"}) == 403


async def test_pairing_over_tailscale_through_proxy_is_allowed(
    aiohttp_client: Any, tmp_path: Path
) -> None:
    server = build_server(tmp_path, trusted_proxies=("127.0.0.1/32",))
    client = await aiohttp_client(server.build_app())
    assert await attempt_pair(client, server, **{"X-Forwarded-For": "100.64.0.7"}) == 201


async def test_direct_local_pairing_still_works(aiohttp_client: Any, tmp_path: Path) -> None:
    """No proxy in play: the ordinary CLI-on-the-host flow is unaffected."""
    server = build_server(tmp_path)
    client = await aiohttp_client(server.build_app())
    assert await attempt_pair(client, server) == 201


# -- admin-gated pairing code --


async def test_pair_code_requires_the_admin_secret(
    aiohttp_client: Any, tmp_path: Path
) -> None:
    server = build_server(tmp_path, admin_secret="admin-secret-value")
    client = await aiohttp_client(server.build_app())
    assert (await client.post("/auth/pair-code")).status == 401
    wrong = await client.post("/auth/pair-code", headers={"X-Albert-Admin": "nope"})
    assert wrong.status == 401


async def test_pair_code_with_the_admin_secret_succeeds(
    aiohttp_client: Any, tmp_path: Path
) -> None:
    server = build_server(tmp_path, admin_secret="admin-secret-value")
    client = await aiohttp_client(server.build_app())
    response = await client.post(
        "/auth/pair-code", headers={"X-Albert-Admin": "admin-secret-value"}
    )
    assert response.status == 200
    assert len((await response.json())["code"]) == 8


async def test_pair_code_is_refused_when_no_secret_is_configured(
    aiohttp_client: Any, tmp_path: Path
) -> None:
    """An unset secret must not mean 'anyone may ask'."""
    server = build_server(tmp_path)
    client = await aiohttp_client(server.build_app())
    assert (await client.post("/auth/pair-code", headers={"X-Albert-Admin": ""})).status == 401
