"""The panel-offline entity: the VISTA reporting itself not online.

A second problem binary sensor beside the ECP link, over the coordinator's
latch on command-22 status records. Two independent facts ride a status
frame - the TYPE says whether the VISTA calls itself online, the CODE says
whether the Tuxedo can hear it - and the four corners of that are the table
in docs/feature-spec-keypad-link-changedby.md section 4, pinned here in full:

    frame                                  link sensor   offline sensor
    0:22:<flag>1Ready To Arm:3             off           on
    0:22:<flag>1Ready To Arm:-1            on            on
    0:21:-1:fe:<flag>1Ready To Arm:2       on            off
    the -1 copies                          unchanged     unchanged

NO CAPTURE HOLDS A 22 OR A -1. This panel has never been offline while
anything was recording; the fake's frames are the shapes read out of the
vendor's handler (tests/fake_panel.py, offline_frames) and reproduced by
tuxweb from v16. Until 0.6.0 every 22 was dropped at _handle_frame, so the
condition was invisible and the -1 a 22 can carry never reached the link
latch either.
"""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import ATTR_DEVICE_CLASS, EntityCategory
from homeassistant.helpers import entity_registry as er

from custom_components.tuxedo_touch.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.fake_panel import status_frame, wait_until

ECP_LINK = "binary_sensor.honeywell_tuxedo_touch_ecp_link"
PANEL_OFFLINE = "binary_sensor.honeywell_tuxedo_touch_panel_offline"
PANEL = "alarm_control_panel.honeywell_tuxedo_touch_partition_1"


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    await wait_until(lambda: coordinator.push.connected)
    return coordinator


def _state(hass, entity_id=PANEL_OFFLINE):
    return hass.states.get(entity_id)


# --------------------------------------------------------------- creation


async def test_created_on_tuxweb_as_a_diagnostic_problem_sensor(
    hass, fake_tuxweb, tuxweb_entry
):
    coordinator = await _setup(hass, tuxweb_entry)
    state = _state(hass)
    assert state is not None
    assert state.state == "off"
    assert state.attributes[ATTR_DEVICE_CLASS] == BinarySensorDeviceClass.PROBLEM
    assert "panel_online_status" not in state.attributes
    row = er.async_get(hass).async_get(PANEL_OFFLINE)
    assert row is not None
    assert row.entity_category is EntityCategory.DIAGNOSTIC
    assert row.unique_id == f"{tuxweb_entry.entry_id}_panel_offline"
    assert coordinator.panel_offline is False
    assert coordinator.panel_offline_code is None


async def test_created_on_stock_too(hass, fake_panel, panel_entry):
    """Unlike the link sensor. The 22 is the vendor's own producer answering
    the VISTA's own online byte, put on the wire by stock and tuxweb alike,
    so there is no capability to gate on and an "off" is a checked answer
    on either firmware."""
    coordinator = await _setup(hass, panel_entry)
    assert coordinator.client.tuxweb is False
    assert _state(hass) is not None
    assert _state(hass).state == "off"
    assert _state(hass, ECP_LINK) is None, "the link sensor stays tuxweb-only"


# ------------------------------------------------------- the four corners


async def test_a_22_with_a_real_code_is_offline_with_the_link_fine(
    hass, fake_tuxweb, tuxweb_entry
):
    """Row 1: the VISTA says it is not online (3, in its busy / downloading /
    offline range) and the Tuxedo is still hearing it. The status text is the
    panel's real prompt and is written through like a 21's."""
    coordinator = await _setup(hass, tuxweb_entry)
    await fake_tuxweb.push_status_text("Ready To Arm", armed=False)
    await wait_until(lambda: _state(hass, PANEL).state == "disarmed")

    await fake_tuxweb.push_offline("Armed Away", armed=True, status_code=3)
    await wait_until(lambda: _state(hass).state == "on")

    assert _state(hass, ECP_LINK).state == "off"
    assert coordinator.ecp_link_down is False
    assert coordinator.panel_offline_code == 3
    assert _state(hass).attributes["panel_online_status"] == 3
    # written through: the alarm entity follows the 22's text and flag
    assert _state(hass, PANEL).state == "armed_away"

    # a 21 clears it; the copies that followed the 22 did not touch it
    await fake_tuxweb.push_status_text("Ready To Arm", armed=False)
    await wait_until(lambda: _state(hass).state == "off")
    assert coordinator.panel_offline_code is None
    assert "panel_online_status" not in _state(hass).attributes
    assert _state(hass, PANEL).state == "disarmed"


