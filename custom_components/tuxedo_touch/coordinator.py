"""Coordinator for the Honeywell Tuxedo Touch panel."""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession
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


class PanelStatusUnavailable(UpdateFailed):
    """The panel answered the status call without a status in it.

    A class of its own so setup can tell it apart from a panel that could not
    be reached: the request, the TLS handshake and the login all worked, so
    the entry is set up and its entity comes up unavailable rather than the
    integration refusing to load. This firmware can answer "Not available"
    for hours, and an entry that never loaded would take the panel's device,
    its entity and its history with it for the whole of that.
    """


class TuxedoTouchCoordinator(DataUpdateCoordinator[TuxedoStatus]):
    """Polls panel status and owns the API client and its HTTP session.

    The session comes from Home Assistant's helper, so it runs on the shared
    connector. It is a session of its own only for its cookie jar: the panel
    sets a session cookie with a random name per login. (The permissive
    SSLContext is passed per-request and would work on any session; it is not
    the reason for a separate session, and it is shared with every other
    client so that they all queue on one pooled connection per panel.)
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
        # DummyCookieJar: the client manages cookies by hand, and the panel
        # names its session cookie randomly per login - a real jar accumulates
        # one dead cookie per re-login and aiohttp overlays jar cookies over
        # the client's explicit Cookie header.
        # auto_cleanup=False because the entry releases the session on unload,
        # which is sooner than Home Assistant's own shutdown hook.
        self.session = async_create_clientsession(
            hass,
            auto_cleanup=False,
            cookie_jar=aiohttp.DummyCookieJar(),
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
        # One poll at a time, and a handle on the one in flight: see
        # async_wait_for_poll.
        self._poll_lock = asyncio.Lock()
        # "Not available" is logged once per outage rather than every poll.
        self._not_available_logged = False

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
        """Poll the panel, one poll at a time.

        The lock serializes polls against each other - the panel serves one
        client at a time - and is the handle async_wait_for_poll needs on a
        poll already running.
        """
        async with self._poll_lock:
            return await self._async_poll()

    async def _async_poll(self) -> TuxedoStatus:
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

        # The panel's own quirk, not this integration's: this firmware
        # intermittently - and on at least one unit, for long stretches -
        # reports "Not available" from GetSecurityStatus even though arm and
        # disarm commands are still reaching it fine (confirmed by comparing
        # against a separate ECP-bus-based alarm integration on the same
        # panel, which tracked the real state correctly while this endpoint
        # stayed stuck). It is a failed read rather than a state: failing the
        # poll leaves the entity unavailable and Home Assistant keeps the last
        # good status in self.data, so a transient blip costs nothing and the
        # first real status ends it.
        #
        # It has to fail on the FIRST poll after a load as well, which is what
        # this used to get wrong. Keeping the last known status only when
        # self.data existed meant a load that opened on "Not available" stored
        # the placeholder as data, and every later "Not available" then
        # preserved it: the entity latched to unknown until an arm or disarm
        # replaced the data by hand.
        if status.status == STATUS_NOT_AVAILABLE:
            if not self._not_available_logged:
                self._not_available_logged = True
                _LOGGER.info(
                    "The panel is answering '%s' instead of a security status. "
                    "This is the panel's own firmware quirk, not a connection "
                    "problem: the alarm entity is unavailable until it reports "
                    "a real status",
                    STATUS_NOT_AVAILABLE,
                )
            raise PanelStatusUnavailable(
                translation_domain=DOMAIN,
                translation_key="status_not_available",
            )

        if self._not_available_logged:
            self._not_available_logged = False
            _LOGGER.info(
                "The panel is reporting a security status again (%s)", status.status
            )

        return status

    async def async_wait_for_poll(self) -> None:
        """Wait for a poll already in flight to finish.

        Unloading an entry stops the next poll being scheduled; it does not
        touch one that is already running, and that poll holds the connection
        Home Assistant's pool keeps to a panel that serves one client at a
        time. The unload path awaits this before it returns, so "the entry is
        stood down" means the panel is actually free by the time the next
        client - the config flow's check - dials it.

        Bounded by the client's own 15 s request timeout, so this cannot hold
        an unload open indefinitely.
        """
        async with self._poll_lock:
            pass

    @callback
    def async_release_session(self) -> None:
        """Give the session up on unload.

        detach(), not close(): the session runs on Home Assistant's shared
        connector, so closing it would close that pool for every integration.
        Home Assistant replaces close() with a warn-and-no-op wrapper for
        exactly this reason. A detached session reports itself closed.

        What detach() does NOT do is close the sockets this entry used: they
        stay in the shared pool until it drops them as idle, fifteen seconds
        after the last poll. That is deliberate rather than tolerated - the
        next client to want this panel, a config flow probe included, is on
        the same pool key (see api._legacy_ssl_context) and picks that
        connection up rather than opening a second one to a unit that serves
        one at a time.
        """
        self.session.detach()
