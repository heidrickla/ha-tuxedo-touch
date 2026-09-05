"""Setup, teardown, and the failure modes that decide which of the two happens."""

from contextlib import contextmanager
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuxedo_touch.api import (
    TuxedoStatus,
    TuxedoTouchAuthError,
    TuxedoTouchError,
)
from custom_components.tuxedo_touch.const import (
    CONF_MAC,
    CONF_PARTITION,
    DOMAIN,
    ISSUE_DUPLICATE_ENTRY,
    issue_id,
)
from custom_components.tuxedo_touch.coordinator import TuxedoTouchCoordinator

from .conftest import ENTRY_DATA, HOST, MAC

STATUS = "custom_components.tuxedo_touch.api.TuxedoTouchClient.get_status"
MAC_LOOKUP = "custom_components.tuxedo_touch.async_panel_mac"


@contextmanager
def _coordinators():
    """Collect the coordinators built during the block, still fully real.

    Setup failures never reach runtime_data, so there is no other handle on
    the session those runs create.
    """
    made: list[TuxedoTouchCoordinator] = []
    real = TuxedoTouchCoordinator.__init__

    def record(self, hass, entry):
        real(self, hass, entry)
        made.append(self)

    with patch.object(TuxedoTouchCoordinator, "__init__", record):
        yield made


# The entity is named after its partition so two partitions on one panel are
# told apart; the device name is the prefix.
PANEL = "alarm_control_panel.honeywell_tuxedo_touch_partition_1"


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_loads_the_panel_entity(hass, config_entry, ready):
    with patch(STATUS, return_value=ready):
        await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    state = hass.states.get(PANEL)
    assert state is not None
    assert state.state == "disarmed"
    assert state.attributes["tuxedo_status"] == "Ready To Arm"
    assert state.name == "Honeywell Tuxedo Touch Partition 1"


async def test_two_partitions_on_one_panel_are_told_apart(
    hass, config_entry_with_mac, ready
):
    """Partitions are separate alarms on one panel. Since Home Assistant
    2026.8 a device belongs to one config entry, so each partition entry
    gets its own device carrying the panel's MAC; both devices are named for
    the panel, and only the partition in the entity name tells them apart."""
    second = MockConfigEntry(
        domain=DOMAIN,
        title=f"Tuxedo Touch ({HOST})",
        unique_id=f"{MAC}_2",
        data={**ENTRY_DATA, CONF_MAC: MAC, CONF_PARTITION: 2},
    )
    config_entry_with_mac.add_to_hass(hass)
    second.add_to_hass(hass)
    # Setting up the first entry loads the component, and loading the
    # component sets up every entry of the domain; a second explicit setup
    # would find an already loaded entry and refuse.
    with patch(STATUS, return_value=ready):
        await hass.config_entries.async_setup(config_entry_with_mac.entry_id)
        await hass.async_block_till_done()

    assert config_entry_with_mac.state is ConfigEntryState.LOADED
    assert second.state is ConfigEntryState.LOADED
    one = hass.states.get(PANEL)
    two = hass.states.get("alarm_control_panel.honeywell_tuxedo_touch_partition_2")
    assert one is not None and two is not None
    assert one.name == "Honeywell Tuxedo Touch Partition 1"
    assert two.name == "Honeywell Tuxedo Touch Partition 2"

    registry = dr.async_get(hass)
    devices = [
        dr.async_entries_for_config_entry(registry, entry.entry_id)
        for entry in (config_entry_with_mac, second)
    ]
    assert [len(found) for found in devices] == [1, 1]
    first_device, second_device = devices[0][0], devices[1][0]
    assert first_device.id != second_device.id
    for device in (first_device, second_device):
        assert (dr.CONNECTION_NETWORK_MAC, MAC) in device.connections
        assert device.name == "Honeywell Tuxedo Touch"


async def test_an_unreachable_panel_is_retried_not_failed(hass, config_entry):
    """A panel that is off when HA starts must not need a manual reload."""
    with patch(STATUS, side_effect=TuxedoTouchError("no route to host")):
        await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_rejected_credentials_start_a_reauth_flow(hass, config_entry):
    """Polling on doomed credentials would re-run the login handshake forever."""
    with patch(STATUS, side_effect=TuxedoTouchAuthError("bad password")):
        await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f for f in flows if f["context"].get("source") == "reauth"]


