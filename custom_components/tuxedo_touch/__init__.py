"""The Honeywell Tuxedo Touch integration."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    ISSUE_DUPLICATE_ENTRY,
    ISSUE_HTTPS_REDIRECT,
    issue_id,
)
from .coordinator import (
    PanelStatusUnavailable,
    TuxedoTouchConfigEntry,
    TuxedoTouchCoordinator,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.ALARM_CONTROL_PANEL]


async def async_setup_entry(hass: HomeAssistant, entry: TuxedoTouchConfigEntry) -> bool:
    await _migrate_entity_unique_ids(hass, entry)
    coordinator = TuxedoTouchCoordinator(hass, entry)
    # Registered before the first refresh: if it raises ConfigEntryNotReady
    # (panel unreachable at HA start) or ConfigEntryAuthFailed, HA still runs
    # the on_unload callbacks, so the session is released instead of leaking
    # once per setup retry.
    entry.async_on_unload(coordinator.async_release_session)
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        # One failure is not a reason to refuse setup: the panel answering
        # "Not available" has already proved the address, the TLS handshake
        # and the credentials - it simply had no status in the answer. The
        # entity comes up unavailable and the push stream corrects it as soon
        # as it is open - that answer comes from a path the placeholder cannot
        # appear on - whereas refusing to load would take the device, the
        # entity and its history away for an outage this firmware can hold for
        # hours. Every other first-refresh failure still means the panel could
        # not be read at all, and is still ConfigEntryNotReady.
        if not isinstance(coordinator.last_exception, PanelStatusUnavailable):
            raise
        _LOGGER.debug(
            "Setting up anyway: the panel answered without a status, which is "
            "a firmware quirk rather than a connection failure"
        )

    entry.runtime_data = coordinator
    # That poll was the initial sync. From here the panel's own push stream
    # reports the state and the poll is the fallback underneath it.
    coordinator.async_start_push()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _migrate_entity_unique_ids(
    hass: HomeAssistant, entry: TuxedoTouchConfigEntry
) -> None:
    """One-time move off the old `<entry_id>_partition_N` unique id.

    The suffix meant a partition reconfigure orphaned the registry row and the
    replacement entity was created with a `_2` object id. The new id is the
    bare entry_id; this keeps existing installs' entity ids intact.
    """

    @callback
    def _migrate(registry_entry: er.RegistryEntry) -> dict[str, str] | None:
        if registry_entry.unique_id.startswith(f"{entry.entry_id}_partition_"):
            return {"new_unique_id": entry.entry_id}
        return None

    await er.async_migrate_entries(hass, entry.entry_id, _migrate)


async def async_unload_entry(
    hass: HomeAssistant, entry: TuxedoTouchConfigEntry
) -> bool:
    # The session is released by the async_on_unload callback registered in
    # async_setup_entry, which HA runs after the platforms unload.
    unloaded: bool = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # The push stream is a request that never ends by itself, so nothing
    # short of cancelling its task closes it; awaiting the cancellation is
    # what makes the connection actually gone rather than scheduled to go.
    await entry.runtime_data.async_stop_push()
    # Unloading cancels the next poll but not one already running, and a poll
    # in flight is holding a connection to this panel. Waiting for it here
    # is what lets a caller treat a returned unload as "the panel is free" -
    # the config flow stands an entry down for exactly that reason before it
    # checks a new address or password.
    await entry.runtime_data.async_wait_for_poll()
    return unloaded


async def async_remove_entry(
    hass: HomeAssistant, entry: TuxedoTouchConfigEntry
) -> None:
    """Take this entry's repair issues with it.

    Nothing is written to the panel, so deleting the entry has nothing else
    to undo; an issue left behind would outlive the thing it is about.
    """
    for key in (ISSUE_DUPLICATE_ENTRY, ISSUE_HTTPS_REDIRECT):
        ir.async_delete_issue(hass, DOMAIN, issue_id(key, entry.entry_id))
