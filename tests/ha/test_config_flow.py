"""The config flow: every branch a user can land on.

The panel is never contacted. `login` is the only call the flow makes against
it, so replacing that one method exercises the flow's real code, including its
error mapping, without a socket.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import (
    SOURCE_DHCP,
    SOURCE_IGNORE,
    SOURCE_USER,
    ConfigEntryState,
)
from homeassistant.const import (
    CONF_CODE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuxedo_touch.api import (
    TuxedoTouchAuthError,
    TuxedoTouchConnectionError,
    TuxedoTouchError,
)
from custom_components.tuxedo_touch.const import (
    CONF_MAC,
    CONF_PARTITION,
    CONF_USE_HTTPS,
    DOMAIN,
    ISSUE_CREDENTIALS_REJECTED,
    ISSUE_DUPLICATE_ENTRY,
    OPT_CREDENTIALS_REJECTED,
    issue_id,
)

from .conftest import ENTRY_DATA, HOST, MAC, PORT

LOGIN = "custom_components.tuxedo_touch.api.TuxedoTouchClient.login"
VALIDATE = "custom_components.tuxedo_touch.config_flow._validate_input"
SETUP = "custom_components.tuxedo_touch.async_setup_entry"
# Patched where an entry has to be really loaded: the coordinator's poll then
# returns a status without a login, leaving `login` free to stand for the
# flow's probe alone.
STATUS = "custom_components.tuxedo_touch.api.TuxedoTouchClient.get_status"
# The dhcp component reports a MAC with no separators, which is what the
# integration's step has to cope with.
RAW_MAC = MAC.replace(":", "")
# What the real unit puts in its lease, measured 2026-09-05: the literal word
# Tux followed by the twelve hex digits of its own MAC, upper case. The
# manifest matches "tux*" because the dhcp component lowercases hostnames.
HOSTNAME = f"Tux{RAW_MAC.upper()}"
# The credentials the confirm step asks for; the lease supplies the address.
CONFIRM_INPUT = {
    CONF_PORT: PORT,
    CONF_USE_HTTPS: True,
    CONF_USERNAME: "installer",
    CONF_PASSWORD: "secret",
    CONF_CODE: "1234",
    CONF_PARTITION: 1,
}


def _lease(ip, macaddress=RAW_MAC, hostname=HOSTNAME):
    """A DHCP lease as the dhcp component reports one: MAC without separators."""
    return DhcpServiceInfo(ip=ip, hostname=hostname, macaddress=macaddress)


async def _dhcp_flow(hass, lease):
    """Run the dhcp step with the entry reload stubbed out.

    Moving an entry schedules a reload, and a real reload would go looking
    for the panel; the flow's own behaviour is what these tests are about.
    """
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_DHCP}, data=lease
        )
        await hass.async_block_till_done()
    return result


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


async def _load(hass, entry, ready):
    """Set an entry up for real, so its coordinator is polling the panel."""
    entry.add_to_hass(hass)
    with patch(STATUS, return_value=ready):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_does_not_probe_what_it_is_not_changing(hass, config_entry):
    """The panel serves one connection at a time, so a login that could only
    confirm what the entry is already doing is one worth not spending.

    Changing the partition or the keypad code touches nothing the login
    handshake depends on, and the entry itself is the standing check on the
    fields that do.
    """
    config_entry.add_to_hass(hass)
    with (
        patch(VALIDATE, AsyncMock(return_value=None)) as validate,
        patch(SETUP, return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": config_entry.entry_id},
            data={**ENTRY_DATA, CONF_PARTITION: 3, CONF_CODE: "4321"},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_PARTITION] == 3
    assert config_entry.data[CONF_CODE] == "4321"
    validate.assert_not_awaited()


async def test_reconfigure_frees_the_panel_before_probing_it(hass, config_entry, ready):
    """A loaded entry is polling, and the panel answers one client at a time:
    a probe alongside the poller hangs until the timeout and reports
    cannot_connect about a panel that is perfectly well. The entry gives its
    session up first and takes it back afterwards."""
    await _load(hass, config_entry, ready)

    seen: list[ConfigEntryState] = []

    async def _probe(hass_, data):
        seen.append(config_entry.state)

    with (
        patch(VALIDATE, AsyncMock(side_effect=_probe)),
        patch(STATUS, return_value=ready),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": config_entry.entry_id},
            data={**ENTRY_DATA, CONF_HOST: "10.10.52.61"},
        )
        await hass.async_block_till_done()

    assert seen == [ConfigEntryState.NOT_LOADED]
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_HOST] == "10.10.52.61"
    assert config_entry.state is ConfigEntryState.LOADED


async def test_the_probe_waits_for_a_poll_that_is_already_running(
    hass, config_entry, ready
):
    """Standing the entry down cancels the next poll, not the one in flight.

    That poll is holding the panel's one connection, so a probe that started
    while it was still running would be the second client the unit answers
    with silence. The fake panel here holds its answer until the test releases
    it: the probe must not have run by then, and must run once it does.
    """
    await _load(hass, config_entry, ready)

    polling = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def _stalled_poll():
        polling.set()
        await release.wait()
        order.append("poll finished")
        return ready

    async def _probe(hass_, data):
        order.append("probe")

    with (
        patch(STATUS, side_effect=_stalled_poll),
        patch(VALIDATE, AsyncMock(side_effect=_probe)),
    ):
        poll = hass.async_create_task(config_entry.runtime_data.async_refresh())
        await polling.wait()

        flow = hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "reconfigure", "entry_id": config_entry.entry_id},
                data={**ENTRY_DATA, CONF_HOST: "10.10.52.61"},
            )
        )
        # Every chance to get ahead of the poll it is supposed to wait for:
        # the flow runs until it is standing the entry down and can go no
        # further, which is where it must stop.
        for _ in range(50):
            await asyncio.sleep(0)
            if config_entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS:
                break
        assert order == []

        release.set()
        await poll
        result = await flow
        # The entry is set up again on the new address, so a third entry -
        # that reload's own first poll - follows; the order of the first two
        # is the point.
        assert order[:2] == ["poll finished", "probe"]
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_HOST] == "10.10.52.61"


async def test_a_reconfigure_that_fails_its_probe_puts_the_entry_back(
    hass, config_entry, ready
):
    """The address was a typo, so the panel at the old one is still there and
    still worth polling. An entry left unloaded would cost more than the
    defect this stops."""
    await _load(hass, config_entry, ready)

    with (
        patch(LOGIN, side_effect=TuxedoTouchConnectionError("down")),
        patch(STATUS, return_value=ready),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": config_entry.entry_id},
            data={**ENTRY_DATA, CONF_HOST: "10.10.52.99"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert config_entry.data[CONF_HOST] == HOST
    assert config_entry.state is ConfigEntryState.LOADED


async def _load_retrying(hass, entry):
    """Set an entry up against a panel that will not answer, so it lands in
    SETUP_RETRY with a retry pending against that same panel."""
    entry.add_to_hass(hass)
    with patch(STATUS, side_effect=TuxedoTouchConnectionError("down")):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_reconfigure_frees_a_retrying_entry_too(hass, config_entry, ready):
    """The state a user actually reconfigures from: the panel moved, so the
    entry is in SETUP_RETRY with a retry on the clock that logs in against the
    same panel. That retry is the same contention as a poll, so the entry is
    stood down before the probe here as well."""
    await _load_retrying(hass, config_entry)

    seen: list[ConfigEntryState] = []

    async def _probe(hass_, data):
        seen.append(config_entry.state)

    with (
        patch(VALIDATE, AsyncMock(side_effect=_probe)),
        patch(STATUS, return_value=ready),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": config_entry.entry_id},
            data={**ENTRY_DATA, CONF_HOST: "10.10.52.61"},
        )
        await hass.async_block_till_done()

    assert seen == [ConfigEntryState.NOT_LOADED]
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_HOST] == "10.10.52.61"
    assert config_entry.state is ConfigEntryState.LOADED


async def test_a_retrying_entry_whose_probe_fails_goes_back_to_retrying(
    hass, config_entry
):
    """The new address was a typo. The entry keeps its old settings and its
    retry loop rather than being left switched off."""
    await _load_retrying(hass, config_entry)

    with (
        patch(LOGIN, side_effect=TuxedoTouchConnectionError("down")),
        patch(STATUS, side_effect=TuxedoTouchConnectionError("down")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": config_entry.entry_id},
            data={**ENTRY_DATA, CONF_HOST: "10.10.52.99"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert config_entry.data[CONF_HOST] == HOST
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


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


def _rejected_entry(fake_panel, password):
    """An entry the panel has already refused, addressed to the fake panel."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Tuxedo Touch (127.0.0.1)",
        unique_id=f"127.0.0.1:{fake_panel.port}:1",
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: fake_panel.port,
            CONF_USE_HTTPS: False,
            CONF_USERNAME: fake_panel.username,
            CONF_PASSWORD: password,
            CONF_CODE: "1234",
            CONF_PARTITION: 1,
        },
        options={OPT_CREDENTIALS_REJECTED: True},
    )


