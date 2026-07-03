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

import base64
import hashlib
import hmac
import json
import logging
import re
import ssl
import time
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import API_BASE_PATH, KEYS_PATH, LOGIN_PATH

_LOGGER = logging.getLogger(__name__)

READIT_RE = re.compile(r'id=["\']readit["\'][^>]*value=["\']([0-9a-fA-F]+)["\']')


class TuxedoTouchError(Exception):
    """Base error talking to the panel."""


class TuxedoTouchAuthError(TuxedoTouchError):
    """Login failed - bad username/password."""


class TuxedoTouchConnectionError(TuxedoTouchError):
    """Could not reach the panel at all."""


@dataclass
class TuxedoStatus:
    status: str
    color: str | None = None


def _legacy_ssl_context() -> ssl.SSLContext:
    """Build an SSLContext that tolerates this device's ancient cert/handshake.

    The unit ships a self-signed ~2009 SharkSSL demo certificate (1024-bit
    RSA, MD5 signature, expired since 2019) and requires legacy/unsafe TLS
    renegotiation. Modern TLS stacks refuse this by default; both flags below
    are required to complete the handshake at all. Confirmed working against
    real hardware with CPython 3.13 / OpenSSL 3.x - no external OpenSSL config
    file needed, unlike getting the system `openssl` CLI to cooperate.
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

        self._ssl_ctx = _legacy_ssl_context() if use_https else None

    @property
    def base_url(self) -> str:
        return f"{self._scheme}://{self._host}:{self._port}"

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
        _LOGGER.debug("Logging in to Tuxedo Touch at %s", self._host)
        login_url = f"{self.base_url}{LOGIN_PATH}?url=tuxedoapi.html"

        try:
            async with self._session.get(
                login_url, ssl=self._ssl_ctx, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    raise TuxedoTouchConnectionError(
                        f"Login page returned HTTP {resp.status}"
                    )
                challenge = resp.headers.get("Random")
                random_id = resp.headers.get("RandomID")
                zfl_cookie = resp.cookies.get("_zFL")
        except aiohttp.ClientError as err:
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
        body = f"log={log_val}&log1={log1_val}&identity={random_id}"

        cookies = {"_zFL": zfl_cookie.value} if zfl_cookie else {}
        try:
            async with self._session.post(
                login_url,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                cookies=cookies,
                ssl=self._ssl_ctx,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=False,
            ) as resp:
                if resp.status not in (200, 302):
                    raise TuxedoTouchAuthError(
                        f"Login POST returned HTTP {resp.status}"
                    )
                session_cookie = None
                for name, morsel in resp.cookies.items():
                    if name.startswith("_zFL"):
                        continue
                    session_cookie = f"{name}={morsel.value}"
                    break
        except aiohttp.ClientError as err:
            raise TuxedoTouchConnectionError(str(err)) from err

        if not session_cookie:
            raise TuxedoTouchAuthError(
                "No session cookie returned - check username/password"
            )

        self._session_cookie = session_cookie
        _LOGGER.debug("Tuxedo Touch login succeeded")

        await self._fetch_keys()

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
                url, headers=headers, ssl=self._ssl_ctx,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    raise TuxedoTouchAuthError(
                        f"Key fetch returned HTTP {resp.status} - session may be invalid"
                    )
                body = await resp.text()
        except aiohttp.ClientError as err:
            raise TuxedoTouchConnectionError(str(err)) from err

        match = READIT_RE.search(body)
        if not match:
            raise TuxedoTouchAuthError(
                "Could not find key material (#readit) in tuxedoapi.html - "
                "session may not actually be authenticated"
            )
        blob = match.group(1)
        if len(blob) < 96:
            raise TuxedoTouchError(f"Key blob shorter than expected ({len(blob)} chars)")

        self._key_hex = blob[0:64]
        self._iv_hex = blob[64:96]
        _LOGGER.debug("Tuxedo Touch API keys retrieved")

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
    def _hmac_hex(key_hex_text: str, message: str, digestmod) -> str:
        return hmac.new(
            key_hex_text.encode("utf-8"), message.encode("utf-8"), digestmod
        ).hexdigest()

    def _aes_encrypt(self, plaintext: str) -> str:
        key = bytes.fromhex(self._key_hex)
        iv = bytes.fromhex(self._iv_hex)
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        ct = encryptor.update(padded) + encryptor.finalize()
        return base64.b64encode(ct).decode("ascii")

    def _aes_decrypt(self, ciphertext_b64: str) -> str:
        key = bytes.fromhex(self._key_hex)
        iv = bytes.fromhex(self._iv_hex)
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
    async def _call(self, endpoint_path: str, plain_params: str, retry: bool = True) -> dict:
        if not self._session_cookie or not self._key_hex:
            await self.login()

        header = f"MACID:Browser,Path:API_REV01{endpoint_path}"
        authtoken = self._hmac_hex(self._key_hex, header, hashlib.sha1)
        enc_data = self._aes_encrypt(plain_params)
        body = (
            f"param={quote(enc_data, safe='')}"
            f"&len={len(enc_data)}"
            f"&tstamp={int(time.time() * 1000)}"
        )

        url = f"{self.base_url}{API_BASE_PATH}{endpoint_path}"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "authtoken": authtoken,
            "identity": self._iv_hex,
            "Cookie": self._session_cookie,
        }

        try:
            async with self._session.post(
                url, data=body, headers=headers, ssl=self._ssl_ctx,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=False,
            ) as resp:
                if resp.status in (401, 302) and retry:
                    _LOGGER.debug("Session expired, re-authenticating")
                    self._session_cookie = None
                    self._key_hex = None
                    self._iv_hex = None
                    return await self._call(endpoint_path, plain_params, retry=False)
                if resp.status != 200:
                    raise TuxedoTouchError(f"API call returned HTTP {resp.status}")
                payload = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise TuxedoTouchConnectionError(str(err)) from err

        result_b64 = payload.get("Result")
        if result_b64 is None:
            raise TuxedoTouchError(f"Unexpected API response shape: {payload}")

        decrypted = self._aes_decrypt(result_b64)
        return json.loads(decrypted)

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------
    async def get_status(self, partition: int = 1) -> TuxedoStatus:
        result = await self._call("/GetSecurityStatus", "operation=get")
        return TuxedoStatus(status=result.get("Status", "Unknown"), color=result.get("Color"))

    async def arm(self, mode: str, code: str, partition: int = 1) -> dict:
        """mode is one of STAY, AWAY, NIGHT."""
        params = f"arming={mode}&pID={partition}&ucode={code}&operation=set"
        return await self._call("/AdvancedSecurity/ArmWithCode", params)

    async def disarm(self, code: str, partition: int = 1) -> dict:
        params = f"pID={partition}&ucode={code}&operation=set"
        return await self._call("/AdvancedSecurity/DisarmWithCode", params)
