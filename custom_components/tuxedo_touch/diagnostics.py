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
    push = coordinator.push
    status = coordinator.data
    return {
        "config": async_redact_data(dict(entry.data), REDACT),
        "partition": coordinator.partition,
        # The fallback poll: its interval, and whether it last worked.
        "update_interval": str(coordinator.update_interval),
        "last_update_success": coordinator.last_update_success,
        # Which source produced the status below, and the strings the panel
        # spelled it with. `source` first, because the two sources fail
        # differently and the rest of the report cannot be read without
        # knowing which was speaking: `stream` is the panel reporting itself,
        # `poll` the fallback status read, `assumed` a command the panel
        # accepted that neither source has confirmed yet.
        # The strings are the last good ones - a `Not available` answer fails
        # the poll rather than being stored. That answer is a firmware quirk
        # rather than a fault, and it is why a poll-only install can sit
        # unavailable while the panel is up; the stream cannot produce it, so
        # the block below is what says whether that is the situation.
        "status_source": status.source if status else None,
        "panel_status": status.status if status else None,
        "panel_color": status.color if status else None,
        # The primary source's own account of itself. `unsupported` is the
        # firmware having answered 404 - permanent, and the whole reason an
        # install would be back on the poll alone. `frames` separates a
        # stream that is up and silent from one that never carried anything,
        # and `reconnect_wait` says how far a failing one has backed off.
        # None of it is user data, so none of it is redacted.
        "push": {
            "connected": push.connected,
            "unsupported": push.unsupported,
            "connection_id": push.connection_id,
            "client_count": push.client_count,
            "frames": push.frames,
            "reconnect_wait": push.reconnect_wait,
        },
    }
