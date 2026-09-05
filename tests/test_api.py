"""Unit tests for the Tuxedo Touch API client's pure logic. No panel, no network, no HA.

    python tests/test_api.py

WHY THESE TESTS AND NOT OTHERS. Everything here is a contract the panel's firmware imposes
on us -- get any of them subtly wrong and every request fails authentication with an error
that looks like "wrong password" or "unreachable", which is exactly how this class of bug
gets misdiagnosed. They are also the parts that can be tested honestly without hardware:
no mock of the panel would prove anything the real device does.

The client is loaded WITHOUT executing the package __init__.py, which imports Home
Assistant. That keeps this suite runnable on a bare Python with only aiohttp and
cryptography installed -- the same reason it can run in CI in seconds.
"""

import asyncio
import base64
import hashlib
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMP = os.path.join(ROOT, "custom_components", "tuxedo_touch")

# Register a stand-in package whose __path__ points at the component directory, so api.py's
# `from .const import ...` resolves normally while __init__.py (which needs Home Assistant)
# is never executed.
_pkg = types.ModuleType("tuxedo_touch")
_pkg.__path__ = [COMP]
sys.modules["tuxedo_touch"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"tuxedo_touch.{name}", os.path.join(COMP, f"{name}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"tuxedo_touch.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


api = _load("api")

FAILURES = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(name)


# 32-byte AES key and 16-byte IV as the panel hands them over: hex TEXT.
KEY_HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
IV_HEX = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
KEY = bytes.fromhex(KEY_HEX)
IV = bytes.fromhex(IV_HEX)


def _client(use_https=True, host="10.0.0.5", port=443):
    """A client with no session -- nothing here performs I/O."""
    c = api.TuxedoTouchClient(None, host, port, use_https, "Admin", "hunter2")
    c._key_hex = KEY_HEX
    c._iv_hex = IV_HEX
    c._key = KEY
    c._iv = IV
    c._session_cookie = "SESSIONID=abc123"
    return c


print("=== HMAC signing key is the hex STRING, not the decoded bytes ===")
# THE load-bearing quirk of this firmware, documented in api.py and the API notes. If someone
# "fixes" this to bytes.fromhex(), every login and every authtoken silently becomes wrong.
# Frozen digest, so the test fails on ANY change of interpretation rather than agreeing with
# whatever the code happens to do.
check(
    "login digest for 'admin'",
    api.TuxedoTouchClient._hmac_hex(KEY_HEX, "admin", hashlib.sha512),
    "4e1bfd7ee252f65ff240adf3cc57379eba8ac21f943d92315875d0ddebce6b6c"
    "636b3f086425138e4db703198428f40e68afbb5ba500e244dfba3c9d04492deb",
)
# And prove the two interpretations genuinely differ, so the frozen value above is not
# accidentally satisfied by the wrong implementation.
import hmac as _hmac  # noqa: E402

check(
    "decoded-key form differs (so the test can actually catch the regression)",
    api.TuxedoTouchClient._hmac_hex(KEY_HEX, "admin", hashlib.sha512)
    != _hmac.new(KEY, b"admin", hashlib.sha512).hexdigest(),
    True,
)
check(
    "username is signed lowercased at the call site, so casing matters here",
    api.TuxedoTouchClient._hmac_hex(KEY_HEX, "admin", hashlib.sha512)
    == api.TuxedoTouchClient._hmac_hex(KEY_HEX, "Admin", hashlib.sha512),
    False,
)

print()
print("=== authtoken: HMAC-SHA1 over 'MACID:Browser,Path:API_REV01<endpoint>' ===")
# The signed path deliberately OMITS the /system_http_api prefix that the request URL carries.
c = _client()
check(
    "GetSecurityStatus token",
    c._authtoken("/GetSecurityStatus"),
    "828c0eeff8a91f3f7f12e73c6568c0bf5d7d89c0",
)
check(
    "ArmWithCode token",
    c._authtoken("/AdvancedSecurity/ArmWithCode"),
    "3e0b676e8e65e1a4ad3f3e702d305173cd457653",
)
check(
    "different endpoints do not share a token",
    c._authtoken("/GetSecurityStatus") == c._authtoken("/AdvancedSecurity/ArmWithCode"),
    False,
)
check(
    "token is cached per endpoint",
    sorted(c._authtokens),
    [
        "/AdvancedSecurity/ArmWithCode",
        "/GetSecurityStatus",
    ],
)

print()
print("=== AES-CBC round trip, and why _call snapshots the key ===")
plain = "operation=get&pID=1"
ct = api.TuxedoTouchClient._aes_encrypt(plain, KEY, IV)
check("round trips", api.TuxedoTouchClient._aes_decrypt(ct, KEY, IV), plain)
check("ciphertext is valid base64", base64.b64encode(base64.b64decode(ct)).decode(), ct)
check(
    "ciphertext is PKCS7-padded to the block size",
    len(base64.b64decode(ct)) % 16,
    0,
)
# A concurrent re-login swaps the key mid-flight; decrypting a response with the NEW key must
# not quietly yield something that looks like data. This is the failure _call's snapshot
# prevents, so assert it is a real hazard rather than a theoretical one.
other_key = bytes.fromhex("ff" * 32)
try:
    got = api.TuxedoTouchClient._aes_decrypt(ct, other_key, IV)
    rt = got == plain
except Exception:
    rt = False
check("decrypting with the wrong key does not round trip", rt, False)

print()
print("=== key blob extraction from tuxedoapi.html ===")
blob = KEY_HEX + IV_HEX
check(
    "double-quoted attribute",
    api.READIT_RE.search(f'<input id="readit" value="{blob}">').group(1),
    blob,
)
check(
    "single-quoted attribute",
    api.READIT_RE.search(f"<input id='readit' value='{blob}'>").group(1),
    blob,
)
check(
    "other attributes between id and value",
    api.READIT_RE.search(f'<input id="readit" type="hidden" value="{blob}">').group(1),
    blob,
)
check("no readit element -> no match", api.READIT_RE.search("<p>logged out</p>"), None)
check("a real blob is 96 hex chars (64 key + 32 iv)", len(blob), 96)

# KNOWN COVERAGE GAP, stated rather than faked: the 64/32 split of that blob, and the
# "shorter than 96 chars" rejection, live inside the async _fetch_keys() which does network
# I/O. Asserting the arithmetic here would test bytes.fromhex(), not this codebase -- a check
# that cannot fail is documentation, not a gate. Covering it honestly needs the parsing pulled
# out of the I/O, which is a source change, not a test change.

print()
print("=== session state ===")
check("https base_url", _client().base_url, "https://10.0.0.5:443")
check(
    "http base_url",
    _client(use_https=False, port=80).base_url,
    "http://10.0.0.5:80",
)
c2 = _client()
c2._authtoken("/GetSecurityStatus")
c2._invalidate_session()
check(
    "invalidate clears every per-session value",
    [c2._session_cookie, c2._key, c2._iv, c2._key_hex, c2._iv_hex, c2._authtokens],
    [None, None, None, None, None, {}],
)

print()
print("=== TLS context is deliberately permissive for 2009-era hardware ===")
# The unit ships an expired 1024-bit MD5 self-signed cert and needs legacy renegotiation.
# These assertions exist so the choice reads as deliberate and cannot be silently reverted.
ctx = api._legacy_ssl_context()
check("hostname checking off", ctx.check_hostname, False)
check("cert verification off", ctx.verify_mode, api.ssl.CERT_NONE)
check(
    "http mode builds no ssl context at all",
    _client(use_https=False, port=80)._ssl_ctx,
    None,
)
# One object for every client, because aiohttp keys its connection pool on the ssl
# argument: a context per client is a second socket to a panel that serves one at a
# time. tests/test_client_io.py checks the pool key that follows from this.
check("the context is cached, not rebuilt", api._legacy_ssl_context() is ctx, True)
check(
    "every client is handed that same context",
    _client()._ssl_ctx is _client()._ssl_ctx is ctx,
    True,
)

print()
print("=== the failed-login budget: one attempt per credential set, ever ===")
# The panel counts failed WEB logins and disables every web account at three:
# no timeout, no self-clear, recovery only by walking to the touchscreen.
# Patched firmware allows five and clears itself after 300 s, and the panel
# publishes no version anywhere, so the number has to be safe on the stricter
# one. Frozen here, so raising it is a deliberate act with a failing test
# attached rather than a quiet edit.
check("budget is one attempt", api.LOGIN_ATTEMPT_BUDGET, 1)
check("a fresh client has spent none of it", _client()._failed_logins, 0)
# Every existing `except TuxedoTouchAuthError` routes the refusal, so nothing
# that already handles a rejection has to learn that this class exists.
check(
    "a refusal is an auth error",
    issubclass(api.TuxedoTouchCredentialsRefused, api.TuxedoTouchAuthError),
    True,
)
# The refusal comes before the login-page GET, and this PROVES that rather
# than asserting it: the client below is built with session=None, so any
# request at all - the GET included - would come out as an AttributeError
# instead. Not taking the connection is part of the fix, because the panel
# serves one at a time and the poll needs it.
_spent = _client()
_spent._failed_logins = 1
try:
    asyncio.run(_spent.login())
    _refusal = "no exception at all"
except api.TuxedoTouchCredentialsRefused:
    _refusal = "refused"
except Exception as err:
    _refusal = f"{type(err).__name__}: {err}"
check("a spent budget refuses before any request is built", _refusal, "refused")
# How many attempts the PANEL sees, and that a login which succeeds hands the
# budget back, are in tests/test_login_budget.py: counting them needs a server
# to count against, which this file deliberately has not got.

print()
print("=== error hierarchy (callers catch the base class) ===")
check(
    "auth error is a TuxedoTouchError",
    issubclass(api.TuxedoTouchAuthError, api.TuxedoTouchError),
    True,
)
check(
    "connection error is a TuxedoTouchError",
    issubclass(api.TuxedoTouchConnectionError, api.TuxedoTouchError),
    True,
)
check(
    "status defaults colour to None",
    api.TuxedoStatus(status="Ready To Arm").color,
    None,
)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("all checks passed")