async def test_resubmitting_the_stored_credentials_asks_the_panel_nothing(
    hass, fake_panel
):
    """The user's submission is the last place a login is spent, and this
    submission does not spend it by itself.

    Sending back exactly what is stored is sending the credentials the panel
    refused, so nothing goes out on the strength of the Submit button: the
    flow asks again, naming the cost, and only a deliberate confirmation
    reaches the panel.
    """
    entry = _rejected_entry(fake_panel, "no longer the password")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    again = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: fake_panel.username,
            CONF_PASSWORD: "no longer the password",
        },
    )

    assert again["type"] is FlowResultType.FORM
    assert again["step_id"] == "reauth_retry"
    assert not again.get("errors")
    assert fake_panel.login_attempts == 0


async def test_the_stored_credentials_can_be_retried_once_when_the_panel_is_fixed(
    hass, fake_panel
):
    """The way out of a flag that was set while the password was right.

    The commonest reason the panel refuses a credential is not a wrong
    password: a panel that has been reset comes back with web access disabled
    per user, so the stored password is refused and becomes correct again the
    moment the owner re-enables it at the touchscreen. Nothing could tell the
    integration that. The card refused a byte-identical resubmission without
    contacting the panel, a reconfigure that changed no probed field cleared
    nothing, and the only submission that did reach the panel was a DIFFERENT
    password - a genuine refused login against a unit that disables every web
    account at three, which is the harm the whole design exists to prevent.

    So: one retry of the stored credentials, spent deliberately.
    """
    entry = _rejected_entry(fake_panel, fake_panel.password)
    entry.add_to_hass(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(ISSUE_CREDENTIALS_REJECTED, entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_CREDENTIALS_REJECTED,
        translation_placeholders={"title": entry.title},
    )
    result = await entry.start_reauth_flow(hass)

    asked = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: fake_panel.username, CONF_PASSWORD: fake_panel.password},
    )
    assert asked["type"] is FlowResultType.FORM
    assert asked["step_id"] == "reauth_retry"
    # The form is the question, not the attempt: nothing has been sent.
    assert fake_panel.login_attempts == 0

    done = await hass.config_entries.flow.async_configure(asked["flow_id"], {})
    await hass.async_block_till_done()

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reauth_successful"
    assert OPT_CREDENTIALS_REJECTED not in entry.options
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, issue_id(ISSUE_CREDENTIALS_REJECTED, entry.entry_id)
        )
        is None
    )
    assert entry.state is ConfigEntryState.LOADED
    # One login for the retry the user asked for, one for the entry that then
    # loaded. Nothing automatic retried anything.
    assert fake_panel.login_attempts == 2