async def test_the_session_is_closed_when_setup_never_completes(hass, config_entry):
    """The session leaks once per retry if teardown waits for LOADED.

    The callback is registered before the first refresh precisely so a panel
    that is down at Home Assistant start does not accumulate sessions.
    Asserted against the real session: mocking the release would prove only
    that the callback fired, while leaking the very session under test. A
    session detached from the shared connector reports itself closed.
    """
    with _coordinators() as made, patch(STATUS, side_effect=TuxedoTouchError("down")):
        await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert made and made[0].session.closed


async def test_unload_removes_the_entity_and_closes_the_session(
    hass, config_entry, ready
):
    with _coordinators() as made, patch(STATUS, return_value=ready):
        await _setup(hass, config_entry)
        assert hass.states.get(PANEL) is not None

        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert made and made[0].session.closed


async def test_a_not_available_poll_does_not_erase_the_known_state(
    hass, config_entry, ready
):
    """The firmware quirk this integration exists to survive.

    GetSecurityStatus intermittently answers "Not available" on a panel that
    is otherwise fine. Writing that through would flip the alarm to unknown
    on a working system.
    """
    with patch(STATUS, return_value=ready) as status:
        await _setup(hass, config_entry)
        assert hass.states.get(PANEL).state == "disarmed"

        status.return_value = TuxedoStatus(status="Not available", color=None)
        await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()

    # A reload starts from no prior data, so the placeholder is all there is
    # and the entity is honestly unknown rather than falsely disarmed.
    assert hass.states.get(PANEL).state == "unknown"


async def test_a_later_not_available_keeps_the_previous_status(
    hass, config_entry, ready
):
    with patch(STATUS, return_value=ready) as status:
        await _setup(hass, config_entry)
        coordinator = config_entry.runtime_data

        status.return_value = TuxedoStatus(status="Not available", color=None)
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.data.status == "Ready To Arm"
    assert hass.states.get(PANEL).state == "disarmed"


async def test_an_existing_entry_adopts_the_mac_on_the_next_start(
    hass, config_entry, ready
):
    """Entries made before MAC identity upgrade themselves once, in place.

    Entities key off entry_id rather than this id, so nothing is orphaned.
    """
    assert CONF_MAC not in config_entry.data

    with patch(STATUS, return_value=ready), patch(MAC_LOOKUP, return_value=MAC):
        await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.data[CONF_MAC] == MAC
    assert config_entry.unique_id == f"{MAC}_1"
    assert hass.states.get(PANEL) is not None


async def test_a_routed_entry_keeps_its_address_identity(hass, config_entry, ready):
    before = config_entry.unique_id
    with patch(STATUS, return_value=ready), patch(MAC_LOOKUP, return_value=None):
        await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert CONF_MAC not in config_entry.data
    assert config_entry.unique_id == before


async def test_mac_adoption_refuses_a_unique_id_another_entry_holds(
    hass, config_entry, config_entry_with_mac, ready
):
    """Two entries reaching the same panel: the second must not corrupt the
    unique-id index by adopting an id the first already owns. Only the user
    can say which of the two to remove, so it is raised as an issue rather
    than fixed here."""
    config_entry_with_mac.add_to_hass(hass)
    config_entry.add_to_hass(hass)
    before = config_entry.unique_id

    with patch(STATUS, return_value=ready), patch(MAC_LOOKUP, return_value=MAC):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.unique_id == before
    assert CONF_MAC not in config_entry.data
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(ISSUE_DUPLICATE_ENTRY, config_entry.entry_id)
    )
    assert issue is not None
    assert not issue.is_fixable
    assert issue.translation_placeholders == {
        "title": config_entry.title,
        "other": config_entry_with_mac.title,
    }


async def test_the_duplicate_issue_goes_once_the_second_entry_does(
    hass, config_entry, config_entry_with_mac, ready
):
    """Removing one of the two is the fix, and the notice must not survive it."""
    config_entry_with_mac.add_to_hass(hass)
    config_entry.add_to_hass(hass)
    with patch(STATUS, return_value=ready), patch(MAC_LOOKUP, return_value=MAC):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    raised = issue_id(ISSUE_DUPLICATE_ENTRY, config_entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, raised) is not None

    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, raised) is None


async def test_a_poll_in_flight_when_a_command_lands_is_discarded(
    hass, config_entry, ready
):
    """The poll's answer predates the command, so writing it through would
    flip the entity back to the state the user just changed."""
    with patch(STATUS, return_value=ready):
        await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data

    async def _command_lands_mid_poll():
        coordinator.set_optimistic_status("Armed Away")
        return TuxedoStatus(status="Ready To Arm", color="green")

    with patch(STATUS, side_effect=_command_lands_mid_poll):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.data.status == "Armed Away"
    assert hass.states.get(PANEL).state == "armed_away"
