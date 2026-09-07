"""Taking the push stream from somewhere other than the panel.

Firmware carrying P13 requires a session on the push path, and a shim can hold
one upstream subscription and fan it out to several consumers — worth doing
because every registration makes the panel flush its reply queue, so one
subscriber costs the panel less than three.

What must NOT move is login and the REST commands. The panel answers `302` to
https on the REST namespace over plain HTTP whatever credentials are presented,
so those stay on the panel; and it binds a session to the address that created
it, so a consumer that logged in against the panel must keep talking to the
panel for everything except the stream. These tests pin that split: the URL
changes for the stream and for nothing else.

Runs without Home Assistant, via `tests.no_ha`.
"""

import asyncio
import inspect

from tests.no_ha import load

push = load("push")
const = load("const")


def test_defaults_leave_the_panel_as_the_source() -> None:
    """An entry with no options must behave exactly as it did before."""
    sig = inspect.signature(push.TuxedoPushStream.__init__)
    assert sig.parameters["push_url"].default is None
    assert sig.parameters["push_token"].default is None


def test_option_keys_exist_and_are_not_confusable_with_credentials() -> None:
    assert const.OPT_PUSH_URL == "push_url"
    assert const.OPT_PUSH_TOKEN == "push_token"
    # A shim token is not a panel credential. Keeping the names distinct stops
    # a reauth flow ever reaching for one.
    assert const.OPT_PUSH_TOKEN != "password"


class _Client:
    """Only what the stream reads off the client."""

    base_url = "https://panel.example:443"


def _stream(url: str | None = None, token: str | None = None):
    return push.TuxedoPushStream(
        _Client(), lambda _s: None, lambda _c: None, push_url=url, push_token=token
    )


def test_url_falls_back_to_the_panel() -> None:
    s = _stream()
    assert s._push_url is None
    # the request builds "<base_url><PUSH_PATH>" when no override is set
    assert const.PUSH_PATH.startswith("/SimpleDebugger.interface/")


def test_configured_url_is_used_verbatim() -> None:
    """No path is appended: a shim may not serve the vendor's path."""
    s = _stream(url="http://shim.example:8081/SimpleDebugger.interface/G.")
    assert s._push_url == "http://shim.example:8081/SimpleDebugger.interface/G."


def test_blank_values_are_treated_as_unset() -> None:
    """An empty options field must not produce a request to "" or a bare token."""
    assert _stream(url="", token="")._push_url is None
    assert _stream(url="", token="")._push_token is None


def test_token_is_sent_both_ways() -> None:
    """A shim may gate on a bearer header or on a cookie; sending the one it
    ignores costs nothing, and guessing wrong costs a silent 401."""
    src = inspect.getsource(push.TuxedoPushStream._async_stream_once)
    assert 'headers["Authorization"] = f"Bearer {self._push_token}"' in src
    assert "tuxweb_token=" in src
    # And the panel session cookie is still sent, because the shim may validate
    # the real session instead of a token. Asserted on its own: an earlier
    # draft wrote `'"Cookie": cookie' in src or "Cookie" in src`, and the
    # second half of that is satisfied by the token line the assert above
    # already requires, so the clause could not fail.
    assert '"Cookie": cookie' in src


def test_only_the_stream_moves() -> None:
    """The override must not appear anywhere near login or the REST call."""
    api = load("api")
    src = inspect.getsource(api)
    assert "push_url" not in src, "the API client must not consult the push override"
    assert "push_token" not in src


# ----------------------------------------------------------------------
# What the request actually carries, rather than what the source says.
#
# The two tests below drive _async_stream_once against a recording session.
# Both use a non-200 status so the method raises before it reaches the frame
# decoder, which is the part that would need a real streaming body.

PANEL_CTX = "the-panels-deliberately-broken-context"


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _RecordingSession:
    """Records the keyword arguments the stream opened its request with."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _Resp(self.status)


class _RecordingClient:
    base_url = "https://panel.example:443"
    ssl_arg = PANEL_CTX

    def __init__(self, status: int) -> None:
        self.session = _RecordingSession(status)

    async def async_session_cookie(self) -> str:
        return "PHPSESSID=abc"


def _run_once(client, url=None, token=None):
    """Drive one stream attempt; returns the exception it raised, or None."""
    stream = push.TuxedoPushStream(
        client, lambda _s: None, lambda _c: None, push_url=url, push_token=token
    )
    try:
        asyncio.run(stream._async_stream_once())
    except Exception as err:
        # Catching broadly on purpose: which exception class comes out is
        # exactly what these tests are about.
        return err
    return None


def test_the_request_goes_to_the_configured_url() -> None:
    """The override, proven by the address the request was opened on.

    The end-to-end test in tests/ha cannot show this on its own: its fake
    panel serves the relay URL too, so the request lands in the same place
    either way and passes with the override reverted.
    """
    client = _RecordingClient(500)
    relay = "https://relay.example:8081/SimpleDebugger.interface/G."
    _run_once(client, url=relay)
    assert client.session.calls[0]["url"] == relay


def test_the_request_goes_to_the_panel_when_no_url_is_set() -> None:
    """And the default really is the panel, not merely a None attribute."""
    client = _RecordingClient(500)
    _run_once(client)
    assert client.session.calls[0]["url"] == (
        f"https://panel.example:443{const.PUSH_PATH}"
    )


def test_the_panel_keeps_its_ssl_exemption() -> None:
    """The panel's expired 2009 self-signed certificate is why that context exists."""
    client = _RecordingClient(500)
    _run_once(client)
    assert client.session.calls[0]["ssl"] == PANEL_CTX


def test_a_relay_does_not_inherit_the_panels_ssl_exemption() -> None:
    """A relay is an address the operator typed, and it receives the push token.

    Handing it the panel's context would mean no certificate verification and no
    hostname check against a host that has no claim on that exemption - the
    token would go to whatever answered.
    """
    client = _RecordingClient(500)
    _run_once(client, url="https://relay.example:8081/SimpleDebugger.interface/G.")
    assert client.session.calls[0]["ssl"] is True


def test_a_panel_401_is_a_session_expiry() -> None:
    """Unchanged behaviour: the cookie died, so log in once and carry on."""
    err = _run_once(_RecordingClient(401))
    assert isinstance(err, push.PushSessionExpired)


def test_a_relay_401_is_not_read_as_a_panel_session_expiry() -> None:
    """Otherwise a wrong push token re-logs into the panel forever.

    The handler for PushSessionExpired invalidates the session and logs in
    again. The panel serves one connection at a time, so a relay rejecting the
    token would spend that connection on a pointless re-login every backoff
    period while the log blamed the panel.
    """
    err = _run_once(
        _RecordingClient(401),
        url="https://relay.example:8081/SimpleDebugger.interface/G.",
        token="wrong",
    )
    assert err is not None
    assert not isinstance(err, push.PushSessionExpired)
    assert "relay refused" in str(err)
