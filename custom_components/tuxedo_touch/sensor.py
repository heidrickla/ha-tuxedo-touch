"""Sensor platform for Honeywell Tuxedo Touch: the keypad display."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import MAX_LENGTH_STATE_STATE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

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
    async_add_entities([TuxedoKeypadDisplay(entry.runtime_data, entry)])


class TuxedoKeypadDisplay(TuxedoTouchEntity, SensorEntity):
    """The panel's two-line keypad LCD, as its console records carry it.

    The one place the panel says things nothing else exposes: which zone is
    faulted BY NAME, the Check messages, bypass notices, trouble and AC-loss
    text. The alarm entity carries the status word; this carries the words.

    One per entry on either firmware. The record shape is the vendor's and
    tuxweb emits the same one, so nothing gates it; what differs is when the
    panel sends any - tuxweb holds console mode on, the vendor sent them only
    while someone had /console.html open - and until one arrives the sensor
    reads unknown rather than inventing a line.
    """

    _attr_translation_key = "keypad_display"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: TuxedoTouchCoordinator, entry: TuxedoTouchConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_keypad_display"

    @property
    def available(self) -> bool:
        """Available while the stream, the only source of the text, is up.

        Not the alarm entity's rule: a working poll keeps that one available
        and carries none of this text, so under it a line from before a
        stream drop would be shown as current. Unavailable is the honest
        reading while nothing is carrying it; see
        TuxedoTouchCoordinator.stream_observing.
        """
        return self.coordinator.stream_observing

    @property
    def native_value(self) -> str | None:
        display = self.coordinator.keypad_display
        if display is None:
            return None
        # Home Assistant drops a state longer than 255 characters and logs
        # it, which would look like the sensor silently dying. A two-line LCD
        # cannot reach that, but the guard costs nothing: the state is cut
        # and the full text stays in the attributes below. An empty display
        # is unknown rather than an empty string, which the frontend shows as
        # a blank that reads as a fault.
        text = display.text
        if not text:
            return None
        return text[:MAX_LENGTH_STATE_STATE]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        display = self.coordinator.keypad_display
        if display is None:
            return {}
        return {
            "line_1": display.line_1,
            "line_2": display.line_2,
            # The console record exactly as it arrived, so a line the
            # decoder read wrongly can be seen as the panel spelled it.
            "raw": display.raw,
        }
