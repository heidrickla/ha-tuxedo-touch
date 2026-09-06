"""The client's failed-login budget, counted by the thing that would pay for it.

A real HTTP server on 127.0.0.1 speaking the panel's own login handshake, so
what is counted here is what the panel counts: credential POSTs it was asked
to judge, refusals included. That is the number that matters, because the unit
disables EVERY web account after three failed web logins - no timeout, no
self-clear, and recovery only by walking to the touchscreen. Patched firmware
allows five and clears itself after five minutes, and the panel publishes no
version anywhere, so the budget has to be safe on the stricter one.

`FakePanel.logins` counts only logins that SUCCEEDED and stays at zero however
hard a wrong password is hammered; every assertion here is on
`login_attempts`, which is incremented before the credential is even compared.

The pure-logic half of the same contract - the budget's value, the exception's
place in the hierarchy, and that a spent budget is refused before any request
is built - is in tests/test_api.py, which needs no server.
"""

import asyncio

import aiohttp
import pytest

from tests.fake_panel import FakePanel
from tests.no_ha import load

api = load("api")


@pytest.fixture(autouse=True)
def _real_sockets(socket_enabled):
    """These tests need a real socket, and pytest-socket blocks them.

    pytest-homeassistant-custom-component disables socket creation for every
    test in the session. Asking for socket_enabled is how a test says it is
    one of the exceptions; the connect() guard installed alongside it still
    allows only 127.0.0.1, which is where the fake panel is.
    """


@pytest.fixture
async def panel():
    made = FakePanel()
    await made.start()
    yield made
    await made.close()


@pytest.fixture
async def session():
    made = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
    yield made
    await made.close()


def _client(panel, session, password):
    return api.TuxedoTouchClient(
        session=session,
        host="127.0.0.1",
        port=panel.port,
        use_https=False,
        username=panel.username,
        password=password,
    )


def test_a_spent_budget_is_refused_before_any_request_is_built():
    """No login page GET either, which this proves rather than asserts.

    The client is built with session=None, so any request at all would come
    out as an AttributeError instead of the refusal. Not taking a connection
    is part of the fix: the panel serves ONE at a time and the poll needs it.

    Also in tests/test_api.py, which runs on a bare Python with no pytest;
    here as well because a failure in that script aborts the pytest run
    instead of reporting, and this is the assertion that must report.
    """
    client = api.TuxedoTouchClient(None, "10.0.0.5", 443, True, "Admin", "hunter2")
    assert client._failed_logins == 0
    assert api.LOGIN_ATTEMPT_BUDGET == 1

    client._failed_logins = api.LOGIN_ATTEMPT_BUDGET
    with pytest.raises(api.TuxedoTouchCredentialsRefused):
        asyncio.run(client.login())


def test_a_refusal_routes_as_an_auth_error():
    """Every existing `except TuxedoTouchAuthError` has to catch it, or the
    coordinator's reauth branch and the stream's terminate branch both miss
    the case they were written for."""
    assert issubclass(api.TuxedoTouchCredentialsRefused, api.TuxedoTouchAuthError)
    assert issubclass(api.TuxedoTouchCredentialsRefused, api.TuxedoTouchError)


async def test_a_refused_password_costs_one_attempt_and_never_a_second(panel, session):
    """The load-bearing assertion of this release.

    One credential POST for a password the panel refuses, for the life of the
    client - not one per poll, not one per reconnect, not one per restart.
    Two below stock firmware's three-strike cliff, and the strike that was
    spent is the one the user's own password change caused.
    """
    client = _client(panel, session, "wrong")

    with pytest.raises(api.TuxedoTouchAuthError):
        await client.login()
    assert panel.login_attempts == 1

    with pytest.raises(api.TuxedoTouchCredentialsRefused):
        await client.login()
    assert panel.login_attempts == 1


async def test_every_route_into_the_client_hits_the_same_budget(panel, session):
    """Not just login(): the poll and the commands all authenticate first.

    A budget that only guarded the direct call would leave the poll, an arm
    and a disarm each free to spend a strike of their own, which is three.
    """
    client = _client(panel, session, "wrong")
    with pytest.raises(api.TuxedoTouchAuthError):
        await client.login()
    assert panel.login_attempts == 1

    for attempt in (
        client.get_status(),
        client.arm("STAY", "1234"),
        client.disarm("1234"),
        client.async_session_cookie(),
    ):
        with pytest.raises(api.TuxedoTouchCredentialsRefused):
            await attempt
    assert panel.login_attempts == 1


async def test_a_connection_failure_is_not_a_strike(panel, session):
    """The panel never saw a credential, so it counted nothing.

    Spending the budget on an unreachable panel would leave a user whose
    switch rebooted needing a reauthentication they cannot complete.
    """
    client = _client(panel, session, panel.password)
    await panel.close()

    with pytest.raises(api.TuxedoTouchConnectionError):
        await client.login()
    assert client._failed_logins == 0


