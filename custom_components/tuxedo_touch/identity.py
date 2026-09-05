"""Working out which panel we are talking to.

The panel reports no identifier of its own. `Registration/AddDeviceMAC` in
Honeywell's API reference enrolls a CLIENT's MAC with the panel; it does not
hand out the unit's, and nothing in the endpoint surface documented in
docs/tuxedo_touch_api_notes.md returns a serial, a hostname or the unit's
network configuration - the status, arm and disarm responses carry a status
string and nothing else.

So the MAC comes from the one place on the network that already holds it: the
panel's DHCP lease, which Home Assistant watches on its own. Measured on the
unit at 00:d0:2d:4d:d7:b6 (2026-09-05): OUI 00:D0:2D is Resideo, and the lease
hostname is `Tux` followed by the twelve hex digits of the MAC, so the
manifest's matcher recognises a Tuxedo panel and the lease hands over its
identity in the same event. An install Home Assistant sees no lease for -
routed, or on another VLAN - keeps an address identity, which is ordinary
rather than a failure.
"""

from __future__ import annotations


def build_unique_id(mac: str | None, host: str, port: int, partition: int) -> str:
    """Identity plus the partition it addresses.

    The MAC is preferred because an address changes on a DHCP lease while the
    panel does not. The partition is part of the id either way: partitions are
    separate alarms and each gets its own entry.
    """
    if mac:
        return f"{mac}_{partition}"
    return f"{host}:{port}:{partition}"
