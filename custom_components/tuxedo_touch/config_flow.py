"""Config flow for Honeywell Tuxedo Touch."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import (
    CONF_CODE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

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
)
from .identity import async_panel_mac, build_unique_id

_LOGGER = logging.getLogger(__name__)

_PASSWORD = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)

# The web password and the keypad code are both secrets: masked in the form,
# never given a default and never sent back as a suggested value. A default
# reaches the frontend, where the field can reveal it, and is also applied
# when the user clears the field.
SECRETS = (CONF_PASSWORD, CONF_CODE)

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

    async def _with_identity(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """The submitted data plus the panel's MAC, when the network knows it."""
        data = dict(user_input)
        mac = await async_panel_mac(self.hass, user_input[CONF_HOST])
        if mac:
            data[CONF_MAC] = mac
        return data

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # Unique ids are a mixed namespace (MAC-based and address-based),
            # so string equality alone misses a same-panel re-add when only
            # one side has a resolved MAC. Matching on the stored connection
            # data closes that hole before any network traffic.
            self._async_abort_entries_match(
                {
                    CONF_HOST: user_input[CONF_HOST],
                    CONF_PORT: user_input[CONF_PORT],
                    CONF_PARTITION: user_input[CONF_PARTITION],
                }
            )
            # Validate first: the login populates the ARP entry the MAC lookup
            # then reads. Ordering it the other way leaves a fresh host
            # unresolvable and silently falls back to an address identity.
            errors = await self._async_validate(user_input)
            if not errors:
                data = await self._with_identity(user_input)
                # An emptied code field must not be stored as a code.
                if not data.get(CONF_CODE):
                    data.pop(CONF_CODE, None)
                await self.async_set_unique_id(
                    build_unique_id(
                        data.get(CONF_MAC),
                        user_input[CONF_HOST],
                        user_input[CONF_PORT],
                        user_input[CONF_PARTITION],
                    )
                )
                # A re-add of a known panel at a fresh address heals the
                # existing entry instead of silently doing nothing - the MAC
                # identity exists precisely so a moved panel is recognised.
                self._abort_if_unique_id_configured(
                    updates={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input[CONF_PORT],
                        CONF_USE_HTTPS: user_input[CONF_USE_HTTPS],
                    }
                )
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
            errors = await self._async_validate(merged)
            if not errors:
                data = await self._with_identity(merged)
                mac = data.get(CONF_MAC)
                known = entry.data.get(CONF_MAC)
                # Only the MAC says which panel this is. Comparing whole unique
                # ids would reject an address change - the thing this step
                # exists to do - and a partition change, which is ordinary
                # reconfiguration rather than a different device.
                if mac and known and mac != known:
                    return self.async_abort(reason="another_panel")
                if not mac and known:
                    # The lookup failed this time. Keep the identity we already
                    # proved rather than quietly demoting to an address.
                    data[CONF_MAC] = known
                unique_id = build_unique_id(
                    data.get(CONF_MAC),
                    user_input[CONF_HOST],
                    user_input[CONF_PORT],
                    user_input[CONF_PARTITION],
                )
                if any(
                    other.entry_id != entry.entry_id and other.unique_id == unique_id
                    for other in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                # The title names the host, so it follows a move - unless the
                # user renamed the entry, in which case their name stays.
                title = entry.title
                if title == _title_for(entry.data[CONF_HOST]):
                    title = _title_for(user_input[CONF_HOST])
                return self.async_update_reload_and_abort(
                    entry, data=data, unique_id=unique_id, title=title
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