async def test_a_server_error_on_the_login_post_is_not_a_strike(panel, session):
    """A 5xx never reached the credential comparison, so nothing was judged.

    The mirror of the test above, and the one that was missing: that one
    measures a panel which DOES judge, so it cannot tell a refusal from an
    embedded web server having a bad moment. Spending the budget here is a
    lockout for a transient fault - the entry is condemned, the stream stops
    and the reauthentication card then refuses the password that was right
    all along.
    """
    client = _client(panel, session, panel.password)
    panel.login_post_status = 503
    panel.login_post_failures_left = 1

    with pytest.raises(api.TuxedoTouchConnectionError):
        await client.login()
    # The panel was asked, but it judged nothing.
    assert panel.login_attempts == 1
    assert panel.logins == 0
    assert client._failed_logins == 0

    # And the budget is whole, so the panel getting better is the whole of
    # the recovery: no reauthentication, no user action.
    await client.login()
    assert panel.logins == 1
    assert client._failed_logins == 0


async def test_a_key_page_that_fails_is_not_a_credential_judgement(panel, session):
    """The login POST already succeeded, so the panel accepted the password.

    Both raises in _fetch_keys happen after the panel returned a session
    cookie - it counted a SUCCESSFUL login. An exception that says "the panel
    refused these credentials" there is a lie the coordinator then writes
    onto the config entry for good.
    """
    for keys_status in (500, 200):
        client = _client(panel, session, panel.password)
        panel.keys_status = keys_status
        panel.keys_failures_left = 1
        logins_before = panel.logins

        with pytest.raises(api.TuxedoTouchSessionError) as raised:
            await client.login()
        # Not an auth error: nothing here is the panel judging a credential.
        assert not isinstance(raised.value, api.TuxedoTouchAuthError)
        assert panel.logins == logins_before + 1
        assert client._failed_logins == 0
        # A cookie with no key material behind it is not a session.
        assert client._session_cookie is None

        # The panel recovers by itself on the next attempt.
        await client.login()
        assert client._failed_logins == 0


async def test_a_login_that_works_leaves_the_budget_whole(panel, session):
    """A success is not a strike, and it clears any that were recorded.

    The clear runs only after _fetch_keys() has returned: a cookie with no
    usable key material behind it is not a working login and must not hand
    the budget back.

    What cannot be driven from outside, and is said here rather than faked:
    no sequence in this integration reaches the clear with a strike already
    counted, because the gate refuses the login that would follow one. The
    line is what stops a spent strike outliving the credentials that earned
    it if any future path ever does.
    """
    client = _client(panel, session, panel.password)
    await client.login()
    assert client._failed_logins == 0
    assert panel.login_attempts == 1

    client.invalidate_session()
    await client.login()
    assert panel.login_attempts == 2
    assert client._failed_logins == 0


async def test_a_password_changed_at_the_keypad_costs_exactly_one(panel, session):
    """The precondition of the whole defect, end to end.

    Credentials that were accepted at setup and are rejected later - somebody
    changed the web password at the touchscreen. The client re-logs in, is
    refused once, and stops; from then on it is the user's move.
    """
    client = _client(panel, session, panel.password)
    await client.login()
    assert panel.login_attempts == 1

    panel.password = "changed at the keypad"
    client.invalidate_session()

    with pytest.raises(api.TuxedoTouchAuthError):
        await client.login()
    assert panel.login_attempts == 2

    for _ in range(5):
        with pytest.raises(api.TuxedoTouchCredentialsRefused):
            await client.get_status()
    assert panel.login_attempts == 2


async def test_an_arm_over_plain_http_is_refused_loudly_not_dropped(session):
    """The REST API mandates TLS, and the failure shape is the dangerous one.

    Measured on the panel: over port 80 every /system_http_api/ request answers
    302 to https://<host>:443/tuxedoapi.html, while LOGIN still succeeds over
    HTTP. So a client can authenticate, fire an arm, receive a perfectly
    plausible HTTP response, and the panel never gets the command. That is an
    alarm silently not arming, which is the worst failure this integration has.

    Anything short of an exception here is a defect. A returned value - even an
    empty one - reaches the entity as a command that was sent.
    """
    from aiohttp import web

    seen = []

    async def redirect_to_https(request: web.Request) -> web.Response:
        seen.append(request.path)
        host = request.host.split(":")[0]
        return web.Response(
            status=302, headers={"Location": f"https://{host}:443/tuxedoapi.html"}
        )

    app = web.Application()
    app.router.add_route("*", "/system_http_api/{tail:.*}", redirect_to_https)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        client = api.TuxedoTouchClient(
            session=session,
            host="127.0.0.1",
            port=runner.addresses[0][1],
            use_https=False,
            username="installer",
            password="secret",
        )
        # Stand in for the login that DOES succeed over HTTP, so this test is
        # about the command rather than about authentication.
        client._session_cookie = "SESSIONID=deadbeef"
        client._key = b"0" * 16
        client._iv = b"1" * 16
        client._key_hex = "30" * 16
        client._iv_hex = "31" * 16

        with pytest.raises(api.TuxedoTouchHttpsRequiredError):
            await client.arm("AWAY", "1234")
        with pytest.raises(api.TuxedoTouchHttpsRequiredError):
            await client.disarm("1234")
    finally:
        await runner.cleanup()

    assert len(seen) == 2, (
        "both commands should have reached the panel and been refused"
    )
