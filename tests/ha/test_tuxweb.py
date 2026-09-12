"""A panel running tuxweb, as Home Assistant sees it, against the fake.

A real HTTP server on 127.0.0.1 in its tuxweb mode - the capability endpoint,
the token-gated plaintext API, the token-gated stream - so what is exercised
is the entry probing, choosing the tuxweb path, polling and streaming on the
token, and reporting commands the way that contract allows: confirmed, or
refused with a reason, rather than sent and assumed.

Every test here that shows something tuxweb does differently sits next to the
stock control, in this file or in tests/ha/test_push.py, that shows stock
still doing what it did. The two contracts share their paths, so a test on
outcome alone could pass with the wrong path taken.
"""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_CODE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuxedo_touch.api import TuxedoStatus, TuxedoTouchError
from custom_components.tuxedo_touch.const import (
    CONF_PARTITION,
    CONF_TUXWEB_TOKEN,
    CONF_USE_HTTPS,
    DOMAIN,
    ISSUE_CREDENTIALS_REJECTED,
    OPT_CREDENTIALS_REJECTED,
    SOURCE_ASSUMED,
    SOURCE_COMMAND,
    issue_id,
)
from custom_components.tuxedo_touch.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.fake_panel import TUXWEB_CAPABILITIES, TUXWEB_TOKEN, wait_until

PANEL = "alarm_control_panel.honeywell_tuxedo_touch_partition_1"
STATUS = "custom_components.tuxedo_touch.api.TuxedoTouchClient.get_status"
SETUP = "custom_components.tuxedo_touch.async_setup_entry"
CONFIRM_TIMEOUT = "custom_components.tuxedo_touch.coordinator.COMMAND_CONFIRM_TIMEOUT"


async def _setup(hass, entry, *, wait_for_stream=True):
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    if wait_for_stream:
        await wait_until(lambda: coordinator.push.connected)
    return coordinator


def _state(hass):
    return hass.states.get(PANEL)


def _stream_tasks():
    return [
        task
        for task in asyncio.all_tasks()
        if "push stream" in task.get_name() and not task.done()
    ]


