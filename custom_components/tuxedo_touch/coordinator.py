"""Coordinator for the Honeywell Tuxedo Touch panel."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from typing import Any

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
    COMMAND_CONFIRM_TIMEOUT,
    CONF_PARTITION,
    CONF_USE_HTTPS,
    DEFAULT_PARTITION,
    DOMAIN,
    ISSUE_HTTPS_REDIRECT,
    SCAN_INTERVAL,
    SOURCE_ASSUMED,
    SOURCE_STREAM,
    STATUS_NOT_AVAILABLE,
    issue_id,
    status_names_a_state,
)
from .push import PushStatus, TuxedoPushStream

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
    """Holds the panel's state, and owns the API client and its HTTP session.

    Two sources, and they are not equals. The push stream is the primary one:
    the panel reports partition status on it as it happens, from a path that
    does not read the status cache behind GetSecurityStatus, so it cannot
    report "Not available". The 30 s poll is the initial sync before the
    stream is open and the fallback for as long as it is not - and while the
    stream is carrying the state, what the poll reads is ignored rather than
    written over it.

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
        self.push = TuxedoPushStream(
            self.client,
            self._async_push_status,
            self._async_push_connection_changed,
        )
        self._push_task: asyncio.Task[None] | None = None
        # Whether the stream has ever delivered a status. Connected but
        # silent is not yet a source of truth: the panel sends its first
        # partition status on connect, and until it arrives the poll's answer
        # is the only one there is.
        self._push_status_seen = False
        self._push_loss_logged = False
        # Commands waiting for the panel to report what they asked for.
        self._command_waiters: list[
            tuple[Callable[[TuxedoStatus], bool], asyncio.Future[None]]
        ] = []

    # ------------------------------------------------------------------
    # The push stream
    # ------------------------------------------------------------------
    @callback
    def async_start_push(self) -> None:
        """Open the stream and keep it open for the life of the entry.

        A background task on the entry, so Home Assistant cancels it if the
        entry goes away without async_unload_entry getting the chance to.
        """
        self._push_task = self._entry.async_create_background_task(
            self.hass,
            self.push.async_run(),
            f"{DOMAIN} push stream {self._entry.entry_id}",
        )

    async def async_stop_push(self) -> None:
        """Cancel the stream task and wait for it to actually be gone.

        Awaited by the unload path, so "the entry is stood down" includes the
        connection the stream was holding - the reconfigure flow's check runs
        against a panel with nothing of ours on it.
        """
        task = self._push_task
        self._push_task = None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    @callback
    def _async_push_status(self, status: PushStatus) -> None:
        """A partition status arrived on the stream: it is the state now."""
        if status.partition is not None and status.partition != self.partition:
            _LOGGER.debug(
                "Ignoring a status for partition %s; this entry is partition %s",
                status.partition,
                self.partition,
            )
            return
        self._push_status_seen = True
        data = TuxedoStatus(
            status=status.text,
            color=status.colour,
            source=SOURCE_STREAM,
            armed=status.armed,
            seconds_remaining=status.seconds_remaining,
        )
        for confirms, future in list(self._command_waiters):
            if not future.done() and confirms(data):
                future.set_result(None)
        if self._not_available_logged:
            # The poll was in an outage and the stream has just said what the
            # panel is doing; close the outage off rather than leaving a log
            # line that never gets its pair.
            self._not_available_logged = False
            _LOGGER.info(
                "The panel is reporting a security status again, on its push "
                "stream (%s)",
                data.status,
            )
        self.async_set_updated_data(data)

    @callback
    def _async_push_connection_changed(self, connected: bool) -> None:
        """Availability follows the stream as well as the poll, so say so."""
        if connected:
            if self._push_loss_logged:
                self._push_loss_logged = False
                _LOGGER.info("The panel's push stream is connected again")
            else:
                _LOGGER.debug("The panel's push stream is connected")
        else:
            self._push_status_seen = False
            if not self._push_loss_logged:
                self._push_loss_logged = True
                _LOGGER.info(
                    "The panel's push stream dropped; reconnecting, with the "
                    "%s status poll carrying the state until it is back",
                    SCAN_INTERVAL,
                )
        self.async_update_listeners()

    @property
    def panel_available(self) -> bool:
        """Whether either source is working.

        The entity is unavailable only when both are down. A poll answering
        "Not available" while the stream is up is not an outage at all - the
        stream is reading a path that placeholder cannot appear on.
        """
        return self.last_update_success or self.push.connected

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    async def async_send_command(
        self,
        command: Coroutine[Any, Any, dict[str, Any]],
        confirms: Callable[[TuxedoStatus], bool],
        assumed_status: str,
    ) -> None:
        """Send one command and let the panel say whether it took effect.

        Arm and disarm answer HTTP 200 with a zero-byte body: the result
        arrives on the push stream, seconds later, as a status frame. So the
        ladder is the panel's own report first (`confirms` reads it), a poll
        second, and only if neither could say anything, the status that was
        asked for - marked as assumed, because nothing confirmed it.
        """
        waiter: tuple[Callable[[TuxedoStatus], bool], asyncio.Future[None]] | None = (
            None
        )
        if self.push.connected:
            # Registered before the command goes out: the panel can push the
            # new status before the request that caused it has returned.
            waiter = (confirms, self.hass.loop.create_future())
            self._command_waiters.append(waiter)
        try:
            await command
        except BaseException:
            self._drop_waiter(waiter)
            raise
        self.async_note_command_landed()

        if waiter is not None:
            try:
                async with asyncio.timeout(COMMAND_CONFIRM_TIMEOUT):
                    await waiter[1]
            except TimeoutError:
                _LOGGER.debug(
                    "The panel did not report the command within %ss; polling",
                    COMMAND_CONFIRM_TIMEOUT,
                )
                # The stream has not reported the state the panel is in now,
                # so it is not carrying it: the poll below must be allowed to
                # write its own answer through rather than being told a
                # pushed status already covers it. The next frame the stream
                # does deliver puts it back in charge.
                self._push_status_seen = False
            else:
                return
            finally:
                self._drop_waiter(waiter)

        await self.async_refresh()
        if self.last_update_success and self.data is not None and confirms(self.data):
            return
        # Neither the stream nor the poll could show the change. The command
        # itself succeeded, so show what it asked for and mark it assumed;
        # the next real status from either source replaces it.
        _LOGGER.debug(
            "Neither the stream nor a poll reported the command; assuming %s",
            assumed_status,
        )
        self.async_set_updated_data(
            TuxedoStatus(
                status=assumed_status,
                color=self.data.color if self.data else None,
                source=SOURCE_ASSUMED,
            )
        )

    @callback
    def async_note_command_landed(self) -> None:
        """Record that the panel has just accepted a command.

        Any poll that started before this moment carries an answer that
        predates the command, and writing it through would flip the entity
        back to the state the user has just changed; _async_poll discards
        such a poll and keeps what the command left instead.
        """
        self._last_command_monotonic = time.monotonic()

    @callback
    def _drop_waiter(
        self,
        waiter: tuple[Callable[[TuxedoStatus], bool], asyncio.Future[None]] | None,
    ) -> None:
        if waiter is not None and waiter in self._command_waiters:
            self._command_waiters.remove(waiter)

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
                "completed - keeping the status the command left (%s)",
                status.status,
                self.data.status,
            )
            held: TuxedoStatus = self.data
            return held

        # The stream is the primary source, so while it is delivering, the
        # poll is only proof that the panel is still answering: its reading
        # comes from the status cache and can be stale, or the "Not
        # available" placeholder, at a moment the stream knows exactly what
        # the partition is doing. Neither is worth writing over a pushed
        # status, so the poll succeeds and changes nothing.
        #
        # Except when the pushed text does not name a state. The stream's
        # flag says whether the partition is armed but never in which mode,
        # so a display text outside the map leaves the mode unsettled, and
        # the poll's own reading is the only thing that can settle it - this
        # is what a firmware spelling "Armed Instant" some other way would
        # look like. That case falls through and the poll is used.
        if (
            self.push.connected
            and self._push_status_seen
            and self.data is not None
            and status_names_a_state(self.data.status)
        ):
            _LOGGER.debug(
                "The push stream is carrying the state (%s); the poll's answer "
                "(%s) is not written over it",
                self.data.status,
                status.status,
            )
            streamed: TuxedoStatus = self.data
            return streamed

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
        #
        # Reaching here at all now means the stream is not carrying the state:
        # the firmware fills that cache from an ECP message and the stream
        # does not read it, so a connected stream never produces this answer.
        if status.status == STATUS_NOT_AVAILABLE:
            if not self._not_available_logged:
                self._not_available_logged = True
                _LOGGER.info(
                    "The panel is answering '%s' instead of a security status. "
                    "This is the panel's own firmware quirk, not a connection "
                    "problem, and it cannot happen on the push stream: while "
                    "the stream is down the alarm entity is unavailable until "
                    "a real status arrives",
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
