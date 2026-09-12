"""The client against a panel running tuxweb, and against stock, side by side.

A real HTTP server on 127.0.0.1 in each of its two modes (tests/fake_panel.py),
so what is exercised is the actual probe, the actual request shapes and the
actual reconnect loop. Every tuxweb assertion here has a stock control next to
it or in the modules alongside: the two contracts share their paths, so a test
that only checked outcomes could pass with the mode detection reverted and the
wrong path taken. The assertions are therefore on the wire - which headers a
request carried and which it did not, whether a login page was ever asked for,
how many requests a refusal cost - rather than on the answer alone.

Runs without Home Assistant, via `tests.no_ha`.
"""

import asyncio
import contextlib

import aiohttp
import pytest

from tests.fake_panel import (
    READY_FRAME,
    TUXWEB_CAPABILITIES,
    TUXWEB_TOKEN,
    FakePanel,
    wait_until,
)
from tests.no_ha import load

api = load("api")
push = load("push")
const = load("const")


@pytest.fixture(autouse=True)
def _real_sockets(socket_enabled):
    """These tests need a real socket, and pytest-socket blocks them.

    pytest-homeassistant-custom-component disables socket creation for every
    test in the session; asking for socket_enabled is how a test says it is
    one of the exceptions. The connect() guard installed alongside it still
    allows only 127.0.0.1, which is where the fake panel is.
    """


@pytest.fixture
async def tuxweb():
    made = FakePanel(tuxweb=True)
    await made.start()
    yield made
    await made.close()


@pytest.fixture
async def stock():
    made = FakePanel()
    await made.start()
    yield made
    await made.close()


@pytest.fixture
async def session():
    made = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
    yield made
    await made.close()


def _client(panel, session, token=TUXWEB_TOKEN):
    return api.TuxedoTouchClient(
        session=session,
        host="127.0.0.1",
        port=panel.port,
        use_https=False,
        username=panel.username,
        password=panel.password,
        tuxweb_token=token,
    )


# ------------------------------------------------------------- detection


async def test_a_stock_panel_answers_the_probe_with_404_and_is_stock(stock, session):
    """One GET, the measured 404, and nothing else: no login page, no POST."""
    client = _client(stock, session)
    assert client.tuxweb is False, "a client nobody has asked for is stock"

    assert await client.async_probe_capabilities() is False

    assert client.tuxweb is False
    assert client.capabilities == frozenset()
    assert client.confirms_commands is False
    assert stock.capability_probes == 1
    assert stock.login_page_requests == 0
    assert stock.login_attempts == 0


async def test_a_tuxweb_panel_answers_200_and_is_tuxweb(tuxweb, session):
    client = _client(tuxweb, session)

    assert await client.async_probe_capabilities() is True

    assert client.tuxweb is True
    assert client.capabilities == frozenset(TUXWEB_CAPABILITIES)
    assert client.confirms_commands is True
    assert tuxweb.capability_probes == 1
    assert tuxweb.login_page_requests == 0


async def test_the_probe_is_asked_once_and_the_answer_kept(tuxweb, session):
    """ "Once, at setup" is the contract, and every later call reads the cache."""
    client = _client(tuxweb, session)
    await client.async_probe_capabilities()
    await client.async_probe_capabilities()
    await client.get_status()
    await client.arm("STAY", "1234")
    assert tuxweb.capability_probes == 1


async def test_detection_branches_on_the_capability_list_and_nothing_else(
    tuxweb, session
):
    """The firmware and contract fields are for people. A newer tuxweb that
    changed both, and declared a capability nothing here knows, is still
    tuxweb - and the unknown string is kept without being acted on."""
    tuxweb.firmware = "somethingelse/9.9.9"
    tuxweb.contract = 7
    tuxweb.capabilities = ["command_result", "a_capability_from_the_future"]
    client = _client(tuxweb, session)

    assert await client.async_probe_capabilities() is True
    assert client.confirms_commands is True
    assert "a_capability_from_the_future" in client.capabilities


async def test_a_tuxweb_without_command_result_does_not_claim_to_confirm(
    tuxweb, session
):
    """The capability decides the behaviour, not the mode. A tuxweb that does
    not declare command_result is still talked to as tuxweb, and its 200s
    are read as "sent", the way stock's are."""
    tuxweb.capabilities = ["panel_link_state", "status_refresh"]
    client = _client(tuxweb, session)

    assert await client.async_probe_capabilities() is True
    assert client.confirms_commands is False


