"""The ECP link entity: the one signal an automation can notify on.

A problem binary sensor over the coordinator's dead-link latch, against the
fake panel in its tuxweb mode - the firmware that declares panel_link_state.
The stock control sits alongside: the capability is absent there, the entity
is not created, and the reason is written in binary_sensor.py.

The latch itself is driven and asserted in tests/ha/test_push.py; what is
pinned here is the entity over it - created or not, on or off, and available
only while something is watching the link.
"""

from unittest.mock import patch

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import ATTR_DEVICE_CLASS, EntityCategory
from homeassistant.helpers import entity_registry as er

from custom_components.tuxedo_touch.api import TuxedoTouchError
from custom_components.tuxedo_touch.const import (
    CAP_PANEL_LINK_STATE,
    OPT_PUSH_TOKEN,
    OPT_PUSH_URL,
)
from tests.fake_panel import TUXWEB_TOKEN, status_frame, wait_until

ECP_LINK = "binary_sensor.honeywell_tuxedo_touch_ecp_link"
PANEL_OFFLINE = "binary_sensor.honeywell_tuxedo_touch_panel_offline"
KEYPAD = "sensor.honeywell_tuxedo_touch_keypad_display"
PANEL = "alarm_control_panel.honeywell_tuxedo_touch_partition_1"
STATUS = "custom_components.tuxedo_touch.api.TuxedoTouchClient.get_status"


async def _setup(hass, entry, *, wait_for_stream=True):
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    if wait_for_stream:
        await wait_until(lambda: coordinator.push.connected)
    return coordinator


def _state(hass, entity_id=ECP_LINK):
    return hass.states.get(entity_id)


def _domains_for(hass, entry):
    """Every entity domain the entry created a registry row in."""
    rows = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    return {row.domain for row in rows}


# --------------------------------------------------------------- creation


async def test_a_tuxweb_declaring_the_link_gets_a_diagnostic_problem_sensor(
    hass, fake_tuxweb, tuxweb_entry
):
    """Created because the panel declares panel_link_state, and off while the
    stream is up and no frame has said the link is down: an "off" here is a
    checked answer, which is the only kind a problem sensor may give."""
    coordinator = await _setup(hass, tuxweb_entry)
    assert CAP_PANEL_LINK_STATE in coordinator.client.capabilities

    state = _state(hass)
    assert state is not None
    assert state.state == "off"
    assert state.attributes[ATTR_DEVICE_CLASS] == BinarySensorDeviceClass.PROBLEM
    row = er.async_get(hass).async_get(ECP_LINK)
    assert row is not None
    assert row.entity_category is EntityCategory.DIAGNOSTIC
    assert row.unique_id == f"{tuxweb_entry.entry_id}_ecp_link"
    assert "binary_sensor" in _domains_for(hass, tuxweb_entry)


async def test_a_tuxweb_that_does_not_declare_the_link_gets_no_entity(
    hass, fake_tuxweb, tuxweb_entry
):
    """The capability decides, not the firmware. A tuxweb that declared
    everything but panel_link_state made no promise about the marker, and
    the entity is not created on a promise never made."""
    fake_tuxweb.capabilities = ["command_result", "status_refresh"]
    coordinator = await _setup(hass, tuxweb_entry)
    assert coordinator.client.tuxweb is True
    assert CAP_PANEL_LINK_STATE not in coordinator.client.capabilities

    assert _state(hass) is None
    # The platform itself still loads: the panel-offline sensor is created
    # on every firmware (tests/ha/test_panel_offline.py), so what is missing
    # is this entity, not the binary_sensor domain.
    assert _state(hass, PANEL_OFFLINE) is not None


async def test_stock_firmware_gets_no_link_entity(hass, fake_panel, panel_entry):
    """Not created on stock, on purpose. The same -1 does arrive there, as a
    side effect the decoder reads and the coordinator acts on - the alarm
    entity still goes unavailable and the diagnostics download still says
    ecp_link_down - but nothing on stock promises it, and a problem sensor
    reading "off" because the promise was never made looks exactly like one
    reading "off" because the link is fine. The keypad sensor and the
    panel-offline sensor are the controls: both are created on stock, so
    what is missing is this entity and not the platforms."""
    coordinator = await _setup(hass, panel_entry)
    assert coordinator.client.tuxweb is False
    assert coordinator.client.capabilities == frozenset()

    assert _state(hass) is None
    assert _domains_for(hass, panel_entry) == {
        "alarm_control_panel",
        "sensor",
        "binary_sensor",
    }
    assert _state(hass, KEYPAD) is not None
    assert _state(hass, PANEL_OFFLINE) is not None