async def test_a_retry_the_panel_still_refuses_says_it_may_be_locked_out(
    hass, fake_panel
):
    """The confirmation buys exactly one login, and no more than one.

    A retry that fails leaves the flag standing and says what a refusal of
    credentials that used to work most likely means, rather than repeating
    "invalid username or password" at somebody whose account may already be
    disabled.
    """
    entry = _rejected_entry(fake_panel, "no longer the password")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    asked = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: fake_panel.username,
            CONF_PASSWORD: "no longer the password",
        },
    )
    refused = await hass.config_entries.flow.async_configure(asked["flow_id"], {})

    assert refused["type"] is FlowResultType.FORM
    assert refused["step_id"] == "reauth_confirm"
    assert refused["errors"] == {"base": "possibly_locked_out"}
    assert entry.options[OPT_CREDENTIALS_REJECTED] is True
    assert fake_panel.login_attempts == 1


async def test_a_rejection_after_a_rejection_says_the_panel_may_be_locked(
    hass, fake_panel
):
    """Two failures that need two different instructions.

    A wrong password on a working account is one thing. A panel refusing
    credentials that used to work is another: the account may already be
    disabled, in which case no password is the right one and typing more of
    them is itself the harm. The second message says that, and says how to
    clear it at the touchscreen.
    """
    entry = _rejected_entry(fake_panel, "no longer the password")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    again = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: fake_panel.username, CONF_PASSWORD: "another guess"},
    )

    assert again["type"] is FlowResultType.FORM
    assert again["errors"] == {"base": "possibly_locked_out"}
    # One attempt, because the user asked for it. Nothing automatic follows.
    assert fake_panel.login_attempts == 1