@pytest.mark.parametrize(
    "body",
    [
        b"<html>a page for every path</html>",
        b'{"firmware":"tuxweb/0.1.0","contract":1}',
        b'{"capabilities":"command_result"}',
        b"[]",
    ],
    ids=["html", "json-without-a-list", "list-is-a-string", "not-an-object"],
)
async def test_a_200_that_declares_no_capability_list_is_stock(stock, session, body):
    """A 200 is not the signal; the list is. An embedded server that answers
    200 with a page for any path must not be taken for a replacement."""
    stock.capability_body = body
    client = _client(stock, session)

    assert await client.async_probe_capabilities() is False
    assert client.capabilities == frozenset()


async def test_a_401_from_the_probe_is_read_as_stock_and_starts_no_login(
    stock, session
):
    """Neither firmware answers 401 here. If one did, it must not become a
    login: the probe is built by hand and never touches the handshake."""
    stock.capability_status = 401
    client = _client(stock, session)

    assert await client.async_probe_capabilities() is False
    assert stock.login_page_requests == 0
    assert stock.login_attempts == 0


async def test_an_unreachable_panel_fails_the_probe_and_caches_nothing(stock, session):
    """A setup retry has to ask again; remembering "stock" from a connection
    that never happened would put a tuxweb panel on the login path."""
    client = _client(stock, session)
    await stock.close()

    with pytest.raises(api.TuxedoTouchConnectionError):
        await client.async_probe_capabilities()
    assert client._tuxweb is None


# -------------------------------------------------------- the API on tuxweb


async def test_status_on_tuxweb_is_one_plaintext_request_on_the_token(tuxweb, session):
    """The bearer header, and NONE of the stock machinery: no authtoken, no
    identity, no cookie, no login page, no credential POST."""
    client = _client(tuxweb, session)
    await client.async_probe_capabilities()

    status = await client.get_status()

    assert status.status == "Ready To Arm"
    assert status.color == "green"
    assert status.armed is False
    assert status.source == const.SOURCE_POLL
    headers = tuxweb.last_api_headers
    assert headers["Authorization"] == f"Bearer {TUXWEB_TOKEN}"
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    for stock_only in ("authtoken", "identity", "Cookie"):
        assert stock_only not in headers, f"{stock_only} was sent to tuxweb"
    assert tuxweb.login_page_requests == 0
    assert tuxweb.login_attempts == 0
    assert tuxweb.polls == 1


async def test_the_stock_wire_shape_is_unchanged_after_the_probe(stock, session):
    """The control for the test above, on the same client class and the same
    sequence: probe, then status. A stock panel gets the login handshake and
    the signed, encrypted request it always did, and never a bearer header.
    Without this the test above could pass with the mode logic reverted to
    "always tuxweb"."""
    client = _client(stock, session)
    await client.async_probe_capabilities()

    status = await client.get_status()

    assert status.status == "Ready To Arm"
    assert status.armed is None, "a stock poll carries no armed flag"
    assert stock.logins == 1
    assert stock.polls == 1
    headers = stock.last_api_headers
    assert "Authorization" not in headers
    assert headers["Cookie"] == stock.cookie
    assert "authtoken" in headers
    assert headers["identity"] == "0f1e2d3c4b5a69788796a5b4c3d2e1f0"


async def test_a_token_given_to_a_stock_panel_is_simply_unused(stock, session):
    """The token being configured does not decide the mode; the panel does."""
    client = _client(stock, session, token=TUXWEB_TOKEN)
    await client.async_probe_capabilities()
    await client.get_status()
    assert client.tuxweb is False
    assert "Authorization" not in stock.last_api_headers
    assert stock.logins == 1


async def test_status_carries_the_armed_flag_and_the_countdown(tuxweb, session):
    """tuxweb's state field is the stream's display field: colour digit,
    then text. Read the same way, so both sources speak one vocabulary."""
    tuxweb.armed = True
    tuxweb.status = "59  Secs Remaining"
    tuxweb.colour = "Red"
    client = _client(tuxweb, session)
    await client.async_probe_capabilities()

    status = await client.get_status()

    assert status.armed is True
    assert status.status == "59  Secs Remaining"
    assert status.color == "red"
    assert status.seconds_remaining == 59


