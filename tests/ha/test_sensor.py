"""The keypad display entity: the panel's own words, as Home Assistant sees them.

Driven through the fake panel's console records over a real socket - the
id-20 record and its three id -1 copies per LCD change, in the shape the
vendor's type-20 handler was read to produce and tuxweb repeats. The decoder
is pinned in tests/test_push_frames.py; what is pinned here is the entity
over it: the joined state, the attributes, the 255-character guard, and an
availability that follows the stream rather than the poll.
"""

from unittest.mock import patch

from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er

from custom_components.tuxedo_touch.api import TuxedoTouchError
from tests.fake_panel import wait_until

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


def _state(hass, entity_id=KEYPAD):
    return hass.states.get(entity_id)


async def _push_console(coordinator, panel, line_1, line_2):
    """One LCD change - four parts - read to the end by the stream."""
    frames = coordinator.push.frames
    await panel.push_console(line_1, line_2)
    await wait_until(lambda: coordinator.push.frames >= frames + 4)


# ----------------------------------------------------------------- shape


async def test_the_entity_is_diagnostic_and_unknown_until_the_panel_draws(
    hass, fake_tuxweb, tuxweb_entry
):
    """Available as soon as the stream is up - the stream is what carries
    the text - and unknown rather than a guess until a console record has
    arrived on it."""
    await _setup(hass, tuxweb_entry)

    state = _state(hass)
    assert state is not None
    assert state.state == "unknown"
    assert "line_1" not in state.attributes
    row = er.async_get(hass).async_get(KEYPAD)
    assert row is not None
    assert row.entity_category is EntityCategory.DIAGNOSTIC
    assert row.unique_id == f"{tuxweb_entry.entry_id}_keypad_display"


async def test_a_console_record_becomes_the_state_and_the_lines_the_attributes(
    hass, fake_tuxweb, tuxweb_entry
):
    """The two LCD lines joined with one space, whitespace collapsed, as the
    state; each line stripped as its own attribute; and the record as it
    arrived, so a line the decoder read wrongly can be seen as the panel
    spelled it. The LCD pads its lines to their width, which is what the
    padding here stands for."""
    coordinator = await _setup(hass, tuxweb_entry)

    await _push_console(
        coordinator, fake_tuxweb, "FAULT 03        ", "FRONT DOOR      "
    )

    state = _state(hass)
    assert state.state == "FAULT 03 FRONT DOOR"
    assert state.attributes["line_1"] == "FAULT 03"
    assert state.attributes["line_2"] == "FRONT DOOR"
    assert state.attributes["raw"] == "0:20:2FAULT 03        |FRONT DOOR      "


async def test_only_the_id_20_record_is_shown_and_it_is_the_mangled_one(
    hass, fake_tuxweb, tuxweb_entry
):
    """Four parts per change and one reading: the id-20 record, whose first
    colon the panel has already replaced with "-". The three id -1 copies
    carry the raw text and are not decoded, so the state shows the mangled
    line rather than flickering between the two spellings."""
    coordinator = await _setup(hass, tuxweb_entry)

    await _push_console(coordinator, fake_tuxweb, "FAULT 03: FRONT", "DOOR OPEN")

    state = _state(hass)
    assert state.state == "FAULT 03- FRONT DOOR OPEN"
    assert state.attributes["line_1"] == "FAULT 03- FRONT"
    assert state.attributes["raw"] == "0:20:2FAULT 03- FRONT|DOOR OPEN"


async def test_console_records_leave_the_alarm_entity_alone(
    hass, fake_tuxweb, tuxweb_entry
):
    """The id -1 copies share their command id with the unsolicited partition
    record, which the alarm entity is fed from. None of the four parts is a
    partition status: the alarm entity keeps its state and its source, and
    the display changes on its own without restarting the poll clock."""
    coordinator = await _setup(hass, tuxweb_entry)
    await fake_tuxweb.push_status_text("Armed Away", armed=True)
    await wait_until(lambda: _state(hass, PANEL).state == "armed_away")
    before = coordinator.data

    await _push_console(coordinator, fake_tuxweb, "ARMED ***AWAY***", "ALL SECURE")

    assert _state(hass).state == "ARMED ***AWAY*** ALL SECURE"
    assert coordinator.data is before
    assert _state(hass, PANEL).state == "armed_away"
    assert _state(hass, PANEL).attributes["tuxedo_source"] == "stream"


async def test_each_repaint_replaces_the_last(hass, fake_tuxweb, tuxweb_entry):
    """The lines a live arm STAY put on the stream, in order: the disarmed
    display, then the exit-delay display repainting every ~2 s with its own
    countdown. Every repaint is a state change, and that is the sensor
    working - it shows the panel's actual words - not a fault to throttle."""
    coordinator = await _setup(hass, tuxweb_entry)
    await _push_console(
        coordinator, fake_tuxweb, "****DISARMED****", "  Ready to Arm  "
    )
    assert _state(hass).state == "****DISARMED**** Ready to Arm"

    for seconds in ("60", "58", "56"):
        await _push_console(
            coordinator, fake_tuxweb, "ARMED ***STAY***", f"May Exit Now  {seconds}"
        )
        assert _state(hass).state == f"ARMED ***STAY*** May Exit Now {seconds}"
        assert _state(hass).attributes["line_2"] == f"May Exit Now  {seconds}"


