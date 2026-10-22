"""The options flow: where the push stream comes from.

Both fields are empty in the supported setup — the panel serves its own stream.
They exist for a firmware that gates the push path on a session and a relay that
holds one upstream subscription for several consumers.

What these tests pin is the part that is easy to get wrong later: blanks must
clear rather than persist as empty strings, the unrelated options key must
survive, and only the stream may move.
"""

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState, OptionsFlowWithReload
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuxedo_touch.config_flow import TuxedoTouchOptionsFlow
from custom_components.tuxedo_touch.const import (
    DOMAIN,
    OPT_CREDENTIALS_REJECTED,
    OPT_PUSH_TOKEN,
    OPT_PUSH_URL,
)
from tests.fake_panel import wait_until

from .test_config_flow import ENTRY_DATA, HOST, PORT

RELAY = "https://relay.example:8081/SimpleDebugger.interface/G."


def _entry(options: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Tuxedo Touch",
        unique_id=f"{HOST}:{PORT}:1",
        data=dict(ENTRY_DATA),
        options=dict(options or {}),
    )


def _suggested(result) -> dict:
    return {
        key.schema: key.description["suggested_value"]
        for key in result["data_schema"].schema
        if getattr(key, "description", None) and "suggested_value" in key.description
    }


def _defaults(result) -> dict:
    return {
        key.schema: key.default()
        for key in result["data_schema"].schema
        if callable(getattr(key, "default", None))
    }