async def test_an_empty_state_is_the_placeholder_the_coordinator_handles(
    tuxweb, session
):
    """tuxweb answers an empty state before the panel's first report. That is
    the stock cache's "Not available" by another name - a failed read rather
    than a state - and it is mapped onto it so the coordinator fails the poll
    the one way it already knows."""
    tuxweb.status = ""
    client = _client(tuxweb, session)
    await client.async_probe_capabilities()

    assert (await client.get_status()).status == const.STATUS_NOT_AVAILABLE


async def test_arm_sends_the_form_body_and_reads_the_vendor_reply(tuxweb, session):
    """Byte for byte: the same parameters, in the same order, that the stock
    path puts inside AES - now as the body itself."""
    client = _client(tuxweb, session)
    await client.async_probe_capabilities()

    result = await client.arm("STAY", "1234", 2)

    assert tuxweb.last_command_body == "arming=STAY&pID=2&ucode=1234&operation=set"
    assert tuxweb.last_api_headers["Content-Type"] == (
        "application/x-www-form-urlencoded"
    )
    assert tuxweb.commands == ["ArmWithCode"]
    assert result == {
        "Status": "Sucess",
        "Result": {"Response": "Command sent sucessfully"},
    }
    assert tuxweb.armed is True, "a 200 is the panel having acted"


async def test_disarm_sends_its_body_and_reads_its_own_inner_key(tuxweb, session):
    """Disarm answers under `Result`, arm under `Response`: the asymmetry is
    the vendor's and tuxweb keeps it, so the client must not expect one
    shape."""
    tuxweb.armed = True
    client = _client(tuxweb, session)
    await client.async_probe_capabilities()

    result = await client.disarm("1234", 2)

    assert tuxweb.last_command_body == "pID=2&ucode=1234&operation=set"
    assert tuxweb.commands == ["DisarmWithCode"]
    assert result == {"Status": "Sucess", "Result": {"Result": "Disarmed"}}
    assert tuxweb.armed is False


async def test_a_command_the_panel_did_not_confirm_is_a_clear_error(tuxweb, session):
    """504: sent, and the state byte never flipped. Not an auth error, not a
    connection error - its own class, named for what happened, so the entity
    fails the service call rather than assuming the alarm armed."""
    tuxweb.confirm_commands = False
    client = _client(tuxweb, session)
    await client.async_probe_capabilities()

    with pytest.raises(api.TuxedoTouchCommandNotConfirmed) as raised:
        await client.arm("AWAY", "1234")

    assert isinstance(raised.value, api.TuxedoTouchError)
    assert not isinstance(raised.value, api.TuxedoTouchAuthError)
    assert "did not confirm" in str(raised.value)
    assert "command sent but not confirmed" in str(raised.value), (
        "the server's own reason is carried through"
    )
    assert tuxweb.armed is False


async def test_a_refused_token_is_an_auth_error_and_never_a_login(tuxweb, session):
    """The whole of the danger on this path is the stock reflex: a 401 used
    to mean "log in again". On tuxweb it means the token, there is no login
    page, and the refusal costs exactly one request."""
    tuxweb.token = "a" * 64
    client = _client(tuxweb, session)
    await client.async_probe_capabilities()

    with pytest.raises(api.TuxedoTouchTokenRejected) as raised:
        await client.get_status()

    assert isinstance(raised.value, api.TuxedoTouchAuthError)
    assert "401" in str(raised.value)
    assert tuxweb.api_requests == 1, "a refused token must not be retried"
    assert tuxweb.login_page_requests == 0
    assert tuxweb.login_attempts == 0
    assert tuxweb.polls == 0


async def test_a_refused_token_on_a_command_is_the_same_refusal(tuxweb, session):
    tuxweb.token = None
    client = _client(tuxweb, session)
    await client.async_probe_capabilities()

    for attempt in (client.arm("STAY", "1234"), client.disarm("1234")):
        with pytest.raises(api.TuxedoTouchTokenRejected):
            await attempt
    assert tuxweb.api_requests == 2
    assert tuxweb.login_attempts == 0
    assert tuxweb.commands == []


async def test_no_token_configured_is_refused_before_any_request(tuxweb, session):
    """A tuxweb panel and a stock entry: the refusal names the missing token
    and sends nothing, rather than sending a bare request to read a 401."""
    client = _client(tuxweb, session, token=None)
    await client.async_probe_capabilities()

    with pytest.raises(api.TuxedoTouchTokenRejected) as raised:
        await client.get_status()

    assert "none is configured" in str(raised.value)
    assert tuxweb.api_requests == 0
    assert tuxweb.login_page_requests == 0


