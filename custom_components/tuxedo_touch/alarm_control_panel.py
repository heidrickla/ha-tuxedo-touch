"""Alarm control panel platform for Honeywell Tuxedo Touch."""
from __future__ import annotations

import logging

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TuxedoTouchCoordinator
from .api import TuxedoStatus
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Status strings observed from GetSecurityStatus. Panel-reported "Secs
# Remaining"/countdown variants and anything unrecognized fall back to None
# (unknown) rather than guessing.
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
    "Entry Delay Active": AlarmControlPanelState.PENDING,
    "Not Ready Alarm": AlarmControlPanelState.TRIGGERED,
    "Armed Stay Alarm": AlarmControlPanelState.TRIGGERED,
    "Armed Night Alarm": AlarmControlPanelState.TRIGGERED,
    "Armed Away Alarm": AlarmControlPanelState.TRIGGERED,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TuxedoTouchCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TuxedoAlarmPanel(coordinator, entry)])


class TuxedoAlarmPanel(CoordinatorEntity[TuxedoTouchCoordinator], AlarmControlPanelEntity):
    """Represents one Tuxedo Touch partition."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_code_format = CodeFormat.NUMBER
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )

    def __init__(self, coordinator: TuxedoTouchCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_partition_{coordinator.partition}"
        # A code is required unless one is stored in config for automations
        # to use without prompting.
        self._attr_code_arm_required = not bool(entry.data.get("code"))
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Honeywell Tuxedo Touch",
            manufacturer="Honeywell",
            model="Tuxedo Touch WIFI",
            configuration_url=(
                f"{'https' if entry.data.get('use_https') else 'http'}"
                f"://{entry.data[CONF_HOST]}"
            ),
        )

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        status = self.coordinator.data
        if status is None:
            return None
        return STATUS_MAP.get(status.status)

    @property
    def extra_state_attributes(self) -> dict:
        status = self.coordinator.data
        if status is None:
            return {}
        return {"tuxedo_status": status.status, "tuxedo_color": status.color}

    def _resolve_code(self, code: str | None) -> str:
        resolved = code or self._entry.data.get("code")
        if not resolved:
            raise ValueError("No code provided and none configured")
        return resolved

    def _set_optimistic_status(self, status: str) -> None:
        """Push a locally-known status immediately after a command succeeds.

        Works around the GetSecurityStatus "Not available" quirk (see
        _async_update_data in __init__.py): rather than waiting on a poll
        that may never report the real state, reflect the command we just
        successfully sent right away. async_set_updated_data also reschedules
        the next automatic poll, which will still correct this if the panel
        ever reports something conflicting.
        """
        color = self.coordinator.data.color if self.coordinator.data else None
        self.coordinator.async_set_updated_data(TuxedoStatus(status=status, color=color))

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self.coordinator.client.disarm(self._resolve_code(code), self.coordinator.partition)
        self._set_optimistic_status("Ready To Arm")

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self.coordinator.client.arm(
            "STAY", self._resolve_code(code), self.coordinator.partition
        )
        self._set_optimistic_status("Armed Stay")

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self.coordinator.client.arm(
            "AWAY", self._resolve_code(code), self.coordinator.partition
        )
        self._set_optimistic_status("Armed Away")

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        await self.coordinator.client.arm(
            "NIGHT", self._resolve_code(code), self.coordinator.partition
        )
        self._set_optimistic_status("Armed Night")