async def test_form_opens_with_the_current_values(hass: HomeAssistant) -> None:
    entry = _entry({OPT_PUSH_URL: RELAY, OPT_PUSH_TOKEN: "stored-token"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    # And it really opens with them. The name of this test promised that while
    # the body only checked that a form appeared, which it would have done with
    # add_suggested_values_to_schema deleted - a stored relay would then come
    # back as an empty box and be cleared by anyone who pressed Submit.
    suggested = {
        key.schema: key.description["suggested_value"]
        for key in result["data_schema"].schema
        if getattr(key, "description", None) and "suggested_value" in key.description
    }
    assert suggested[OPT_PUSH_URL] == RELAY
    # The token is a secret: neither suggested nor a default, where the
    # password field could reveal it.
    assert OPT_PUSH_TOKEN not in suggested
    assert "stored-token" not in _defaults(result).values()


async def test_setting_a_relay_stores_both_fields(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={OPT_PUSH_URL: RELAY, OPT_PUSH_TOKEN: "s3cr3t"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][OPT_PUSH_URL] == RELAY
    assert result["data"][OPT_PUSH_TOKEN] == "s3cr3t"


async def test_blank_clears_rather_than_storing_an_empty_string(
    hass: HomeAssistant,
) -> None:
    """A cleared box must mean "use the panel", not "fetch the stream from ''"."""
    entry = _entry({OPT_PUSH_URL: RELAY, OPT_PUSH_TOKEN: "old"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={OPT_PUSH_URL: "", OPT_PUSH_TOKEN: "   "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert OPT_PUSH_URL not in result["data"]
    assert OPT_PUSH_TOKEN not in result["data"]


async def test_whitespace_is_trimmed(hass: HomeAssistant) -> None:
    """A pasted URL with a trailing newline must not become a 404."""
    entry = _entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={OPT_PUSH_URL: f"  {RELAY}  ", OPT_PUSH_TOKEN: " tok "},
    )
    assert result["data"][OPT_PUSH_URL] == RELAY
    assert result["data"][OPT_PUSH_TOKEN] == "tok"


async def test_an_unrelated_option_survives(hass: HomeAssistant) -> None:
    """This form does not own the credentials-rejected flag, and must not eat it."""
    entry = _entry({OPT_CREDENTIALS_REJECTED: True})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={OPT_PUSH_URL: RELAY, OPT_PUSH_TOKEN: "tok"}
    )
    assert result["data"][OPT_CREDENTIALS_REJECTED] is True
    assert result["data"][OPT_PUSH_URL] == RELAY


async def test_the_entry_loads_and_streams_from_the_relay(
    hass: HomeAssistant, panel_entry: MockConfigEntry, fake_panel
) -> None:
    """End to end through the integration: login on the panel, stream from the
    configured URL, with the push token attached and nothing of the panel's.

    The fake panel stands in for the relay here — what is being proven is that
    the override is honoured and what the relay is sent, not that a relay
    exists. The fake serves no TLS, so the https rule is patched open.
    """
    panel_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        panel_entry,
        options={
            OPT_PUSH_URL: (
                f"http://127.0.0.1:{fake_panel.port}/SimpleDebugger.interface/G."
            ),
            OPT_PUSH_TOKEN: "s3cr3t",
        },
    )
    with patch("custom_components.tuxedo_touch.push.is_https_url", return_value=True):
        await hass.config_entries.async_setup(panel_entry.entry_id)
        await hass.async_block_till_done()
        assert panel_entry.state is ConfigEntryState.LOADED
        await wait_until(lambda: fake_panel.stream_requests > 0)

    # The stream request carried the token both ways and not the panel's
    # session cookie. Asserted unconditionally: a test that skips when the
    # fixture lacks a field is a test that cannot fail.
    seen = fake_panel.last_push_headers
    assert seen, "no stream request reached the configured URL"
    assert seen.get("Authorization") == "Bearer s3cr3t"
    assert seen.get("Cookie") == "tuxweb_token=s3cr3t"
    assert fake_panel.cookie not in seen.get("Cookie", "")


async def test_a_url_without_a_scheme_is_refused(hass: HomeAssistant) -> None:
    """A host typed with no scheme must not be saved.

    It cannot be caught later: an unparseable URL fails inside the stream's
    reconnect loop, which logs at debug and retries for ever, so the form
    would have closed on a green tick over a stream that never starts.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={OPT_PUSH_URL: "10.0.0.5:8081/x", OPT_PUSH_TOKEN: "s3cr3t"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {OPT_PUSH_URL: "invalid_push_url"}
    assert entry.options == {}, "a refused URL must not reach the entry"


async def test_a_refused_form_comes_back_holding_what_was_typed(
    hass: HomeAssistant,
) -> None:
    """Retyping a long URL to add a scheme is how a person gives up on a form."""
    entry = _entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={OPT_PUSH_URL: "10.0.0.5:8081/x", OPT_PUSH_TOKEN: "s3cr3t"},
    )
    suggested = _suggested(result)
    assert suggested[OPT_PUSH_URL] == "10.0.0.5:8081/x"
    # What was typed into the token box is not echoed either.
    assert OPT_PUSH_TOKEN not in suggested


async def test_only_an_https_relay_is_accepted(hass: HomeAssistant) -> None:
    """The relay is sent the push token, which plain http carries in clear text.

    The accepted URL is the positive control: the check refuses http and
    credentials in the URL, not everything.
    """
    for url, accepted in (
        ("HTTPS://relay.example/x", True),
        ("http://relay.example:8081/x", False),
        ("https://user:pw@relay.example/x", False),
        ("https:///x", False),
    ):
        entry = _entry()
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={OPT_PUSH_URL: url, OPT_PUSH_TOKEN: "tok"}
        )
        if accepted:
            assert result["type"] is FlowResultType.CREATE_ENTRY, url
            assert result["data"][OPT_PUSH_URL] == url
        else:
            assert result["type"] is FlowResultType.FORM, url
            assert result["errors"] == {OPT_PUSH_URL: "invalid_push_url"}, url
            assert entry.options == {}


async def test_a_relay_without_a_token_is_refused(hass: HomeAssistant) -> None:
    """A relay is never opened on the panel's credentials in place of a token."""
    entry = _entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={OPT_PUSH_URL: RELAY, OPT_PUSH_TOKEN: ""}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {OPT_PUSH_TOKEN: "push_token_required"}
    assert entry.options == {}


async def test_a_blank_token_keeps_the_stored_one(hass: HomeAssistant) -> None:
    """The box is never filled in, so blank has to mean keep."""
    entry = _entry({OPT_PUSH_URL: RELAY, OPT_PUSH_TOKEN: "stored-token"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={OPT_PUSH_URL: RELAY, OPT_PUSH_TOKEN: ""}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][OPT_PUSH_TOKEN] == "stored-token"


async def test_clearing_the_url_clears_the_token(hass: HomeAssistant) -> None:
    entry = _entry({OPT_PUSH_URL: RELAY, OPT_PUSH_TOKEN: "stored-token"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={OPT_PUSH_URL: "", OPT_PUSH_TOKEN: "typed"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert OPT_PUSH_URL not in result["data"]
    assert OPT_PUSH_TOKEN not in result["data"]


async def test_a_relay_stream_does_not_vouch_for_the_panel(
    hass: HomeAssistant, panel_entry: MockConfigEntry, fake_panel
) -> None:
    """A relay that stays up while the panel dies must not hold the entity
    available on the last state it forwarded.

    `push.connected` becomes true when the stream request returns 200, which
    for a relay says the relay is up and nothing at all about the panel. The
    panel-fed control below is the other half: there the same open stream is
    exactly the evidence the availability rule is entitled to use.
    """
    relay = f"http://127.0.0.1:{fake_panel.port}/SimpleDebugger.interface/G."
    panel_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(panel_entry, options={OPT_PUSH_URL: relay})
    await hass.config_entries.async_setup(panel_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = panel_entry.runtime_data
    assert coordinator.push.from_relay is True
    assert coordinator.data is not None
    # The panel stops answering its poll while the relay socket is up. Both
    # states are set rather than awaited: `connected` follows a live socket
    # that the fake panel drops and reopens, so reading it is a race, and the
    # question here is what panel_available does with those two inputs.
    coordinator.last_update_success = False
    coordinator.push.connected = True
    assert coordinator.panel_available is False, (
        "a relay's open socket was taken as proof the panel is reachable"
    )


async def test_the_panels_own_stream_still_vouches_for_it(
    hass: HomeAssistant, panel_entry: MockConfigEntry, fake_panel
) -> None:
    """The positive control for the test above.

    Without it that test would also pass if availability had simply been
    broken for everyone, which is the failure this pair exists to tell apart.
    """
    panel_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(panel_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = panel_entry.runtime_data
    assert coordinator.push.from_relay is False
    assert coordinator.data is not None
    # Same two inputs as the test above, set the same way and for the same
    # reason. The only difference between the pair is where the stream comes
    # from, which is what makes this a control rather than a second case.
    coordinator.last_update_success = False
    coordinator.push.connected = True
    assert coordinator.panel_available is True, (
        "the panel's own live stream is evidence the panel is reachable"
    )


async def test_the_options_flow_is_the_reloading_kind() -> None:
    """Structural guard on the base class, so the effect test below has a reason.

    Home Assistant reloads an entry after an options flow only when the flow is
    an OptionsFlowWithReload. Swapping the base back would make saving the form
    a silent no-op, and this says so at the point of the change.
    """
    assert issubclass(TuxedoTouchOptionsFlow, OptionsFlowWithReload)


async def test_saving_the_form_takes_effect_without_a_restart(
    hass: HomeAssistant, panel_entry: MockConfigEntry, fake_panel
) -> None:
    """The one that matters: save the form on a LOADED entry and the running
    stream must be the one described by what was just saved.

    The coordinator reads both options once, when it is constructed, and the
    integration registers no update listener. So this passes only because the
    options flow reloads the entry. Under a plain OptionsFlow the save still
    succeeds, the values still land in entry.options, and the stream carries on
    talking to the panel until Home Assistant is restarted.
    """
    panel_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(panel_entry.entry_id)
    await hass.async_block_till_done()
    assert panel_entry.state is ConfigEntryState.LOADED
    assert panel_entry.runtime_data.push._push_url is None, "precondition: on the panel"

    relay = f"https://127.0.0.1:{fake_panel.port}/SimpleDebugger.interface/G."
    result = await hass.config_entries.options.async_init(panel_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={OPT_PUSH_URL: relay, OPT_PUSH_TOKEN: "s3cr3t"},
    )
    await hass.async_block_till_done()

    assert panel_entry.state is ConfigEntryState.LOADED
    assert panel_entry.runtime_data.push._push_url == relay, (
        "the entry did not reload, so the saved push source never took effect"
    )
    assert panel_entry.runtime_data.push._push_token == "s3cr3t"
