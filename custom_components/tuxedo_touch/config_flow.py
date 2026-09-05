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
    ISSUE_CREDENTIALS_REJECTED,
    ISSUE_DUPLICATE_ENTRY,
    OPT_CREDENTIALS_REJECTED,
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


def _holds_the_panel(state: ConfigEntryState) -> bool:
    """Whether this entry has to be stood down before the panel is probed.

    LOADED is the coordinator polling. SETUP_RETRY is the state a user most
    often reconfigures from - the panel moved, the password changed - and it
    is not idle: a retry is on the clock and logs in against the same panel,
    which is the contention this whole path exists to remove. SETUP_ERROR
    holds nothing, but unloading it costs nothing either and keeps the rule
    one sentence long.

    NOT_LOADED is already stood down, and the states that are not recoverable
    (SETUP_IN_PROGRESS, UNLOAD_IN_PROGRESS, FAILED_UNLOAD, MIGRATION_ERROR)
    cannot be unloaded at all - async_unload raises OperationNotAllowed on
    them. Both take the probe as it comes.
    """
    return state.recoverable and state is not ConfigEntryState.NOT_LOADED


def _without_secrets(data: Mapping[str, Any]) -> dict[str, Any]:
    """What may go back to the browser as suggested values: never a secret."""
    return {k: v for k, v in data.items() if k not in SECRETS}


def _needs_a_probe(entry: ConfigEntry[Any], data: Mapping[str, Any]) -> bool:
    """Whether anything the login handshake depends on has changed."""
    return any(data.get(field) != entry.data.get(field) for field in PROBED)


async def _validate_input(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Attempt a real login against the panel; raises on failure.

    A session of its own for its cookie jar, but on Home Assistant's shared
    connector - the same pool an entry's coordinator uses. With one SSLContext
    shared by every client (see api._legacy_ssl_context) that pool key matches
    the coordinator's, so this login lands on the connection the entry left
    behind rather than opening a second one to a panel that serves one at a
    time.
    """
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
        - otherwise the entry is stood down first, which stops the poll loop
          and cancels any pending setup retry, and set up again if the probe
          fails. A probe that succeeds leaves it unloaded for the caller's
          reload, which is the next thing to happen.

        Standing the entry down stops it starting new work and waits for the
        poll it already had in flight (async_unload_entry awaits the
        coordinator's poll lock), so by the time the probe runs nothing of
        ours is mid-request against the panel. What it does not do is close
        the socket that poll last used, which Home Assistant's pool holds as
        idle for fifteen seconds. That socket is the one this probe then uses:
        every client shares an SSLContext, so the pool key matches (see
        api._legacy_ssl_context) and the two take turns on one connection
        instead of the panel seeing a second.

        Not shared with the reauth step, which reaches the same conclusion
        from the other direction: there "unchanged" means the credentials the
        panel has just refused, and re-sending those would spend one of the
        three failed logins that disable its web accounts.
        """
        if not _needs_a_probe(entry, data):
            return {}
        if not _holds_the_panel(entry.state):
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
            probed = _needs_a_probe(entry, merged)
            errors = await self._async_validate_reconfigure(entry, merged)
            if not errors:
                # The title names the host, so it follows a move - unless the
                # user renamed the entry, in which case their name stays.
                title = entry.title
                if title == _title_for(entry.data[CONF_HOST]):
                    title = _title_for(user_input[CONF_HOST])
                # A probe that succeeded is the panel accepting these
                # credentials, so an entry flagged as refused stops being one.
                # Without this the reload below would meet a flag nothing
                # cleared, refuse its own setup, and leave the user correcting
                # a password that was already right. A reconfigure that
                # changed nothing the login depends on probed nothing and
                # proves nothing, so it leaves the flag alone.
                options = dict(entry.options)
                if probed and options.pop(OPT_CREDENTIALS_REJECTED, None):
                    ir.async_delete_issue(
                        self.hass,
                        DOMAIN,
                        issue_id(ISSUE_CREDENTIALS_REJECTED, entry.entry_id),
                    )
                return self.async_update_reload_and_abort(
                    entry,
                    data=merged,
                    unique_id=unique_id,
                    title=title,
                    options=options,
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
        """Take one login attempt from the user, and only from the user.

        Every automatic path has stopped by the time this form appears - the
        poll, the push stream and setup itself all refuse to spend another
        login on credentials the panel has refused - because three refused
        web logins disable the panel's web accounts, and on unpatched
        firmware that is permanent. This step is the one place a login is
        still spent, and it spends at most one per submission:

        - credentials byte-identical to the stored ones are the ones already
          refused, so they are answered from here with no request at all;
        - anything else is a genuinely different guess and is probed once.

        A failed probe on an entry already flagged says so differently: at
        that point the account may be locked rather than the password wrong,
        and the two need different instructions.
        """
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        already_rejected = bool(reauth_entry.options.get(OPT_CREDENTIALS_REJECTED))
        if user_input is not None:
            if all(
                user_input.get(field) == reauth_entry.data.get(field)
                for field in (CONF_USERNAME, CONF_PASSWORD)
            ):
                errors = {"base": "invalid_auth"}
            else:
                errors = await self._async_validate({**reauth_entry.data, **user_input})
                if not errors:
                    ir.async_delete_issue(
                        self.hass,
                        DOMAIN,
                        issue_id(ISSUE_CREDENTIALS_REJECTED, reauth_entry.entry_id),
                    )
                    return self.async_update_reload_and_abort(
                        reauth_entry,
                        data_updates=user_input,
                        # The panel accepts these, so the entry stops being
                        # one whose credentials are refused - and the setup
                        # that the reload runs next reads exactly this.
                        options={
                            key: value
                            for key, value in reauth_entry.options.items()
                            if key != OPT_CREDENTIALS_REJECTED
                        },
                    )
                if already_rejected and errors.get("base") == "invalid_auth":
                    errors = {"base": "possibly_locked_out"}

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_REAUTH_SCHEMA,
                {CONF_USERNAME: reauth_entry.data.get(CONF_USERNAME, "")},
            ),
            description_placeholders={"host": reauth_entry.data[CONF_HOST]},
            errors=errors,
        )
