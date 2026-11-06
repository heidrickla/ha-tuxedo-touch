"""Local API client for Honeywell Tuxedo Touch WIFI (TUXWIFIS/TUXWIFIW).

This targets firmware TUXW_V5.3.21.0_VA (and presumably other V5.x releases),
which requires an authenticated web session for ALL local access - including
the /tuxedoapi.html page that hands out the AES key/IV used to sign and
encrypt every subsequent API call. Older firmware (~V4.x) reportedly allows
unauthenticated access to that page; this client always logs in first, which
should be harmless even if a given unit doesn't strictly require it.

See ../../../docs/tuxedo_touch_api_notes.md for the full reverse-engineering
writeup this implementation is based on (login flow, HMAC/AES quirks, TLS
gotchas, known device bugs).

A panel running tuxweb - the replacement web server - is a second, simpler
contract on the same paths: no login page, no key page, no AES, and a
pre-shared bearer token instead of a session. The client asks once which of
the two it is talking to (async_probe_capabilities) and keeps the answer; the
stock path below is untouched by that answer being "stock". The one thing that
reopens the question is a tuxweb refusal - a 401 on the token, or a tuxweb
path answering 404 - which asks once more before it is believed
(async_recheck_capabilities), so a panel rolled back to stock under a running
entry is talked to as stock from the next call rather than for ever as tuxweb.

TLS follows the firmware. Stock is reached with the legacy context its 2009
certificate needs. A tuxweb panel over HTTPS is trusted on first use: once
the probe has said tuxweb, the client reads the certificate authority the
panel presents, proves it by repeating the probe with a context that
verifies against it, and from then on every request of that client verifies
against that authority (panel_ca). A pinned client never goes back to the
legacy context: a certificate it does not verify is
TuxedoTouchCertificateError.

A pinned client may also trust the entry's own authority (own_ca), the one
Home Assistant issues the panel's certificate from: the verifying context then
loads both, so connections keep verifying while the panel switches from one
certificate to the other (async_install_certificate).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import ssl
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import cache
from typing import Any

import aiohttp
from cryptography import x509
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import (
    API_BASE_PATH,
    CAP_CERTIFICATE_INSTALL,
    CAP_COMMAND_RESULT,
    CAPABILITIES_PATH,
    COLOURS,
    COUNTDOWN_RE,
    INSTALL_BODY_LIMIT,
    INSTALL_CERTIFICATE_PATH,
    KEYS_PATH,
    LOGIN_PATH,
    SOURCE_POLL,
    STATUS_NOT_AVAILABLE,
)

_LOGGER = logging.getLogger(__name__)

READIT_RE = re.compile(r'id=["\']readit["\'][^>]*value=["\']([0-9a-fA-F]+)["\']')

# An alarm code is ASCII digits. The stock command body is a form string built
# by hand, so a code carrying "&" or "=" would add fields to it.
ALARM_CODE_RE = re.compile(r"[0-9]+")

# The panel is a slow embedded device, but anything beyond this budget
# indicates a hang, not a slow success. aiohttp raises TimeoutError (not a
# ClientError) when this expires - see the except clauses below.
_TIMEOUT = aiohttp.ClientTimeout(total=15)
# The same budget for the handshake-only connection that reads the chain.
_CHAIN_TIMEOUT = 15.0

# How many credential POSTs one client may spend on credentials the panel is
# refusing. One - and that number comes from the panel, not from politeness.
# On stock firmware THREE failed web logins disable EVERY web account
# permanently: no timeout, no self-clear, and recovery only at the touchscreen
# (Setup, then the account screen, re-enable web access, Enable All, Apply).
# Patched firmware allows five and clears itself after five minutes, but the
# panel exposes no version anywhere, so the budget has to be safe on the
# stricter one. A longer backoff is not an alternative: any nonzero automatic
# retry rate reaches three eventually, and then the panel is bricked to the
# web for good.
LOGIN_ATTEMPT_BUDGET = 1

# Login-POST statuses that can only mean the panel compared a credential and
# refused it. Not how THIS firmware refuses - it answers 200 with no session
# cookie (docs/tuxedo_touch_api_notes.md line 92, and tests/fake_panel.py
# models it) - but a firmware that used the conventional codes would still be
# understood. Everything else outside (200, 302) is the embedded web server
# failing above the login logic, which never reached the comparison and must
# not spend the budget above: one 503 from a busy panel would otherwise
# condemn the entry for good.
CREDENTIAL_VERDICT_STATUSES = frozenset({401, 403})
# Statuses that say "ask again later" rather than anything about the account.
RETRYABLE_LOGIN_STATUSES = frozenset({408, 429})


class TuxedoTouchError(Exception):
    """Base error talking to the panel."""


class TuxedoTouchAuthError(TuxedoTouchError):
    """The panel judged a credential of ours and said no.

    Raised at exactly the places the panel's own three-strike counter moves,
    which are the two `_failed_logins += 1` sites in login(), and at the one
    other place a credential is judged: tuxweb refusing the bearer token,
    which has its own subclass below. Nothing else may raise it, and that is
    load-bearing rather than tidy: the coordinator turns this class into a
    permanent `credentials_rejected` flag on the config entry and the push
    stream stops for good on it, so a server or session fault wearing this
    class condemns the entry - and the reauthentication card then refuses
    the password that was right all along. Faults after an accepted login
    are TuxedoTouchSessionError; a login POST that answered without judging
    anything is a connection or panel error.
    """


class TuxedoTouchTokenRejected(TuxedoTouchAuthError):
    """tuxweb would not take the bearer token, or there was none to send.

    A credential judged and refused, so it routes as the auth error it is:
    the poll stops and asks the user, the stream stops for good, and a
    command fails with a reason. What it must NOT do is start a login - there
    is no login page on tuxweb, and the stock path's re-login on 401 is
    exactly the reflex this class exists to keep out. Its own class because
    the consequences differ in one way that matters: tuxweb counts nothing
    and locks nothing, so the coordinator does not write the three-strike
    flag for it, and the wording the user reads names the token rather than
    the web password.
    """


class TuxedoTouchCommandNotConfirmed(TuxedoTouchError):
    """tuxweb sent the command and the panel did not act on it in time.

    Its 504: the state byte never flipped within tuxweb's own 8 s ceiling,
    so the panel refused the arm (a faulted zone, say) or the disarm (a code
    it does not accept), or is simply slow. Either way nothing is assumed:
    the entity keeps showing what the panel reports, and the service call
    fails with this so an automation is not told an alarm armed when it did
    not. Stock firmware cannot say this - it answers 200 whatever the panel
    then does - so it is raised on the tuxweb path only.
    """


class TuxedoTouchFirmwareChanged(TuxedoTouchError):
    """A tuxweb refusal turned out to be the panel no longer running tuxweb.

    Raised once, from the call that was refused, after the re-probe it
    triggered came back with no capability list: the panel has gone back to
    stock firmware under a running entry. By the time this is raised the
    client is a stock client - the next call logs in with the web credentials
    and speaks the stock contract - and the call that ends here was never
    retried on that path, so it spent no login and the caller's one-login
    accounting is untouched. Deliberately NOT a TuxedoTouchAuthError: the
    panel refused a token it no longer recognises, which says nothing about
    the web credentials the entry also holds, and an auth error here would
    stop the poll and ask the user for a token the panel has stopped wanting.
    It is an ordinary failed read instead - UpdateFailed on the poll, a
    reconnect on the stream - and the retry that follows is the stock one.
    """


class TuxedoTouchCredentialsRefused(TuxedoTouchAuthError):
    """The budget above is spent: these credentials are not tried again.

    A subclass of the auth error so every existing `except
    TuxedoTouchAuthError` routes it exactly as it routes a real rejection,
    and its own class so a caller can tell "the panel said no" from "we did
    not ask". Raised before any request is built, so the panel never sees the
    attempt and never counts it.
    """


class TuxedoTouchSessionError(TuxedoTouchError):
    """An accepted session did not yield what it should have.

    Deliberately NOT a TuxedoTouchAuthError. Every raise site is downstream of
    a login POST the panel answered with a session cookie - it accepted the
    credential and counted a SUCCESSFUL login - so the page behind that
    session answering badly is a server or session fault. It retries like any
    other fault: the next poll logs in from scratch, which is the right
    response to one bad key page, and neither the entry nor the stream is
    condemned for it.
    """


class TuxedoTouchConnectionError(TuxedoTouchError):
    """Could not reach the panel at all."""


class TuxedoTouchUnreachable(TuxedoTouchConnectionError):
    """The request failed below HTTP: nothing answered it.

    Raised for a failed connection, handshake or timeout. Its parent alone
    is a panel that answered HTTP with something unusable, such as a login
    page that is not there.
    """


class TuxedoTouchInvalidCode(TuxedoTouchError):
    """The alarm code is not digits only. Raised before any request is built."""


class TuxedoTouchHttpsRequiredError(TuxedoTouchError):
    """The panel redirected a plain-HTTP API call to HTTPS.

    Its own class because this is a persistent misconfiguration with one
    known fix, not a transient failure: the coordinator turns it into a
    repair issue rather than another retry.
    """


class TuxedoTouchCertificateError(TuxedoTouchError):
    """The panel's TLS did not verify against the authority this entry trusts.

    Raised only by a client holding a pinned authority, and only for a
    request made with the verifying context: a certificate that authority
    did not issue, a name that does not match, or a handshake the verifying
    context refuses (a panel back on stock firmware ends there, before any
    certificate is compared). Never retried on the legacy context, never read
    as the panel having gone back to stock, and never a reason to log in: the
    coordinator raises the panel_certificate repair, whose fix flow shows the
    certificate the panel now presents and asks before trusting it.
    """


class TuxedoTouchInstallRefused(TuxedoTouchError):
    """The panel answered InstallCertificate with something other than success.

    `reason` is tuxweb's own word for it where it gives one (bad_chain,
    key_mismatch, tls_not_configured, write_failed and the rest), and
    otherwise names the status. The panel keeps serving the pair it had.
    Never retried by the client, and never a login.
    """

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(f"The panel refused the certificate: {reason}")
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class InstalledCertificate:
    """tuxweb's answer to an InstallCertificate it accepted."""

    not_after: str
    sha256: str


