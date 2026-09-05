"""The API client's request and response handling, without a panel.

Every branch here is a shape the firmware or the network can produce: a login
page missing its challenge headers, a session that expired mid-poll, the
plain-HTTP redirect, a 200 carrying an HTML error page. The panel is replaced
by a session stand-in rather than mocked at the client's own methods, so the
code under test is the real request building, cookie handling and decryption.

Loaded the same way as tests/test_api.py: by path, so the package __init__
(which imports Home Assistant) never runs and this file needs only aiohttp
and cryptography.
"""

import base64
import hashlib
import http.cookies
import importlib.util
import json
import os
import sys
import types

import aiohttp
import pytest
from aiohttp.client_reqrep import ConnectionKey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMP = os.path.join(ROOT, "custom_components", "tuxedo_touch")

_pkg = types.ModuleType("tuxedo_touch")
_pkg.__path__ = [COMP]
sys.modules.setdefault("tuxedo_touch", _pkg)


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"tuxedo_touch.{name}", os.path.join(COMP, f"{name}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"tuxedo_touch.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


api = _load("api")

KEY_HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
IV_HEX = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
KEY = bytes.fromhex(KEY_HEX)
IV = bytes.fromhex(IV_HEX)
BLOB = KEY_HEX + IV_HEX
CHALLENGE = "a1b2c3d4"
RANDOM_ID = "42"


class FakeResponse:
    """One canned answer, used as `async with session.get(...) as resp`."""

    def __init__(self, status=200, headers=None, cookies=None, text="", payload=None):
        self.status = status
        self.headers = headers or {}
        self.cookies = http.cookies.SimpleCookie()
        for name, value in (cookies or {}).items():
            self.cookies[name] = value
        self._text = text
        self._payload = payload

    async def text(self):
        return self._text

    async def json(self, content_type=None):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeRaise:
    """An answer that fails the way aiohttp fails, at request time."""

    def __init__(self, error):
        self._error = error

    async def __aenter__(self):
        raise self._error

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    """Hands out the queued answers in order and records what was asked."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def get(self, url, **kwargs):
        return self._answer("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._answer("POST", url, kwargs)

    def _answer(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        assert self.answers, f"no answer queued for {method} {url}"
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            return FakeRaise(answer)
        return answer


def login_answers(session_cookie="SESSIONID=abc123", blob=BLOB):
    """The three exchanges a successful login makes."""
    return [
        FakeResponse(
            headers={"Random": CHALLENGE, "RandomID": RANDOM_ID},
            cookies={"_zFL": "correlate"},
        ),
        FakeResponse(status=302, cookies=dict([session_cookie.split("=", 1)])),
        FakeResponse(text=f'<input id="readit" value="{blob}">'),
    ]


def encrypted(payload):
    """A Result field as the panel sends it."""
    return {"Result": api.TuxedoTouchClient._aes_encrypt(json.dumps(payload), KEY, IV)}


def client(answers, use_https=True, port=443):
    session = FakeSession(answers)
    made = api.TuxedoTouchClient(session, "10.0.0.5", port, use_https, "Admin", "pw")
    return made, session


# --------------------------------------------------------------- login


async def test_login_signs_the_challenge_and_keeps_the_session_cookie():
    """The digests are the whole login: the password itself is never sent."""
    made, session = client(login_answers())
    await made.login()

    _, url, kwargs = session.calls[1]
    body = kwargs["data"]
    assert url.endswith("/authenticated/index.html?url=tuxedoapi.html")
    assert body["identity"] == RANDOM_ID
    assert body["log"] == api.TuxedoTouchClient._hmac_hex(
        CHALLENGE, "admin", hashlib.sha512
    )
    assert "pw" not in json.dumps(body)
    # The short-lived correlation cookie from the GET must come back on the POST.
    assert kwargs["cookies"] == {"_zFL": "correlate"}
    assert made._session_cookie == "SESSIONID=abc123"
    assert made._key == KEY and made._iv == IV


async def test_a_login_page_that_is_not_200_reads_as_unreachable():
    made, _ = client([FakeResponse(status=503)])
    with pytest.raises(api.TuxedoTouchConnectionError):
        await made.login()


async def test_a_dropped_connection_on_the_login_page_is_a_connection_error():
    made, _ = client([aiohttp.ClientError("reset")])
    with pytest.raises(api.TuxedoTouchConnectionError):
        await made.login()


async def test_a_login_page_without_the_challenge_headers_is_unknown_firmware():
    """Neither a wrong password nor an unreachable panel: say so separately."""
    made, _ = client([FakeResponse(headers={"RandomID": RANDOM_ID})])
    with pytest.raises(api.TuxedoTouchError) as raised:
        await made.login()
    assert not isinstance(raised.value, api.TuxedoTouchAuthError)
    assert "Random" in str(raised.value)


async def test_a_login_page_without_the_correlation_cookie_still_posts():
    """The _zFL cookie is not always set; its absence must not stop the login."""
    answers = login_answers()
    answers[0] = FakeResponse(headers={"Random": CHALLENGE, "RandomID": RANDOM_ID})
    made, session = client(answers)
    await made.login()
    assert session.calls[1][2]["cookies"] == {}


async def test_a_rejected_login_post_is_an_auth_error():
    """401 is a status that can only be a verdict on the account."""
    answers = login_answers()
    answers[1] = FakeResponse(status=401)
    made, _ = client(answers)
    with pytest.raises(api.TuxedoTouchAuthError):
        await made.login()
    assert made._failed_logins == 1


async def test_a_server_error_on_the_login_post_is_not_a_verdict():
    """500 never reached the credential comparison, so it judged nothing.

    Counting it spends the one-login budget on a busy embedded web server,
    which the coordinator then writes onto the config entry as "the panel
    refused these credentials" - permanently, with no UI path back to the
    password that was right the whole time.
    """
    for status in (500, 503, 429, 408):
        answers = login_answers()
        answers[1] = FakeResponse(status=status)
        made, _ = client(answers)
        with pytest.raises(api.TuxedoTouchConnectionError):
            await made.login()
        assert made._failed_logins == 0


async def test_an_odd_login_post_status_is_a_panel_error_not_a_verdict():
    """A 404 says the path or the port is wrong, not that the account is."""
    answers = login_answers()
    answers[1] = FakeResponse(status=404)
    made, _ = client(answers)
    with pytest.raises(api.TuxedoTouchError) as raised:
        await made.login()
    assert not isinstance(raised.value, api.TuxedoTouchAuthError)
    assert made._failed_logins == 0


async def test_a_timeout_on_the_login_post_is_a_connection_error():
    answers = login_answers()
    answers[1] = TimeoutError()
    made, _ = client(answers)
    with pytest.raises(api.TuxedoTouchConnectionError):
        await made.login()


async def test_a_login_that_returns_no_session_cookie_is_an_auth_error():
    """Only the correlation cookie came back, which means it did not log in."""
    answers = login_answers()
    answers[1] = FakeResponse(status=200, cookies={"_zFL": "correlate"})
    made, _ = client(answers)
    with pytest.raises(api.TuxedoTouchAuthError):
        await made.login()


# ----------------------------------------------------------- key fetch


async def test_a_key_page_that_refuses_is_a_session_fault_not_a_refusal():
    """The credential POST already returned a session cookie.

    So the panel accepted the password and counted a SUCCESSFUL login; the
    page behind that session answering badly says nothing about the password.
    Raising the auth class here had the coordinator write a permanent
    "credentials rejected" flag onto the config entry, stop the push stream
    for good, and then refuse the correct stored password on the
    reauthentication card without asking the panel - for one HTTP hiccup.
    """
    answers = login_answers()
    answers[2] = FakeResponse(status=302)
    made, _ = client(answers)
    with pytest.raises(api.TuxedoTouchSessionError) as raised:
        await made.login()
    assert not isinstance(raised.value, api.TuxedoTouchAuthError)
    assert made._failed_logins == 0


async def test_a_key_page_without_the_readit_element_is_a_session_fault():
    answers = login_answers()
    answers[2] = FakeResponse(text="<p>please log in</p>")
    made, _ = client(answers)
    with pytest.raises(api.TuxedoTouchSessionError) as raised:
        await made.login()
    assert not isinstance(raised.value, api.TuxedoTouchAuthError)
    assert made._failed_logins == 0


async def test_a_failed_key_fetch_leaves_no_half_built_session_behind():
    """A cookie with no key material behind it is not a session.

    Left on the client it is carried into every later call, which then fails
    on the missing key instead of logging in again.
    """
    answers = login_answers()
    answers[2] = FakeResponse(status=500)
    made, _ = client(answers)
    with pytest.raises(api.TuxedoTouchSessionError):
        await made.login()
    assert made._session_cookie is None
    assert made._key is None


async def test_a_short_key_blob_is_rejected_rather_than_sliced():
    """A 96-character blob is 32 key bytes plus 16 IV bytes; less is not."""
    answers = login_answers(blob="aabbccdd")
    made, _ = client(answers)
    with pytest.raises(api.TuxedoTouchError) as raised:
        await made.login()
    assert "shorter than expected" in str(raised.value)


async def test_a_dropped_connection_on_the_key_page_is_a_connection_error():
    answers = login_answers()
    answers[2] = aiohttp.ClientError("reset")
    made, _ = client(answers)
    with pytest.raises(api.TuxedoTouchConnectionError):
        await made.login()


async def test_fetching_keys_before_logging_in_is_refused():
    made, _ = client([])
    with pytest.raises(api.TuxedoTouchError):
        await made._fetch_keys()


async def test_an_authtoken_without_a_key_is_refused():
    made, _ = client([])
    with pytest.raises(api.TuxedoTouchError):
        made._authtoken("/GetSecurityStatus")


# ------------------------------------------------------------ API calls


async def test_get_status_logs_in_signs_and_decrypts():
    answers = [
        *login_answers(),
        FakeResponse(payload=encrypted({"Status": "Armed Away", "Color": "Red"})),
    ]
    made, session = client(answers)

    status = await made.get_status()

    assert status.status == "Armed Away"
    assert status.color == "red"
    _, url, kwargs = session.calls[3]
    assert url == "https://10.0.0.5:443/system_http_api/API_REV01/GetSecurityStatus"
    assert kwargs["headers"]["Cookie"] == "SESSIONID=abc123"
    assert kwargs["headers"]["identity"] == IV_HEX
    assert kwargs["allow_redirects"] is False
    body = kwargs["data"]
    assert body["len"] == str(len(body["param"]))
    assert api.TuxedoTouchClient._aes_decrypt(body["param"], KEY, IV) == "operation=get"


@pytest.mark.parametrize(
    ("sent", "expected"),
    [("Green", "green"), ("Red", "red"), ("Yellow", "yellow"), (None, None)],
)
async def test_the_polls_colour_is_lower_cased_on_the_way_in(sent, expected):
    """The panel capitalises its colour and the stream sends a digit named in
    lower case. Both have to leave the client in one vocabulary, or a template
    comparing the attribute to `green` stops matching whenever the source
    changes - which it does at every setup and every stream drop."""
    answer: dict[str, str] = {"Status": "Ready To Arm"}
    if sent is not None:
        answer["Color"] = sent
    answers = [*login_answers(), FakeResponse(payload=encrypted(answer))]
    made, _ = client(answers)

    assert (await made.get_status()).color == expected


async def test_a_second_call_reuses_the_session_rather_than_logging_in_again():
    answers = [
        *login_answers(),
        FakeResponse(payload=encrypted({"Status": "Ready To Arm"})),
        FakeResponse(payload=encrypted({"Status": "Ready To Arm"})),
    ]
    made, session = client(answers)
    await made.get_status()
    await made.get_status()
    assert len(session.calls) == 5


async def test_arm_and_disarm_send_the_partition_and_the_code():
    answers = [
        *login_answers(),
        FakeResponse(payload=encrypted({"Result": "OK"})),
        FakeResponse(payload=encrypted({"Result": "OK"})),
    ]
    made, session = client(answers)

    await made.arm("STAY", "1234", 2)
    await made.disarm("1234", 2)

    armed = api.TuxedoTouchClient._aes_decrypt(
        session.calls[3][2]["data"]["param"], KEY, IV
    )
    disarmed = api.TuxedoTouchClient._aes_decrypt(
        session.calls[4][2]["data"]["param"], KEY, IV
    )
    assert armed == "arming=STAY&pID=2&ucode=1234&operation=set"
    assert disarmed == "pID=2&ucode=1234&operation=set"
    assert session.calls[3][1].endswith("/AdvancedSecurity/ArmWithCode")
    assert session.calls[4][1].endswith("/AdvancedSecurity/DisarmWithCode")


async def test_the_https_redirect_gets_its_own_error():
    """Re-logging in can never clear this, so it must not look like an expired
    session: the coordinator turns this class into a repair issue."""
    answers = [
        *login_answers(),
        FakeResponse(
            status=302, headers={"Location": "https://10.0.0.5/system_http_api"}
        ),
    ]
    made, _ = client(answers, use_https=False, port=80)
    with pytest.raises(api.TuxedoTouchHttpsRequiredError):
        await made.get_status()


async def test_an_expired_session_is_retried_once_after_a_fresh_login():
    answers = [
        *login_answers(),
        FakeResponse(status=401),
        *login_answers(session_cookie="OTHERID=zzz"),
        FakeResponse(payload=encrypted({"Status": "Ready To Arm"})),
    ]
    made, session = client(answers)

    status = await made.get_status()

    assert status.status == "Ready To Arm"
    assert made._session_cookie == "OTHERID=zzz"
    assert session.calls[-1][2]["headers"]["Cookie"] == "OTHERID=zzz"


async def test_a_redirect_on_https_is_treated_as_an_expired_session():
    """Only the http-to-https redirect is the misconfiguration; any other 302
    is the panel having forgotten the session."""
    answers = [
        *login_answers(),
        FakeResponse(status=302, headers={"Location": "/login"}),
        *login_answers(session_cookie="OTHERID=zzz"),
        FakeResponse(payload=encrypted({"Status": "Ready To Arm"})),
    ]
    made, _ = client(answers)
    assert (await made.get_status()).status == "Ready To Arm"


async def test_a_second_refusal_is_not_retried_again():
    answers = [
        *login_answers(),
        FakeResponse(status=401),
        *login_answers(),
        FakeResponse(status=401),
    ]
    made, _ = client(answers)
    with pytest.raises(api.TuxedoTouchError) as raised:
        await made.get_status()
    assert "401" in str(raised.value)


async def test_an_unexpected_status_is_reported_with_its_code():
    answers = [*login_answers(), FakeResponse(status=500)]
    made, _ = client(answers)
    with pytest.raises(api.TuxedoTouchError) as raised:
        await made.get_status()
    assert "500" in str(raised.value)


async def test_a_200_carrying_html_does_not_escape_as_a_json_error():
    answers = [*login_answers(), FakeResponse(payload=ValueError("no json"))]
    made, _ = client(answers)
    with pytest.raises(api.TuxedoTouchError) as raised:
        await made.get_status()
    assert "non-JSON" in str(raised.value)


async def test_a_response_that_is_not_an_object_is_refused():
    answers = [*login_answers(), FakeResponse(payload=["nope"])]
    made, _ = client(answers)
    with pytest.raises(api.TuxedoTouchError):
        await made.get_status()


async def test_a_response_without_a_result_field_is_refused():
    answers = [*login_answers(), FakeResponse(payload={"Error": "busy"})]
    made, _ = client(answers)
    with pytest.raises(api.TuxedoTouchError):
        await made.get_status()


async def test_a_result_that_will_not_decrypt_stays_inside_the_error_hierarchy():
    """Bad padding raises out of cryptography; callers catch one clean failure."""
    answers = [
        *login_answers(),
        FakeResponse(payload={"Result": base64.b64encode(b"not ciphertext").decode()}),
    ]
    made, _ = client(answers)
    with pytest.raises(api.TuxedoTouchError) as raised:
        await made.get_status()
    assert "decrypt" in str(raised.value)


async def test_a_dropped_connection_during_a_call_is_a_connection_error():
    answers = [*login_answers(), aiohttp.ClientError("reset")]
    made, _ = client(answers)
    with pytest.raises(api.TuxedoTouchConnectionError):
        await made.get_status()


async def test_a_call_made_without_a_key_refuses_rather_than_signing_nothing():
    """Guards the window where a concurrent caller invalidated the session."""
    made, _ = client([])

    async def _already_authenticated():
        return None

    made._ensure_authenticated = _already_authenticated
    with pytest.raises(api.TuxedoTouchError) as raised:
        await made._call("/GetSecurityStatus", "operation=get")
    assert "no session key" in str(raised.value)


async def test_a_missing_cookie_is_simply_not_sent():
    """The header is omitted rather than sent empty when there is no cookie."""
    made, session = client(
        [FakeResponse(payload=encrypted({"Status": "Ready To Arm"}))]
    )
    made._key_hex, made._iv_hex, made._key, made._iv = KEY_HEX, IV_HEX, KEY, IV

    async def _already_authenticated():
        return None

    made._ensure_authenticated = _already_authenticated
    await made.get_status()
    assert "Cookie" not in session.calls[0][2]["headers"]


# ------------------------------------------------- the panel's one connection


def _pool_key(ssl_arg, host="10.0.0.5", port=443):
    """The key aiohttp files a connection under, built from aiohttp's own type.

    Constructed from _fields rather than positionally so that a future aiohttp
    adding a member does not quietly turn this into a different assertion.
    """
    fields = dict.fromkeys(ConnectionKey._fields)
    fields.update(host=host, port=port, is_ssl=True, ssl=ssl_arg)
    return ConnectionKey(**fields)


async def test_the_probe_and_the_poller_land_on_one_pool_key():
    """Two clients for one panel must share Home Assistant's pooled connection.

    The unit serves one connection at a time and answers a second with
    silence, so the config flow's probe has to reuse the socket the entry's
    coordinator left in the pool rather than open its own. aiohttp keys that
    pool on the ssl argument as well as the host and port, so this holds only
    while every client passes the SAME context object.
    """
    poller, poller_session = client(login_answers())
    probe, probe_session = client(login_answers())
    await poller.login()
    await probe.login()

    poller_ssl = poller_session.calls[0][2]["ssl"]
    probe_ssl = probe_session.calls[0][2]["ssl"]
    assert poller_ssl is probe_ssl
    assert _pool_key(poller_ssl) == _pool_key(probe_ssl)
    assert hash(_pool_key(poller_ssl)) == hash(_pool_key(probe_ssl))


def test_a_context_per_client_would_split_the_pool():
    """The control for the test above: it is the sharing that does the work.

    Built from the undecorated function, which is what a per-client context
    would be. Two contexts are two keys - a second socket to the panel - so
    the assertion above is not passing on the host and port alone.
    """
    own = api._legacy_ssl_context.__wrapped__()
    assert own is not api._legacy_ssl_context()
    assert _pool_key(own) != _pool_key(api._legacy_ssl_context())


async def test_plain_http_clients_share_a_pool_key_too():
    """No context is built at all for HTTP, so both send the same plain True."""
    first, first_session = client(login_answers(), use_https=False, port=80)
    second, second_session = client(login_answers(), use_https=False, port=80)
    await first.login()
    await second.login()

    assert first._ssl_ctx is None and second._ssl_ctx is None
    assert first_session.calls[0][2]["ssl"] is second_session.calls[0][2]["ssl"] is True