async def test_a_blank_token_is_no_token(tuxweb, session):
    """An emptied form field must not become a bearer header of nothing."""
    client = _client(tuxweb, session, token="")
    await client.async_probe_capabilities()
    with pytest.raises(api.TuxedoTouchTokenRejected):
        await client.get_status()
    assert tuxweb.api_requests == 0


async def test_a_missing_code_is_the_servers_400_with_its_reason(tuxweb, session):
    """tuxweb refuses code 0 before sending, because the declined path moves
    panel state. The refusal is a panel error carrying the server's words -
    neither "not confirmed" nor anything to do with the token."""
    client = _client(tuxweb, session)
    await client.async_probe_capabilities()

    with pytest.raises(api.TuxedoTouchError) as raised:
        await client.arm("STAY", "0")

    assert not isinstance(raised.value, api.TuxedoTouchCommandNotConfirmed)
    assert not isinstance(raised.value, api.TuxedoTouchAuthError)
    assert "400" in str(raised.value)
    assert "a user code is required" in str(raised.value)


@pytest.mark.parametrize(
    "body",
    [
        b"<html>an error page</html>",
        b'["not", "an", "object"]',
        b'{"partition": 1}',
        b'{"partition": 1, "armed": "yes", "state": "1Ready To Arm"}',
    ],
    ids=["html", "not-an-object", "no-state", "armed-not-a-bool"],
)
async def test_a_status_answer_of_the_wrong_shape_is_one_clean_failure(
    tuxweb, session, body
):
    """An embedded server answering 200 with something else must not escape
    as a JSONDecodeError or a KeyError: callers catch one failed call."""
    tuxweb.tuxweb_status_body = body
    client = _client(tuxweb, session)
    await client.async_probe_capabilities()

    with pytest.raises(api.TuxedoTouchError) as raised:
        await client.get_status()
    assert "shape" in str(raised.value)
    assert not isinstance(raised.value, api.TuxedoTouchAuthError)


async def test_a_dropped_connection_on_tuxweb_is_a_connection_error(tuxweb, session):
    """Same class as on stock, so the coordinator retries it the same way."""
    client = _client(tuxweb, session)
    await client.async_probe_capabilities()
    await tuxweb.close()

    with pytest.raises(api.TuxedoTouchConnectionError):
        await client.get_status()


async def test_login_is_refused_outright_in_tuxweb_mode(tuxweb, session):
    """The chokepoint. Every stock request authenticates through login(), so
    this one guard is what makes "tuxweb never logs in" a property of the
    client rather than of each caller remembering."""
    client = _client(tuxweb, session)
    await client.async_probe_capabilities()

    with pytest.raises(api.TuxedoTouchError) as raised:
        await client.login()
    assert not isinstance(raised.value, api.TuxedoTouchAuthError)
    with pytest.raises(api.TuxedoTouchError):
        await client.async_session_cookie()
    assert tuxweb.login_page_requests == 0
    assert tuxweb.login_attempts == 0


async def test_check_credentials_spends_a_status_read_on_tuxweb_and_a_login_on_stock(
    tuxweb, stock, session
):
    """The config flow's probe, in both modes, counted by what each panel saw."""
    await _client(tuxweb, session).async_check_credentials()
    assert tuxweb.login_attempts == 0
    assert tuxweb.polls == 1

    await _client(stock, session).async_check_credentials()
    assert stock.login_attempts == 1
    assert stock.logins == 1
    assert stock.polls == 0


async def test_check_credentials_reports_a_refused_token(tuxweb, session):
    tuxweb.token = "b" * 64
    with pytest.raises(api.TuxedoTouchTokenRejected):
        await _client(tuxweb, session).async_check_credentials()
    assert tuxweb.login_attempts == 0


# -------------------------------------------------------- the stream on tuxweb


class Collector:
    def __init__(self):
        self.statuses = []

    def status(self, status):
        self.statuses.append(status)


