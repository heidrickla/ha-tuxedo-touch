"""What a config entry is keyed on, and why the partition is always in it.

The panel reports no identifier of its own, so the MAC only ever arrives with
a DHCP lease - tests/ha/test_config_flow.py drives that. What is left here is
the rule for turning whatever is known into a unique id.
"""

from custom_components.tuxedo_touch.identity import build_unique_id


def test_the_unique_id_prefers_the_mac_and_always_carries_the_partition():
    assert build_unique_id("aa:bb:cc:dd:ee:ff", "10.10.52.60", 443, 2) == (
        "aa:bb:cc:dd:ee:ff_2"
    )
    assert build_unique_id(None, "10.10.52.60", 443, 1) == "10.10.52.60:443:1"


def test_an_address_identity_keeps_the_port_apart():
    """Two entries reaching one panel on different ports get different ids,
    which is the duplicate the repair notification exists for."""
    assert build_unique_id(None, "10.10.52.60", 80, 1) != build_unique_id(
        None, "10.10.52.60", 443, 1
    )
