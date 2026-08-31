"""The config flow: every branch a user can land on.

The panel is never contacted. `login` is the only call the flow makes against
it, so replacing that one method exercises the flow's real code, including its
error mapping, without a socket.
"""

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuxedo_touch.api import (
    TuxedoTouchAuthError,
    TuxedoTouchConnectionError,
    TuxedoTouchError,
)
from custom_components.tuxedo_touch.const import CONF_MAC, CONF_PARTITION, DOMAIN

from .conftest import ENTRY_DATA, HOST, MAC, PORT

LOGIN = "custom_components.tuxedo_touch.api.TuxedoTouchClient.login"


async def test_user_step_creates_the_entry(hass):
    with patch(LOGIN, return_value=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=dict(ENTRY_DATA)
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Tuxedo Touch ({HOST})"
    assert result["data"][CONF_HOST] == HOST


async def test_the_first_form_is_shown_with_no_input(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not result["errors"]


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (TuxedoTouchAuthError("no"), "invalid_auth"),
        (TuxedoTouchConnectionError("down"), "cannot_connect"),
        (TuxedoTouchError("no Random header"), "panel_error"),
        (ValueError("something else"), "unknown"),
    ],
)
async def test_each_failure_maps_to_its_own_message(hass, raised, expected):
    """A wrong password and an unreachable panel must not read the same.

    panel_error is the one worth having: a panel that answered but not
    usefully, which is neither of the two obvious diagnoses.
    """
    with patch(LOGIN, side_effect=raised):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=dict(ENTRY_DATA)
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


async def test_the_form_is_offered_again_after_a_failure(hass):
    """A rejected password must be correctable without restarting the flow."""
    with patch(LOGIN, side_effect=TuxedoTouchAuthError("no")):
        first = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=dict(ENTRY_DATA)
        )
    with patch(LOGIN, return_value=None):
        second = await hass.config_entries.flow.async_configure(
            first["flow_id"], dict(ENTRY_DATA)
        )
    assert second["type"] is FlowResultType.CREATE_ENTRY


async def test_the_same_panel_and_partition_cannot_be_added_twice(hass, config_entry):
    config_entry.add_to_hass(hass)
    with patch(LOGIN, return_value=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=dict(ENTRY_DATA)
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_a_second_partition_on_the_same_panel_is_allowed(hass, config_entry):
    """Partitions are separate alarms, so the unique id includes the partition."""
    config_entry.add_to_hass(hass)
    with patch(LOGIN, return_value=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data={**ENTRY_DATA, CONF_PARTITION: 2},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_reconfigure_moves_the_panel_to_a_new_address(hass, config_entry):
    config_entry.add_to_hass(hass)
    with patch(LOGIN, return_value=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": config_entry.entry_id},
            data={**ENTRY_DATA, CONF_HOST: "10.10.52.61"},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_HOST] == "10.10.52.61"
    assert config_entry.unique_id == f"10.10.52.61:{PORT}:1"


async def test_reconfigure_can_change_the_partition(hass, config_entry):
    """The unique id carries the partition, so this changes it too."""
    config_entry.add_to_hass(hass)
    with patch(LOGIN, return_value=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": config_entry.entry_id},
            data={**ENTRY_DATA, CONF_PARTITION: 3},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.unique_id == f"{HOST}:{PORT}:3"


async def test_reconfigure_refuses_to_collide_with_another_entry(hass, config_entry):
    """Two entries on the same panel and partition would double every entity."""
    config_entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Tuxedo Touch (other)",
        unique_id=f"{HOST}:{PORT}:2",
        data={**ENTRY_DATA, CONF_PARTITION: 2},
    )
    other.add_to_hass(hass)

    with patch(LOGIN, return_value=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": config_entry.entry_id},
            data={**ENTRY_DATA, CONF_PARTITION: 2},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data[CONF_PARTITION] == 1


async def test_reconfigure_shows_the_error_rather_than_saving(hass, config_entry):
    config_entry.add_to_hass(hass)
    with patch(LOGIN, side_effect=TuxedoTouchConnectionError("down")):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": config_entry.entry_id},
            data={**ENTRY_DATA, CONF_HOST: "10.10.52.61"},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert config_entry.data[CONF_HOST] == HOST


async def test_reauth_updates_only_the_credentials(hass, config_entry):
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch(LOGIN, return_value=None):
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "installer", CONF_PASSWORD: "newsecret"},
        )
    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "newsecret"
    # The address was not on the reauth form and must survive it.
    assert config_entry.data[CONF_HOST] == HOST
    assert config_entry.data[CONF_PORT] == PORT


async def test_reauth_reports_a_still_wrong_password(hass, config_entry):
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    with patch(LOGIN, side_effect=TuxedoTouchAuthError("no")):
        again = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "installer", CONF_PASSWORD: "wrong"},
        )
    assert again["type"] is FlowResultType.FORM
    assert again["errors"] == {"base": "invalid_auth"}
    assert config_entry.data[CONF_PASSWORD] == "secret"


