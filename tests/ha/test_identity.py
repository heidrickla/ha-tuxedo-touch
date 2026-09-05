"""Resolving the panel's identity, and what each answer means.

ARP is the only source: the panel reports no serial and no MAC of its own.
None is an ordinary answer here, not a failure, so every caller has to be
able to tell the two apart.
"""

from unittest.mock import patch

import pytest

from custom_components.tuxedo_touch.identity import async_panel_mac, build_unique_id

GETMAC = "custom_components.tuxedo_touch.identity.get_mac_address"


async def test_an_ipv4_address_is_looked_up_as_an_ip(hass):
    """A hostname passed as ip= matches nothing and answers None silently."""
    with patch(GETMAC, return_value="AA:BB:CC:DD:EE:FF") as lookup:
        assert await async_panel_mac(hass, "10.10.52.60") == "aa:bb:cc:dd:ee:ff"
    assert lookup.call_args.kwargs == {"ip": "10.10.52.60"}


async def test_a_hostname_is_resolved_first(hass):
    with patch(GETMAC, return_value="aa:bb:cc:dd:ee:ff") as lookup:
        assert await async_panel_mac(hass, "tuxedo.lan") == "aa:bb:cc:dd:ee:ff"
    assert lookup.call_args.kwargs == {"hostname": "tuxedo.lan"}


async def test_an_ipv6_address_goes_through_the_ipv6_argument(hass):
    with patch(GETMAC, return_value="aa:bb:cc:dd:ee:ff") as lookup:
        assert await async_panel_mac(hass, "fd00::5") == "aa:bb:cc:dd:ee:ff"
    assert lookup.call_args.kwargs == {"ip6": "fd00::5"}


@pytest.mark.parametrize("answer", [None, "", "00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"])
async def test_a_placeholder_answer_is_not_an_identity(hass, answer):
    """getmac returns the all-zero and broadcast addresses rather than None
    when it cannot resolve, and keying an entry on either would collide."""
    with patch(GETMAC, return_value=answer):
        assert await async_panel_mac(hass, "10.10.52.60") is None


async def test_a_lookup_that_raises_is_an_absent_mac_not_a_failed_setup(hass):
    """A routed install must still set up; this must never escape."""
    with patch(GETMAC, side_effect=OSError("no arp table")):
        assert await async_panel_mac(hass, "10.10.52.60") is None


def test_the_unique_id_prefers_the_mac_and_always_carries_the_partition():
    assert build_unique_id("aa:bb:cc:dd:ee:ff", "10.10.52.60", 443, 2) == (
        "aa:bb:cc:dd:ee:ff_2"
    )
    assert build_unique_id(None, "10.10.52.60", 443, 1) == "10.10.52.60:443:1"