# ------------------------------------------------------------------ state


async def test_the_entity_turns_on_when_the_panel_says_the_link_is_down(
    hass, fake_tuxweb, tuxweb_entry
):
    """A frame carrying the panel-status code -1 is the Tuxedo saying it has
    lost the ECP bus to the VISTA. The alarm entity goes unavailable for the
    same frame; this entity is the one that says why, and the one an
    automation can act on. Off again on the next frame with a real code."""
    coordinator = await _setup(hass, tuxweb_entry)
    await fake_tuxweb.push_status_text("Armed Away", armed=True)
    await wait_until(lambda: _state(hass, PANEL).state == "armed_away")
    assert _state(hass).state == "off"

    await fake_tuxweb.push(status_frame("Armed Away", armed=True, status_code=-1))
    await wait_until(lambda: _state(hass).state == "on")

    assert coordinator.ecp_link_down is True
    assert _state(hass, PANEL).state == "unavailable"
    # The stream is up and the poll is fine: this is exactly the moment the
    # entity exists for, and it must not be unavailable then.
    assert coordinator.push.connected
    assert coordinator.last_update_success

    await fake_tuxweb.push_status_text("Ready To Arm", armed=False)
    await wait_until(lambda: _state(hass).state == "off")

    assert coordinator.ecp_link_down is False
    assert _state(hass, PANEL).state == "disarmed"


async def test_the_entity_is_unavailable_while_nothing_is_watching_the_link(
    hass, fake_tuxweb, tuxweb_entry
):
    """Only the stream can see the link. With the stream down, "off" would be
    "checked, fine" about a link nobody is checking, so the entity reads
    unavailable until the stream is back - and the poll succeeding
    throughout changes nothing, because the poll cannot see the link."""
    coordinator = await _setup(hass, tuxweb_entry)
    assert _state(hass).state == "off"

    # A reconnect would otherwise wait out the five-second floor first.
    coordinator.push.reconnect_wait = 0.01
    fake_tuxweb.push_status = 500
    fake_tuxweb.drop_stream()
    await wait_until(lambda: not coordinator.push.connected)
    await hass.async_block_till_done()

    assert _state(hass).state == "unavailable"
    assert coordinator.last_update_success

    fake_tuxweb.push_status = 200
    await wait_until(lambda: coordinator.push.connected)
    await hass.async_block_till_done()

    assert _state(hass).state == "off"


async def test_the_entity_starts_unavailable_on_a_stream_that_never_opens(
    hass, fake_tuxweb, tuxweb_entry
):
    """The entry loads on the poll while the stream is refused, and until a
    stream is up nothing has looked at the link."""
    fake_tuxweb.push_status = 500
    coordinator = await _setup(hass, tuxweb_entry, wait_for_stream=False)

    assert coordinator.last_update_success
    assert coordinator.push.connected is False
    assert _state(hass).state == "unavailable"


async def test_a_relay_stream_needs_the_poll_answering_too(
    hass, fake_tuxweb, tuxweb_entry
):
    """A relay's open socket is a statement about the relay. When the stream
    is somebody else's, the panel's poll has to be answering as well before
    the link is counted as watched - a relay that stays up while the panel
    dies behind it would otherwise hold this entity on whatever it last
    forwarded. The panel-fed control is the test below."""
    relay = f"http://127.0.0.1:{fake_tuxweb.port}/SimpleDebugger.interface/G."
    tuxweb_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        tuxweb_entry, options={OPT_PUSH_URL: relay, OPT_PUSH_TOKEN: TUXWEB_TOKEN}
    )
    await hass.config_entries.async_setup(tuxweb_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = tuxweb_entry.runtime_data
    await wait_until(lambda: coordinator.push.connected)
    assert coordinator.push.from_relay is True
    assert _state(hass).state == "off"

    with patch(STATUS, side_effect=TuxedoTouchError("panel busy")):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert coordinator.push.connected, "the relay socket is still open"
    assert _state(hass).state == "unavailable"

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert _state(hass).state == "off"


async def test_the_panels_own_stream_watches_the_link_through_a_failed_poll(
    hass, fake_tuxweb, tuxweb_entry
):
    """The control for the relay test: the same failed poll under the panel's
    own stream leaves the link watched, because that socket is the panel's."""
    coordinator = await _setup(hass, tuxweb_entry)
    assert coordinator.push.from_relay is False

    with patch(STATUS, side_effect=TuxedoTouchError("panel busy")):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert _state(hass).state == "off"