MAC_LOOKUP = "custom_components.tuxedo_touch.config_flow.async_panel_mac"


async def test_the_mac_becomes_the_identity_when_the_network_knows_it(hass):
    """An address is a lease, not an identity."""
    with patch(LOGIN, return_value=None), patch(MAC_LOOKUP, return_value=MAC):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=dict(ENTRY_DATA)
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == f"{MAC}_1"
    assert result["data"][CONF_MAC] == MAC


async def test_a_routed_panel_falls_back_to_its_address(hass):
    """ARP only answers on the same segment. That is ordinary, not a failure."""
    with patch(LOGIN, return_value=None), patch(MAC_LOOKUP, return_value=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=dict(ENTRY_DATA)
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == f"{HOST}:{PORT}:1"
    assert CONF_MAC not in result["data"]


async def test_the_same_panel_at_a_new_address_keeps_its_identity(
    hass, config_entry_with_mac
):
    """The reason for all of this: a DHCP lease change must be a reconfigure,
    not a delete-and-re-add."""
    config_entry_with_mac.add_to_hass(hass)
    with patch(LOGIN, return_value=None), patch(MAC_LOOKUP, return_value=MAC):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": "reconfigure",
                "entry_id": config_entry_with_mac.entry_id,
            },
            data={**ENTRY_DATA, CONF_HOST: "10.10.52.61"},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry_with_mac.data[CONF_HOST] == "10.10.52.61"
    assert config_entry_with_mac.unique_id == f"{MAC}_1"


async def test_a_different_panel_at_that_address_is_refused(
    hass, config_entry_with_mac
):
    """Now a real check rather than a circular one: the panel that answered
    reports a different MAC than the one this entry was set up for."""
    config_entry_with_mac.add_to_hass(hass)
    with (
        patch(LOGIN, return_value=None),
        patch(MAC_LOOKUP, return_value="11:22:33:44:55:66"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": "reconfigure",
                "entry_id": config_entry_with_mac.entry_id,
            },
            data={**ENTRY_DATA, CONF_HOST: "10.10.52.61"},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "another_panel"
    assert config_entry_with_mac.data[CONF_HOST] == HOST


async def test_a_failed_lookup_does_not_demote_a_known_identity(
    hass, config_entry_with_mac
):
    """One unlucky ARP miss must not silently revert the entry to an address."""
    config_entry_with_mac.add_to_hass(hass)
    with patch(LOGIN, return_value=None), patch(MAC_LOOKUP, return_value=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": "reconfigure",
                "entry_id": config_entry_with_mac.entry_id,
            },
            data={**ENTRY_DATA, CONF_HOST: "10.10.52.61"},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry_with_mac.data[CONF_MAC] == MAC
    assert config_entry_with_mac.unique_id == f"{MAC}_1"
