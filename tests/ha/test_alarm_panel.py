"""The panel entity: status mapping, codes, and optimistic updates."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.tuxedo_touch.api import TuxedoStatus, TuxedoTouchError

STATUS = "custom_components.tuxedo_touch.api.TuxedoTouchClient.get_status"
ARM = "custom_components.tuxedo_touch.api.TuxedoTouchClient.arm"
DISARM = "custom_components.tuxedo_touch.api.TuxedoTouchClient.disarm"

PANEL = "alarm_control_panel.honeywell_tuxedo_touch"


async def _setup(hass, entry, status="Ready To Arm"):
    entry.add_to_hass(hass)
    with patch(STATUS, return_value=TuxedoStatus(status=status, color=None)):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("Ready To Arm", "disarmed"),
        ("Ready Fault", "disarmed"),
        ("Not Ready", "disarmed"),
        ("Armed Stay", "armed_home"),
        ("Armed Away Fault", "armed_away"),
        ("Armed Night", "armed_night"),
        ("Armed Instant", "armed_night"),
        ("Entry Delay Active", "pending"),
        ("Armed Away Alarm", "triggered"),
        ("59  Secs Remaining", "arming"),
        ("1  Secs Remaining", "arming"),
        ("something the firmware never documented", "unknown"),
    ],
)
async def test_each_panel_status_maps_to_one_alarm_state(
    hass, config_entry, reported, expected
):
    """The countdown string carries a double space; matching it loosely
    would also swallow statuses that are not a countdown at all."""
    await _setup(hass, config_entry, reported)
    assert hass.states.get(PANEL).state == expected


async def test_arming_uses_the_stored_code_and_updates_at_once(hass, config_entry):
    """The entity must not wait for a poll that may report "Not available"."""
    await _setup(hass, config_entry)
    assert hass.states.get(PANEL).state == "disarmed"

    with patch(ARM, AsyncMock(return_value={})) as arm:
        await hass.services.async_call(
            ALARM_DOMAIN,
            "alarm_arm_home",
            {ATTR_ENTITY_ID: PANEL},
            blocking=True,
        )
    arm.assert_awaited_once_with("STAY", "1234", 1)
    assert hass.states.get(PANEL).state == "armed_home"


async def test_disarm_reports_disarmed_without_a_poll(hass, config_entry):
    await _setup(hass, config_entry, "Armed Away")
    assert hass.states.get(PANEL).state == "armed_away"

    with patch(DISARM, AsyncMock(return_value={})) as disarm:
        await hass.services.async_call(
            ALARM_DOMAIN,
            "alarm_disarm",
            {ATTR_ENTITY_ID: PANEL},
            blocking=True,
        )
    disarm.assert_awaited_once_with("1234", 1)
    assert hass.states.get(PANEL).state == "disarmed"


async def test_a_code_given_in_the_call_beats_the_stored_one(hass, config_entry):
    await _setup(hass, config_entry)
    with patch(ARM, AsyncMock(return_value={})) as arm:
        await hass.services.async_call(
            ALARM_DOMAIN,
            "alarm_arm_away",
            {ATTR_ENTITY_ID: PANEL, "code": "9999"},
            blocking=True,
        )
    arm.assert_awaited_once_with("AWAY", "9999", 1)


async def test_no_code_anywhere_is_a_validation_error(hass, config_entry_no_code):
    """Wrong error class here shows the user a traceback instead of a reason."""
    await _setup(hass, config_entry_no_code)

    with (
        patch(ARM, AsyncMock(return_value={})),
        pytest.raises(ServiceValidationError),
    ):
        await hass.services.async_call(
            ALARM_DOMAIN,
            "alarm_arm_away",
            {ATTR_ENTITY_ID: PANEL},
            blocking=True,
        )


async def test_a_refused_command_surfaces_as_an_error_not_a_state_change(
    hass, config_entry
):
    """An optimistic update after a failed command would be a lie."""
    await _setup(hass, config_entry)

    with (
        patch(ARM, AsyncMock(side_effect=TuxedoTouchError("panel said no"))),
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call(
            ALARM_DOMAIN,
            "alarm_arm_away",
            {ATTR_ENTITY_ID: PANEL},
            blocking=True,
        )
    assert hass.states.get(PANEL).state == "disarmed"


async def test_a_stored_code_means_the_user_is_not_prompted(hass, config_entry):
    await _setup(hass, config_entry)
    assert hass.states.get(PANEL).attributes["code_arm_required"] is False


async def test_without_a_stored_code_the_user_is_prompted(hass, config_entry_no_code):
    await _setup(hass, config_entry_no_code)
    assert hass.states.get(PANEL).attributes["code_arm_required"] is True


async def test_the_entity_is_unique_per_partition(hass, config_entry):
    await _setup(hass, config_entry)
    from homeassistant.helpers import entity_registry as er

    entity = er.async_get(hass).async_get(PANEL)
    assert entity.unique_id == f"{config_entry.entry_id}_partition_1"
