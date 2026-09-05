"""The Honeywell Tuxedo Touch integration."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_MAC,
    DOMAIN,
    ISSUE_DUPLICATE_ENTRY,
    ISSUE_HTTPS_REDIRECT,
    issue_id,
)
from .coordinator import TuxedoTouchConfigEntry, TuxedoTouchCoordinator
from .identity import async_panel_mac, build_unique_id

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.ALARM_CONTROL_PANEL]


async def async_setup_entry(hass: HomeAssistant, entry: TuxedoTouchConfigEntry) -> bool:
    await _migrate_entity_unique_ids(hass, entry)
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


async def _async_adopt_mac_identity(
    hass: HomeAssistant, entry: TuxedoTouchConfigEntry, partition: int
) -> None:
    """Upgrade an entry that predates MAC identity, once, in place.

    Runs after the first refresh so the ARP entry is warm. Entities key off
    `entry_id`, not this id, so nothing is orphaned by the change. A routed
    install never resolves a MAC and simply keeps its address identity.
    """
    ir.async_delete_issue(hass, DOMAIN, issue_id(ISSUE_DUPLICATE_ENTRY, entry.entry_id))
    if entry.data.get(CONF_MAC):
        return
    mac = await async_panel_mac(hass, entry.data[CONF_HOST])
    if not mac:
        return
    unique_id = build_unique_id(
        mac, entry.data[CONF_HOST], entry.data[CONF_PORT], partition
    )
    holder = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
    if holder is not None and holder.entry_id != entry.entry_id:
        # Two entries reaching the same panel (say, by IP and by hostname).
        # Taking the id would corrupt the unique-id index; keep the address
        # identity and tell the user which entry is the duplicate. Only the
        # user can decide which of the two to remove, so the issue is not
        # fixable from here.
        _LOGGER.warning(
            "Not adopting MAC identity for %s: config entry %s already is %s",
            entry.title,
            holder.title,
            unique_id,
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id(ISSUE_DUPLICATE_ENTRY, entry.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_DUPLICATE_ENTRY,
            translation_placeholders={"title": entry.title, "other": holder.title},
        )
        return
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_MAC: mac},
        unique_id=unique_id,
    )
    _LOGGER.debug("Panel identity is now its MAC rather than %s", entry.data[CONF_HOST])


async def async_unload_entry(
    hass: HomeAssistant, entry: TuxedoTouchConfigEntry
) -> bool:
    # The session is closed by the async_on_unload callback registered in
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
