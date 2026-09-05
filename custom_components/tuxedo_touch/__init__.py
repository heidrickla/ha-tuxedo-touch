"""The Honeywell Tuxedo Touch integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    ISSUE_DUPLICATE_ENTRY,
    ISSUE_HTTPS_REDIRECT,
    issue_id,
)
from .coordinator import TuxedoTouchConfigEntry, TuxedoTouchCoordinator

PLATFORMS: list[Platform] = [Platform.ALARM_CONTROL_PANEL]


async def async_setup_entry(hass: HomeAssistant, entry: TuxedoTouchConfigEntry) -> bool:
    await _migrate_entity_unique_ids(hass, entry)
    coordinator = TuxedoTouchCoordinator(hass, entry)
    # Registered before the first refresh: if it raises ConfigEntryNotReady
    # (panel unreachable at HA start) or ConfigEntryAuthFailed, HA still runs
    # the on_unload callbacks, so the session is released instead of leaking
    # once per setup retry.
    entry.async_on_unload(coordinator.async_release_session)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
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