@dataclass(frozen=True)
class PanelCertificate:
    """A certificate authority as the panel presented it, in readable form.

    `fingerprint` is the SHA-256 of the authority's DER encoding in colon
    hex, `leaf_expires` the expiry of the certificate the panel serves (None
    when only the authority is known, as for a stored pin).
    """

    anchor_pem: str
    subject: str
    issuer: str
    fingerprint: str
    leaf_expires: str | None = None


@dataclass(frozen=True)
class TuxedoStatus:
    """One reported panel status, and where it came from.

    A poll fills in the two fields the REST answer carries. The push stream
    fills in the rest: it says outright whether the partition is armed (its
    0xFE/0xFF flag) and carries the exit-delay countdown, neither of which
    the display text on its own settles.
    """

    status: str
    # Lower case whichever source filled it in: "green", "red", "yellow".
    color: str | None = None
    source: str = SOURCE_POLL
    # None from a stock poll, which reports display text and nothing else. A
    # tuxweb poll reads the same state model the stream is fed from and
    # carries the flag, so it fills this in too.
    armed: bool | None = None
    seconds_remaining: int | None = None


@cache
def _legacy_ssl_context() -> ssl.SSLContext:
    """Build an SSLContext that tolerates this device's ancient cert/handshake.

    The unit ships a self-signed ~2009 SharkSSL demo certificate (1024-bit
    RSA, MD5 signature, expired since 2019) and requires legacy/unsafe TLS
    renegotiation. Modern TLS stacks refuse this by default; both flags below
    are required to complete the handshake at all. Confirmed working against
    real hardware with CPython 3.13 / OpenSSL 3.x - no external OpenSSL config
    file needed, unlike getting the system `openssl` CLI to cooperate.

    ONE context object for every client, and that is load-bearing rather than
    a saving. aiohttp keys its connection pool on the ssl argument as well as
    the host and port, and SSLContext defines no equality, so two contexts are
    two pool keys: a client built with its own context opens a second socket
    to a panel that serves ONE connection at a time, even though Home
    Assistant's pool already holds an idle connection to it. Sharing the
    object puts the config flow's probe and an entry's poller on the same key,
    so they take turns on one connection instead of contending for the unit.
    Nothing here is per-panel - hostname checking and verification are both
    off - so there is nothing to keep apart.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    ctx.set_ciphers("DEFAULT@SECLEVEL=0")
    return ctx


def trust_anchors(panel_ca: str | None, own_ca: str | None) -> str | None:
    """The cadata a pinned client verifies against: the pin, and the entry's own CA.

    None without a pin. The pin alone when there is no own authority or it
    is the pin. Otherwise both PEMs, pin first, in one string built the same
    way by every caller, so every client of an entry hands aiohttp the same
    cached context.
    """
    if panel_ca is None:
        return None
    if own_ca is None or own_ca == panel_ca:
        return panel_ca
    return f"{panel_ca.rstrip()}\n{own_ca}"


@cache
def _verified_ssl_context(pem: str) -> ssl.SSLContext:
    """A context that verifies a tuxweb panel against the authority pinned for it.

    Certificate and hostname verification on, TLS 1.2 at least, default
    ciphers and no legacy options. The authority is loaded from `cadata`
    only: Home Assistant's blocking-call guard flags a load from a file path
    in the event loop, and a PEM string needs no file. `pem` may hold more
    than one authority (trust_anchors).

    ONE object per exact cadata string, cached for the same reason
    _legacy_ssl_context is: aiohttp keys its connection pool on the ssl
    argument and SSLContext defines no equality, so the config flow's probe
    and the entry's coordinator must be handed the same object to share a
    pool key. Building one per client would put two connections on a panel
    the rest of this module treats as one-connection.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cadata=pem)
    return ctx


