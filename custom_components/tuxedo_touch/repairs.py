"""Fix flows for the repair issues this integration raises.

Two are fixable from here: the HTTPS redirect, and a tuxweb panel whose
certificate no longer verifies against the authority the entry trusts. The
other two carry their explanation and no flow, because neither is something
Home Assistant can put right on its own:

- a duplicate entry needs the user to choose which of the two to remove;
- rejected credentials need credentials only the user has, and that issue
  exists to say why Home Assistant must not go on guessing at them. It rides
  alongside the reauthentication card rather than replacing it - the card
  takes the new password, this carries the warning the card has no room for,
  and it appears even where async_start_reauth_if_available could start no
  card at all.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import PanelCertificate, TuxedoTouchClient, TuxedoTouchError
from .const import (
    CONF_PANEL_CA,
    CONF_TUXWEB_TOKEN,
    CONF_USE_HTTPS,
    DEFAULT_PORT_HTTP,
    DEFAULT_PORT_HTTPS,
    ISSUE_HTTPS_REDIRECT,
    ISSUE_PANEL_CERTIFICATE,
)


class _EntryRepairFlow(RepairsFlow):
    """A fix flow about one config entry, which may be gone by the time it runs."""

    def __init__(self, entry_id: str | None) -> None:
        self._entry_id = entry_id

    def _entry(self) -> ConfigEntry[Any] | None:
        # The entry can be removed while the issue stands: nothing to fix
        # then, and writing to a stale id would be worse than saying so.
        if not self._entry_id:
            return None
        return self.hass.config_entries.async_get_entry(self._entry_id)

    async def _async_store_and_reload(
        self, entry: ConfigEntry[Any], data: dict[str, Any]
    ) -> RepairsFlowResult:
        self.hass.config_entries.async_update_entry(entry, data=data)
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_create_entry(data={})


class HttpsRedirectRepairFlow(_EntryRepairFlow):
    """Turn the entry's scheme over to HTTPS, which is the whole fix.

    The panel redirects every plain-HTTP API call to HTTPS while "Secured Web
    Server Access" is enabled, and the redirect names no other cause.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")

        if user_input is not None:
            data = {**entry.data, CONF_USE_HTTPS: True}
            # Only the plain-HTTP default is moved. A user running HTTPS on a
            # port of their own keeps it.
            if data[CONF_PORT] == DEFAULT_PORT_HTTP:
                data[CONF_PORT] = DEFAULT_PORT_HTTPS
            return await self._async_store_and_reload(entry, data)

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "title": entry.title,
                "port": str(entry.data[CONF_PORT]),
            },
        )


class PanelCertificateRepairFlow(_EntryRepairFlow):
    """Show the certificate the panel presents now, and trust it on confirmation.

    The panel is asked the same question as at first use - the capability
    GET, on the legacy context, with no login - by a client holding no pin,
    so it learns the authority the panel presents and proves it by repeating
    the GET with a context that verifies against it. What it found decides
    the step:

    - a tuxweb panel whose authority verifies its certificate: shown, and
      pinned on confirmation;
    - a panel declaring no capabilities, which has gone back to stock:
      confirming removes the pin, and the entry talks to it as stock;
    - a tuxweb panel whose authority does not verify its certificate: aborted,
      since there is nothing that could be pinned.
    """

    _found: PanelCertificate

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")
        session = async_create_clientsession(self.hass, auto_cleanup=False)
        try:
            client = TuxedoTouchClient(
                session=session,
                host=entry.data[CONF_HOST],
                port=entry.data[CONF_PORT],
                use_https=entry.data[CONF_USE_HTTPS],
                username=entry.data[CONF_USERNAME],
                password=entry.data[CONF_PASSWORD],
                tuxweb_token=entry.data.get(CONF_TUXWEB_TOKEN),
            )
            await client.async_probe_capabilities()
        except TuxedoTouchError:
            return self.async_abort(reason="cannot_connect")
        finally:
            # detach(), not close(): see config_flow._validate_input.
            session.detach()
        if not client.tuxweb:
            return await self.async_step_remove_pin()
        found = client.presented_certificate
        if client.panel_ca is None or found is None:
            return self.async_abort(reason="certificate_unverifiable")
        self._found = found
        return await self.async_step_accept()

    async def async_step_accept(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")
        if user_input is not None:
            return await self._async_store_and_reload(
                entry, {**entry.data, CONF_PANEL_CA: self._found.anchor_pem}
            )
        return self.async_show_form(
            step_id="accept",
            data_schema=vol.Schema({}),
            description_placeholders={
                "title": entry.title,
                "subject": self._found.subject,
                "issuer": self._found.issuer,
                "fingerprint": self._found.fingerprint,
                "expires": self._found.leaf_expires or "",
            },
        )

    async def async_step_remove_pin(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")
        if user_input is not None:
            return await self._async_store_and_reload(
                entry, {k: v for k, v in entry.data.items() if k != CONF_PANEL_CA}
            )
        return self.async_show_form(
            step_id="remove_pin",
            data_schema=vol.Schema({}),
            description_placeholders={"title": entry.title},
        )


# Which flow fixes which issue. The issue id is the key, then the entry id.
FIX_FLOWS: dict[str, Callable[[str | None], RepairsFlow]] = {
    ISSUE_HTTPS_REDIRECT: HttpsRedirectRepairFlow,
    ISSUE_PANEL_CERTIFICATE: PanelCertificateRepairFlow,
}


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Home Assistant asks for the flow that repairs one issue."""
    entry_id = data.get("entry_id") if data else None
    entry = str(entry_id) if entry_id else None
    for key, flow in FIX_FLOWS.items():
        if issue_id.startswith(f"{key}_"):
            return flow(entry)
    return HttpsRedirectRepairFlow(entry)
