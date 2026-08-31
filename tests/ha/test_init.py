"""Setup, teardown, and the failure modes that decide which of the two happens."""

from contextlib import contextmanager
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState

from custom_components.tuxedo_touch.api import (
    TuxedoStatus,
    TuxedoTouchAuthError,
    TuxedoTouchError,
)
from custom_components.tuxedo_touch.const import CONF_MAC, DOMAIN
from custom_components.tuxedo_touch.coordinator import TuxedoTouchCoordinator

from .conftest import MAC

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


PANEL = "alarm_control_panel.honeywell_tuxedo_touch"


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
    """The dedicated session leaks once per retry if teardown waits for LOADED.

    The callback is registered before the first refresh precisely so a panel
    that is down at Home Assistant start does not accumulate sessions. Asserted
    against the real session: mocking async_close would prove only that the
    callback fired, while leaking the very session under test.
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
    unique-id index by adopting an id the first already owns."""
    config_entry_with_mac.add_to_hass(hass)
    config_entry.add_to_hass(hass)
    before = config_entry.unique_id

    with patch(STATUS, return_value=ready), patch(MAC_LOOKUP, return_value=MAC):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.unique_id == before
    assert CONF_MAC not in config_entry.data
