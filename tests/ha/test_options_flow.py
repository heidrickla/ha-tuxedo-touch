"""The options flow: where the push stream comes from.

Both fields are empty in the supported setup — the panel serves its own stream.
They exist for a firmware that gates the push path on a session and a relay that
holds one upstream subscription for several consumers.

What these tests pin is the part that is easy to get wrong later: blanks must
clear rather than persist as empty strings, the unrelated options key must
survive, and only the stream may move.
"""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuxedo_touch.const import (
    DOMAIN,
    OPT_CREDENTIALS_REJECTED,
    OPT_PUSH_TOKEN,
    OPT_PUSH_URL,
)

from .test_config_flow import ENTRY_DATA, HOST, PORT

RELAY = "http://10.0.0.5:8081/SimpleDebugger.interface/G."


def _entry(options: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Tuxedo Touch",
        unique_id=f"{HOST}:{PORT}:1",
        data=dict(ENTRY_DATA),
        options=dict(options or {}),
    )


async def test_form_opens_with_the_current_values(hass: HomeAssistant) -> None:
    entry = _entry({OPT_PUSH_URL: RELAY})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"


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
        result["flow_id"], user_input={OPT_PUSH_URL: RELAY, OPT_PUSH_TOKEN: ""}
    )
    assert result["data"][OPT_CREDENTIALS_REJECTED] is True
    assert result["data"][OPT_PUSH_URL] == RELAY


async def test_the_entry_loads_and_streams_from_the_relay(
    hass: HomeAssistant, panel_entry: MockConfigEntry, fake_panel
) -> None:
    """End to end through the integration: login on the panel, stream from the
    configured URL, with the token attached.

    The fake panel stands in for the relay here — what is being proven is that
    the override is honoured and the token is sent, not that a relay exists.
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
    await hass.config_entries.async_setup(panel_entry.entry_id)
    await hass.async_block_till_done()
    assert panel_entry.state is ConfigEntryState.LOADED

    # The stream request carried the token both ways. Asserted
    # unconditionally: a test that skips when the fixture lacks a field is a
    # test that cannot fail.
    seen = fake_panel.last_push_headers
    assert seen, "no stream request reached the configured URL"
    assert seen.get("Authorization") == "Bearer s3cr3t"
    assert "tuxweb_token=s3cr3t" in seen.get("Cookie", "")