async def _stop(task):
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_the_stream_opens_on_the_bearer_token_with_no_login(tuxweb, session):
    """Same path, same frames; only the auth differs. The token goes as the
    bearer header the panel documents first, and the panel's cookie - there
    is none - is not sent at all."""
    client = _client(tuxweb, session)
    await client.async_probe_capabilities()
    collector = Collector()
    stream = push.TuxedoPushStream(client, collector.status, lambda up: None)
    task = asyncio.create_task(stream.async_run())
    try:
        await wait_until(lambda: stream.connected)
        headers = tuxweb.last_push_headers
        assert headers["Authorization"] == f"Bearer {TUXWEB_TOKEN}"
        assert "Cookie" not in headers
        assert tuxweb.login_page_requests == 0
        assert tuxweb.login_attempts == 0

        # And a frame reads exactly as it does on stock: same decoder, same
        # flag byte, same text.
        await tuxweb.push(READY_FRAME)
        await wait_until(lambda: collector.statuses)
        status = collector.statuses[0]
        assert status.armed is False
        assert status.text == "Ready To Arm"
        assert status.colour == "green"
    finally:
        await _stop(task)


async def test_a_refused_token_on_the_stream_ends_the_task_without_a_login(
    tuxweb, session
):
    """The terminal branch: one request, the 401, and the task is over. Not
    the session-expiry branch, which would invalidate a session that does
    not exist and log in against a page that does not either."""
    tuxweb.token = "c" * 64
    client = _client(tuxweb, session)
    await client.async_probe_capabilities()
    stream = push.TuxedoPushStream(client, lambda s: None, lambda up: None)

    await asyncio.wait_for(stream.async_run(), timeout=5)

    assert stream.auth_failed is True
    assert stream.connected is False
    assert tuxweb.stream_requests == 1
    assert tuxweb.login_page_requests == 0
    assert tuxweb.login_attempts == 0


async def test_a_stream_on_tuxweb_without_a_token_ends_the_same_way(tuxweb, session):
    client = _client(tuxweb, session, token=None)
    await client.async_probe_capabilities()
    stream = push.TuxedoPushStream(client, lambda s: None, lambda up: None)

    await asyncio.wait_for(stream.async_run(), timeout=5)

    assert stream.auth_failed is True
    assert tuxweb.stream_requests == 0, "nothing is sent without a token"
    assert tuxweb.login_page_requests == 0


async def test_a_relay_in_front_of_tuxweb_gets_the_relay_token_alone(tuxweb, session):
    """No panel cookie exists to ride alongside the relay's token, so the
    cookie form carries the token by itself rather than the word None."""
    client = _client(tuxweb, session)
    await client.async_probe_capabilities()
    relay = f"http://127.0.0.1:{tuxweb.port}/SimpleDebugger.interface/G."
    stream = push.TuxedoPushStream(
        client,
        lambda s: None,
        lambda up: None,
        push_url=relay,
        push_token=TUXWEB_TOKEN,
    )
    task = asyncio.create_task(stream.async_run())
    try:
        await wait_until(lambda: stream.connected)
        headers = tuxweb.last_push_headers
        assert headers["Authorization"] == f"Bearer {TUXWEB_TOKEN}"
        assert headers["Cookie"] == f"tuxweb_token={TUXWEB_TOKEN}"
    finally:
        await _stop(task)


# ---------------------------------------------------------- pure structure


def test_the_new_errors_sit_where_callers_expect_them():
    """A refused token routes as an auth error everywhere one is caught; a
    command the panel did not confirm routes as an ordinary failure and
    never as a credential judgement."""
    assert issubclass(api.TuxedoTouchTokenRejected, api.TuxedoTouchAuthError)
    assert issubclass(api.TuxedoTouchCommandNotConfirmed, api.TuxedoTouchError)
    assert not issubclass(api.TuxedoTouchCommandNotConfirmed, api.TuxedoTouchAuthError)


def test_the_option_key_is_distinct_from_the_relay_token():
    """Two tokens, two meanings: the relay's gates a stream somewhere else,
    the panel's is a credential for the panel itself."""
    assert const.CONF_TUXWEB_TOKEN == "tuxweb_token"
    assert const.CONF_TUXWEB_TOKEN != const.OPT_PUSH_TOKEN
    assert const.CONF_TUXWEB_TOKEN != "password"


def test_the_capability_strings_are_spelled_as_tuxweb_spells_them():
    """Detection is on these exact strings, so they are pinned to the server's."""
    assert const.CAP_COMMAND_RESULT in TUXWEB_CAPABILITIES
    assert const.CAP_STATUS_REFRESH in TUXWEB_CAPABILITIES
    assert const.CAP_PANEL_LINK_STATE in TUXWEB_CAPABILITIES
    assert const.CAPABILITIES_PATH == "/GetCapabilities"
