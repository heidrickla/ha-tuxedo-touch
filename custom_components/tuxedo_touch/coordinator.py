"""Coordinator for the Honeywell Tuxedo Touch panel."""

from __future__ import annotations

import logging
import time

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    TuxedoStatus,
    TuxedoTouchAuthError,
    TuxedoTouchClient,
    TuxedoTouchError,
    TuxedoTouchHttpsRequiredError,
)
from .const import (
    CONF_PARTITION,
    CONF_USE_HTTPS,
    DEFAULT_PARTITION,
    DOMAIN,
    ISSUE_HTTPS_REDIRECT,
    SCAN_INTERVAL,
    STATUS_NOT_AVAILABLE,
    issue_id,
)

_LOGGER = logging.getLogger(__name__)

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
            config_entry=entry,
            update_interval=SCAN_INTERVAL,
            always_update=False,
        )
        # Kept explicitly: DataUpdateCoordinator.config_entry is typed
        # optional, and every use here is on an entry that certainly exists.
        self._entry = entry
        self.partition = entry.data.get(CONF_PARTITION, DEFAULT_PARTITION)
        # keep-alive must outlive SCAN_INTERVAL: aiohttp's default (15s) would
        # guarantee a fresh TCP + legacy-TLS handshake - by far the most
        # expensive part of talking to this panel - on every 30s poll, since
        # the pooled connection always expires client-side first. Whether the
        # panel's embedded server honors keep-alive is its call; the client at
        # least must not preclude it. limit=2 lets a user arm/disarm go out
        # while a poll is in flight without ever opening more than two
        # connections against the embedded server.
        # DummyCookieJar: the client manages cookies by hand, and the panel
        # names its session cookie randomly per login - a real jar accumulates
        # one dead cookie per re-login and aiohttp overlays jar cookies over
        # the client's explicit Cookie header.
        self.session = aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),
            connector=aiohttp.TCPConnector(
                keepalive_timeout=SCAN_INTERVAL.total_seconds() + 15,
                limit=2,
            ),
        )
        self._https_issue_raised = False
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

    @property
    def _https_issue_id(self) -> str:
        """One issue per entry: two entries can be wrong independently."""
        return issue_id(ISSUE_HTTPS_REDIRECT, self._entry.entry_id)

    @callback
    def _async_raise_https_issue(self) -> None:
        """Offer the fix for a condition no retry can clear.

        The panel redirects every plain-HTTP API call to HTTPS while "Secured
        Web Server Access" is on, so the entry stays broken until its scheme
        changes. repairs.py flips it.
        """
        if self._https_issue_raised:
            return
        self._https_issue_raised = True
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._https_issue_id,
            data={"entry_id": self._entry.entry_id},
            is_fixable=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_HTTPS_REDIRECT,
            translation_placeholders={"title": self._entry.title},
        )

    @callback
    def _async_clear_https_issue(self) -> None:
        """Clear it on any good poll, including one after a restart.

        Deleting unconditionally rather than only when this coordinator
        raised it: the scheme can also be fixed on the panel, and an issue
        left standing after that would outlive the condition.
        """
        self._https_issue_raised = False
        ir.async_delete_issue(self.hass, DOMAIN, self._https_issue_id)

    async def _async_update_data(self) -> TuxedoStatus:
        poll_started = time.monotonic()
        try:
            status = await self.client.get_status()
        except TuxedoTouchAuthError as err:
            # Halts polling and starts a reauth flow instead of re-running the
            # full login handshake against doomed credentials every poll.
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="auth_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        except TuxedoTouchHttpsRequiredError as err:
            self._async_raise_https_issue()
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="https_required",
            ) from err
        except TuxedoTouchError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        self._async_clear_https_issue()

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
