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
    # and the panel session cookie is still sent, because the shim may validate
    # the real session instead of a token
    assert '"Cookie": cookie' in src or "Cookie" in src


def test_only_the_stream_moves() -> None:
    """The override must not appear anywhere near login or the REST call."""
    api = load("api")
    src = inspect.getsource(api)
    assert "push_url" not in src, "the API client must not consult the push override"
    assert "push_token" not in src
