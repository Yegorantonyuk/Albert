"""Gateway: authenticated multi-agent entry point for the Albert app.

The gateway is the only component intended to be reachable from outside the
host.  It sits in front of the per-agent ``ApiServer`` instances and enforces
two independent authentication layers:

1. **mTLS** -- a client certificate issued during device pairing.  Traffic
   without a valid certificate is rejected during the TLS handshake by the
   reverse proxy and never reaches this process.
2. **Request signing** -- every request carries an Ed25519 signature produced
   by a private key that never leaves the device's secure hardware.  This layer
   is transport-independent, so it keeps working on platforms where client
   certificates are impractical.

Neither layer is sufficient alone: mTLS proves *which device* is talking,
signing proves *that this exact request is live and not a replay*.
"""

from __future__ import annotations

from ductor_bot.gateway.models import Device, PairingCode
from ductor_bot.gateway.signing import SignatureError, SignedRequest, verify_signature

__all__ = [
    "Device",
    "PairingCode",
    "SignatureError",
    "SignedRequest",
    "verify_signature",
]
