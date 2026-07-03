"""The Honeywell Tuxedo Touch integration."""
from __future__ import annotations

import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TuxedoStatus, TuxedoTouchAuthError, TuxedoTouchClient, TuxedoTouchError
from .const import (
    CONF_PARTITION,
    CONF_USE_HTTPS,
    DEFAULT_PARTITION,
    DOMAIN,
    SCAN_INTERVAL,
    STATUS_NOT_AVAILABLE,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.ALARM_CONTROL_PANEL]


class TuxedoTouchCoordinator(DataUpdateCoordinator[TuxedoStatus]):
    """Polls panel status and owns the API client + its dedicated HTTP session.

    A dedicated aiohttp session (rather than Home Assistant's shared one) is
    used because this device needs a non-default SSLContext (legacy TLS
    renegotiation, no cert verification) that can't be layered onto the
    shared session per-request in aiohttp.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.entry = entry
        self.partition = entry.data.get(CONF_PARTITION, DEFAULT_PARTITION)
        self.session = aiohttp.ClientSession()
        self.client = TuxedoTouchClient(
            session=self.session,
            host=entry.data[CONF_HOST],
            port=entry.data[CONF_PORT],
            use_https=entry.data[CONF_USE_HTTPS],
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
        )

    async def _async_update_data(self) -> TuxedoStatus:
        try:
            status = await self.client.get_status(self.partition)
        except TuxedoTouchAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except TuxedoTouchError as err:
            raise UpdateFailed(str(err)) from err

        # Quirk workaround: this firmware intermittently - and on at least one
        # unit, persistently - reports "Not available" from GetSecurityStatus
        # even though arm/disarm commands are still reaching the panel fine
        # (confirmed by comparing against a separate ECP-bus-based alarm
        # integration on the same panel, which tracked the real state
        # correctly while this endpoint stayed stuck). Treat "Not available"
        # as "no new information" rather than a real status: keep whatever we
        # last knew (including optimistic updates set immediately after a
        # successful arm/disarm - see alarm_control_panel.py) instead of
        # clobbering good data with this placeholder every poll.
        if status.status == STATUS_NOT_AVAILABLE and self.data is not None:
            _LOGGER.debug(
                "GetSecurityStatus returned 'Not available' - keeping last "
                "known status (%s) instead of overwriting it",
                self.data.status,
            )
            return self.data

        return status

    async def async_close(self) -> None:
        await self.session.close()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = TuxedoTouchCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: TuxedoTouchCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_close()
    return unload_ok
