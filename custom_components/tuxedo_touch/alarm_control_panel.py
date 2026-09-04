"""Alarm control panel platform for Honeywell Tuxedo Touch."""

from __future__ import annotations

import re
from collections.abc import Coroutine
from typing import Any

from homeassistant.components.alarm_control_panel import AlarmControlPanelEntity
from homeassistant.components.alarm_control_panel.const import (
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.const import CONF_CODE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import TuxedoTouchError
from .const import CONF_MAC, DOMAIN
from .coordinator import TuxedoTouchConfigEntry, TuxedoTouchCoordinator

# The panel is a fragile embedded web server with per-session crypto state;
# serialize entity service calls so concurrent arm/disarm from automations
# can't interleave against it (coordinator polling is already centralized,
# and the API client additionally locks its login sequence).
PARALLEL_UPDATES = 1

# Status strings observed from GetSecurityStatus. The exit-delay countdown
# is matched separately below (SECS_REMAINING_RE); anything else
# unrecognized falls back to None (unknown) rather than guessing.
STATUS_MAP: dict[str, AlarmControlPanelState] = {
    "Ready To Arm": AlarmControlPanelState.DISARMED,
    "Ready Fault": AlarmControlPanelState.DISARMED,
    "Not Ready": AlarmControlPanelState.DISARMED,
    "Not Ready Fault": AlarmControlPanelState.DISARMED,
    "Armed Stay": AlarmControlPanelState.ARMED_HOME,
    "Armed Stay Fault": AlarmControlPanelState.ARMED_HOME,
    "Armed Away": AlarmControlPanelState.ARMED_AWAY,
    "Armed Away Fault": AlarmControlPanelState.ARMED_AWAY,
    "Armed Night": AlarmControlPanelState.ARMED_NIGHT,
    "Armed Night Fault": AlarmControlPanelState.ARMED_NIGHT,
    "Armed Instant": AlarmControlPanelState.ARMED_NIGHT,
    "Armed Instant Fault": AlarmControlPanelState.ARMED_NIGHT,
    "Armed Instant Alarm": AlarmControlPanelState.TRIGGERED,
    "Entry Delay Active": AlarmControlPanelState.PENDING,
    "Not Ready Alarm": AlarmControlPanelState.TRIGGERED,
    "Armed Stay Alarm": AlarmControlPanelState.TRIGGERED,
    "Armed Night Alarm": AlarmControlPanelState.TRIGGERED,
    "Armed Away Alarm": AlarmControlPanelState.TRIGGERED,
}

# Exit-delay countdown while arming, e.g. "59  Secs Remaining" (note the
# double space - format confirmed against real hardware polling through a
# full Arm Stay exit delay).
SECS_REMAINING_RE = re.compile(r"^\d+\s+Secs Remaining$")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TuxedoTouchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([TuxedoAlarmPanel(entry.runtime_data, entry)])


class TuxedoAlarmPanel(
    CoordinatorEntity[TuxedoTouchCoordinator], AlarmControlPanelEntity
):
    """Represents one Tuxedo Touch partition."""

    _attr_has_entity_name = True
    # Named after the partition, not the device: two partition entries on one
    # panel share a device (merged on the MAC connection) and would otherwise
    # be two entities with the same name.
    _attr_translation_key = "partition"
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )

    def __init__(
        self, coordinator: TuxedoTouchCoordinator, entry: TuxedoTouchConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        # No partition suffix: the entry's OWN unique id already carries the
        # partition, and a partition change is a reconfigure of the same entry
        # - a suffix here orphaned the registry row on every such change.
        self._attr_unique_id = entry.entry_id
        self._attr_translation_placeholders = {"partition": str(coordinator.partition)}
        # A code is required unless one is stored in config for automations
        # to use without prompting. code_format follows the same logic: with a
        # stored code the dashboard must not demand one for disarm either.
        stored_code = bool(entry.data.get(CONF_CODE))
        self._attr_code_arm_required = not stored_code
        self._attr_code_format = None if stored_code else CodeFormat.NUMBER
        mac = entry.data.get(CONF_MAC)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            # Ties the panel to the same device the router reports, so it does
            # not appear twice in the device list.
            connections={(CONNECTION_NETWORK_MAC, mac)} if mac else set(),
            name="Honeywell Tuxedo Touch",
            manufacturer="Honeywell",
            model="Tuxedo Touch WIFI",
            configuration_url=coordinator.client.base_url,
        )

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        status = self.coordinator.data
        if status is None:
            return None
        if (state := STATUS_MAP.get(status.status)) is not None:
            return state
        if SECS_REMAINING_RE.match(status.status.strip()):
            return AlarmControlPanelState.ARMING
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self.coordinator.data
        if status is None:
            return {}
        return {"tuxedo_status": status.status, "tuxedo_color": status.color}

    def _resolve_code(self, code: str | None) -> str:
        resolved = code or self._entry.data.get(CONF_CODE)
        if not resolved:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_code"
            )
        return resolved

    async def _async_command(
        self, command: Coroutine[Any, Any, dict[str, Any]], optimistic_status: str
    ) -> None:
        """Run one panel command, then reflect it in the entity immediately.

        The optimistic update works around the GetSecurityStatus
        "Not available" quirk - see TuxedoTouchCoordinator.set_optimistic_status.
        """
        try:
            await command
        except TuxedoTouchError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.coordinator.set_optimistic_status(optimistic_status)

    async def _async_arm(
        self, mode: str, optimistic_status: str, code: str | None
    ) -> None:
        await self._async_command(
            self.coordinator.client.arm(
                mode, self._resolve_code(code), self.coordinator.partition
            ),
            optimistic_status,
        )

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self._async_command(
            self.coordinator.client.disarm(
                self._resolve_code(code), self.coordinator.partition
            ),
            "Ready To Arm",
        )

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self._async_arm("STAY", "Armed Stay", code)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._async_arm("AWAY", "Armed Away", code)

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        await self._async_arm("NIGHT", "Armed Night", code)
