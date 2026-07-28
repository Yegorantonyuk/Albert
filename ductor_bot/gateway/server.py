"""Gateway HTTP server: pairing, tokens, and device management.

Binds to loopback by default.  Everything reachable from outside the host is
expected to arrive through a reverse proxy that terminates TLS and enforces
client certificates; this process is the second of the two auth layers, not the
first.

Failure responses are deliberately uniform.  Distinguishing "unknown device"
from "bad signature" from "revoked" would let an attacker enumerate device ids
by watching which error comes back, so every authentication failure returns the
same status and the same body.  The specific reason goes to the audit log,
which only the operator can read.
"""

from __future__ import annotations

import hmac
import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias

from aiohttp import web

from ductor_bot.gateway.audit import AuditLog
from ductor_bot.gateway.ca import ClientCertificateAuthority
from ductor_bot.gateway.devices import (
    DeviceRegistry,
    PairingError,
    UntrustedProxyError,
    is_trusted_pairing_source,
    resolve_client_ip,
)
from ductor_bot.gateway.signing import NonceCache, SignatureError, SignedRequest, verify_signature
from ductor_bot.gateway.tokens import TokenError, TokenIssuer

logger = logging.getLogger(__name__)

_UNAUTHENTICATED_PATHS = frozenset({"/health", "/auth/pair", "/auth/pair-code"})

# Sliding-window limit applied per source address, before any crypto runs, so a
# flood costs the server a dictionary lookup rather than a signature check.
_RATE_LIMIT_REQUESTS = 60
_RATE_LIMIT_WINDOW_SECONDS = 60

_AUTH_FAILED = {"error": "authentication_failed"}

Handler: TypeAlias = Callable[[web.Request], Awaitable[web.StreamResponse]]


