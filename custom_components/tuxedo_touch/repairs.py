"""Fix flows for the repair issues this integration raises.

Only the HTTPS redirect is fixable from here. The other two carry their
explanation and no flow, because neither is something Home Assistant can put
right on its own:

- a duplicate entry needs the user to choose which of the two to remove;
- rejected credentials need credentials only the user has, and that issue
  exists to say why Home Assistant must not go on guessing at them. It rides
  alongside the reauthentication card rather than replacing it - the card
  takes the new password, this carries the warning the card has no room for,
  and it appears even where async_start_reauth_if_available could start no
  card at all.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.const import CONF_PORT
from homeassistant.core import HomeAssistant

from .const import CONF_USE_HTTPS, DEFAULT_PORT_HTTP, DEFAULT_PORT_HTTPS


class HttpsRedirectRepairFlow(RepairsFlow):
    """Turn the entry's scheme over to HTTPS, which is the whole fix.

    The panel redirects every plain-HTTP API call to HTTPS while "Secured Web
    Server Access" is enabled, and the redirect names no other cause.
    """

    def __init__(self, entry_id: str | None) -> None:
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        entry = (
            self.hass.config_entries.async_get_entry(self._entry_id)
            if self._entry_id
            else None
        )
        if entry is None:
            # The entry was removed while the issue stood: nothing to fix, and
            # writing to a stale id would be worse than saying so.
            return self.async_abort(reason="entry_gone")

        if user_input is not None:
            data = {**entry.data, CONF_USE_HTTPS: True}
            # Only the plain-HTTP default is moved. A user running HTTPS on a
            # port of their own keeps it.
            if data[CONF_PORT] == DEFAULT_PORT_HTTP:
                data[CONF_PORT] = DEFAULT_PORT_HTTPS
            self.hass.config_entries.async_update_entry(entry, data=data)
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "title": entry.title,
                "port": str(entry.data[CONF_PORT]),
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Home Assistant asks for the flow that repairs one issue."""
    entry_id = data.get("entry_id") if data else None
    return HttpsRedirectRepairFlow(str(entry_id) if entry_id else None)
