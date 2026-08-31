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
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import (
    TuxedoTouchAuthError,
    TuxedoTouchClient,
    TuxedoTouchConnectionError,
    TuxedoTouchError,
)
from .const import (
    CONF_PARTITION,
    CONF_USE_HTTPS,
    DEFAULT_PARTITION,
    DEFAULT_PORT_HTTPS,
    DEFAULT_USE_HTTPS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT_HTTPS): int,
        vol.Required(CONF_USE_HTTPS, default=DEFAULT_USE_HTTPS): bool,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_CODE): str,
        vol.Optional(CONF_PARTITION, default=DEFAULT_PARTITION): int,
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


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


# `domain=` is real on Home Assistant's ConfigFlow; it only looks wrong when
# HA is absent and the base class degrades to `object`.
class TuxedoTouchConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}:{user_input[CONF_PARTITION]}"
            )
            self._abort_if_unique_id_configured()

            errors = await self._async_validate(user_input)
            if not errors:
                return self.async_create_entry(
                    title=f"Tuxedo Touch ({user_input[CONF_HOST]})", data=user_input
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the panel's address or partition without re-adding the entry."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            errors = await self._async_validate(user_input)
            if not errors:
                unique_id = (
                    f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
                    f":{user_input[CONF_PARTITION]}"
                )
                # The unique id is built from the address because the panel
                # reports no serial, so a moved panel changes it legitimately.
                # Comparing it against this entry's own previous id would
                # reject every address change - the thing this step exists to
                # do. Only a clash with a DIFFERENT entry is a real problem.
                if any(
                    other.entry_id != entry.entry_id and other.unique_id == unique_id
                    for other in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                return self.async_update_reload_and_abort(
                    entry, data=user_input, unique_id=unique_id
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, {**entry.data, **(user_input or {})}
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
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={"host": reauth_entry.data[CONF_HOST]},
            errors=errors,
        )
