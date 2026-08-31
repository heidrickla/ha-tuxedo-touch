"""Working out which panel we are talking to.

The panel reports no serial and no MAC of its own. `Registration/AddDeviceMAC`
in Honeywell's API reference enrolls a CLIENT's MAC with the panel; it does not
hand out the unit's. So the only stable identifier available is the one the
network already holds.
"""

from __future__ import annotations

import logging
from functools import partial

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import format_mac

_LOGGER = logging.getLogger(__name__)

# getmac returns these rather than None when it cannot resolve the address.
_NOT_A_MAC = {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}


async def async_panel_mac(hass: HomeAssistant, host: str) -> str | None:
    """The panel's MAC, or None when it cannot be resolved.

    This is an ARP lookup, so it only answers on the same layer 2 segment. A
    routed install gets None and keeps an address-based identity - every
    caller must treat None as ordinary rather than as a failure.

    Call it after a successful request to the panel, so the ARP entry it reads
    has just been populated.
    """
    try:
        from getmac import get_mac_address
    except ImportError:
        _LOGGER.debug("getmac is unavailable, so the panel keeps an address identity")
        return None

    try:
        mac = await hass.async_add_executor_job(partial(get_mac_address, ip=host))
    except Exception:
        _LOGGER.debug("MAC lookup for %s failed", host, exc_info=True)
        return None

    if not mac or mac.lower() in _NOT_A_MAC:
        return None
    return format_mac(mac)


def build_unique_id(mac: str | None, host: str, port: int, partition: int) -> str:
    """Identity plus the partition it addresses.

    The MAC is preferred because an address changes on a DHCP lease while the
    panel does not. The partition is part of the id either way: partitions are
    separate alarms and each gets its own entry.
    """
    if mac:
        return f"{mac}_{partition}"
    return f"{host}:{port}:{partition}"