async def test_an_empty_display_is_unknown_not_an_empty_string(
    hass, fake_tuxweb, tuxweb_entry
):
    """A blank LCD is a record with nothing in either line. The frontend
    shows an empty string as a blank that reads as a fault; unknown is what
    it is."""
    coordinator = await _setup(hass, tuxweb_entry)
    await _push_console(coordinator, fake_tuxweb, "DISARMED CHIME", "Ready to Arm")
    assert _state(hass).state == "DISARMED CHIME Ready to Arm"

    await _push_console(coordinator, fake_tuxweb, "    ", "    ")

    assert _state(hass).state == "unknown"
    assert _state(hass).attributes["line_1"] == ""
    assert _state(hass).attributes["raw"] == "0:20:2    |    "


async def test_a_state_longer_than_home_assistant_allows_is_cut_not_dropped(
    hass, fake_tuxweb, tuxweb_entry
):
    """Home Assistant refuses a state over 255 characters and writes unknown
    in its place, with an error in the log - which would look like the
    sensor silently dying. A two-line LCD cannot reach that, so this drives
    the guard with a record no panel would send: the state is the first 255
    characters and the full text stays in the attributes."""
    coordinator = await _setup(hass, tuxweb_entry)
    line_1, line_2 = "A" * 200, "B" * 200

    await _push_console(coordinator, fake_tuxweb, line_1, line_2)

    state = _state(hass)
    assert state.state == f"{line_1} {line_2}"[:255]
    assert len(state.state) == 255
    assert state.attributes["line_1"] == line_1
    assert state.attributes["line_2"] == line_2
    assert state.attributes["raw"] == f"0:20:2{line_1}|{line_2}"


# ----------------------------------------------------------- availability


async def test_the_entity_follows_the_stream_not_the_poll(
    hass, fake_tuxweb, tuxweb_entry
):
    """The text has no other source. When the stream drops the last line is
    stale by definition, so the entity reads unavailable rather than holding
    it - while the poll goes on succeeding, which keeps the alarm entity up
    and says nothing about the LCD - and unknown once the stream is back,
    until the panel draws again."""
    coordinator = await _setup(hass, tuxweb_entry)
    await _push_console(coordinator, fake_tuxweb, "DISARMED CHIME", "Ready to Arm")
    assert _state(hass).state == "DISARMED CHIME Ready to Arm"

    # A reconnect would otherwise wait out the five-second floor first.
    coordinator.push.reconnect_wait = 0.01
    fake_tuxweb.push_status = 500
    fake_tuxweb.drop_stream()
    await wait_until(lambda: not coordinator.push.connected)
    await hass.async_block_till_done()

    assert _state(hass).state == "unavailable"
    assert coordinator.keypad_display is None
    assert coordinator.last_update_success
    assert _state(hass, PANEL).state != "unavailable"

    fake_tuxweb.push_status = 200
    await wait_until(lambda: coordinator.push.connected)
    await hass.async_block_till_done()

    assert _state(hass).state == "unknown"
    assert "line_1" not in _state(hass).attributes

    await _push_console(coordinator, fake_tuxweb, "DISARMED", "Ready to Arm")
    assert _state(hass).state == "DISARMED Ready to Arm"


async def test_a_failed_poll_does_not_take_the_entity_away(
    hass, fake_tuxweb, tuxweb_entry
):
    """The other half of the same rule: the poll carries none of this text,
    so its failing is no evidence about the line the stream is carrying."""
    coordinator = await _setup(hass, tuxweb_entry)
    await _push_console(coordinator, fake_tuxweb, "DISARMED CHIME", "Ready to Arm")

    with patch(STATUS, side_effect=TuxedoTouchError("panel busy")):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert _state(hass).state == "DISARMED CHIME Ready to Arm"


# ----------------------------------------------------------------- stock


async def test_stock_firmware_gets_the_entity_and_reads_the_same_record(
    hass, fake_panel, panel_entry
):
    """One per entry on either firmware. The record shape is the vendor's -
    tuxweb repeats it - so nothing gates the entity; what differs is when
    the panel sends any, which is why it reads unknown until one arrives."""
    coordinator = await _setup(hass, panel_entry)
    assert coordinator.client.tuxweb is False
    assert _state(hass).state == "unknown"

    await _push_console(coordinator, fake_panel, "FAULT 03", "FRONT DOOR")

    assert _state(hass).state == "FAULT 03 FRONT DOOR"
    assert _state(hass).attributes["line_2"] == "FRONT DOOR"