async def test_a_reauth_that_works_clears_the_flag_and_reloads(hass, fake_panel):
    """The way out.

    Setup reads the flag before it touches the network, so a flag left
    standing after a successful reauthentication would refuse a working entry
    for ever. The issue has to go with it.
    """
    entry = _rejected_entry(fake_panel, "no longer the password")
    entry.add_to_hass(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(ISSUE_CREDENTIALS_REJECTED, entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_CREDENTIALS_REJECTED,
        translation_placeholders={"title": entry.title},
    )
    result = await entry.start_reauth_flow(hass)

    done = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: fake_panel.username, CONF_PASSWORD: fake_panel.password},
    )
    await hass.async_block_till_done()

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == fake_panel.password
    assert OPT_CREDENTIALS_REJECTED not in entry.options
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, issue_id(ISSUE_CREDENTIALS_REJECTED, entry.entry_id)
        )
        is None
    )
    # And the entry really comes back, rather than being left refusing its
    # own setup on a flag nothing cleared.
    assert entry.state is ConfigEntryState.LOADED


async def test_a_reconfigure_that_proves_new_credentials_clears_the_flag(
    hass, fake_panel
):
    """The other door out of a refused entry, and it has to open too.

    A reconfigure that changes anything the login depends on probes the
    panel, so a probe that succeeded is the panel accepting these
    credentials. A flag left standing would have the reload refuse its own
    setup and send the user back to correct a password that was already
    right.
    """
    entry = _rejected_entry(fake_panel, "no longer the password")
    entry.add_to_hass(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(ISSUE_CREDENTIALS_REJECTED, entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_CREDENTIALS_REJECTED,
        translation_placeholders={"title": entry.title},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
        data={**dict(entry.data), CONF_PASSWORD: fake_panel.password},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert OPT_CREDENTIALS_REJECTED not in entry.options
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, issue_id(ISSUE_CREDENTIALS_REJECTED, entry.entry_id)
        )
        is None
    )
    assert entry.state is ConfigEntryState.LOADED
    # One for the probe the user asked for, one for the entry that then
    # loaded. Neither is automatic retrying.
    assert fake_panel.login_attempts == 2


async def test_a_reconfigure_that_probes_nothing_leaves_the_flag_alone(
    hass, fake_panel
):
    """Changing the partition proves nothing about the credentials.

    The reconfigure step deliberately skips the probe when nothing the login
    depends on changed - it would be a login spent for no information. So
    there is nothing to clear the flag on, and it must not be cleared anyway:
    the panel is still refusing what is stored.
    """
    entry = _rejected_entry(fake_panel, "no longer the password")
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
        data={**dict(entry.data), CONF_PARTITION: 2},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert entry.options[OPT_CREDENTIALS_REJECTED] is True
    assert fake_panel.login_attempts == 0


async def test_the_reauth_form_suggests_the_username_but_not_the_password(
    hass, config_entry
):
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    fields = _fields(result)
    assert _suggested(fields[CONF_USERNAME]) == "installer"
    assert _suggested(fields[CONF_PASSWORD]) in (None, "")


