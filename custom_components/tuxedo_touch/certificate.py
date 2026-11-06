"""The certificates Home Assistant issues a tuxweb panel, and installing them.

A panel that declares `certificate_install` is issued its TLS certificate
from an authority of the entry's own, generated the first time and never
again while one exists, and the certificate is installed on the panel with
one InstallCertificate call. The key generation and signing here are
blocking work and run in the executor; the install goes over the client's
verifying context, or for a panel whose address moved over the context that
skips only the name check.
"""

from __future__ import annotations

import datetime
import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .api import (
    TuxedoTouchCertificateError,
    TuxedoTouchClient,
    TuxedoTouchConnectionError,
    TuxedoTouchError,
    TuxedoTouchInstallRefused,
    TuxedoTouchTokenRejected,
)
from .const import (
    CA_VALIDITY,
    CONF_CA_CERT,
    CONF_CA_KEY,
    CONF_CERT_NOT_AFTER,
    CONF_PANEL_CA,
    CONF_USE_HTTPS,
    LEAF_VALIDITY,
)

# Backdating: the panel refuses a certificate whose notBefore is after its own
# clock, with no allowance, so both certificates start five minutes early.
_CLOCK_SKEW = datetime.timedelta(minutes=5)
# The longest common name X.509 allows; a longer host goes in the SAN only.
_CN_MAX = 64


class CertificateRenewalFailed(TuxedoTouchError):
    """Issuing or installing the panel's certificate failed; nothing was stored.

    `reason` names the cause for the user (tuxweb's own word for a refusal).
    `refused` is a refusal that asking again will not change - a 4xx, or no
    token to send - which no automatic path retries.
    """

    def __init__(self, reason: str, *, refused: bool) -> None:
        super().__init__(f"The panel's certificate was not renewed: {reason}")
        self.reason = reason
        self.refused = refused


@dataclass(frozen=True)
class IssuedLeaf:
    """A certificate for the panel, its key, and what identifies it."""

    chain_pem: str
    key_pem: str
    not_after: str
    sha256: str


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def authority_name(entry_id: str) -> str:
    """The common name of an entry's authority: the entry id's last 8 characters.

    The tail rather than the head: entry ids are ULIDs, whose leading
    characters are a timestamp shared by entries made close together.
    """
    return f"Tuxedo Touch CA {entry_id[-8:]}"


def generate_authority(entry_id: str) -> tuple[str, str]:
    """A new self-signed authority for one entry: (certificate PEM, key PEM).

    EC P-256, CA:TRUE with path length 0, keyCertSign only, ten years. The
    key is PKCS#8 PEM, unencrypted. Blocking: run in the executor.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, authority_name(entry_id)),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Home Assistant"),
        ]
    )
    start = _now() - _CLOCK_SKEW
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + CA_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return _pem(cert), _key_pem(key)


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _key_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def _host_name(host: str) -> x509.GeneralName:
    """The subjectAltName entry for a host: an IP address if it is one."""
    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)


def issue_leaf(ca_cert_pem: str, ca_key_pem: str, host: str) -> IssuedLeaf:
    """A certificate for the panel at `host`, signed by the entry's authority.

    EC P-256, 397 days, CN and subjectAltName the host (an IP address entry
    when it parses as one, else a DNS name), serverAuth. The chain is the
    leaf then the authority. Blocking: run in the executor.
    """
    authority = x509.load_pem_x509_certificate(ca_cert_pem.encode("ascii"))
    authority_key = serialization.load_pem_private_key(
        ca_key_pem.encode("ascii"), password=None
    )
    if not isinstance(authority_key, ec.EllipticCurvePrivateKey):
        raise ValueError("the entry's authority key is not an EC key")
    key = ec.generate_private_key(ec.SECP256R1())
    subject = (
        [x509.NameAttribute(NameOID.COMMON_NAME, host)] if len(host) <= _CN_MAX else []
    )
    start = _now() - _CLOCK_SKEW
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name(subject))
        .issuer_name(authority.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + LEAF_VALIDITY)
        .add_extension(
            x509.SubjectAlternativeName([_host_name(host)]),
            critical=not subject,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                authority_key.public_key()
            ),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(authority_key, hashes.SHA256())
    )
    return IssuedLeaf(
        chain_pem=_pem(leaf) + _pem(authority),
        key_pem=_key_pem(key),
        not_after=leaf.not_valid_after_utc.isoformat(),
        sha256=leaf.fingerprint(hashes.SHA256()).hex(),
    )


def leaf_names_host(leaf: x509.Certificate, host: str) -> bool:
    """Whether a certificate's subjectAltName names `host`."""
    try:
        names = leaf.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except x509.ExtensionNotFound:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        wanted = host.lower().rstrip(".")
        return any(
            name.lower().rstrip(".") == wanted
            for name in names.get_values_for_type(x509.DNSName)
        )
    return address in names.get_values_for_type(x509.IPAddress)


