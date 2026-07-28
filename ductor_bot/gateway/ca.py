"""Client-certificate authority for the mTLS layer.

The gateway runs its own tiny private CA.  Its only job is to issue one
certificate per paired device, so that the reverse proxy can reject anything
else during the TLS handshake -- before a request reaches application code.

This is deliberately *not* a general-purpose PKI.  There is one CA, it signs
only client certificates, and it never signs another CA.  Trust is anchored by
copying ``ca.crt`` into the proxy configuration.

Revocation is handled at the application layer via the device registry rather
than by a CRL: the certificate proves *which* device is calling, and the
registry decides whether that device is still allowed.  A CRL would add a
distribution problem for no extra safety on a single-host deployment.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING, NamedTuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_CA_COMMON_NAME = "Albert Gateway CA"
_CA_VALID_YEARS = 10
_CLIENT_VALID_DAYS = 397  # the maximum most TLS stacks accept without complaint

# 0600: the CA key can mint credentials for the whole gateway.  Anything more
# permissive would make the mTLS layer decorative.
_KEY_MODE = 0o600
_CERT_MODE = 0o644


class IssuedCredential(NamedTuple):
    """A freshly minted client credential.  The key is returned once and never stored."""

    certificate_pem: str
    private_key_pem: str
    serial: str


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)


class ClientCertificateAuthority:
    """Issues and stores client certificates for paired devices.

    Uses P-256 rather than RSA: the keys are far smaller, which matters when a
    credential has to be carried through a QR code or a mobile keychain, and
    every current TLS stack supports it.
    """

    def __init__(self, ca_dir: Path) -> None:
        self._dir = ca_dir
        self._cert_path = ca_dir / "ca.crt"
        self._key_path = ca_dir / "ca.key"

    @property
    def certificate_path(self) -> Path:
        """Path to the CA certificate, to be trusted by the reverse proxy."""
        return self._cert_path

    @property
    def exists(self) -> bool:
        return self._cert_path.is_file() and self._key_path.is_file()

    def ensure(self) -> None:
        """Create the CA on first use.  Idempotent."""
        if self.exists:
            return
        logger.info("Creating gateway client CA in %s", self._dir)
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _CA_COMMON_NAME)])
        now = _utcnow()
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=365 * _CA_VALID_YEARS))
            # path_length=0: this CA may sign leaf certificates and nothing else,
            # so a stolen client key can never be used to mint further devices.
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_cert_sign=True,
                    crl_sign=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        _write(
            self._key_path,
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            _KEY_MODE,
        )
        _write(
            self._cert_path,
            certificate.public_bytes(serialization.Encoding.PEM),
            _CERT_MODE,
        )

    def _load(self) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
        key = serialization.load_pem_private_key(self._key_path.read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise TypeError("gateway CA key is not an EC key")
        return key, x509.load_pem_x509_certificate(self._cert_path.read_bytes())

    def issue_for_device(self, device_id: str, *, device_name: str = "") -> IssuedCredential:
        """Mint a client certificate whose common name is the device id.

        Binding the device id into the certificate lets the proxy pass it
        upstream, so the mTLS identity and the signing identity can be compared
        -- a certificate for one device paired with another device's signature
        is then a detectable mismatch rather than a silent pass.
        """
        self.ensure()
        ca_key, ca_cert = self._load()

        device_key = ec.generate_private_key(ec.SECP256R1())
        attributes = [x509.NameAttribute(NameOID.COMMON_NAME, device_id)]
        if device_name:
            attributes.append(x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, device_name))

        now = _utcnow()
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name(attributes))
            .issuer_name(ca_cert.subject)
            .public_key(device_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=_CLIENT_VALID_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            # CLIENT_AUTH only: this credential can authenticate to the gateway
            # and cannot be repurposed to impersonate a server.
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]),
                critical=True,
            )
            .sign(ca_key, hashes.SHA256())
        )

        logger.info("Issued client certificate for device %s", device_id)
        return IssuedCredential(
            certificate_pem=certificate.public_bytes(serialization.Encoding.PEM).decode(),
            private_key_pem=device_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode(),
            serial=format(certificate.serial_number, "x"),
        )

    def certificate_pem(self) -> str:
        """The CA certificate the client must trust and the proxy must verify against."""
        self.ensure()
        return self._cert_path.read_text(encoding="utf-8")
