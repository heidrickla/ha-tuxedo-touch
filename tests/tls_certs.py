"""Certificates for a fake panel served over TLS, made fresh for each test run.

The shape of the live tuxweb panel's: an EC P-256 leaf naming its IP address
in the subject alternative name, issued by a self-signed authority, and served
as the chain leaf + authority. The leaf names 127.0.0.1, which is where the
fake panel listens.

Server contexts are built from files, which is the only way the ssl module
loads a server certificate; callers build them outside the event loop, where
Home Assistant's blocking-call guard does not apply.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import ssl
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

LEAF_IP = "127.0.0.1"


@dataclass(frozen=True)
class Authority:
    """A self-signed certificate authority and its key."""

    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate

    @property
    def pem(self) -> str:
        return self.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")

    @property
    def fingerprint(self) -> str:
        return self.cert.fingerprint(hashes.SHA256()).hex(":").upper()

    @property
    def subject(self) -> str:
        return self.cert.subject.rfc4514_string()


def _name(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Tuxedo Touch"),
        ]
    )


def make_authority(common_name: str = "Tuxedo Owner CA") -> Authority:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(_name(common_name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
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
    return Authority(key, cert)


@dataclass(frozen=True)
class Leaf:
    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate

    @property
    def expires(self) -> str:
        return self.cert.not_valid_after_utc.isoformat()


def make_leaf(authority: Authority, ip: str = LEAF_IP, days: int = 397) -> Leaf:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ip)]))
        .issuer_name(authority.cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                authority.key.public_key()
            ),
            critical=False,
        )
        .sign(authority.key, hashes.SHA256())
    )
    return Leaf(key, cert)


def server_context(
    directory: str, leaf: Leaf, chain: list[x509.Certificate]
) -> ssl.SSLContext:
    """A server context serving `leaf` followed by `chain`, as tuxweb does."""
    os.makedirs(directory, exist_ok=True)
    name = leaf.cert.fingerprint(hashes.SHA256()).hex()[:16]
    cert_path = os.path.join(directory, f"{name}.chain.pem")
    key_path = os.path.join(directory, f"{name}.key.pem")
    with open(cert_path, "wb") as handle:
        for cert in (leaf.cert, *chain):
            handle.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as handle:
        handle.write(
            leaf.key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


@dataclass(frozen=True)
class PanelTls:
    """Everything one test needs: two authorities and the server contexts.

    `trusted` serves a leaf from authority A with A in the chain; `other`
    serves a leaf from authority B with B; `broken` serves a leaf from A with
    the unrelated authority C last in the chain, so the anchor the panel
    presents does not verify the leaf it serves.
    """

    authority: Authority
    other_authority: Authority
    unrelated: Authority
    leaf: Leaf
    other_leaf: Leaf
    trusted: ssl.SSLContext
    other: ssl.SSLContext
    broken: ssl.SSLContext


def make_panel_tls(directory: str) -> PanelTls:
    authority = make_authority("Tuxedo Owner CA")
    other_authority = make_authority("Replacement CA")
    unrelated = make_authority("Unrelated CA")
    leaf = make_leaf(authority)
    other_leaf = make_leaf(other_authority)
    return PanelTls(
        authority=authority,
        other_authority=other_authority,
        unrelated=unrelated,
        leaf=leaf,
        other_leaf=other_leaf,
        trusted=server_context(directory, leaf, [authority.cert]),
        other=server_context(directory, other_leaf, [other_authority.cert]),
        broken=server_context(
            os.path.join(directory, "broken"), leaf, [unrelated.cert]
        ),
    )


def same_certificate(pem_a: str | None, pem_b: str) -> bool:
    """Whether two PEM strings hold the same certificate, whatever the wrapping."""
    if pem_a is None:
        return False
    return x509.load_pem_x509_certificate(
        pem_a.encode("ascii")
    ) == x509.load_pem_x509_certificate(pem_b.encode("ascii"))
