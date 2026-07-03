"""Config flow for Honeywell Tuxedo Touch."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .api import (
    TuxedoTouchAuthError,
    TuxedoTouchClient,
    TuxedoTouchConnectionError,
)
from .const import (
    CONF_CODE,
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


async def _validate_input(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Attempt a real login against the panel; raises on failure."""
    async with aiohttp.ClientSession() as session:
        client = TuxedoTouchClient(
            session=session,
            host=data[CONF_HOST],
            port=data[CONF_PORT],
            use_https=data[CONF_USE_HTTPS],
            username=data[CONF_USERNAME],
            password=data[CONF_PASSWORD],
        )
        await client.login()


class TuxedoTouchConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Honeywell Tuxedo Touch."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}:{user_input[CONF_PARTITION]}"
            )
            self._abort_if_unique_id_configured()

            try:
                await _validate_input(self.hass, user_input)
            except TuxedoTouchAuthError:
                errors["base"] = "invalid_auth"
            except TuxedoTouchConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating Tuxedo Touch connection")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"Tuxedo Touch ({user_input[CONF_HOST]})", data=user_input
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )
