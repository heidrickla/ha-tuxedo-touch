"""Downloadable diagnostics."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_MAC
from .coordinator import TuxedoTouchConfigEntry

# The host is on the user's LAN and the credentials open their alarm panel.
REDACT = {CONF_PASSWORD, CONF_USERNAME, CONF_HOST, CONF_MAC, "code"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TuxedoTouchConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    status = coordinator.data
    return {
        "config": async_redact_data(dict(entry.data), REDACT),
        "partition": coordinator.partition,
        "update_interval": str(coordinator.update_interval),
        "last_update_success": coordinator.last_update_success,
        # The raw strings the panel returned - the last good ones, since a
        # `Not available` answer fails the poll rather than being stored.
        # That answer is a firmware quirk rather than a fault, and is the
        # usual reason the entity is unavailable while the panel is up, so
        # `last_update_success` beside them is the first thing worth seeing
        # in a bug report.
        "panel_status": status.status if status else None,
        "panel_color": status.color if status else None,
    }
