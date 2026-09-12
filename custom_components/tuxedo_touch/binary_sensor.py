"""Binary sensor platform for Honeywell Tuxedo Touch: the ECP link."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CAP_PANEL_LINK_STATE
from .coordinator import TuxedoTouchConfigEntry, TuxedoTouchCoordinator
from .entity import TuxedoTouchEntity

# Read-only, and fed by the coordinator: nothing here calls the panel, so
# there is nothing to serialise.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TuxedoTouchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if CAP_PANEL_LINK_STATE not in coordinator.client.capabilities:
        # Not created on stock, and the reason is what a problem sensor's
        # "off" means. On tuxweb the panel DECLARES panel_link_state: the
        # dead-link marker is produced on purpose, so an "off" here is a
        # checked answer - the link was looked at and it is up. On stock the
        # same -1 does arrive, as a side effect of the vendor's producer that
        # the decoder happens to read, and the coordinator acts on it there
        # too; but nothing on stock promises it, and a problem sensor that is
        # off because the promise was never made reads exactly like one that
        # is off because the link is fine. "Checked, fine" on a panel that
        # made no such promise is worse than no sensor. The diagnostics
        # download still reports ecp_link_down on every firmware.
        return
    async_add_entities([TuxedoEcpLink(coordinator, entry)])


class TuxedoEcpLink(TuxedoTouchEntity, BinarySensorEntity):
    """Whether the Tuxedo has lost its ECP link to the alarm panel behind it.

    On while the coordinator's latch says the link is down - every frame
    carrying the panel-status code -1 beside the last text the Tuxedo drew -
    and off once a frame with a real code clears it. The alarm entity goes
    unavailable for the same condition; this is the entity an automation can
    notify on, which an unavailable alarm is not.

    No second entity for the stream's relay_unreachable flag, deliberately.
    It is a different condition, so the two would not always agree - but it
    is a property of an optional relay rather than of the panel, applies to
    no default install, and would sit permanently off on every panel-fed
    entry, which is the "checked, fine" reading this platform refuses to
    give above. When a relay is down this entity reads unavailable, which is
    the honest answer, and the log names the relay once.
    """

    _attr_translation_key = "ecp_link"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: TuxedoTouchCoordinator, entry: TuxedoTouchConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ecp_link"

    @property
    def available(self) -> bool:
        """Available while the stream, the only observer of the link, is up.

        Not the alarm entity's rule: that one goes unavailable ON a dead link,
        which is the one moment this entity exists to report. See
        TuxedoTouchCoordinator.stream_observing.
        """
        return self.coordinator.stream_observing

    @property
    def is_on(self) -> bool:
        return self.coordinator.ecp_link_down