@cache
def _install_ssl_context(pem: str) -> ssl.SSLContext:
    """The verifying context without the name check, for InstallCertificate only.

    The chain is verified against the same authorities as
    _verified_ssl_context(pem), so only a panel holding a certificate they
    issued completes the handshake; the name it carries is not compared.
    That is what lets a panel whose address moved be issued a certificate for
    the new one. Cached per cadata string like the others, and never used for
    status, commands or the stream.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cadata=pem)
    return ctx


def _verifies(ssl_arg: ssl.SSLContext | bool) -> bool:
    """Whether a request made with this ssl argument verifies the panel."""
    return isinstance(ssl_arg, ssl.SSLContext) and ssl_arg.verify_mode in (
        ssl.CERT_REQUIRED,
        ssl.CERT_OPTIONAL,
    )


def _transport_error(
    err: aiohttp.ClientError | TimeoutError, ssl_arg: ssl.SSLContext | bool
) -> TuxedoTouchError:
    """The client's error for a request that failed below HTTP.

    A TLS failure on a verifying request is a certificate error, whether the
    certificate was refused or the handshake never reached it; everything
    else, and every failure on the legacy context, is a connection error as
    it always was.
    """
    if _verifies(ssl_arg) and isinstance(err, aiohttp.ClientSSLError):
        return TuxedoTouchCertificateError(
            "The panel's TLS connection did not verify against the certificate "
            f"authority this entry trusts: {err}"
        )
    return TuxedoTouchUnreachable(str(err))


def _der_of(item: object) -> bytes | None:
    """One certificate of a peer chain as DER bytes.

    get_unverified_chain answers DER bytes on Python 3.14; on 3.13 the same
    call has answered certificate objects, whose public_bytes() defaults to
    PEM. Both are taken, and anything else is dropped.
    """
    if isinstance(item, (bytes, bytearray)):
        return bytes(item)
    public_bytes = getattr(item, "public_bytes", None)
    if public_bytes is None:
        return None
    encoded = public_bytes()
    if isinstance(encoded, bytes) and not encoded.startswith(b"-----"):
        return encoded
    if isinstance(encoded, bytes):
        encoded = encoded.decode("ascii")
    if isinstance(encoded, str):
        return ssl.PEM_cert_to_DER_cert(encoded)
    return None


def _chain_of(ssl_object: object) -> list[bytes]:
    """The certificate chain a TLS connection's peer sent, leaf first, as DER.

    Empty when the TLS object offers no chain.
    """
    chain_of = getattr(ssl_object, "get_unverified_chain", None)
    if chain_of is None:
        return []
    return [der for item in chain_of() or [] if (der := _der_of(item)) is not None]


def _fingerprint(certificate: x509.Certificate) -> str:
    return certificate.fingerprint(hashes.SHA256()).hex(":").upper()


def presented_certificate(chain: list[bytes]) -> PanelCertificate | None:
    """The trust anchor of a peer chain - its last certificate - and the leaf's expiry.

    None for an empty chain or one that does not parse.
    """
    if not chain:
        return None
    try:
        leaf = x509.load_der_x509_certificate(chain[0])
        anchor = x509.load_der_x509_certificate(chain[-1])
    except ValueError:
        return None
    return PanelCertificate(
        anchor_pem=ssl.DER_cert_to_PEM_cert(chain[-1]),
        subject=anchor.subject.rfc4514_string(),
        issuer=anchor.issuer.rfc4514_string(),
        fingerprint=_fingerprint(anchor),
        leaf_expires=leaf.not_valid_after_utc.isoformat(),
    )


def describe_certificate(pem: str) -> PanelCertificate | None:
    """A stored authority in readable form, for diagnostics.

    None if it does not parse.
    """
    try:
        certificate = x509.load_pem_x509_certificate(pem.encode("ascii"))
    except ValueError:
        return None
    return PanelCertificate(
        anchor_pem=pem,
        subject=certificate.subject.rfc4514_string(),
        issuer=certificate.issuer.rfc4514_string(),
        fingerprint=_fingerprint(certificate),
    )


class TuxedoTouchClient:
    """Handles login/session/crypto for one Tuxedo Touch unit."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        use_https: bool,
        username: str,
        password: str,
        tuxweb_token: str | None = None,
        panel_ca: str | None = None,
        *,
        trust_on_first_use: bool = True,
        own_ca: str | None = None,
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._scheme = "https" if use_https else "http"
        self._username = username
        self._password = password
        # Blank and absent are the same thing: no token was configured.
        self._tuxweb_token = tuxweb_token or None
        # Which of the two contracts this panel speaks. None until
        # async_probe_capabilities has asked; a client nobody asked on behalf
        # of is a stock client, which is what every caller before tuxweb
        # existed was. The capability set is empty on stock.
        self._tuxweb: bool | None = None
        self._capabilities: frozenset[str] = frozenset()

        self._session_cookie: str | None = None
        self._key_hex: str | None = None
        self._iv_hex: str | None = None
        self._key: bytes | None = None
        self._iv: bytes | None = None
        self._authtokens: dict[str, str] = {}
        self._login_lock = asyncio.Lock()
        # Credential POSTs this client has spent on a password the panel
        # refused. Cleared only by a login that completes, so it survives
        # every reconnect, poll and command for as long as the client lives.
        self._failed_logins = 0

        # The certificate authority this panel is trusted through, as PEM:
        # stored on the entry, or learnt by this client's own probe. Used
        # only over HTTPS and only while the panel is not known to be stock.
        self._panel_ca = panel_ca or None
        # The entry's own authority, trusted beside the pin; see trust_anchors.
        self._own_ca = own_ca or None
        # What the panel presented to the probe that learnt (or failed to
        # learn) the pin, for the certificate repair to show.
        self._presented: PanelCertificate | None = None
        # Whether a proven authority is pinned at once (a new entry) or only
        # offered (an existing one, which pins after the user confirms).
        self._trust_on_first_use = trust_on_first_use
        self._trustable: PanelCertificate | None = None

    @property
    def base_url(self) -> str:
        return f"{self._scheme}://{self._host}:{self._port}"

    @property
    def _ssl_ctx(self) -> ssl.SSLContext | None:
        """The context the next request uses, decided at the time it is made.

        None on plain HTTP. The verifying context once an authority is pinned,
        for the probe, the re-check and every tuxweb request; it is given up
        only for a panel that answered stock through it. Otherwise the legacy
        context stock firmware needs.
        """
        if self._scheme != "https":
            return None
        anchors = self._anchors
        if anchors is not None and self._tuxweb is not False:
            return _verified_ssl_context(anchors)
        return _legacy_ssl_context()

    @property
    def _anchors(self) -> str | None:
        return trust_anchors(self._panel_ca, self._own_ca)

    @property
    def panel_ca(self) -> str | None:
        """The pinned certificate authority as PEM, or None."""
        return self._panel_ca

    @property
    def own_ca(self) -> str | None:
        """The entry's own authority this client trusts beside the pin, or None."""
        return self._own_ca

    def trust_own_ca(self, pem: str | None) -> None:
        """Trust the entry's own authority beside the pin, from the next request.

        Called before a certificate from a new authority is installed, so a
        request made while the panel switches still verifies, and with the
        stored value again if the install failed.
        """
        self._own_ca = pem or None

    def pin_own_ca(self) -> None:
        """Make the own authority the pin, dropping the old one.

        After the panel has accepted a certificate it issued: the old
        authority no longer issues anything the panel serves.
        """
        if self._own_ca is not None:
            self._panel_ca = self._own_ca

    @property
    def certificate_install(self) -> bool:
        """Whether the panel can be issued a certificate from Home Assistant.

        tuxweb over HTTPS, declaring certificate_install. False on stock, on
        plain HTTP, and on a tuxweb that does not declare it.
        """
        return (
            self.tuxweb
            and self._scheme == "https"
            and CAP_CERTIFICATE_INSTALL in self._capabilities
        )

    @property
    def presented_certificate(self) -> PanelCertificate | None:
        """What the panel presented to this client's first-use probe, if it ran."""
        return self._presented

    @property
    def trustable_certificate(self) -> PanelCertificate | None:
        """An authority that proved itself but was not pinned, awaiting a yes.

        Set only on a client built with trust_on_first_use off; see
        _async_trust_on_first_use and async_look_for_tuxweb.
        """
        return self._trustable

    @property
    def tls(self) -> str:
        """How this client reaches the panel: verified, legacy, or plain."""
        ctx = self._ssl_ctx
        if ctx is None:
            return "plain"
        return "verified" if _verifies(ctx) else "legacy"

    def certificate_error(
        self, err: aiohttp.ClientError
    ) -> TuxedoTouchCertificateError | None:
        """The certificate error a failed request of this client amounts to, if any.

        For a caller that made the request itself on this client's ssl_arg -
        the push stream - and has to tell a certificate failure from a drop.
        """
        failed = _transport_error(err, self.ssl_arg)
        return failed if isinstance(failed, TuxedoTouchCertificateError) else None

    @property
    def session(self) -> aiohttp.ClientSession:
        """The session every request of this client's runs on.

        Exposed so the push stream opens its long-lived request on the same
        session, and therefore the same cookie handling and the same
        connection pool, rather than making a second one of its own.
        """
        return self._session

    @property
    def ssl_arg(self) -> ssl.SSLContext | bool:
        """What to pass as aiohttp's `ssl=`, shared for the pool key's sake.

        Read per request, so the push stream opens with the context current
        when it connects - the verifying one once the probe has pinned.
        """
        return self._ssl_ctx or True

    # ------------------------------------------------------------------
    # Which firmware: one GET, once, before anything else
    # ------------------------------------------------------------------
    @property
    def tuxweb(self) -> bool:
        """Whether this panel runs tuxweb. False until a probe says so."""
        return self._tuxweb is True

    @property
    def capabilities(self) -> frozenset[str]:
        """What tuxweb declared, as it spelled it. Empty on stock."""
        return self._capabilities

    @property
    def confirms_commands(self) -> bool:
        """Whether a 200 to arm or disarm means the panel ACTED.

        tuxweb with `command_result` answers 200 only once it has seen the
        state byte flip, and 504 otherwise. Stock answers 200 for a command
        it sent, whatever the panel then does; the coordinator's ladder of
        stream, poll and assumed status exists for exactly that.
        """
        return CAP_COMMAND_RESULT in self._capabilities

    async def async_probe_capabilities(self) -> bool:
        """Ask the panel, once, whether it runs tuxweb. Cached for good.

        GET GetCapabilities, which needs no session and no token on either
        firmware. 200 with a JSON body carrying a `capabilities` list is
        tuxweb; anything else is stock - a 404 with the measured body on
        stock firmware, but equally a 302 to https over plain HTTP, a 200
        that is an HTML page, or a body with no list in it. The branch is on
        the list being there and on the strings in it; `firmware` and
        `contract` are never consulted, and strings nothing here names are
        kept but ignored.

        Nothing on this path can log in: the request is built by hand below
        and never touches _ensure_authenticated, so a panel answering 401
        here - which neither firmware does - would read as stock rather than
        start a login handshake. A connection failure is raised and NOT
        cached, so the next setup asks again rather than remembering an
        answer it never got.

        "For good" has one exception, and it only ever moves the verdict one
        way: a tuxweb refusal asks again (async_recheck_capabilities), and a
        panel that has stopped declaring the list drops the client to stock.
        A stock client is never re-asked - its 401 is a session to renew, its
        login budget is the guard, and a move TO tuxweb takes a reload.

        Over HTTPS with no pinned authority, a tuxweb answer is also where
        the pin is learnt, or offered: see _async_trust_on_first_use. With a
        pin, this GET is made with the verifying context, and a certificate
        it does not verify raises TuxedoTouchCertificateError, uncached,
        rather than being asked again on the legacy context.
        """
        if self._tuxweb is not None:
            return self._tuxweb
        learning = self._scheme == "https" and self._panel_ca is None
        status, capabilities = await self._async_ask_capabilities()
        self._tuxweb = capabilities is not None
        self._capabilities = capabilities or frozenset()
        if self._tuxweb and learning:
            await self._async_trust_on_first_use()
        if self._tuxweb:
            _LOGGER.debug(
                "The panel at %s runs tuxweb, declaring %s",
                self._host,
                sorted(self._capabilities),
            )
        else:
            _LOGGER.debug(
                "The panel at %s answered HTTP %s to the capability probe: "
                "stock firmware",
                self._host,
                status,
            )
        return self._tuxweb

    async def async_recheck_capabilities(self, status: int) -> None:
        """Ask again, after a tuxweb refusal, whether the panel is still tuxweb.

        The stale case the probe above cannot see: the verdict is cached for
        the client's lifetime, so a panel rolled back to stock while Home
        Assistant runs went on being spoken to as tuxweb - bearer token,
        plaintext body, `command_result` semantics - and its refusals were
        surfaced as a token the user should re-enter. A refusal that is
        consistent with the contract having gone away - `status` is the 401
        or 404 the caller just read - is worth one more question before it
        is believed, and that question is the same GET as the setup probe,
        under the same rules: no login, no session, and a connection failure
        raised rather than turned into a verdict.

        Returns if the panel still declares a list: the refusal was real, and
        the caller raises it exactly as it always did. Raises
        TuxedoTouchFirmwareChanged if the list is gone, with the client
        already in stock mode: the caller lets that through unretried, so the
        call that was refused spends no login, and the next call is an
        ordinary stock call. One question per refusal and never a retry of
        the refused request, which is what keeps a panel that is genuinely
        refusing a bad token from turning into a probe loop - after the poll
        surfaces the auth error Home Assistant stops polling, and the stream
        stops for good, so the two re-probes that took are the last.

        The capability set is refreshed either way. A tuxweb that still
        answers is believed about what it offers now, not what it offered at
        setup.
        """
        if not self.tuxweb:
            raise TuxedoTouchError("async_recheck_capabilities is for tuxweb mode only")
        # On a pinned client this GET verifies, and a certificate failure is
        # raised as itself: it says nothing about which firmware answered.
        probe_status, capabilities = await self._async_ask_capabilities()
        if capabilities is not None:
            self._capabilities = capabilities
            _LOGGER.debug(
                "The panel at %s answered HTTP %s as tuxweb and still declares "
                "%s: the refusal stands",
                self._host,
                status,
                sorted(capabilities),
            )
            return
        self._tuxweb = False
        self._capabilities = frozenset()
        _LOGGER.warning(
            "The panel at %s answered HTTP %s to a tuxweb request and now "
            "answers HTTP %s to the capability probe, with no capability list: "
            "it has gone back to stock firmware. This entry talks to it on the "
            "stock contract from here - the web login, not the token - and "
            "a return to tuxweb needs the entry reloaded",
            self._host,
            status,
            probe_status,
        )
        raise TuxedoTouchFirmwareChanged(
            f"The panel answered HTTP {status} as tuxweb and then declared no "
            "capabilities: it has gone back to stock firmware, and this entry "
            "talks to it on the stock contract from the next request"
        )

    async def _async_ask_capabilities(
        self, *, ssl_ctx: ssl.SSLContext | None = None
    ) -> tuple[int, frozenset[str] | None]:
        """One GET of GetCapabilities: the status, and the list if it declared one.

        The request behind the probe, the re-check and the first-use proof,
        so they cannot drift: built by hand, no session and no token,
        redirects not followed, and a connection failure raised as such. None
        for the list is "not tuxweb", whatever the status was; see
        _capabilities_of for why an empty list is not the same thing.
        `ssl_ctx` overrides the client's own context, for the proof only.
        """
        url = f"{self.base_url}{API_BASE_PATH}{CAPABILITIES_PATH}"
        ssl_arg: ssl.SSLContext | bool = ssl_ctx or self.ssl_arg
        try:
            async with self._session.get(
                url,
                ssl=ssl_arg,
                timeout=_TIMEOUT,
                allow_redirects=False,
            ) as resp:
                status = resp.status
                capabilities = None
                if status == 200:
                    try:
                        payload = await resp.json(content_type=None)
                    except ValueError:
                        payload = None
                    capabilities = _capabilities_of(payload)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise _transport_error(err, ssl_arg) from err
        return status, capabilities

    async def _async_read_chain(self) -> list[bytes]:
        """The certificate chain the panel serves, read from a handshake alone.

        Its own connection, on the legacy context, closed as soon as the
        handshake is done and nothing sent on it. The probe's own response
        cannot supply the chain: aiohttp hands the connection back to the
        pool as soon as a body has arrived with the headers, which the
        capability answer always does, so the response no longer holds it.
        Empty when the connection cannot be made; the proof that follows is
        what the chain is judged by in any case.
        """
        try:
            async with asyncio.timeout(_CHAIN_TIMEOUT):
                _, writer = await asyncio.open_connection(
                    self._host, self._port, ssl=_legacy_ssl_context()
                )
        except (OSError, TimeoutError) as err:
            _LOGGER.debug(
                "Could not read the certificate chain at %s: %s", self._host, err
            )
            return []
        try:
            return _chain_of(writer.get_extra_info("ssl_object"))
        finally:
            writer.close()
            with suppress(OSError, TimeoutError):
                async with asyncio.timeout(_CHAIN_TIMEOUT):
                    await writer.wait_closed()

    async def async_read_presented_chain(self) -> list[bytes]:
        """The chain the panel serves now, leaf first, as DER; empty if unread.

        A handshake on the legacy context and nothing sent: for reading the
        expiry and the names of the certificate the panel presents.
        """
        return await self._async_read_chain()

    async def async_install_certificate(
        self, chain_pem: str, key_pem: str, *, ignore_hostname: bool = False
    ) -> InstalledCertificate:
        """Install a certificate pair on the panel with tuxweb's InstallCertificate.

        Over HTTPS on the verifying context only, with the bearer token as
        every tuxweb call carries it; with `ignore_hostname`, on the context
        that verifies the chain against the same authorities without the name
        check (_install_ssl_context), for a panel whose address moved. One
        POST: no login, no capability re-check, never retried. A status other
        than 200 is TuxedoTouchInstallRefused with tuxweb's reason; a 200 is
        checked to say installed. A failure below HTTP raises as any request
        does, as a certificate error on the verifying context.
        """
        anchors = self._anchors
        if self._scheme != "https" or anchors is None or self._tuxweb is False:
            raise TuxedoTouchError(
                "A certificate is installed only over a verified HTTPS connection "
                "to a tuxweb panel"
            )
        headers = {**self._tuxweb_headers(), "Content-Type": "application/json"}
        body = json.dumps({"chain": chain_pem, "key": key_pem}).encode("utf-8")
        if len(body) > INSTALL_BODY_LIMIT:
            raise TuxedoTouchInstallRefused("too_large")
        ssl_arg = (
            _install_ssl_context(anchors)
            if ignore_hostname
            else _verified_ssl_context(anchors)
        )
        url = f"{self.base_url}{API_BASE_PATH}{INSTALL_CERTIFICATE_PATH}"
        try:
            async with self._session.post(
                url,
                data=body,
                headers=headers,
                ssl=ssl_arg,
                timeout=_TIMEOUT,
                allow_redirects=False,
            ) as resp:
                status = resp.status
                try:
                    payload = await resp.json(content_type=None)
                except ValueError:
                    payload = None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise _transport_error(err, ssl_arg) from err
        if status != 200:
            raise TuxedoTouchInstallRefused(_install_reason(status, payload), status)
        if (
            not isinstance(payload, dict)
            or payload.get("installed") is not True
            or not isinstance(payload.get("not_after"), str)
            or not isinstance(payload.get("sha256"), str)
        ):
            raise TuxedoTouchInstallRefused("unexpected_answer", status)
        return InstalledCertificate(
            not_after=payload["not_after"], sha256=payload["sha256"].lower()
        )

    async def async_look_for_tuxweb(self) -> PanelCertificate | None:
        """Ask a stock client's panel again whether it now runs tuxweb.

        For a panel updated while Home Assistant runs: the coordinator calls
        this once after it has lost the panel and got it back. The same
        capability GET as the probe - no login, no session, the client's own
        context and so its own pool key - and, if a list comes back over
        HTTPS, the same chain read and proof as first use. The authority that
        proves itself is returned and kept as trustable_certificate; nothing
        is pinned, and the client stays stock until the entry is reloaded.
        None for a panel still stock, a client that is not stock, plain HTTP,
        or a pinned client. A connection failure is raised.
        """
        if (
            self._tuxweb is not False
            or self._scheme != "https"
            or self._panel_ca is not None
        ):
            return None
        _, capabilities = await self._async_ask_capabilities()
        if capabilities is None:
            return None
        _LOGGER.debug(
            "The panel at %s was stock and now declares %s",
            self._host,
            sorted(capabilities),
        )
        self._trustable = await self._async_prove_anchor()
        return self._trustable

    async def _async_trust_on_first_use(self) -> None:
        """Pin the authority a tuxweb panel presented, once it has proved itself.

        The proof is _async_prove_anchor. A client built with
        trust_on_first_use off keeps the proven authority as
        trustable_certificate instead and goes on with the legacy context:
        an existing entry pins only once the user has confirmed it.
        """
        anchor = await self._async_prove_anchor()
        if anchor is None:
            return
        if not self._trust_on_first_use:
            self._trustable = anchor
            _LOGGER.info(
                "The panel at %s presents the certificate authority %s "
                "(SHA-256 %s), which verifies its certificate; it is trusted "
                "once accepted from the repair notification",
                self._host,
                anchor.subject,
                anchor.fingerprint,
            )
            return
        self._panel_ca = anchor.anchor_pem
        _LOGGER.info(
            "The panel at %s is trusted through the certificate authority %s "
            "(SHA-256 %s), and every connection to it is verified from here on",
            self._host,
            anchor.subject,
            anchor.fingerprint,
        )

    async def _async_prove_anchor(self) -> PanelCertificate | None:
        """The authority a tuxweb panel presents, if it verifies the panel.

        The anchor is the last certificate of the chain the panel sends
        (_async_read_chain). It is returned only if the capability GET,
        repeated once with a context that verifies against it - hostname
        included - still answers as tuxweb: that is the anchor verifying the
        certificate of the panel answering HTTP, by effect. If it does not,
        None, with one warning. A proof that could not connect at all is
        None with nothing more than debug; the next setup asks again.
        """
        self._presented = presented_certificate(await self._async_read_chain())
        if self._presented is None:
            _LOGGER.warning(
                "The panel at %s runs tuxweb over HTTPS but its certificate chain "
                "could not be read, so its certificate is not verified",
                self._host,
            )
            return None
        anchor = self._presented
        try:
            context = _verified_ssl_context(anchor.anchor_pem)
            _, capabilities = await self._async_ask_capabilities(ssl_ctx=context)
        except (ssl.SSLError, TuxedoTouchCertificateError) as err:
            _LOGGER.warning(
                "The panel at %s runs tuxweb over HTTPS, but the certificate "
                "authority it presents (%s, SHA-256 %s) does not verify its own "
                "certificate (%s), so its certificate is not verified and the "
                "connection is made as before",
                self._host,
                anchor.subject,
                anchor.fingerprint,
                err,
            )
            return None
        except TuxedoTouchError as err:
            _LOGGER.debug(
                "Could not repeat the capability probe at %s to prove its "
                "certificate (%s); nothing is pinned this time",
                self._host,
                err,
            )
            return None
        if capabilities is None:
            _LOGGER.warning(
                "The panel at %s verified against the certificate authority it "
                "presents (%s) but then declared no capabilities, so nothing is "
                "pinned",
                self._host,
                anchor.subject,
            )
            return None
        return anchor

    def _tuxweb_headers(self) -> dict[str, str]:
        """The whole of tuxweb's auth: the token, as a bearer header.

        tuxweb takes the same token as a `tuxweb_token` cookie too, but the
        header is the form it documents first and one form is enough. Raised
        rather than sent empty when there is no token: a request without one
        is a 401 the caller would then have to read, and on this class a
        refusal must never look like a session that could be renewed.
        """
        if self._tuxweb_token is None:
            raise TuxedoTouchTokenRejected(
                "This panel runs tuxweb, which needs a bearer token, and none "
                "is configured for this entry - issue one on the panel with "
                "'tuxweb --issue-token' and enter it on the re-authentication card"
            )
        return {"Authorization": f"Bearer {self._tuxweb_token}"}

    def stream_headers(self) -> dict[str, str]:
        """What the push stream sends instead of a session cookie on tuxweb."""
        if not self.tuxweb:
            raise TuxedoTouchError("stream_headers is for tuxweb mode only")
        return self._tuxweb_headers()

    async def async_session_cookie(self) -> str:
        """Log in if needed and hand back the cookie a stream opens with.

        The push endpoint authenticates on the session cookie alone - no
        authtoken, no identity header, no encrypted body - so this is the
        whole of what it needs from the client.
        """
        if self.tuxweb:
            # There is no session on tuxweb to hand out, and reaching for one
            # would be a login: see stream_headers.
            raise TuxedoTouchError("tuxweb has no session cookie; use stream_headers")
        await self._ensure_authenticated()
        if self._session_cookie is None:
            raise TuxedoTouchError("no session cookie after authenticating")
        return self._session_cookie

    def invalidate_session(self) -> None:
        """Drop the session so the next caller logs in again.

        The stream calls this when the panel refuses the cookie it opened
        with; _call() does the same thing itself on a 401.
        """
        self._invalidate_session()

    # ------------------------------------------------------------------
    # Login
    #
    # 1. GET the login page (no auth). It returns a "Random" header (the
    #    HMAC challenge, also embedded in the page as JS var `login`) and a
    #    "RandomID" header, plus a short-lived _zFL correlation cookie.
    # 2. Compute:
    #      log  = HMAC-SHA512(key=challenge, msg=username.lower())
    #      log1 = HMAC-SHA512(key=challenge, msg=username.lower()+password)
    #    The challenge is used as the literal UTF-8 text of the hex string,
    #    NOT hex-decoded bytes - this quirk shows up again in the API
    #    signing below.
    # 3. POST log/log1/identity=RandomID with the _zFL cookie attached. The
    #    real login page's j_username/j_password fields live outside the
    #    <form> that actually gets submitted - only these HMAC digests are
    #    ever sent, never the raw password.
    # 4. The response sets a real session cookie (random name per login) -
    #    store it and attach it to every request from here on.
    # ------------------------------------------------------------------
    async def login(self) -> None:
        if self.tuxweb:
            # The one chokepoint every stock request passes through, so this
            # is what makes "tuxweb mode never logs in" a property of the
            # client rather than a habit of its callers. tuxweb serves no
            # login page - the GET below would answer 404 - and a 401 from it
            # is a refused token, which _tuxweb_call raises as such before
            # any retry could land here.
            raise TuxedoTouchError("tuxweb has no web login; the token is the auth")
        if self._failed_logins >= LOGIN_ATTEMPT_BUDGET:
            # Before the login-page GET, not merely before the POST: the unit
            # serves ONE connection at a time, so an attempt that is going to
            # be refused should not take the connection either.
            raise TuxedoTouchCredentialsRefused(
                "The panel refused these credentials and Home Assistant is "
                "not trying them again - repeated failed web logins can "
                "disable the panel's web accounts"
            )
        _LOGGER.debug("Logging in to Tuxedo Touch at %s", self._host)
        login_url = f"{self.base_url}{LOGIN_PATH}?url=tuxedoapi.html"

        try:
            async with self._session.get(
                login_url, ssl=self.ssl_arg, timeout=_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    raise TuxedoTouchConnectionError(
                        f"Login page returned HTTP {resp.status}"
                    )
                challenge = resp.headers.get("Random")
                random_id = resp.headers.get("RandomID")
                zfl_cookie = resp.cookies.get("_zFL")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise _transport_error(err, self.ssl_arg) from err

        if not challenge or not random_id:
            raise TuxedoTouchError(
                "Login page did not return Random/RandomID headers - "
                "unexpected firmware/response"
            )

        username_lower = self._username.lower()
        log_val = self._hmac_hex(challenge, username_lower, hashlib.sha512)
        log1_val = self._hmac_hex(
            challenge, username_lower + self._password, hashlib.sha512
        )
        body = {"log": log_val, "log1": log1_val, "identity": random_id}

        cookies = {"_zFL": zfl_cookie.value} if zfl_cookie else {}
        try:
            async with self._session.post(
                login_url,
                data=body,
                cookies=cookies,
                ssl=self.ssl_arg,
                timeout=_TIMEOUT,
                allow_redirects=False,
            ) as resp:
                if resp.status in CREDENTIAL_VERDICT_STATUSES:
                    # The panel has now seen a credential and rejected it.
                    # Counted here and at the missing-cookie raise below,
                    # which are the only two places that is true; a connection
                    # failure never reaches the panel and never counts, and
                    # nor does the branch below.
                    self._failed_logins += 1
                    raise TuxedoTouchAuthError(
                        f"Login POST returned HTTP {resp.status}"
                    )
                if resp.status not in (200, 302):
                    # A status the panel cannot have reached the credential
                    # comparison to produce: the embedded web server is busy,
                    # mid-firmware-write, or answering for something else
                    # entirely. The login-page GET twenty lines up already
                    # draws exactly this line; drawing it here too is what
                    # keeps a server fault from spending the one-login budget
                    # and latching the entry into "credentials rejected".
                    transient = (
                        500 <= resp.status <= 599
                        or resp.status in RETRYABLE_LOGIN_STATUSES
                    )
                    message = f"Login POST returned HTTP {resp.status}"
                    if transient:
                        raise TuxedoTouchConnectionError(message)
                    raise TuxedoTouchError(message)
                session_cookie = None
                for name, morsel in resp.cookies.items():
                    if name.startswith("_zFL"):
                        continue
                    session_cookie = f"{name}={morsel.value}"
                    break
        except (aiohttp.ClientError, TimeoutError) as err:
            raise _transport_error(err, self.ssl_arg) from err

        if not session_cookie:
            self._failed_logins += 1
            raise TuxedoTouchAuthError(
                "No session cookie returned - check username/password"
            )

        self._session_cookie = session_cookie
        _LOGGER.debug("Tuxedo Touch login succeeded")

        try:
            await self._fetch_keys()
        except TuxedoTouchError:
            # A cookie with no key material behind it is not a session, and
            # leaving one on the client makes every later call carry a
            # half-built one. Drop it so the next caller starts clean.
            self._invalidate_session()
            raise
        # Only a login that ran the whole way through - cookie AND key
        # material - proves the credentials, so only that gives the budget
        # back. A cookie with no usable keys behind it is not a working login.
        self._failed_logins = 0

    # ------------------------------------------------------------------
    # Key retrieval - GET /tuxedoapi.html WITH the session cookie, then
    # pull the hex key/IV blob out of the id="readit" element. Observed
    # blob is exactly 96 hex chars: 64 (32-byte AES-256 key) + 32 (16-byte
    # CBC IV).
    # ------------------------------------------------------------------
    async def _fetch_keys(self) -> None:
        if not self._session_cookie:
            raise TuxedoTouchError("_fetch_keys called before login")

        url = f"{self.base_url}{KEYS_PATH}"
        headers = {"Cookie": self._session_cookie}
        try:
            async with self._session.get(
                url,
                headers=headers,
                ssl=self.ssl_arg,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    raise TuxedoTouchSessionError(
                        f"Key fetch returned HTTP {resp.status} - "
                        "session may be invalid"
                    )
                body = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise _transport_error(err, self.ssl_arg) from err

        match = READIT_RE.search(body)
        if not match:
            raise TuxedoTouchSessionError(
                "Could not find key material (#readit) in tuxedoapi.html - "
                "session may not actually be authenticated"
            )
        blob = match.group(1)
        if len(blob) < 96:
            raise TuxedoTouchError(
                f"Key blob shorter than expected ({len(blob)} chars)"
            )

        self._key_hex = blob[0:64]
        self._iv_hex = blob[64:96]
        self._key = bytes.fromhex(self._key_hex)
        self._iv = bytes.fromhex(self._iv_hex)
        self._authtokens.clear()
        _LOGGER.debug("Tuxedo Touch API keys retrieved")

    def _invalidate_session(self) -> None:
        self._session_cookie = None
        self._key_hex = None
        self._iv_hex = None
        self._key = None
        self._iv = None
        self._authtokens.clear()

    async def _ensure_authenticated(self) -> None:
        """Log in (once) if there is no usable session.

        Serialized with a lock so a status poll and a user command that both
        notice a missing/expired session don't race two logins: the session
        cookie and the AES key/IV are per-session values set in two steps, and
        interleaved logins can pair the cookie from one login with the keys
        from the other, silently invalidating every subsequent authtoken.
        Whoever loses the lock race finds the state already restored and
        piggybacks on the winner's login.
        """
        async with self._login_lock:
            if self._session_cookie and self._key:
                return
            await self.login()

    # ------------------------------------------------------------------
    # Crypto helpers
    #
    # IMPORTANT: the HMAC signing key is the hex string ITSELF, treated as
    # UTF-8 text bytes - not the bytes you get from hex-decoding it. That's
    # what the firmware actually expects for both the login challenge and
    # the API authtoken. The AES key/IV, by contrast, ARE the hex-decoded
    # raw bytes.
    # ------------------------------------------------------------------
    @staticmethod
    def _hmac_hex(key_hex_text: str, message: str, digestmod: Callable[[], Any]) -> str:
        return hmac.new(
            key_hex_text.encode("utf-8"), message.encode("utf-8"), digestmod
        ).hexdigest()

    def _authtoken(self, endpoint_path: str) -> str:
        # The token depends only on the key and endpoint path, so it is
        # stable between logins - cache per endpoint, cleared on re-login.
        token = self._authtokens.get(endpoint_path)
        if token is None:
            if self._key_hex is None:
                raise TuxedoTouchError("no session key after authenticating")
            header = f"MACID:Browser,Path:API_REV01{endpoint_path}"
            token = self._hmac_hex(self._key_hex, header, hashlib.sha1)
            self._authtokens[endpoint_path] = token
        return token

    # Key/IV are passed explicitly rather than read from self so that _call
    # can snapshot them at request-build time: a concurrent caller may
    # invalidate and re-login mid-flight, and a response must be decrypted
    # with the key of the session it was actually sent under.
    @staticmethod
    def _aes_encrypt(plaintext: str, key: bytes, iv: bytes) -> str:
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        ct = encryptor.update(padded) + encryptor.finalize()
        return base64.b64encode(ct).decode("ascii")

    @staticmethod
    def _aes_decrypt(ciphertext_b64: str, key: bytes, iv: bytes) -> str:
        ct = base64.b64decode(ciphertext_b64)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ct) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")

    # ------------------------------------------------------------------
    # API calls
    #
    # POST, application/x-www-form-urlencoded body of
    # "param=<url-encoded base64 ciphertext>&len=<ciphertext length>&tstamp=<ms>"
    # plus the session cookie. Nothing is appended to the URL/query string.
    #
    # authtoken header = HMAC-SHA1("MACID:Browser,Path:API_REV01<endpoint>", keyHex)
    # Note the signed path omits the "/system_http_api" prefix even though
    # the request URL includes it, and uses the literal device id "Browser".
    # ------------------------------------------------------------------
    async def _call(
        self,
        endpoint_path: str,
        plain_params: str,
        retry: bool = True,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        await self._ensure_authenticated()
        # Snapshot the whole session state this request runs under: a
        # concurrent caller hitting a 401 can invalidate and re-login while
        # our request is in flight, and decrypting the response with the NEW
        # key would garble it (the key/IV are per-session values).
        cookie_used = self._session_cookie
        key_used = self._key
        iv_used = self._iv
        iv_hex_used = self._iv_hex
        if key_used is None or iv_used is None or iv_hex_used is None:
            raise TuxedoTouchError("no session key after authenticating")

        enc_data = self._aes_encrypt(plain_params, key_used, iv_used)
        # len is the RAW ciphertext length, measured before any url-encoding.
        body = {
            "param": enc_data,
            "len": str(len(enc_data)),
            "tstamp": str(int(time.time() * 1000)),
        }

        url = f"{self.base_url}{API_BASE_PATH}{endpoint_path}"
        headers = {
            "authtoken": self._authtoken(endpoint_path),
            "identity": iv_hex_used,
        }
        if cookie_used is not None:
            headers["Cookie"] = cookie_used

        try:
            async with self._session.post(
                url,
                data=body,
                headers=headers,
                ssl=self.ssl_arg,
                timeout=_TIMEOUT,
                allow_redirects=False,
            ) as resp:
                if resp.status == 302:
                    location = resp.headers.get("Location", "")
                    if self._scheme == "http" and location.startswith("https:"):
                        # Persistent condition, not an expired session: with
                        # "Secured Web Server Access" enabled the API endpoints
                        # redirect every plain-HTTP request to HTTPS (see
                        # docs/tuxedo_touch_api_notes.md). Re-logging-in can
                        # never fix this, so fail with something actionable
                        # instead of burning a full login per attempt.
                        raise TuxedoTouchHttpsRequiredError(
                            "Panel redirected the API call to HTTPS - "
                            "reconfigure the integration with 'Use HTTPS' enabled"
                        )
                if resp.status in (401, 302) and retry:
                    _LOGGER.debug("Session expired, re-authenticating")
                    # Only wipe state that is still the state this request was
                    # built from - a concurrent caller may have re-logged-in
                    # since, and clearing its fresh session would force yet
                    # another login.
                    if self._session_cookie == cookie_used:
                        self._invalidate_session()
                    return await self._call(
                        endpoint_path,
                        plain_params,
                        retry=False,
                        allow_empty=allow_empty,
                    )
                if resp.status != 200:
                    raise TuxedoTouchError(f"API call returned HTTP {resp.status}")
                try:
                    payload = await resp.json(content_type=None)
                except ValueError as err:
                    # An embedded server answering 200 with an HTML error page
                    # must not escape as a raw JSONDecodeError.
                    raise TuxedoTouchError(
                        f"API answered 200 with a non-JSON body: {err}"
                    ) from err
                if payload is None:
                    # aiohttp answers None, not a decode error, for a body
                    # that is empty or all whitespace - which is exactly what
                    # the command endpoints send. A command answering 200
                    # with nothing in it has done the thing and said so on
                    # the push stream instead: the panel's own client reads
                    # command results there, not in the reply. Only the
                    # callers that expect that accept it; an empty answer to
                    # a read is still a failed read.
                    if not allow_empty:
                        raise TuxedoTouchError("API answered 200 with an empty body")
                    _LOGGER.debug(
                        "%s answered 200 with an empty body; the result "
                        "comes on the push stream",
                        endpoint_path,
                    )
                    return {}
        except (aiohttp.ClientError, TimeoutError) as err:
            raise _transport_error(err, self.ssl_arg) from err

        if not isinstance(payload, dict):
            raise TuxedoTouchError(f"Unexpected API response shape: {payload!r}")
        result_b64 = payload.get("Result")
        if result_b64 is None:
            raise TuxedoTouchError(f"Unexpected API response shape: {payload}")

        try:
            decrypted = self._aes_decrypt(result_b64, key_used, iv_used)
            parsed: dict[str, Any] = json.loads(decrypted)
            return parsed
        except (ValueError, TypeError) as err:
            # Bad padding / undecodable bytes / non-JSON plaintext - keep it
            # inside the client's error hierarchy so callers see one cleanly
            # failed call instead of a raw traceback.
            raise TuxedoTouchError(f"Could not decrypt API response: {err}") from err

    # ------------------------------------------------------------------
    # The same calls on tuxweb
    #
    # Same paths, same parameter names, and the same reply shapes as stock -
    # `Sucess`, and arm answering under "Response" while disarm answers under
    # "Result" - because tuxweb reproduces the vendor's, misspellings and all,
    # so that one integration serves both. What differs is everything around
    # the call: the parameters go as a plain form body rather than inside
    # AES, the token goes as a bearer header rather than a cookie plus
    # authtoken plus identity, and the status codes mean something. 200 on a
    # command is the panel having ACTED; 504 is sent-but-not-confirmed; 400
    # is a missing code; 401 is the token.
    # ------------------------------------------------------------------
    async def _tuxweb_call(
        self, endpoint_path: str, params: dict[str, str]
    ) -> dict[str, Any]:
        headers = self._tuxweb_headers()
        url = f"{self.base_url}{API_BASE_PATH}{endpoint_path}"
        try:
            async with self._session.post(
                url,
                # A dict is sent application/x-www-form-urlencoded, in this
                # order, which is the order the stock path spells the same
                # parameters in.
                data=params,
                headers=headers,
                ssl=self.ssl_arg,
                timeout=_TIMEOUT,
                allow_redirects=False,
            ) as resp:
                status = resp.status
                try:
                    payload = await resp.json(content_type=None)
                except ValueError:
                    # The failure bodies are JSON too, but a reason is worth
                    # less than the status it came with; nothing below needs
                    # the body to be readable.
                    payload = None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise _transport_error(err, self.ssl_arg) from err

        if status == 401:
            # Not a session that expired, and never retried: there is no
            # login to fall back on, and the stock path's re-login on 401 is
            # the reflex this branch exists to keep off the tuxweb path.
            #
            # But asked about once first. Stock firmware answers a request
            # carrying no session with exactly this status, so a panel rolled
            # back to stock looks, from here, like a token it refuses - and
            # until the re-check existed that was surfaced as the token, for
            # ever. If the panel still declares tuxweb's list the refusal is
            # real and raised below; if it does not, the re-check raises
            # TuxedoTouchFirmwareChanged with the client already in stock
            # mode, and this request is not retried on that path.
            await self.async_recheck_capabilities(status)
            raise TuxedoTouchTokenRejected(
                "tuxweb refused the bearer token (HTTP 401) - it has been "
                "revoked or reissued on the panel, so enter the current one"
            )
        if status == 404:
            # The other answer consistent with tuxweb no longer being there:
            # its paths are the vendor's, but a panel answering 404 on one it
            # declared a moment ago is either not that panel any more or a
            # server fault. Same one question; a tuxweb that still declares
            # the list gets the ordinary failure below.
            await self.async_recheck_capabilities(status)
        reason = _tuxweb_reason(payload)
        if status == 504:
            raise TuxedoTouchCommandNotConfirmed(
                f"The panel did not confirm the command: tuxweb sent it and saw "
                f"no change of state within its 8 s ceiling (HTTP 504{reason})"
            )
        if status != 200:
            raise TuxedoTouchError(
                f"tuxweb answered HTTP {status} to {endpoint_path}{reason}"
            )
        if not isinstance(payload, dict):
            raise TuxedoTouchError(f"Unexpected tuxweb response shape: {payload!r}")
        return payload

    async def async_check_credentials(self) -> None:
        """Prove the stored credentials against the panel, once.

        The config flow's probe. On stock that is a full login - cookie and
        key material, the same thing a poll would need - and it spends one
        credential POST. On tuxweb it is one status read on the token, which
        is the cheapest request the token gates; a refused token raises
        TuxedoTouchTokenRejected, and a missing one raises it before anything
        is sent. Which of the two it is comes from the probe, so a token
        entered for a stock panel is simply not used and a tuxweb panel is
        never asked for a login page it does not serve.
        """
        if await self.async_probe_capabilities():
            await self.get_status()
            return
        await self.login()

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------
    async def get_status(self) -> TuxedoStatus:
        """Fetch the panel's cached status. The fallback under the stream.

        GetSecurityStatus takes no partition parameter on this firmware - the
        plaintext body is just "operation=get", unlike arm/disarm which do
        send a pID. The panel's own client sends an empty body here and the
        endpoint accepts both, so the parameter is ignored (see
        docs/tuxedo_touch_api_notes.md).

        What it reads is a cache the firmware fills from ECP messages, which
        is why it can answer "Not available" on a panel that is working
        perfectly. The push stream does not read it.

        On tuxweb the same call reads the live state model instead - the one
        the stream is fed from - so it carries the armed flag and cannot
        answer the placeholder; see _tuxweb_status for the one case that is
        mapped onto it anyway.
        """
        if self.tuxweb:
            return _tuxweb_status(
                await self._tuxweb_call("/GetSecurityStatus", {"operation": "get"})
            )
        result = await self._call("/GetSecurityStatus", "operation=get")
        colour = result.get("Color")
        return TuxedoStatus(
            status=result.get("Status", "Unknown"),
            # Normalised here so one vocabulary leaves the client. The panel
            # capitalises its own word ("Green"); the stream sends a digit
            # that push.py names in lower case. Left raw, the attribute would
            # change case with the source and a template comparing it to
            # "green" would quietly stop matching every time the poll spoke.
            color=colour.lower() if isinstance(colour, str) else None,
        )

    async def arm(self, mode: str, code: str, partition: int = 1) -> dict[str, Any]:
        """mode is one of STAY, AWAY, NIGHT.

        The return value carries nothing worth acting on, and it is worth being
        precise about why, because the obvious reading of it is wrong.

        This endpoint DOES answer with a body - measured 2026-09-06 against
        TUXW_V5.3.21.0_VA:

            arm     {"Status":"Sucess","Result":{"Response":"Command sent sucessfully"}}
            disarm  {"Status":"Sucess","Result":{"Result":"Command sent sucessfully"}}

        Vendor's spelling, and note the inner key is "Response" for arm but
        "Result" for disarm, so there is not even one shape to key on. (The
        zero-byte body belongs to /handlerequest.html, a different surface
        entirely, and does not apply here.)

        But read what it claims: command SENT, not code accepted. A wrong user
        code would presumably still dispatch successfully, so this says nothing
        about whether the panel acted on it. Keying control flow on a
        misspelled vendor string, observed once, whose shape already differs
        between two sibling calls, would be a dependency on far less than it
        appears to offer.

        What the panel actually DID shows up on the push stream as a change in
        the state flag - fe ready/disarmed, ff arming/armed - and that is what
        async_send_command waits for. Measured request-to-frame latency:
        1.5 s arming, 1.8 s disarming, against COMMAND_CONFIRM_TIMEOUT of 8 s.

        The panel's own accept/decline frames are not used: a full arm/disarm
        cycle driven through this endpoint emitted none
        (docs/push-stream-evidence.md, "User code accept and decline").

        All of the above is stock. On tuxweb the same reply IS worth acting
        on: it is sent only once the state byte has flipped, and a command
        the panel did not act on comes back as TuxedoTouchCommandNotConfirmed
        instead. That is what `confirms_commands` reports to the coordinator.
        """
        _require_alarm_code(code)
        if self.tuxweb:
            return await self._tuxweb_call(
                "/AdvancedSecurity/ArmWithCode",
                {
                    "arming": mode,
                    "pID": str(partition),
                    "ucode": code,
                    "operation": "set",
                },
            )
        params = f"arming={mode}&pID={partition}&ucode={code}&operation=set"
        return await self._call(
            "/AdvancedSecurity/ArmWithCode", params, allow_empty=True
        )

    async def disarm(self, code: str, partition: int = 1) -> dict[str, Any]:
        _require_alarm_code(code)
        if self.tuxweb:
            return await self._tuxweb_call(
                "/AdvancedSecurity/DisarmWithCode",
                {"pID": str(partition), "ucode": code, "operation": "set"},
            )
        params = f"pID={partition}&ucode={code}&operation=set"
        return await self._call(
            "/AdvancedSecurity/DisarmWithCode", params, allow_empty=True
        )


def _require_alarm_code(code: str) -> None:
    if not ALARM_CODE_RE.fullmatch(code):
        raise TuxedoTouchInvalidCode("The alarm code must be digits only")


def _capabilities_of(payload: Any) -> frozenset[str] | None:
    """The capability strings in a probe answer, or None if it declared none.

    None rather than an empty set is the point: a 200 whose body carries no
    `capabilities` list - an HTML page, a JSON document about something else
    - is not tuxweb, whereas tuxweb declaring an empty list would still be
    tuxweb with nothing to offer. Only strings count; anything else in the
    list is dropped rather than compared.
    """
    if not isinstance(payload, dict):
        return None
    declared = payload.get("capabilities")
    if not isinstance(declared, list):
        return None
    return frozenset(item for item in declared if isinstance(item, str))


# What InstallCertificate's statuses mean when the body names no reason.
_INSTALL_STATUS_REASONS = {
    401: "token_rejected",
    403: "tls_required",
    404: "not_supported",
    405: "not_supported",
    409: "tls_not_configured",
    413: "too_large",
    500: "write_failed",
}


def _install_reason(status: int, payload: Any) -> str:
    """tuxweb's `error` word for a refused install, or one for its status."""
    if isinstance(payload, dict):
        reason = payload.get("error")
        if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,40}", reason):
            return reason
    return _INSTALL_STATUS_REASONS.get(status, f"http_{status}")


def _tuxweb_reason(payload: Any) -> str:
    """The `Result.Response` text of a tuxweb failure body, for an error message."""
    if isinstance(payload, dict) and isinstance(payload.get("Result"), dict):
        response = payload["Result"].get("Response")
        if isinstance(response, str) and response:
            return f": {response}"
    return ""


def _tuxweb_status(result: dict[str, Any]) -> TuxedoStatus:
    """tuxweb's status answer as a TuxedoStatus.

        {"partition": 1, "armed": true, "state": "2Armed Away"}

    `state` is the stream's display field exactly as it follows the raw flag
    byte: the colour digit first, then the text - so it is read the way
    push.decode_status_frame reads that field, and both sources leave the
    client in one vocabulary. `armed` is the flag itself, which a stock poll
    never carries.

    An empty `state` is tuxweb having seen no partition status yet, which is
    what it answers between its own start and the panel's first report. That
    is the same condition the stock cache reports as "Not available" - a
    failed read rather than a state - and it is mapped onto that placeholder
    so the coordinator handles both the one way it already does: the poll
    fails, nothing is stored, and the stream's first frame ends it.
    """
    state = result.get("state")
    armed = result.get("armed")
    if not isinstance(state, str) or not isinstance(armed, bool):
        raise TuxedoTouchError(f"Unexpected tuxweb status shape: {result}")
    colour = None
    if state[:1] in COLOURS:
        colour = COLOURS[state[0]]
        state = state[1:]
    text = state.strip()
    if not text:
        return TuxedoStatus(status=STATUS_NOT_AVAILABLE)
    countdown = COUNTDOWN_RE.match(text)
    return TuxedoStatus(
        status=text,
        color=colour,
        armed=armed,
        seconds_remaining=int(countdown.group(1)) if countdown else None,
    )
