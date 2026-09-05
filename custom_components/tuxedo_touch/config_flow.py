"""Config flow for Honeywell Tuxedo Touch."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import (
    CONF_CODE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .api import (
    TuxedoTouchAuthError,
    TuxedoTouchClient,
    TuxedoTouchConnectionError,
    TuxedoTouchError,
)
from .const import (
    CONF_MAC,
    CONF_PARTITION,
    CONF_USE_HTTPS,
    DEFAULT_PARTITION,
    DEFAULT_PORT_HTTPS,
    DEFAULT_USE_HTTPS,
    DOMAIN,
    ISSUE_DUPLICATE_ENTRY,
    issue_id,
)
from .identity import build_unique_id

_LOGGER = logging.getLogger(__name__)

_PASSWORD = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)

# The web password and the keypad code are both secrets: masked in the form,
# never given a default and never sent back as a suggested value. A default
# reaches the frontend, where the field can reveal it, and is also applied
# when the user clears the field.
SECRETS = (CONF_PASSWORD, CONF_CODE)

# Everything the login handshake depends on. A reconfigure that changes none
# of these - a different partition, a new keypad code - has nothing to prove
# against the panel, and on this device an unnecessary probe is not free: see
# _async_validate_reconfigure.
PROBED = (CONF_HOST, CONF_PORT, CONF_USE_HTTPS, CONF_USERNAME, CONF_PASSWORD)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT_HTTPS): int,
        vol.Required(CONF_USE_HTTPS, default=DEFAULT_USE_HTTPS): bool,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): _PASSWORD,
        vol.Optional(CONF_CODE): _PASSWORD,
        vol.Optional(CONF_PARTITION, default=DEFAULT_PARTITION): int,
    }
)

# Same fields, but both secrets may be left blank to keep the stored values.
STEP_RECONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT_HTTPS): int,
        vol.Required(CONF_USE_HTTPS, default=DEFAULT_USE_HTTPS): bool,
        vol.Required(CONF_USERNAME): str,
        vol.Optional(CONF_PASSWORD): _PASSWORD,
        vol.Optional(CONF_CODE): _PASSWORD,
        vol.Optional(CONF_PARTITION, default=DEFAULT_PARTITION): int,
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): _PASSWORD,
    }
)

# The discovery confirm step: the lease supplies the address, so the address is
# the one field this form does not ask for. The port and the HTTPS toggle stay
# on it because a lease says nothing about the unit's web server, and a panel
# with "Secured Web Server Access" turned off answers on 80 - without these two
# fields such a panel could be discovered and then never set up.
STEP_DHCP_CONFIRM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PORT, default=DEFAULT_PORT_HTTPS): int,
        vol.Required(CONF_USE_HTTPS, default=DEFAULT_USE_HTTPS): bool,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): _PASSWORD,
        vol.Optional(CONF_CODE): _PASSWORD,
        vol.Optional(CONF_PARTITION, default=DEFAULT_PARTITION): int,
    }
)


def _title_for(host: str) -> str:
    return f"Tuxedo Touch ({host})"


def _without_secrets(data: Mapping[str, Any]) -> dict[str, Any]:
    """What may go back to the browser as suggested values: never a secret."""
    return {k: v for k, v in data.items() if k not in SECRETS}


async def _validate_input(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Attempt a real login against the panel; raises on failure."""
    session = async_create_clientsession(hass, auto_cleanup=False)
    try:
        client = TuxedoTouchClient(
            session=session,
            host=data[CONF_HOST],
            port=data[CONF_PORT],
            use_https=data[CONF_USE_HTTPS],
            username=data[CONF_USERNAME],
            password=data[CONF_PASSWORD],
        )
        await client.login()
    finally:
        # detach(), not close(): helper-created sessions share HA's connector
        # pool, so HA replaces close() with a warn-and-no-op wrapper. detach()
        # releases the session without touching the shared connector.
        session.detach()


class TuxedoTouchConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Honeywell Tuxedo Touch."""

    VERSION = 1

    # Set by async_step_dhcp and read by its confirm step. Declared rather
    # than assigned in __init__ so mypy sees the types without this class
    # taking over ConfigFlow's construction.
    _discovered_host: str
    _discovered_mac: str

    async def _async_validate(self, data: dict[str, Any]) -> dict[str, str]:
        """Validate credentials against the panel, returning form errors."""
        try:
            await _validate_input(self.hass, data)
        except TuxedoTouchAuthError:
            return {"base": "invalid_auth"}
        except TuxedoTouchConnectionError:
            return {"base": "cannot_connect"}
        except TuxedoTouchError as err:
            # The panel answered, but not usefully - e.g. the login page came
            # back without the Random/RandomID headers, or the key blob was
            # short. That is neither "wrong password" nor "unreachable", and
            # lumping it into "unknown" hides a diagnosable condition: the
            # message itself names the problem, so surface it.
            _LOGGER.error("Tuxedo Touch setup failed: %s", err)
            return {"base": "panel_error"}
        except Exception:
            _LOGGER.exception("Unexpected error validating Tuxedo Touch connection")
            return {"base": "unknown"}
        return {}

    async def _async_validate_reconfigure(
        self, entry: ConfigEntry[Any], data: dict[str, Any]
    ) -> dict[str, str]:
        """Validate a reconfigure without fighting this entry for the panel.

        The unit serves ONE connection at a time and contention presents as a
        hang rather than a refusal (see docs/tuxedo_touch_api_notes.md), so a
        probe run while this entry's coordinator is polling can time out
        against a panel that is answering perfectly and report cannot_connect
        on every attempt. Two things keep the probe out of the poller's way:

        - nothing the login depends on changed, so there is nothing to probe.
          The entry itself is the check on those fields - a loaded one is
          already logging in with them, and one that is failing to set up
          reports why on its own card.
        - otherwise the entry is unloaded first, which stops the poll loop and
          releases its session, and set up again if the probe fails. A probe
          that succeeds leaves it unloaded for the caller's reload, which is
          the next thing to happen.

        Not shared with the reauth step: there "unchanged" means the password
        the panel has just rejected, which is exactly what has to be probed.
        """
        if all(data.get(field) == entry.data.get(field) for field in PROBED):
            return {}
        if entry.state is not ConfigEntryState.LOADED:
            return await self._async_validate(data)

        unloaded = await self.hass.config_entries.async_unload(entry.entry_id)
        restore = True
        try:
            errors = await self._async_validate(data)
            restore = bool(errors)
            return errors
        finally:
            if unloaded and restore:
                self.hass.config_entries.async_schedule_reload(entry.entry_id)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # Unique ids are a mixed namespace (MAC-based for a panel that was
            # discovered, address-based for one typed in here), so string
            # equality alone misses a same-panel re-add. Matching on the stored
            # connection data closes that hole before any network traffic.
            self._async_abort_entries_match(
                {
                    CONF_HOST: user_input[CONF_HOST],
                    CONF_PORT: user_input[CONF_PORT],
                    CONF_PARTITION: user_input[CONF_PARTITION],
                }
            )
            errors = await self._async_validate(user_input)
            if not errors:
                data = dict(user_input)
                # An emptied code field must not be stored as a code.
                if not data.get(CONF_CODE):
                    data.pop(CONF_CODE, None)
                # A panel typed in by hand carries no MAC: the panel reports
                # none over its API and only a DHCP lease has one. The entry
                # gains it the first time Home Assistant sees a lease for the
                # address it holds - see async_step_dhcp.
                await self.async_set_unique_id(
                    build_unique_id(
                        None,
                        user_input[CONF_HOST],
                        user_input[CONF_PORT],
                        user_input[CONF_PARTITION],
                    )
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=_title_for(user_input[CONF_HOST]), data=data
                )

        # After an error the non-secret fields come back filled in; the
        # secrets are typed again.
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, _without_secrets(user_input or {})
            ),
            errors=errors,
        )

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """A DHCP lease from a Tuxedo Touch panel.

        Two matchers reach here. `{"hostname": "tux*", "macaddress": "00D02D*"}`
        is the panel itself: measured on the unit at 00:d0:2d:4d:d7:b6
        (2026-09-05), 00:D0:2D is Resideo's OUI and the unit puts `Tux` plus
        the twelve hex digits of its own MAC in the lease. `registered_devices`
        adds every MAC this integration already has a device for, so a panel
        whose lease hostname was changed still follows a move.

        Three things can be true of the lease, in this order:

        - it belongs to a panel already set up, which has just moved;
        - it belongs to a panel set up by hand, which has been identified by
          address until now and can adopt the MAC;
        - it belongs to a panel nobody has added, which is offered for setup.
        """
        mac = format_mac(discovery_info.macaddress)
        host = discovery_info.ip
        entries = self._async_current_entries(include_ignore=False)

        # One panel can hold several entries (one per partition), all of which
        # move together, so every entry carrying this MAC is corrected.
        known = [entry for entry in entries if entry.data.get(CONF_MAC) == mac]
        for entry in known:
            self._async_follow_move(entry, host)
        # An entry made from the user step is keyed on the address, because
        # nothing there can learn the MAC. This lease is the first thing that
        # can, so the entry upgrades in place; entities key off entry_id, not
        # the unique id, so nothing is orphaned by the change.
        adopting = [
            entry
            for entry in entries
            if not entry.data.get(CONF_MAC) and entry.data[CONF_HOST] == host
        ]
        for entry in adopting:
            self._async_adopt_mac(entry, mac)
        if known or adopting:
            return self.async_abort(reason="already_configured")

        # A panel nobody has set up. The partition is not known until the user
        # picks one, so the MAC alone is the flow's id for now; it stops a
        # second lease from opening a second form for the same panel.
        await self.async_set_unique_id(mac)
        # A panel the user chose to ignore holds an entry keyed on this MAC and
        # was filtered out of the scans above, which pass include_ignore=False.
        # Without this the next lease renewal would raise the discovery card
        # again. No updates= here: an ignore entry carries no host to correct,
        # and a configured panel's host has already been followed above.
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"name": _title_for(host)}
        self._discovered_host = host
        self._discovered_mac = mac
        return await self.async_step_dhcp_confirm()

    @callback
    def _async_follow_move(self, entry: ConfigEntry[Any], host: str) -> None:
        """Point a configured entry at the address its lease now holds."""
        if entry.data[CONF_HOST] == host:
            return
        # The title names the host, so it follows the move - unless the user
        # renamed the entry, in which case their name stays.
        title = entry.title
        if title == _title_for(entry.data[CONF_HOST]):
            title = _title_for(host)
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_HOST: host}, title=title
        )
        self.hass.config_entries.async_schedule_reload(entry.entry_id)

    @callback
    def _async_adopt_mac(self, entry: ConfigEntry[Any], mac: str) -> None:
        """Move an address-identified entry onto the panel's MAC."""
        unique_id = build_unique_id(
            mac,
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            entry.data.get(CONF_PARTITION, DEFAULT_PARTITION),
        )
        holder = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, unique_id
        )
        if holder is not None and holder.entry_id != entry.entry_id:
            # Two entries reaching the same panel and partition by different
            # addresses or ports. Taking the id would corrupt the unique-id
            # index; keep the address identity and tell the user which entry
            # is the duplicate. Only the user can decide which of the two to
            # remove, so the issue is not fixable from here.
            _LOGGER.warning(
                "Not adopting MAC identity for %s: config entry %s already is %s",
                entry.title,
                holder.title,
                unique_id,
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id(ISSUE_DUPLICATE_ENTRY, entry.entry_id),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_DUPLICATE_ENTRY,
                translation_placeholders={"title": entry.title, "other": holder.title},
            )
            return
        ir.async_delete_issue(
            self.hass, DOMAIN, issue_id(ISSUE_DUPLICATE_ENTRY, entry.entry_id)
        )
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_MAC: mac}, unique_id=unique_id
        )
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        _LOGGER.debug(
            "Panel identity is now its MAC rather than %s", entry.data[CONF_HOST]
        )

    async def async_step_dhcp_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for what a DHCP lease cannot supply, then create the entry."""
        errors: dict[str, str] = {}
        host = self._discovered_host
        mac = self._discovered_mac
        if user_input is not None:
            data = {**user_input, CONF_HOST: host, CONF_MAC: mac}
            errors = await self._async_validate(data)
            if not errors:
                # An emptied code field must not be stored as a code.
                if not data.get(CONF_CODE):
                    data.pop(CONF_CODE, None)
                await self.async_set_unique_id(
                    build_unique_id(mac, host, data[CONF_PORT], data[CONF_PARTITION])
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=_title_for(host), data=data)

        return self.async_show_form(
            step_id="dhcp_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_DHCP_CONFIRM_SCHEMA, _without_secrets(user_input or {})
            ),
            description_placeholders={"host": host, "mac": mac},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the panel's address or partition without re-adding the entry."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            # A blank secret keeps the stored one. The stored values are never
            # shown on the form, so blank is the ordinary way to leave them be.
            merged = dict(user_input)
            for secret in SECRETS:
                if not merged.get(secret):
                    if entry.data.get(secret):
                        merged[secret] = entry.data[secret]
                    else:
                        merged.pop(secret, None)
            # Nothing on this form can learn the panel's MAC - only a DHCP
            # lease has one - so a reconfigure neither gains nor loses it.
            # The stored identity is carried across unchanged, which is
            # what makes an address change a move rather than a new panel.
            if mac := entry.data.get(CONF_MAC):
                merged[CONF_MAC] = mac
            unique_id = build_unique_id(
                merged.get(CONF_MAC),
                user_input[CONF_HOST],
                user_input[CONF_PORT],
                user_input[CONF_PARTITION],
            )
            # Settled before the probe: a form that cannot be saved should not
            # cost the panel a login, and on a loaded entry buying that login
            # means stopping the poller for it.
            if any(
                other.entry_id != entry.entry_id and other.unique_id == unique_id
                for other in self._async_current_entries()
            ):
                return self.async_abort(reason="already_configured")
            errors = await self._async_validate_reconfigure(entry, merged)
            if not errors:
                # The title names the host, so it follows a move - unless the
                # user renamed the entry, in which case their name stays.
                title = entry.title
                if title == _title_for(entry.data[CONF_HOST]):
                    title = _title_for(user_input[CONF_HOST])
                return self.async_update_reload_and_abort(
                    entry, data=merged, unique_id=unique_id, title=title
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_RECONFIGURE_SCHEMA, _without_secrets(user_input or entry.data)
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth after the panel rejected the stored credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None:
            errors = await self._async_validate({**reauth_entry.data, **user_input})
            if not errors:
                return self.async_update_reload_and_abort(
                    reauth_entry, data_updates=user_input
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_REAUTH_SCHEMA,
                {CONF_USERNAME: reauth_entry.data.get(CONF_USERNAME, "")},
            ),
            description_placeholders={"host": reauth_entry.data[CONF_HOST]},
            errors=errors,
        )