def _leaf(chain: list[bytes]) -> x509.Certificate | None:
    if not chain:
        return None
    try:
        return x509.load_der_x509_certificate(chain[0])
    except ValueError:
        return None


def leaf_expiry(chain: list[bytes]) -> datetime.datetime | None:
    """When the certificate the panel presents expires; None if unread."""
    leaf = _leaf(chain)
    return leaf.not_valid_after_utc if leaf is not None else None


def moved_from(chain: list[bytes], own_ca_pem: str, host: str) -> bool:
    """Whether the panel serves a certificate of ours that names another address.

    The panel's address changed and its certificate still names the old one:
    the leaf it presents was issued by the entry's own authority and does not
    name `host`.
    """
    leaf = _leaf(chain)
    if leaf is None:
        return False
    try:
        authority = x509.load_pem_x509_certificate(own_ca_pem.encode("ascii"))
        leaf.verify_directly_issued_by(authority)
    except (ValueError, TypeError, InvalidSignature):
        return False
    return not leaf_names_host(leaf, host)


def failure_reason(err: Exception) -> str:
    """A short word for why an install failed, for messages and the issue."""
    if isinstance(err, TuxedoTouchInstallRefused):
        return err.reason
    if isinstance(err, TuxedoTouchTokenRejected):
        return "token_rejected"
    if isinstance(err, TuxedoTouchCertificateError):
        return "certificate_untrusted"
    if isinstance(err, TuxedoTouchConnectionError):
        return "unreachable"
    return "panel_error"


def _refused(err: Exception) -> bool:
    if isinstance(err, TuxedoTouchTokenRejected):
        return True
    if isinstance(err, TuxedoTouchInstallRefused):
        return err.status is None or 400 <= err.status < 500
    return False


def _authority_and_leaf(
    ca_cert: str | None, ca_key: str | None, entry_id: str, host: str
) -> tuple[str, str, IssuedLeaf]:
    """The stored authority, or a new one, and a leaf it issued for `host`."""
    if ca_cert is None or ca_key is None:
        ca_cert, ca_key = generate_authority(entry_id)
    return ca_cert, ca_key, issue_leaf(ca_cert, ca_key, host)


async def async_issue_and_install(
    hass: HomeAssistant,
    client: TuxedoTouchClient,
    data: Mapping[str, Any],
    *,
    entry_id: str,
    ignore_hostname: bool = False,
) -> dict[str, Any]:
    """Issue the panel a certificate from the entry's authority and install it.

    The authority is the stored one, or a new one if none is stored. The
    client trusts it beside the pin before the install goes out, so requests
    made while the panel switches still verify, and pins it once the panel
    has accepted the certificate. Returns the entry data to store: the
    authority, the pin moved to it, and the new certificate's expiry. The
    leaf's private key goes to the panel and nowhere else. Raises
    CertificateRenewalFailed with the client as it was.
    """
    try:
        ca_cert, ca_key, leaf = await hass.async_add_executor_job(
            _authority_and_leaf,
            data.get(CONF_CA_CERT) or None,
            data.get(CONF_CA_KEY) or None,
            entry_id,
            data[CONF_HOST],
        )
    except ValueError as err:
        raise CertificateRenewalFailed("bad_authority", refused=True) from err
    stored = client.own_ca
    client.trust_own_ca(ca_cert)
    try:
        answer = await client.async_install_certificate(
            leaf.chain_pem, leaf.key_pem, ignore_hostname=ignore_hostname
        )
        if answer.sha256 != leaf.sha256:
            raise TuxedoTouchInstallRefused("unexpected_answer", 200)
    except TuxedoTouchError as err:
        client.trust_own_ca(stored)
        raise CertificateRenewalFailed(
            failure_reason(err), refused=_refused(err)
        ) from err
    client.pin_own_ca()
    return {
        CONF_CA_CERT: ca_cert,
        CONF_CA_KEY: ca_key,
        CONF_PANEL_CA: ca_cert,
        CONF_CERT_NOT_AFTER: leaf.not_after,
    }


async def async_follow_move(
    hass: HomeAssistant, client: TuxedoTouchClient, data: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Re-issue the certificate of a panel whose address moved, if that is the case.

    For a request that failed on the certificate. Applies only to an entry
    pinned to its own authority over HTTPS whose panel presents a
    certificate of that authority naming another address; then the install
    goes over the context that skips the name check. None when it does not
    apply. Raises CertificateRenewalFailed when it applied and failed.
    """
    own = data.get(CONF_CA_CERT)
    if (
        not own
        or not data.get(CONF_CA_KEY)
        or data.get(CONF_PANEL_CA) != own
        or not data.get(CONF_USE_HTTPS)
    ):
        return None
    chain = await client.async_read_presented_chain()
    if not moved_from(chain, own, data[CONF_HOST]):
        return None
    return await async_issue_and_install(
        hass, client, data, entry_id="", ignore_hostname=True
    )
