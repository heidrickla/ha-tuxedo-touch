"""Setup, teardown, and the failure modes that decide which of the two happens."""

import logging
from contextlib import contextmanager
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_CODE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
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
    CONF_USE_HTTPS,
    DOMAIN,
    ISSUE_CREDENTIALS_REJECTED,
    OPT_CREDENTIALS_REJECTED,
    SOURCE_ASSUMED,
    issue_id,
)
from custom_components.tuxedo_touch.coordinator import TuxedoTouchCoordinator

from .conftest import ENTRY_DATA, HOST, MAC

STATUS = "custom_components.tuxedo_touch.api.TuxedoTouchClient.get_status"
# What the panel answers instead of a status during one of its outages.
NOT_AVAILABLE = TuxedoStatus(status="Not available", color=None)


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


async def test_a_rejection_at_runtime_is_written_down_without_a_reload(
    hass, config_entry, ready
):
    """The state Home Assistant leaves this integration in, and why it has to
    be recorded on the entry.

    A poll that raises ConfigEntryAuthFailed on a LOADED entry does not
    unload it: update_coordinator sets last_update_success, starts the reauth
    flow, and leaves everything running. So the entry survives to the next
    restart, and without something written down that restart would open with
    a fresh login handshake against the credentials the panel has just
    refused - a second strike out of the three that disable its web accounts.

    Writing the option must not itself reload the entry, or the reload would
    spend the very login this is preventing. The coordinator identity is what
    proves it: a reload builds a new one.
    """
    with patch(STATUS, return_value=ready):
        await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data

    with patch(STATUS, side_effect=TuxedoTouchAuthError("password changed")):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert config_entry.options[OPT_CREDENTIALS_REJECTED] is True
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data is coordinator
    # The card cannot carry the "do not keep guessing" warning, so the issue
    # stands beside it.
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(ISSUE_CREDENTIALS_REJECTED, config_entry.entry_id)
    )
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR


async def test_a_flagged_entry_spends_no_login_at_setup(hass, fake_panel):
    """A restart with credentials already known to be refused costs nothing.

    The whole point of persisting the flag: setup refuses before the client
    is even built, so the panel sees no login page request and no credential
    POST. It still ends where the user needs it to - the reauthentication
    card and the issue beside it - because SETUP_ERROR from
    ConfigEntryAuthFailed is what starts the flow.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Tuxedo Touch (127.0.0.1)",
        unique_id=f"127.0.0.1:{fake_panel.port}:1",
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: fake_panel.port,
            CONF_USE_HTTPS: False,
            CONF_USERNAME: fake_panel.username,
            CONF_PASSWORD: "no longer the password",
            CONF_CODE: "1234",
            CONF_PARTITION: 1,
        },
        options={OPT_CREDENTIALS_REJECTED: True},
    )
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert fake_panel.login_attempts == 0
    assert fake_panel.polls == 0
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f for f in flows if f["context"].get("source") == "reauth"]
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, issue_id(ISSUE_CREDENTIALS_REJECTED, entry.entry_id)
        )
        is not None
    )


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


async def test_a_first_poll_of_not_available_leaves_the_entity_unavailable(
    hass, config_entry, ready
):
    """The latch this release removes.

    "Not available" used to be stored as data when there was no earlier
    status to keep - which is exactly the first poll after a load - and every
    later "Not available" then preserved that stored placeholder, so the
    entity read unknown until somebody armed or disarmed. It is a failed read
    on the first poll as much as on the hundredth, so nothing is stored and
    the entity is unavailable. The entry still loads: the panel answered, so
    the address and the credentials are proven, and a real status still takes
    effect the moment one arrives.
    """
    with patch(STATUS, return_value=NOT_AVAILABLE) as status:
        await _setup(hass, config_entry)
        assert config_entry.state is ConfigEntryState.LOADED
        coordinator = config_entry.runtime_data
        assert coordinator.data is None
        assert hass.states.get(PANEL).state == "unavailable"

        # A second one changes nothing: there is no placeholder to preserve.
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.data is None
        assert hass.states.get(PANEL).state == "unavailable"

        # And a real status still lands, with no arm or disarm to unstick it.
        status.return_value = ready
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert hass.states.get(PANEL).state == "disarmed"


async def test_a_not_available_after_a_good_poll_recovers_by_itself(
    hass, config_entry, ready
):
    """A transient blip mid-run: the last good status is kept, the entity is
    unavailable while the panel is answering the placeholder, and the next
    real status brings it straight back with no arm or disarm needed."""
    with patch(STATUS, return_value=ready) as status:
        await _setup(hass, config_entry)
        coordinator = config_entry.runtime_data
        assert hass.states.get(PANEL).state == "disarmed"

        status.return_value = NOT_AVAILABLE
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert not coordinator.last_update_success
        # The good status is still held, so nothing has to be rediscovered.
        assert coordinator.data.status == "Ready To Arm"
        assert hass.states.get(PANEL).state == "unavailable"

        status.return_value = TuxedoStatus(status="Armed Away", color="red")
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert hass.states.get(PANEL).state == "armed_away"


async def test_an_outage_is_logged_once_and_so_is_the_recovery(
    hass, config_entry, ready, caplog
):
    """log-when-unavailable: one line for the outage however long it runs,
    one when it ends - not one per poll."""
    with patch(STATUS, return_value=ready) as status:
        await _setup(hass, config_entry)
        coordinator = config_entry.runtime_data

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="custom_components.tuxedo_touch"):
            status.return_value = NOT_AVAILABLE
            await coordinator.async_refresh()
            await coordinator.async_refresh()
            await coordinator.async_refresh()

            status.return_value = ready
            await coordinator.async_refresh()
            await hass.async_block_till_done()

    messages = [record.getMessage() for record in caplog.records]
    assert len([m for m in messages if "instead of a security status" in m]) == 1
    assert len([m for m in messages if "reporting a security status again" in m]) == 1


async def test_a_poll_in_flight_when_a_command_lands_is_discarded(
    hass, config_entry, ready
):
    """The poll's answer predates the command, so writing it through would
    flip the entity back to the state the user just changed."""
    with patch(STATUS, return_value=ready):
        await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data

    async def _command_lands_mid_poll():
        coordinator.async_note_command_landed()
        coordinator.async_set_updated_data(
            TuxedoStatus(status="Armed Away", color="red", source=SOURCE_ASSUMED)
        )
        return TuxedoStatus(status="Ready To Arm", color="green")

    with patch(STATUS, side_effect=_command_lands_mid_poll):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.data.status == "Armed Away"
    assert hass.states.get(PANEL).state == "armed_away"