async def test_a_panel_added_by_hand_is_identified_by_its_address(hass):
    """Nothing on the user form can learn the MAC: the panel reports none over
    its API, and only a DHCP lease carries one. That is ordinary, not a
    failure - the entry upgrades the first time a lease arrives."""
    with patch(LOGIN, return_value=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=dict(ENTRY_DATA)
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == f"{HOST}:{PORT}:1"
    assert CONF_MAC not in result["data"]


async def test_the_same_panel_at_a_new_address_keeps_its_identity(
    hass, config_entry_with_mac
):
    """The reason for all of this: a lease change must be a reconfigure, not a
    delete-and-re-add. Reconfigure cannot learn a MAC, so it carries the one
    the entry already has rather than demoting it to an address."""
    config_entry_with_mac.add_to_hass(hass)
    with patch(LOGIN, return_value=None):
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
    assert config_entry_with_mac.data[CONF_MAC] == MAC
    assert config_entry_with_mac.unique_id == f"{MAC}_1"


async def test_dhcp_follows_a_configured_panel_to_a_new_address(
    hass, config_entry_with_mac
):
    """discovery-update-info: a lease for a MAC an entry holds corrects the
    stored address rather than offering the panel as something new."""
    config_entry_with_mac.add_to_hass(hass)
    result = await _dhcp_flow(hass, _lease("10.10.52.61"))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry_with_mac.data[CONF_HOST] == "10.10.52.61"
    assert config_entry_with_mac.title == "Tuxedo Touch (10.10.52.61)"


async def test_dhcp_at_the_same_address_changes_nothing(hass, config_entry_with_mac):
    config_entry_with_mac.add_to_hass(hass)
    result = await _dhcp_flow(hass, _lease(HOST))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry_with_mac.data[CONF_HOST] == HOST
    assert config_entry_with_mac.title == f"Tuxedo Touch ({HOST})"


async def test_dhcp_keeps_a_title_the_user_chose(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Downstairs alarm",
        unique_id=f"{MAC}_1",
        data={**ENTRY_DATA, CONF_MAC: MAC},
    )
    entry.add_to_hass(hass)
    result = await _dhcp_flow(hass, _lease("10.10.52.61"))
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "10.10.52.61"
    assert entry.title == "Downstairs alarm"


async def test_dhcp_moves_every_partition_of_the_same_panel(
    hass, config_entry_with_mac
):
    """Partitions are separate entries on one unit, so one lease moves them all."""
    config_entry_with_mac.add_to_hass(hass)
    second = MockConfigEntry(
        domain=DOMAIN,
        title=f"Tuxedo Touch ({HOST})",
        unique_id=f"{MAC}_2",
        data={**ENTRY_DATA, CONF_MAC: MAC, CONF_PARTITION: 2},
    )
    second.add_to_hass(hass)
    result = await _dhcp_flow(hass, _lease("10.10.52.61"))
    assert result["reason"] == "already_configured"
    assert config_entry_with_mac.data[CONF_HOST] == "10.10.52.61"
    assert second.data[CONF_HOST] == "10.10.52.61"


async def test_a_lease_from_a_panel_nobody_has_added_offers_setup(hass):
    """discovery: the measured matcher - hostname `tux*` and OUI 00D02D - fires
    for a panel that is not set up, and the flow asks for what a lease cannot
    supply instead of aborting."""
    result = await _dhcp_flow(hass, _lease("10.10.52.61"))
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "dhcp_confirm"
    assert result["description_placeholders"] == {"host": "10.10.52.61", "mac": MAC}
    # The address came from the lease, so it is not asked for again.
    assert CONF_HOST not in _fields(result)


async def test_the_discovered_panel_is_created_with_its_mac_as_the_identity(hass):
    form = await _dhcp_flow(hass, _lease("10.10.52.61"))
    with patch(LOGIN, return_value=None), patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], dict(CONFIRM_INPUT)
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Tuxedo Touch (10.10.52.61)"
    assert result["data"][CONF_HOST] == "10.10.52.61"
    assert result["data"][CONF_MAC] == MAC
    assert result["data"][CONF_USERNAME] == "installer"
    assert result["data"][CONF_CODE] == "1234"
    assert result["result"].unique_id == f"{MAC}_1"


async def test_the_discovery_form_recovers_from_a_wrong_password(hass):
    """A typo on the confirm form is corrected there, not by waiting for the
    next lease."""
    form = await _dhcp_flow(hass, _lease("10.10.52.61"))
    with patch(LOGIN, side_effect=TuxedoTouchAuthError("no")):
        again = await hass.config_entries.flow.async_configure(
            form["flow_id"], {**CONFIRM_INPUT, CONF_PASSWORD: "wrong"}
        )
    assert again["type"] is FlowResultType.FORM
    assert again["errors"] == {"base": "invalid_auth"}
    fields = _fields(again)
    assert _suggested(fields[CONF_USERNAME]) == "installer"
    assert _suggested(fields[CONF_PASSWORD]) in (None, "")
    assert _suggested(fields[CONF_CODE]) in (None, "")

    with patch(LOGIN, return_value=None), patch(SETUP, return_value=True):
        done = await hass.config_entries.flow.async_configure(
            again["flow_id"], dict(CONFIRM_INPUT)
        )
        await hass.async_block_till_done()
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["result"].unique_id == f"{MAC}_1"