async def test_a_22_carrying_minus_one_is_both_conditions_at_once(
    hass, fake_tuxweb, tuxweb_entry
):
    """Row 2: not online AND not talking. The -1 reaches the link latch from
    a 22 exactly as from a 21, so the alarm entity goes unavailable and the
    link sensor trips, while the offline latch reads the type."""
    coordinator = await _setup(hass, tuxweb_entry)
    await fake_tuxweb.push_status_text("Ready To Arm", armed=False)
    await wait_until(lambda: _state(hass, PANEL).state == "disarmed")

    await fake_tuxweb.push_offline("Ready To Arm", armed=False, status_code=-1)
    await wait_until(
        lambda: _state(hass).state == "on" and _state(hass, ECP_LINK).state == "on"
    )

    assert coordinator.ecp_link_down is True
    assert coordinator.panel_offline is True
    assert coordinator.panel_offline_code == -1
    assert _state(hass, PANEL).state == "unavailable"

    # the recovery is one ordinary 21: both latches clear on it
    await fake_tuxweb.push_status_text("Ready To Arm", armed=False)
    await wait_until(
        lambda: _state(hass).state == "off" and _state(hass, ECP_LINK).state == "off"
    )
    assert _state(hass, PANEL).state == "disarmed"


async def test_a_21_with_minus_one_is_the_link_alone(hass, fake_tuxweb, tuxweb_entry):
    """Row 3: online as far as the VISTA last said, but not talking."""
    coordinator = await _setup(hass, tuxweb_entry)
    await fake_tuxweb.push_status_text("Ready To Arm", armed=False)
    await wait_until(lambda: _state(hass, PANEL).state == "disarmed")

    await fake_tuxweb.push(status_frame("Ready To Arm", armed=False, status_code=-1))
    await wait_until(lambda: _state(hass, ECP_LINK).state == "on")

    assert _state(hass).state == "off"
    assert coordinator.panel_offline is False
    assert coordinator.panel_offline_code is None


async def test_the_copies_move_neither_latch(hass, fake_tuxweb, tuxweb_entry):
    """Row 4: the -1 copies say nothing about either condition. Pinned by
    sending a 22 and then its copies on their own - what push_offline sends
    after the record - and reading both latches unchanged; and the control,
    copies after a 21, which leave both off."""
    coordinator = await _setup(hass, tuxweb_entry)
    await fake_tuxweb.push_status_text("Ready To Arm", armed=False)
    await wait_until(lambda: _state(hass, PANEL).state == "disarmed")

    copy = b"['ud','SimpleDbgServer2ClientIntf','statusMessageText',[\"0:-1:\xfe1Ready To Arm\"]]"

    async def _copy_then_mark(mark: str) -> None:
        # The stream is in order, so a console record after the copy having
        # been decoded proves the copy was read before the assertions run.
        await fake_tuxweb.push(copy)
        await fake_tuxweb.push_console(mark, "x")
        await wait_until(
            lambda: (
                coordinator.keypad_display is not None
                and coordinator.keypad_display.line_1 == mark
            )
        )

    await fake_tuxweb.push_offline("Ready To Arm", armed=False, status_code=2)
    await wait_until(lambda: _state(hass).state == "on")
    await _copy_then_mark("MARK ONE")
    assert coordinator.panel_offline is True
    assert coordinator.ecp_link_down is False
    assert _state(hass).state == "on"

    await fake_tuxweb.push_status_text("Ready To Arm", armed=False)
    await wait_until(lambda: _state(hass).state == "off")
    await _copy_then_mark("MARK TWO")
    assert coordinator.panel_offline is False
    assert coordinator.ecp_link_down is False


# ----------------------------------------------------------- availability


async def test_unavailable_while_the_stream_is_down(hass, fake_tuxweb, tuxweb_entry):
    """The stream is the only carrier of the record, so nothing else can
    vouch for the reading while it is down; the latch itself is kept, as
    the link's is, and the next typed frame moves it."""
    coordinator = await _setup(hass, tuxweb_entry)
    await fake_tuxweb.push_offline("Ready To Arm", armed=False, status_code=4)
    await wait_until(lambda: _state(hass).state == "on")

    # A reconnect would otherwise wait out the five-second floor first.
    coordinator.push.reconnect_wait = 0.01
    fake_tuxweb.push_status = 500
    fake_tuxweb.drop_stream()
    await wait_until(lambda: not coordinator.push.connected)
    await hass.async_block_till_done()
    assert _state(hass).state == "unavailable"
    assert coordinator.panel_offline is True, "the latch is kept across the drop"

    fake_tuxweb.push_status = 200
    await wait_until(lambda: coordinator.push.connected)
    await hass.async_block_till_done()
    assert _state(hass).state == "on", "still on until a typed frame moves it"


# ------------------------------------------------------------ diagnostics


async def test_diagnostics_carry_the_latch_and_the_code(
    hass, fake_tuxweb, tuxweb_entry
):
    coordinator = await _setup(hass, tuxweb_entry)
    report = await async_get_config_entry_diagnostics(hass, tuxweb_entry)
    assert report["panel_offline"] is False
    assert report["panel_offline_code"] is None

    await fake_tuxweb.push_offline("Ready To Arm", armed=False, status_code=2)
    await wait_until(lambda: coordinator.panel_offline)
    report = await async_get_config_entry_diagnostics(hass, tuxweb_entry)
    assert report["panel_offline"] is True
    assert report["panel_offline_code"] == 2
