"""The base every entity of this integration shares: one device, one name rule."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MAC, DOMAIN
from .coordinator import TuxedoTouchConfigEntry, TuxedoTouchCoordinator


class TuxedoTouchEntity(CoordinatorEntity[TuxedoTouchCoordinator]):
    """An entity of the one device a config entry is: the panel.

    Subclasses set their own unique id and translation key. The alarm panel's
    unique id is the bare entry id for a historical reason given there; the
    entities added later suffix it.
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TuxedoTouchCoordinator, entry: TuxedoTouchConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        mac = entry.data.get(CONF_MAC)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            # The MAC records which physical panel this is. Since HA 2026.8 a
            # device belongs to one config entry, so a second partition entry
            # gets its own device carrying the same connection, not a merge.
            connections={(CONNECTION_NETWORK_MAC, mac)} if mac else set(),
            name="Honeywell Tuxedo Touch",
            manufacturer="Honeywell",
            model="Tuxedo Touch WIFI",
            configuration_url=coordinator.client.base_url,
        )