def _user_input(panel, **overrides):
    """The user form filled in for the fake, plain HTTP."""
    data = {
        CONF_HOST: "127.0.0.1",
        CONF_PORT: panel.port,
        CONF_USE_HTTPS: False,
        CONF_USERNAME: panel.username,
        CONF_PASSWORD: panel.password,
        CONF_CODE: "1234",
        CONF_PARTITION: 1,
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------- setup


async def test_the_entry_loads_on_the_token_and_never_logs_in(
    hass, fake_tuxweb, tuxweb_entry
):
    """One probe, then the token everywhere: the poll, the stream. No login
    page is asked for and no credential POST is made, because tuxweb serves
    neither - and the entity is carried exactly as on stock, poll first and
    then the stream."""
    coordinator = await _setup(hass, tuxweb_entry)

    assert tuxweb_entry.state is ConfigEntryState.LOADED
    assert coordinator.client.tuxweb is True
    assert fake_tuxweb.capability_probes == 1
    assert fake_tuxweb.login_page_requests == 0
    assert fake_tuxweb.login_attempts == 0
    assert fake_tuxweb.polls == 1
    assert _state(hass).state == "disarmed"
    assert _state(hass).attributes["tuxedo_source"] == "poll"
    assert fake_tuxweb.last_push_headers["Authorization"] == f"Bearer {TUXWEB_TOKEN}"
    assert "Cookie" not in fake_tuxweb.last_push_headers

    await fake_tuxweb.push_status_text("Armed Stay", armed=True)
    await wait_until(lambda: _state(hass).state == "armed_home")
    assert _state(hass).attributes["tuxedo_source"] == "stream"


async def test_a_stock_entry_is_probed_once_and_then_unchanged(
    hass, fake_panel, panel_entry
):
    """The control: the same setup on stock costs one probe answering 404,
    and everything after it is the login handshake and the signed poll it
    always was."""
    coordinator = await _setup(hass, panel_entry)

    assert coordinator.client.tuxweb is False
    assert fake_panel.capability_probes == 1
    assert fake_panel.logins == 1
    assert fake_panel.polls == 1
    assert "Authorization" not in fake_panel.last_api_headers
    assert "authtoken" in fake_panel.last_api_headers
    assert fake_panel.last_push_headers["Cookie"] == fake_panel.cookie
    assert _state(hass).state == "disarmed"


async def test_a_panel_that_is_down_at_the_probe_is_retried_not_failed(
    hass, fake_tuxweb, tuxweb_entry
):
    """The probe failing to connect is a setup retry, like a poll failing
    would be - and it spends nothing, because it never touched a login."""
    await fake_tuxweb.close()
    tuxweb_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(tuxweb_entry.entry_id)
    await hass.async_block_till_done()

    assert tuxweb_entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_tuxweb_panel_with_no_token_asks_for_one_and_logs_in_nowhere(
    hass, fake_tuxweb
):
    """A stock entry pointed at a tuxweb panel. Detection says tuxweb, there
    is no token to send, and the honest answer is the reauthentication card
    naming the token - not a login page GET answering 404 read as "cannot
    connect", and not a bare request sent to read a 401."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Tuxedo Touch (127.0.0.1)",
        unique_id=f"127.0.0.1:{fake_tuxweb.port}:1",
        data=_user_input(fake_tuxweb),
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f for f in flows if f["context"].get("source") == "reauth"]
    assert fake_tuxweb.capability_probes == 1
    assert fake_tuxweb.login_page_requests == 0
    assert fake_tuxweb.login_attempts == 0
    assert fake_tuxweb.api_requests == 0
    # No lockout exists to record: the flag and the issue are stock's.
    assert OPT_CREDENTIALS_REJECTED not in entry.options


# ------------------------------------------------------------- commands


async def test_a_confirmed_arm_is_reported_by_the_stream_first(
    hass, fake_tuxweb, tuxweb_entry
):
    """The ladder is unchanged on top: the frame the panel pushed is the
    report, and no poll is needed to get it."""
    fake_tuxweb.auto_push = True
    await _setup(hass, tuxweb_entry)
    polls_before = fake_tuxweb.polls

    await hass.services.async_call(
        ALARM_DOMAIN, "alarm_arm_home", {ATTR_ENTITY_ID: PANEL}, blocking=True
    )
    await hass.async_block_till_done()

    assert fake_tuxweb.commands == ["ArmWithCode"]
    assert fake_tuxweb.last_command_body == "arming=STAY&pID=1&ucode=1234&operation=set"
    assert fake_tuxweb.polls == polls_before
    assert _state(hass).state == "arming"
    assert _state(hass).attributes["tuxedo_source"] == "stream"


async def test_a_confirmed_command_the_stream_missed_is_settled_by_the_poll(
    hass, fake_tuxweb, tuxweb_entry
):
    """The second rung on tuxweb reads the live state model, so it carries
    the armed flag and confirms the command outright."""
    await _setup(hass, tuxweb_entry)

    with patch(CONFIRM_TIMEOUT, 0.05):
        await hass.services.async_call(
            ALARM_DOMAIN, "alarm_arm_away", {ATTR_ENTITY_ID: PANEL}, blocking=True
        )
        await hass.async_block_till_done()

    assert fake_tuxweb.commands == ["ArmWithCode"]
    assert _state(hass).state == "arming"
    assert _state(hass).attributes["tuxedo_source"] == "poll"
    assert _state(hass).attributes["arming_seconds_remaining"] == 59


async def test_a_confirmed_command_nothing_reports_is_labelled_command_not_assumed(
    hass, fake_tuxweb, tuxweb_entry
):
    """The bottom rung, and the one thing command_result changes about it.

    No frame arrives and the poll fails, so neither source can speak. On
    stock that is the assumed rung - the reply said "sent" and nothing else.
    On tuxweb the reply was the panel confirming it acted, so what was asked
    for is what the panel did, and the entity says so with a label that is
    not "assumed".
    """
    await _setup(hass, tuxweb_entry)

    with (
        patch(CONFIRM_TIMEOUT, 0.05),
        patch(STATUS, side_effect=TuxedoTouchError("panel busy")),
    ):
        await hass.services.async_call(
            ALARM_DOMAIN, "alarm_arm_away", {ATTR_ENTITY_ID: PANEL}, blocking=True
        )
        await hass.async_block_till_done()

    assert fake_tuxweb.commands == ["ArmWithCode"]
    assert fake_tuxweb.armed is True
    assert _state(hass).state == "armed_away"
    assert _state(hass).attributes["tuxedo_source"] == SOURCE_COMMAND


async def test_the_same_silence_on_stock_is_still_assumed(
    hass, fake_panel, panel_entry
):
    """The control for the test above, same sequence on the stock fake: a
    200 that says "sent" confirms nothing, and the label says assumed."""
    await _setup(hass, panel_entry)

    with (
        patch(CONFIRM_TIMEOUT, 0.05),
        patch(STATUS, side_effect=TuxedoTouchError("panel busy")),
    ):
        await hass.services.async_call(
            ALARM_DOMAIN, "alarm_arm_away", {ATTR_ENTITY_ID: PANEL}, blocking=True
        )
        await hass.async_block_till_done()

    assert fake_panel.commands == ["ArmWithCode"]
    assert _state(hass).state == "armed_away"
    assert _state(hass).attributes["tuxedo_source"] == SOURCE_ASSUMED


async def test_a_confirmed_command_the_poll_then_contradicts_is_not_a_refusal(
    hass, fake_tuxweb, tuxweb_entry
):
    """The panel acted - tuxweb saw the state byte flip before answering -
    and by the time the poll runs it reports the other state: disarmed at
    the keypad in the same breath. On stock that poll is the only evidence
    and reads as a refusal; here the reply already settled that the command
    was carried out, so the call succeeds and the panel's current reading
    stands, unassumed."""
    await _setup(hass, tuxweb_entry)

    def _disarmed_again():
        return TuxedoStatus(status="Ready To Arm", color="green", armed=False)

    with (
        patch(CONFIRM_TIMEOUT, 0.05),
        patch(STATUS, side_effect=_disarmed_again),
    ):
        await hass.services.async_call(
            ALARM_DOMAIN, "alarm_arm_away", {ATTR_ENTITY_ID: PANEL}, blocking=True
        )
        await hass.async_block_till_done()

    assert fake_tuxweb.commands == ["ArmWithCode"]
    assert fake_tuxweb.armed is True, "the fake confirmed the arm"
    assert _state(hass).state == "disarmed"
    assert _state(hass).attributes["tuxedo_source"] == "poll"


async def test_a_command_the_panel_did_not_confirm_fails_and_assumes_nothing(
    hass, fake_tuxweb, tuxweb_entry
):
    """504: the state byte never flipped. The call fails with the reason and
    the entity keeps showing what the panel reports - a faulted zone did not
    arm, and no automation is told it did."""
    await _setup(hass, tuxweb_entry)
    await fake_tuxweb.push_status_text("Ready To Arm", armed=False)
    await wait_until(lambda: _state(hass).attributes["tuxedo_source"] == "stream")
    fake_tuxweb.confirm_commands = False

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            ALARM_DOMAIN, "alarm_arm_away", {ATTR_ENTITY_ID: PANEL}, blocking=True
        )
    await hass.async_block_till_done()

    assert raised.value.translation_key == "command_failed"
    assert "did not confirm" in str(raised.value)
    assert fake_tuxweb.commands == ["ArmWithCode"]
    assert _state(hass).state == "disarmed"
    assert _state(hass).attributes["tuxedo_source"] == "stream"


async def test_a_disarm_is_confirmed_by_its_reply_and_its_own_key(
    hass, fake_tuxweb, tuxweb_entry
):
    fake_tuxweb.auto_push = True
    fake_tuxweb.armed = True
    fake_tuxweb.status = "Armed Away"
    fake_tuxweb.colour = "Red"
    await _setup(hass, tuxweb_entry)
    assert _state(hass).state == "armed_away"

    await hass.services.async_call(
        ALARM_DOMAIN, "alarm_disarm", {ATTR_ENTITY_ID: PANEL}, blocking=True
    )
    await hass.async_block_till_done()

    assert fake_tuxweb.commands == ["DisarmWithCode"]
    assert fake_tuxweb.last_command_body == "pID=1&ucode=1234&operation=set"
    assert _state(hass).state == "disarmed"


# ----------------------------------------------------------------- auth


async def test_a_token_refused_at_runtime_starts_reauth_without_the_lockout_flag(
    hass, fake_tuxweb, tuxweb_entry
):
    """The token is reissued on the panel. The next poll is refused, the
    reauthentication card comes up, and the stream stops - but nothing is
    written to the entry and no lockout issue is raised, because tuxweb
    counts nothing and the flag exists for a panel that does. No login is
    spent anywhere along the way, because there is none to spend."""
    coordinator = await _setup(hass, tuxweb_entry)
    fake_tuxweb.token = "f" * 64

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f for f in flows if f["context"].get("source") == "reauth"]
    assert OPT_CREDENTIALS_REJECTED not in tuxweb_entry.options
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, issue_id(ISSUE_CREDENTIALS_REJECTED, tuxweb_entry.entry_id)
        )
        is None
    )

    # The stream is still holding the socket it opened on the old token; a
    # drop makes it reconnect, and the refusal ends it rather than backing
    # it off.
    coordinator.push.reconnect_wait = 0.01
    fake_tuxweb.drop_stream()
    await wait_until(lambda: not _stream_tasks())

    assert coordinator.push.auth_failed is True
    assert fake_tuxweb.login_page_requests == 0
    assert fake_tuxweb.login_attempts == 0
    # Each refusal was asked about exactly once before it was believed - the
    # poll's and the stream's, after the probe at setup - and the panel still
    # declaring its list is what makes both refusals the token's.
    assert fake_tuxweb.capability_probes == 3
    assert coordinator.client.tuxweb is True


async def test_a_panel_rolled_back_to_stock_is_polled_as_stock_from_the_next_poll(
    hass, fake_tuxweb, tuxweb_entry
):
    """The stale verdict. The panel goes back to stock under a running entry
    and refuses the token-only poll with the 401 stock gives a request that
    carries no session. Before the re-check that was a refused token for
    ever: the reauthentication card, asking for a token the panel had
    stopped wanting. Now it is one failed poll - no card, no lockout flag -
    and the poll after it is the stock one: the login handshake, the cookie,
    the signed and encrypted request. The re-check itself logged in nowhere."""
    coordinator = await _setup(hass, tuxweb_entry)
    assert fake_tuxweb.capability_probes == 1
    fake_tuxweb.tuxweb = False

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert coordinator.client.tuxweb is False
    assert fake_tuxweb.capability_probes == 2
    assert fake_tuxweb.login_page_requests == 0
    assert fake_tuxweb.login_attempts == 0
    assert tuxweb_entry.state is ConfigEntryState.LOADED
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert not [f for f in flows if f["context"].get("source") == "reauth"]
    assert OPT_CREDENTIALS_REJECTED not in tuxweb_entry.options

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert fake_tuxweb.logins == 1
    assert "Authorization" not in fake_tuxweb.last_api_headers
    assert "authtoken" in fake_tuxweb.last_api_headers
    assert fake_tuxweb.last_api_headers["Cookie"] == fake_tuxweb.cookie
    assert fake_tuxweb.capability_probes == 2, "a stock client is not re-asked"
    assert _state(hass).state == "disarmed"
    assert _state(hass).attributes["tuxedo_source"] == "poll"


async def test_a_stream_refused_after_a_rollback_comes_back_on_the_stock_session(
    hass, fake_tuxweb, tuxweb_entry
):
    """The same rollback seen by the stream first. Stock redirects the
    token-only stream request to its login page, which on tuxweb is the
    token being refused and the end of the task for good. The re-check
    finds no list, so the drop is an ordinary reconnect, and the next
    connection opens on a session cookie from the stock path's own login -
    the stream keeps its primary source, and no card comes up."""
    coordinator = await _setup(hass, tuxweb_entry)
    fake_tuxweb.tuxweb = False
    # A reconnect would otherwise wait out the five-second floor first.
    coordinator.push.reconnect_wait = 0.01
    fake_tuxweb.drop_stream()
    await wait_until(lambda: not coordinator.push.connected)
    await wait_until(lambda: coordinator.push.connected)

    assert coordinator.push.auth_failed is False
    assert _stream_tasks()
    assert coordinator.client.tuxweb is False
    assert fake_tuxweb.capability_probes == 2
    assert "Authorization" not in fake_tuxweb.last_push_headers
    assert fake_tuxweb.last_push_headers["Cookie"] == fake_tuxweb.cookie
    assert fake_tuxweb.logins == 1
    assert fake_tuxweb.login_attempts == 1
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert not [f for f in flows if f["context"].get("source") == "reauth"]

    await fake_tuxweb.push_status_text("Armed Stay", armed=True)
    await wait_until(lambda: _state(hass).state == "armed_home")
    assert _state(hass).attributes["tuxedo_source"] == "stream"


# ---------------------------------------------------------- config flow


async def test_the_user_step_sets_up_a_tuxweb_panel_on_its_token(hass, fake_tuxweb):
    """The form's probe on tuxweb is one status read, not a login."""
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data=_user_input(fake_tuxweb, **{CONF_TUXWEB_TOKEN: TUXWEB_TOKEN}),
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TUXWEB_TOKEN] == TUXWEB_TOKEN
    assert fake_tuxweb.capability_probes == 1
    assert fake_tuxweb.polls == 1
    assert fake_tuxweb.login_page_requests == 0
    assert fake_tuxweb.login_attempts == 0


