"""Binary sensor platform for Honeywell Tuxedo Touch: the ECP link and the panel."""

from __future__ import annotations

from typing import Any

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
    # The panel-offline sensor is created on BOTH firmwares. The command-22
    # record it reads is the vendor's own producer answering the VISTA's own
    # online byte, put on the wire by stock and by tuxweb alike, so there is
    # no capability to gate on and none of the promise-never-made problem the
    # link sensor below has: an "off" here is the VISTA saying it is online.
    entities: list[BinarySensorEntity] = [TuxedoPanelOffline(coordinator, entry)]
    if CAP_PANEL_LINK_STATE in coordinator.client.capabilities:
        # The link sensor is NOT created on stock, and the reason is what a
        # problem sensor's "off" means. On tuxweb the panel DECLARES
        # panel_link_state: the dead-link marker is produced on purpose, so
        # an "off" here is a checked answer - the link was looked at and it
        # is up. On stock the same -1 does arrive, as a side effect of the
        # vendor's producer that the decoder happens to read, and the
        # coordinator acts on it there too; but nothing on stock promises
        # it, and a problem sensor that is off because the promise was never
        # made reads exactly like one that is off because the link is fine.
        # "Checked, fine" on a panel that made no such promise is worse than
        # no sensor. The diagnostics download still reports ecp_link_down on
        # every firmware.
        entities.append(TuxedoEcpLink(coordinator, entry))
    async_add_entities(entities)


class TuxedoPanelOffline(TuxedoTouchEntity, BinarySensorEntity):
    """Whether the alarm panel behind the Tuxedo reports itself as not online.

    On while the latest status frame was a command-22 record - the VISTA
    answering the Tuxedo's status poll with an online byte other than 1, its
    busy, downloading and offline family - and off once a 21 arrives again.
    Not the ECP link: that sensor says whether the Tuxedo can HEAR the panel,
    this one what the panel SAYS about itself, and the producer sets the two
    independently, so a panel can be talking and offline (a 22 with a real
    code) or silent and, as far as anyone knew, online (a 21 with -1). An
    automation that wants "the alarm is not fully in service" wants both.

    Stated plainly: no capture from this panel holds a 22. The record's
    shape was read out of the vendor's handler and reproduced by tuxweb from
    v16, and the vendor firmware has emitted it since long before this
    integration; what has not happened is the VISTA going offline while
    anyone was recording. The raw code the frame carried is in diagnostics.
    """

    _attr_translation_key = "panel_offline"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: TuxedoTouchCoordinator, entry: TuxedoTouchConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_panel_offline"

    @property
    def available(self) -> bool:
        """Available while the stream, the only carrier of the record, is up."""
        return self.coordinator.stream_observing

    @property
    def is_on(self) -> bool:
        return self.coordinator.panel_offline

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # The VISTA's own online byte from the last 22: 2..4 by construction,
        # or -1 when the ECP link was down at the same time. Absent while the
        # panel reports itself online.
        code = self.coordinator.panel_offline_code
        return {} if code is None else {"panel_online_status": code}


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
