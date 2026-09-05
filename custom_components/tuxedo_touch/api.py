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
from dataclasses import dataclass
from functools import cache
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import API_BASE_PATH, KEYS_PATH, LOGIN_PATH, SOURCE_POLL

_LOGGER = logging.getLogger(__name__)

READIT_RE = re.compile(r'id=["\']readit["\'][^>]*value=["\']([0-9a-fA-F]+)["\']')

# The panel is a slow embedded device, but anything beyond this budget
# indicates a hang, not a slow success. aiohttp raises TimeoutError (not a
# ClientError) when this expires - see the except clauses below.
_TIMEOUT = aiohttp.ClientTimeout(total=15)

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
    which are the two `_failed_logins += 1` sites in login(). Nothing else may
    raise it, and that is load-bearing rather than tidy: the coordinator turns
    this class into a permanent `credentials_rejected` flag on the config
    entry and the push stream stops for good on it, so a server or session
    fault wearing this class condemns the entry - and the reauthentication
    card then refuses the password that was right all along. Faults after an
    accepted login are TuxedoTouchSessionError; a login POST that answered
    without judging anything is a connection or panel error.
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


class TuxedoTouchHttpsRequiredError(TuxedoTouchError):
    """The panel redirected a plain-HTTP API call to HTTPS.

    Its own class because this is a persistent misconfiguration with one
    known fix, not a transient failure: the coordinator turns it into a
    repair issue rather than another retry.
    """


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
    # None from a poll, which reports display text and nothing else.
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
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._scheme = "https" if use_https else "http"
        self._username = username
        self._password = password

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

        self._ssl_ctx: ssl.SSLContext | None = (
            _legacy_ssl_context() if use_https else None
        )

    @property
    def base_url(self) -> str:
        return f"{self._scheme}://{self._host}:{self._port}"

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
        """What to pass as aiohttp's `ssl=`, shared for the pool key's sake."""
        return self._ssl_ctx or True

    async def async_session_cookie(self) -> str:
        """Log in if needed and hand back the cookie a stream opens with.

        The push endpoint authenticates on the session cookie alone - no
        authtoken, no identity header, no encrypted body - so this is the
        whole of what it needs from the client.
        """
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
                login_url, ssl=self._ssl_ctx or True, timeout=_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    raise TuxedoTouchConnectionError(
                        f"Login page returned HTTP {resp.status}"
                    )
                challenge = resp.headers.get("Random")
                random_id = resp.headers.get("RandomID")
                zfl_cookie = resp.cookies.get("_zFL")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise TuxedoTouchConnectionError(str(err)) from err

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
                ssl=self._ssl_ctx or True,
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
            raise TuxedoTouchConnectionError(str(err)) from err

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
                ssl=self._ssl_ctx or True,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    raise TuxedoTouchSessionError(
                        f"Key fetch returned HTTP {resp.status} - "
                        "session may be invalid"
                    )
                body = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise TuxedoTouchConnectionError(str(err)) from err

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
                ssl=self._ssl_ctx or True,
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
            raise TuxedoTouchConnectionError(str(err)) from err

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
        """
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

        The return value carries nothing worth acting on - it is the panel
        acknowledging the request, and on some firmware it is an empty body.
        What the panel then did shows up on the push stream.
        """
        params = f"arming={mode}&pID={partition}&ucode={code}&operation=set"
        return await self._call(
            "/AdvancedSecurity/ArmWithCode", params, allow_empty=True
        )

    async def disarm(self, code: str, partition: int = 1) -> dict[str, Any]:
        params = f"pID={partition}&ucode={code}&operation=set"
        return await self._call(
            "/AdvancedSecurity/DisarmWithCode", params, allow_empty=True
        )