async def test_the_user_step_names_a_wrong_token(hass, fake_tuxweb):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data=_user_input(fake_tuxweb, **{CONF_TUXWEB_TOKEN: "not the token"}),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_token"}
    assert fake_tuxweb.login_attempts == 0


async def test_the_user_step_names_a_missing_token_on_tuxweb(hass, fake_tuxweb):
    """Not "cannot connect": the panel answered, and what it needs is named."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=_user_input(fake_tuxweb)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "tuxweb_token_required"}
    assert fake_tuxweb.login_page_requests == 0
    assert fake_tuxweb.api_requests == 0


async def test_the_user_step_on_stock_ignores_a_token_and_logs_in(hass, fake_panel):
    """The control: a token typed for a stock panel changes nothing about
    how the panel is probed, and is stored for the day the panel changes."""
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data=_user_input(fake_panel, **{CONF_TUXWEB_TOKEN: TUXWEB_TOKEN}),
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert fake_panel.logins == 1
    assert fake_panel.polls == 0
    assert result["data"][CONF_TUXWEB_TOKEN] == TUXWEB_TOKEN


async def test_a_blank_token_is_not_stored(hass, fake_panel):
    """An emptied token field is absent from the entry, as the code is: the
    entry without a token is the stock entry, and "" is not that."""
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data=_user_input(fake_panel, **{CONF_TUXWEB_TOKEN: ""}),
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_TUXWEB_TOKEN not in result["data"]


async def test_reauth_takes_a_new_token_and_spends_no_login(
    hass, fake_tuxweb, tuxweb_entry
):
    """The card answered for a token: the web login fields come back as they
    were, the token is new, and the probe behind the Submit is one status
    read on it."""
    tuxweb_entry.add_to_hass(hass)
    fake_tuxweb.token = "e" * 64
    result = await tuxweb_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    with patch(SETUP, return_value=True):
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_USERNAME: fake_tuxweb.username,
                CONF_PASSWORD: fake_tuxweb.password,
                CONF_TUXWEB_TOKEN: "e" * 64,
            },
        )
        await hass.async_block_till_done()

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reauth_successful"
    assert tuxweb_entry.data[CONF_TUXWEB_TOKEN] == "e" * 64
    assert fake_tuxweb.login_attempts == 0
    assert fake_tuxweb.polls == 1


async def test_reauth_with_the_stored_token_asks_the_panel_nothing(
    hass, fake_tuxweb, tuxweb_entry
):
    """Resubmitting the refused token is refused on the form, named as the
    token rather than as a password, and reaches the panel not at all."""
    tuxweb_entry.add_to_hass(hass)
    fake_tuxweb.token = "d" * 64
    result = await tuxweb_entry.start_reauth_flow(hass)

    again = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: fake_tuxweb.username,
            CONF_PASSWORD: fake_tuxweb.password,
            CONF_TUXWEB_TOKEN: TUXWEB_TOKEN,
        },
    )

    assert again["type"] is FlowResultType.FORM
    assert again["errors"] == {"base": "invalid_token"}
    assert fake_tuxweb.api_requests == 0
    assert fake_tuxweb.login_attempts == 0


async def test_reauth_with_a_blank_token_keeps_the_stored_one(
    hass, fake_tuxweb, tuxweb_entry
):
    """A blank secret keeps the stored value on every form, this one too -
    so a stored token plus a blank field is the stored token, resubmitted."""
    tuxweb_entry.add_to_hass(hass)
    fake_tuxweb.token = "d" * 64
    result = await tuxweb_entry.start_reauth_flow(hass)

    again = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: fake_tuxweb.username, CONF_PASSWORD: fake_tuxweb.password},
    )

    assert again["type"] is FlowResultType.FORM
    assert again["errors"] == {"base": "invalid_token"}
    assert tuxweb_entry.data[CONF_TUXWEB_TOKEN] == TUXWEB_TOKEN
    assert fake_tuxweb.api_requests == 0


async def test_reconfigure_with_a_blank_token_keeps_it_and_probes_nothing(
    hass, fake_tuxweb, tuxweb_entry
):
    """Changing the partition alone touches nothing the auth depends on."""
    tuxweb_entry.add_to_hass(hass)
    result = await tuxweb_entry.start_reconfigure_flow(hass)
    with patch(SETUP, return_value=True):
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "127.0.0.1",
                CONF_PORT: fake_tuxweb.port,
                CONF_USE_HTTPS: False,
                CONF_USERNAME: fake_tuxweb.username,
                CONF_PARTITION: 2,
            },
        )
        await hass.async_block_till_done()

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reconfigure_successful"
    assert tuxweb_entry.data[CONF_TUXWEB_TOKEN] == TUXWEB_TOKEN
    assert tuxweb_entry.data[CONF_PARTITION] == 2
    assert fake_tuxweb.capability_probes == 0
    assert fake_tuxweb.api_requests == 0


async def test_the_reconfigure_form_never_shows_the_token(
    hass, fake_tuxweb, tuxweb_entry
):
    """A secret like the password: never a suggested value. The host IS
    suggested, which is what makes the empty answer for the token a check
    rather than a form with no suggestions at all."""
    tuxweb_entry.add_to_hass(hass)
    result = await tuxweb_entry.start_reconfigure_flow(hass)
    fields = {str(key): key for key in result["data_schema"].schema}

    def suggested(marker):
        return (marker.description or {}).get("suggested_value")

    assert suggested(fields[CONF_HOST]) == "127.0.0.1"
    assert suggested(fields[CONF_TUXWEB_TOKEN]) in (None, "")
    assert suggested(fields[CONF_PASSWORD]) in (None, "")


# ---------------------------------------------------------- diagnostics


async def test_the_report_names_the_firmware_and_never_the_token(
    hass, fake_tuxweb, tuxweb_entry
):
    await _setup(hass, tuxweb_entry)

    report = await async_get_config_entry_diagnostics(hass, tuxweb_entry)

    assert report["firmware"] == "tuxweb"
    assert report["capabilities"] == sorted(TUXWEB_CAPABILITIES)
    assert report["config"][CONF_TUXWEB_TOKEN] == "**REDACTED**"
    assert TUXWEB_TOKEN not in str(report)


async def test_the_report_on_stock_says_stock(hass, fake_panel, panel_entry):
    await _setup(hass, panel_entry)

    report = await async_get_config_entry_diagnostics(hass, panel_entry)

    assert report["firmware"] == "stock"
    assert report["capabilities"] == []
