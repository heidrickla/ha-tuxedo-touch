"""The config flow: every branch a user can land on.

The panel is never contacted. `login` is the only call the flow makes against
it, so replacing that one method exercises the flow's real code, including its
error mapping, without a socket.
"""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import (
    CONF_CODE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
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
VALIDATE = "custom_components.tuxedo_touch.config_flow._validate_input"


def _fields(result):
    """The form's schema markers by field name, for looking at suggested values."""
    return {str(key): key for key in result["data_schema"].schema}


def _suggested(marker):
    return (marker.description or {}).get("suggested_value")


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


async def test_the_form_after_an_error_keeps_everything_but_the_secrets(hass):
    """The address comes back filled in; the password and code are typed again.

    A suggested value is sent to the browser, where a password field can
    reveal it, so the secrets must never be among them.
    """
    with patch(LOGIN, side_effect=TuxedoTouchAuthError("no")):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=dict(ENTRY_DATA)
        )
    fields = _fields(result)
    assert _suggested(fields[CONF_HOST]) == HOST
    assert _suggested(fields[CONF_USERNAME]) == "installer"
    assert _suggested(fields[CONF_PASSWORD]) in (None, "")
    assert _suggested(fields[CONF_CODE]) in (None, "")


async def test_an_emptied_code_field_is_not_stored_as_a_code(hass):
    """An empty string would count as "no code" everywhere, but storing it
    would still be a lie about what the user configured."""
    with patch(LOGIN, return_value=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data={**ENTRY_DATA, CONF_CODE: ""},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_CODE not in result["data"]


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


async def test_reconfigure_recovers_from_an_unreachable_panel(hass, config_entry):
    """A typo in the new address is corrected on the same form, not by
    starting over."""
    config_entry.add_to_hass(hass)
    with patch(LOGIN, side_effect=TuxedoTouchConnectionError("down")):
        first = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": config_entry.entry_id},
            data={**ENTRY_DATA, CONF_HOST: "10.10.52.99"},
        )
    assert first["type"] is FlowResultType.FORM
    assert first["errors"] == {"base": "cannot_connect"}

    with patch(LOGIN, return_value=None):
        second = await hass.config_entries.flow.async_configure(
            first["flow_id"], {**ENTRY_DATA, CONF_HOST: "10.10.52.61"}
        )
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_HOST] == "10.10.52.61"


async def test_the_reconfigure_form_never_shows_the_stored_secrets(hass, config_entry):
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": config_entry.entry_id},
    )
    assert result["type"] is FlowResultType.FORM
    fields = _fields(result)
    assert _suggested(fields[CONF_HOST]) == HOST
    assert _suggested(fields[CONF_PASSWORD]) in (None, "")
    assert _suggested(fields[CONF_CODE]) in (None, "")


async def test_reconfigure_with_blank_secrets_keeps_the_stored_ones(hass, config_entry):
    """Blank is the only way to say "unchanged" for a field that is never
    shown, and the login check must run with the stored password, not with
    the blank."""
    config_entry.add_to_hass(hass)
    # The successful step reloads the entry, and that setup logs in for
    # real unless login is patched like everywhere else in this file. The
    # flow's own check is replaced wholesale so its input can be inspected.
    with (
        patch(VALIDATE, AsyncMock(return_value=None)) as validate,
        patch(LOGIN, return_value=None),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": config_entry.entry_id},
            data={
                **ENTRY_DATA,
                CONF_HOST: "10.10.52.61",
                CONF_PASSWORD: "",
                CONF_CODE: "",
            },
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert validate.await_args.args[1][CONF_PASSWORD] == "secret"
    assert config_entry.data[CONF_HOST] == "10.10.52.61"
    assert config_entry.data[CONF_PASSWORD] == "secret"
    assert config_entry.data[CONF_CODE] == "1234"


async def test_reconfigure_with_a_new_password_stores_it(hass, config_entry):
    config_entry.add_to_hass(hass)
    with patch(LOGIN, return_value=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": config_entry.entry_id},
            data={**ENTRY_DATA, CONF_PASSWORD: "newsecret", CONF_CODE: "4321"},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_PASSWORD] == "newsecret"
    assert config_entry.data[CONF_CODE] == "4321"


async def test_the_title_follows_the_panel_to_its_new_address(hass, config_entry):
    """The default title names the host; a stale one would point at the old
    address forever."""
    config_entry.add_to_hass(hass)
    assert config_entry.title == f"Tuxedo Touch ({HOST})"
    with patch(LOGIN, return_value=None):
        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": config_entry.entry_id},
            data={**ENTRY_DATA, CONF_HOST: "10.10.52.61"},
        )
    assert config_entry.title == "Tuxedo Touch (10.10.52.61)"


async def test_a_renamed_entry_keeps_its_name_on_a_move(hass):
    renamed = MockConfigEntry(
        domain=DOMAIN,
        title="Garage alarm",
        unique_id=f"{HOST}:{PORT}:1",
        data=dict(ENTRY_DATA),
    )
    renamed.add_to_hass(hass)
    with patch(LOGIN, return_value=None):
        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": renamed.entry_id},
            data={**ENTRY_DATA, CONF_HOST: "10.10.52.61"},
        )
    assert renamed.data[CONF_HOST] == "10.10.52.61"
    assert renamed.title == "Garage alarm"


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


async def test_reauth_recovers_from_a_wrong_password(hass, config_entry):
    """The second attempt on the same form must finish the reauth."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    with patch(LOGIN, side_effect=TuxedoTouchAuthError("no")):
        again = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "installer", CONF_PASSWORD: "wrong"},
        )
    assert again["type"] is FlowResultType.FORM

    with patch(LOGIN, return_value=None):
        done = await hass.config_entries.flow.async_configure(
            again["flow_id"],
            {CONF_USERNAME: "installer", CONF_PASSWORD: "right"},
        )
    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "right"


async def test_the_reauth_form_suggests_the_username_but_not_the_password(
    hass, config_entry
):
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    fields = _fields(result)
    assert _suggested(fields[CONF_USERNAME]) == "installer"
    assert _suggested(fields[CONF_PASSWORD]) in (None, "")


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


async def test_reconfigure_adopts_the_mac_an_address_entry_lacked(hass, config_entry):
    """An entry set up from a routed segment, reconfigured from a local one:
    the lookup answers for the first time and the identity upgrades."""
    config_entry.add_to_hass(hass)
    assert CONF_MAC not in config_entry.data
    with patch(LOGIN, return_value=None), patch(MAC_LOOKUP, return_value=MAC):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": config_entry.entry_id},
            data=dict(ENTRY_DATA),
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_MAC] == MAC
    assert config_entry.unique_id == f"{MAC}_1"


async def test_a_duplicate_re_add_heals_the_stored_address(hass, config_entry_with_mac):
    """Adding the same panel again at its new address is how a user tells us
    it moved; the abort now carries the correction instead of discarding it."""
    config_entry_with_mac.add_to_hass(hass)
    with patch(LOGIN, return_value=None), patch(MAC_LOOKUP, return_value=MAC):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data={**ENTRY_DATA, CONF_HOST: "10.10.52.61"},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry_with_mac.data[CONF_HOST] == "10.10.52.61"