async def test_an_emptied_code_on_the_discovery_form_is_not_stored(hass):
    form = await _dhcp_flow(hass, _lease("10.10.52.61"))
    with patch(LOGIN, return_value=None), patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {**CONFIRM_INPUT, CONF_CODE: ""}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_CODE not in result["data"]


async def test_a_second_lease_does_not_open_a_second_form(hass):
    """A lease is renewed while the form sits open; one panel is one flow."""
    first = await _dhcp_flow(hass, _lease("10.10.52.61"))
    assert first["type"] is FlowResultType.FORM
    again = await _dhcp_flow(hass, _lease("10.10.52.61"))
    assert again["type"] is FlowResultType.ABORT
    assert again["reason"] == "already_in_progress"


async def test_an_ignored_panel_is_not_offered_again(hass):
    """The user dismissed the discovery card, so Home Assistant holds an entry
    with source `ignore` keyed on the panel's MAC. Every later lease renewal
    has to stay silent; the scans above skip ignored entries deliberately, so
    the unique-id abort is what makes the dismissal stick."""
    MockConfigEntry(
        domain=DOMAIN,
        source=SOURCE_IGNORE,
        unique_id=MAC,
        data={},
        title="Tuxedo Touch",
    ).add_to_hass(hass)
    result = await _dhcp_flow(hass, _lease("10.10.52.61"))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_dhcp_gives_a_hand_added_entry_the_panels_mac(hass, config_entry):
    """The entry was typed in, so it was keyed on the address. The first lease
    for that address is the first thing that can say which panel it is."""
    config_entry.add_to_hass(hass)
    assert CONF_MAC not in config_entry.data
    result = await _dhcp_flow(hass, _lease(HOST))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data[CONF_MAC] == MAC
    assert config_entry.unique_id == f"{MAC}_1"


async def test_adoption_refuses_a_unique_id_another_entry_holds(
    hass, config_entry_with_mac
):
    """Two entries reaching one panel and partition, one by port 80 and one by
    443. The second must not corrupt the unique-id index by taking an id the
    first owns; only the user can say which to remove, so it is raised as an
    issue rather than fixed here."""
    config_entry_with_mac.add_to_hass(hass)
    plain = MockConfigEntry(
        domain=DOMAIN,
        title=f"Tuxedo Touch ({HOST})",
        unique_id=f"{HOST}:80:1",
        data={**ENTRY_DATA, CONF_PORT: 80, CONF_USE_HTTPS: False},
    )
    plain.add_to_hass(hass)

    result = await _dhcp_flow(hass, _lease(HOST))
    assert result["reason"] == "already_configured"
    assert plain.unique_id == f"{HOST}:80:1"
    assert CONF_MAC not in plain.data
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(ISSUE_DUPLICATE_ENTRY, plain.entry_id)
    )
    assert issue is not None
    assert not issue.is_fixable
    assert issue.translation_placeholders == {
        "title": plain.title,
        "other": config_entry_with_mac.title,
    }


async def test_the_duplicate_notice_goes_when_the_entry_does(
    hass, config_entry_with_mac
):
    """Removing one of the two is the fix, and the notice must not survive it."""
    config_entry_with_mac.add_to_hass(hass)
    plain = MockConfigEntry(
        domain=DOMAIN,
        title=f"Tuxedo Touch ({HOST})",
        unique_id=f"{HOST}:80:1",
        data={**ENTRY_DATA, CONF_PORT: 80, CONF_USE_HTTPS: False},
    )
    plain.add_to_hass(hass)
    await _dhcp_flow(hass, _lease(HOST))

    raised = issue_id(ISSUE_DUPLICATE_ENTRY, plain.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, raised) is not None

    await hass.config_entries.async_remove(plain.entry_id)
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, raised) is None