class RateLimiter:
    """Fixed-capacity sliding window keyed by client address."""

    __slots__ = ("_hits", "_limit", "_window")

    def __init__(
        self,
        *,
        limit: int = _RATE_LIMIT_REQUESTS,
        window: int = _RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._limit = limit
        self._window = window

    def allow(self, key: str, *, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        bucket = self._hits[key]
        cutoff = moment - self._window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self._limit:
            return False
        bucket.append(moment)
        return True


class GatewayServer:
    """The authenticated entry point for app clients."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        devices: DeviceRegistry,
        tokens: TokenIssuer,
        audit: AuditLog,
        ca: ClientCertificateAuthority,
        host: str = "127.0.0.1",
        port: int = 8750,
        trusted_proxies: tuple[str, ...] = (),
        admin_secret: str | None = None,
    ) -> None:
        self._devices = devices
        self._tokens = tokens
        self._audit = audit
        self._ca = ca
        self._host = host
        self._port = port
        self._trusted_proxies = trusted_proxies
        self._admin_secret = admin_secret
        self._nonces = NonceCache()
        self._rate = RateLimiter()
        self._runner: web.AppRunner | None = None

    # -- wiring --

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth_middleware])
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/auth/pair", self._handle_pair)
        app.router.add_post("/auth/pair-code", self._handle_issue_pair_code)
        app.router.add_post("/auth/token", self._handle_token)
        app.router.add_get("/devices", self._handle_list_devices)
        app.router.add_delete("/devices/{device_id}", self._handle_revoke_device)
        app.router.add_post("/devices/revoke-all", self._handle_revoke_all)
        app.router.add_get("/activity", self._handle_activity)
        return app

    async def start(self) -> None:
        self._ca.ensure()
        self._runner = web.AppRunner(self.build_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("Gateway listening on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- authentication --

    def _remote_ip(self, request: web.Request) -> str:
        """Best-effort client address for logging and rate limiting.

        Falls back to the raw peer when the forwarded chain cannot be trusted;
        callers that make security decisions must use ``_client_ip`` instead,
        which refuses to guess.
        """
        try:
            return self._client_ip(request)
        except UntrustedProxyError:
            return request.remote or ""

    def _client_ip(self, request: web.Request) -> str:
        """Client address suitable for access-control decisions.

        Raises ``UntrustedProxyError`` when the deployment is ambiguous.
        """
        return resolve_client_ip(
            request.remote or "",
            request.headers.get("X-Forwarded-For"),
            self._trusted_proxies,
        )

    @web.middleware
    async def _auth_middleware(
        self,
        request: web.Request,
        handler: Handler,
    ) -> web.StreamResponse:
        remote = self._remote_ip(request)

        if not self._rate.allow(remote):
            self._audit.record(
                "http.rate_limited", "denied", remote_ip=remote, detail={"path": request.path}
            )
            return web.json_response({"error": "rate_limited"}, status=429)

        if request.path in _UNAUTHENTICATED_PATHS:
            return await handler(request)

        device_id = await self._authenticate(request, remote)
        if device_id is None:
            return web.json_response(_AUTH_FAILED, status=401)

        request["device_id"] = device_id
        self._devices.touch(device_id)
        return await handler(request)

    async def _authenticate(self, request: web.Request, remote: str) -> str | None:
        """Return the authenticated device id, or None.

        A bearer token is accepted as a shortcut for an already-authenticated
        device; a signature is required to obtain one in the first place.
        """
        bearer = request.headers.get("Authorization", "")
        if bearer.startswith("Bearer "):
            try:
                device_id = self._tokens.verify(bearer.removeprefix("Bearer ").strip())
            except TokenError as exc:
                self._audit.record(
                    "auth.token", "denied", remote_ip=remote, detail={"reason": str(exc)}
                )
                return None
            # A token outlives revocation unless the registry is consulted on
            # every request, so the kill switch takes effect immediately.
            if self._devices.get(device_id) is None:
                self._audit.record(
                    "auth.token", "denied", device_id=device_id, remote_ip=remote,
                    detail={"reason": "device revoked"},
                )
                return None
            return device_id

        return await self._authenticate_signature(request, remote)

    async def _authenticate_signature(self, request: web.Request, remote: str) -> str | None:
        try:
            signed = SignedRequest.from_headers(dict(request.headers))
        except SignatureError as exc:
            self._audit.record(
                "auth.signature", "denied", remote_ip=remote, detail={"reason": str(exc)}
            )
            return None

        device = self._devices.get(signed.device_id)
        if device is None:
            # Still burn the work of reading the body so that an unknown device
            # is not distinguishable from a bad signature by response timing.
            await request.read()
            self._audit.record(
                "auth.signature", "denied", device_id=signed.device_id, remote_ip=remote,
                detail={"reason": "unknown or revoked device"},
            )
            return None

        try:
            verify_signature(
                signed,
                public_key_b64=device.public_key,
                method=request.method,
                path=request.path,
                body=await request.read(),
                nonce_cache=self._nonces,
            )
        except SignatureError as exc:
            self._audit.record(
                "auth.signature", "denied", device_id=device.id, remote_ip=remote,
                detail={"reason": str(exc)},
            )
            return None

        return device.id

    # -- handlers --

    async def _handle_health(self, _request: web.Request) -> web.StreamResponse:
        return web.json_response({"status": "ok", "service": "albert-gateway"})

    async def _handle_pair(self, request: web.Request) -> web.StreamResponse:
        try:
            remote = self._client_ip(request)
        except UntrustedProxyError as exc:
            # Deployment is ambiguous: a proxy is forwarding but we cannot tell
            # who the real caller is. Refusing is the only safe answer, and it
            # surfaces the misconfiguration instead of hiding it.
            self._audit.record(
                "auth.pair", "denied", remote_ip=request.remote,
                detail={"reason": str(exc)},
            )
            return web.json_response({"error": "pairing_not_allowed"}, status=403)

        if not is_trusted_pairing_source(remote):
            self._audit.record("auth.pair", "denied", remote_ip=remote,
                               detail={"reason": "untrusted network"})
            return web.json_response({"error": "pairing_not_allowed"}, status=403)

        try:
            payload = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid_json"}, status=400)

        code = str(payload.get("code", ""))
        name = str(payload.get("name", "")).strip()
        public_key = str(payload.get("public_key", ""))
        platform = payload.get("platform", "unknown")
        if not code or not name or not public_key:
            return web.json_response({"error": "missing_fields"}, status=400)

        try:
            device = self._devices.redeem_pairing_code(
                code, name=name, platform=platform, public_key=public_key, remote_ip=remote
            )
        except PairingError as exc:
            self._audit.record("auth.pair", "denied", remote_ip=remote,
                               detail={"reason": str(exc)})
            return web.json_response({"error": "pairing_failed"}, status=403)

        credential = self._ca.issue_for_device(device.id, device_name=device.name)
        device.cert_serial = credential.serial
        self._audit.record("auth.pair", "allowed", device_id=device.id, remote_ip=remote,
                           detail={"name": device.name, "platform": device.platform})

        # The private key is transmitted exactly once, over the trusted network
        # the pairing was restricted to, and is never persisted server-side.
        return web.json_response(
            {
                "device_id": device.id,
                "client_certificate": credential.certificate_pem,
                "client_private_key": credential.private_key_pem,
                "ca_certificate": self._ca.certificate_pem(),
                "token": self._tokens.issue(device.id),
                "token_ttl_seconds": self._tokens.ttl_seconds,
            },
            status=201,
        )

    async def _handle_issue_pair_code(self, request: web.Request) -> web.StreamResponse:
        """Issue a pairing code to the operator running the CLI on this host.

        Authenticated by a shared secret readable only by the file's owner,
        rather than by source address. Source address is not usable here: behind
        a reverse proxy every request appears to originate from loopback, so an
        address check would hand this endpoint to the internet.
        """
        remote = self._remote_ip(request)
        presented = request.headers.get("X-Albert-Admin", "")
        if not self._admin_secret or not hmac.compare_digest(presented, self._admin_secret):
            self._audit.record("auth.pair_code", "denied", remote_ip=remote)
            return web.json_response(_AUTH_FAILED, status=401)

        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        code = self._devices.issue_pairing_code(label=str(payload.get("label", "")))
        self._audit.record("auth.pair_code", "allowed", remote_ip=remote)
        return web.json_response(
            {"code": code.code, "expires_at": code.expires_at.isoformat()}
        )

    async def _handle_token(self, request: web.Request) -> web.StreamResponse:
        device_id = request["device_id"]
        self._audit.record("auth.token", "allowed", device_id=device_id,
                           remote_ip=self._remote_ip(request))
        return web.json_response(
            {"token": self._tokens.issue(device_id), "ttl_seconds": self._tokens.ttl_seconds}
        )

    async def _handle_list_devices(self, request: web.Request) -> web.StreamResponse:
        devices: list[dict[str, Any]] = [
            {
                "id": d.id,
                "name": d.name,
                "platform": d.platform,
                "created_at": d.created_at,
                "last_seen_at": d.last_seen_at,
                "is_current": d.id == request["device_id"],
            }
            for d in self._devices.list_devices()
        ]
        return web.json_response({"devices": devices})

    async def _handle_revoke_device(self, request: web.Request) -> web.StreamResponse:
        target = request.match_info["device_id"]
        revoked = self._devices.revoke(target)
        self._audit.record(
            "device.revoke", "allowed" if revoked else "error",
            device_id=request["device_id"], remote_ip=self._remote_ip(request),
            detail={"target": target},
        )
        if not revoked:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({"revoked": target})

    async def _handle_revoke_all(self, request: web.Request) -> web.StreamResponse:
        count = self._devices.revoke_all()
        self._audit.record("device.revoke_all", "allowed", device_id=request["device_id"],
                           remote_ip=self._remote_ip(request), detail={"count": count})
        return web.json_response({"revoked": count})

    async def _handle_activity(self, request: web.Request) -> web.StreamResponse:
        try:
            limit = min(int(request.query.get("limit", "100")), 1000)
        except ValueError:
            limit = 100
        events = [
            {
                "timestamp": e.timestamp,
                "action": e.action,
                "outcome": e.outcome,
                "device_id": e.device_id,
                "agent_id": e.agent_id,
                "detail": e.detail,
            }
            for e in self._audit.tail(limit)
        ]
        return web.json_response({"events": events})
