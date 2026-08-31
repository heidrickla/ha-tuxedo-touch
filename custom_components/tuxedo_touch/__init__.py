"""The Honeywell Tuxedo Touch integration."""

from __future__ import annotations

import logging
import time

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TuxedoStatus, TuxedoTouchAuthError, TuxedoTouchClient, TuxedoTouchError
from .const import (
    CONF_MAC,
    CONF_PARTITION,
    CONF_USE_HTTPS,
    DEFAULT_PARTITION,
    DOMAIN,
    SCAN_INTERVAL,
    STATUS_NOT_AVAILABLE,
)
from .identity import async_panel_mac, build_unique_id

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.ALARM_CONTROL_PANEL]

type TuxedoTouchConfigEntry = ConfigEntry[TuxedoTouchCoordinator]


class TuxedoTouchCoordinator(DataUpdateCoordinator[TuxedoStatus]):
    """Polls panel status and owns the API client + its dedicated HTTP session.

    A dedicated aiohttp session (rather than Home Assistant's shared one) is
    used for cookie isolation - the panel sets a session cookie with a random
    name per login - and so the connector below can be tuned for this device.
    (The client's custom SSLContext is passed per-request and would work on
    any session; it is not the reason for the dedicated session.)
    """

    def __init__(self, hass: HomeAssistant, entry: TuxedoTouchConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
            always_update=False,
        )
        self.partition = entry.data.get(CONF_PARTITION, DEFAULT_PARTITION)
        # keep-alive must outlive SCAN_INTERVAL: aiohttp's default (15s) would
        # guarantee a fresh TCP + legacy-TLS handshake - by far the most
        # expensive part of talking to this panel - on every 30s poll, since
        # the pooled connection always expires client-side first. Whether the
        # panel's embedded server honors keep-alive is its call; the client at
        # least must not preclude it. limit=2 lets a user arm/disarm go out
        # while a poll is in flight without ever opening more than two
        # connections against the embedded server.
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                keepalive_timeout=SCAN_INTERVAL.total_seconds() + 15,
                limit=2,
            )
        )
        self.client = TuxedoTouchClient(
            session=self.session,
            host=entry.data[CONF_HOST],
            port=entry.data[CONF_PORT],
            use_https=entry.data[CONF_USE_HTTPS],
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
        )
        self._last_command_monotonic = 0.0

    @callback
    def set_optimistic_status(self, status: str) -> None:
        """Push a locally-known status immediately after a command succeeds.

        Works around the GetSecurityStatus "Not available" quirk (see
        _async_update_data): rather than waiting on a poll that may never
        report the real state, reflect the command we just successfully sent
        right away. async_set_updated_data also reschedules the next automatic
        poll, which will still correct this if the panel ever reports
        something conflicting. The timestamp lets _async_update_data discard a
        poll that was already in flight when the command landed - its result
        predates the command and would flip the entity back to the
        pre-command state.
        """
        self._last_command_monotonic = time.monotonic()
        color = self.data.color if self.data else None
        self.async_set_updated_data(TuxedoStatus(status=status, color=color))

    async def _async_update_data(self) -> TuxedoStatus:
        poll_started = time.monotonic()
        try:
            status = await self.client.get_status()
        except TuxedoTouchAuthError as err:
            # Halts polling and starts a reauth flow instead of re-running the
            # full login handshake against doomed credentials every poll.
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except TuxedoTouchError as err:
            raise UpdateFailed(str(err)) from err

        if self._last_command_monotonic > poll_started and self.data is not None:
            _LOGGER.debug(
                "Discarding poll result (%s) that was in flight when a command "
                "completed - keeping optimistic status (%s)",
                status.status,
                self.data.status,
            )
            held: TuxedoStatus = self.data
            return held

        # Quirk workaround: this firmware intermittently - and on at least one
        # unit, persistently - reports "Not available" from GetSecurityStatus
        # even though arm/disarm commands are still reaching the panel fine
        # (confirmed by comparing against a separate ECP-bus-based alarm
        # integration on the same panel, which tracked the real state
        # correctly while this endpoint stayed stuck). Treat "Not available"
        # as "no new information" rather than a real status: keep whatever we
        # last knew (including optimistic updates set immediately after a
        # successful arm/disarm - see set_optimistic_status above) instead of
        # clobbering good data with this placeholder every poll.
        if status.status == STATUS_NOT_AVAILABLE and self.data is not None:
            _LOGGER.debug(
                "GetSecurityStatus returned 'Not available' - keeping last "
                "known status (%s) instead of overwriting it",
                self.data.status,
            )
            kept: TuxedoStatus = self.data
            return kept

        return status

    async def async_close(self) -> None:
        await self.session.close()


async def async_setup_entry(hass: HomeAssistant, entry: TuxedoTouchConfigEntry) -> bool:
    coordinator = TuxedoTouchCoordinator(hass, entry)
    # Registered before the first refresh: if it raises ConfigEntryNotReady
    # (panel unreachable at HA start) or ConfigEntryAuthFailed, HA still runs
    # the on_unload callbacks, so the dedicated session is closed instead of
    # leaking once per setup retry.
    entry.async_on_unload(coordinator.async_close)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await _async_adopt_mac_identity(hass, entry, coordinator.partition)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_adopt_mac_identity(
    hass: HomeAssistant, entry: TuxedoTouchConfigEntry, partition: int
) -> None:
    """Upgrade an entry that predates MAC identity, once, in place.

    Runs after the first refresh so the ARP entry is warm. Entities key off
    `entry_id`, not this id, so nothing is orphaned by the change. A routed
    install never resolves a MAC and simply keeps its address identity.
    """
    if entry.data.get(CONF_MAC):
        return
    mac = await async_panel_mac(hass, entry.data[CONF_HOST])
    if not mac:
        return
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_MAC: mac},
        unique_id=build_unique_id(
            mac, entry.data[CONF_HOST], entry.data[CONF_PORT], partition
        ),
    )
    _LOGGER.debug("Panel identity is now its MAC rather than %s", entry.data[CONF_HOST])


async def async_unload_entry(
    hass: HomeAssistant, entry: TuxedoTouchConfigEntry
) -> bool:
    # The session is closed by the async_on_unload callback registered in
    # async_setup_entry, which HA runs after the platforms unload.
    unloaded: bool = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unloaded
