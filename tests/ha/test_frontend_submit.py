"""Each setup form with an Advanced section, submitted the way the dialog sends it.

The data comes from the schema the frontend receives, through a port of its
initial-data code (frontend_form.py); Advanced is never opened.
"""

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

from custom_components.tuxedo_touch.api import TuxedoTouchConnectionError
from custom_components.tuxedo_touch.const import (
    CONF_ADVANCED,
    CONF_MAC,
    CONF_PARTITION,
    CONF_USE_HTTPS,
    DOMAIN,
    ISSUE_CREDENTIALS_REJECTED,
    issue_id,
)

from .conftest import HOST, MAC
from .frontend_form import frontend_submission
from .test_config_flow import LOGIN, SETUP, _dhcp_flow, _lease, _rejected_entry

TYPED = {CONF_HOST: HOST, CONF_USERNAME: "installer", CONF_PASSWORD: "secret"}


async def test_the_first_screen_submits_with_advanced_collapsed(hass):
    form = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(LOGIN, return_value=None), patch(SETUP, return_value=True):
        done = await hass.config_entries.flow.async_configure(
            form["flow_id"], frontend_submission(form, TYPED)
        )
        await hass.async_block_till_done()
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["data"] == {
        **TYPED,
        CONF_PORT: 443,
        CONF_USE_HTTPS: True,
        CONF_PARTITION: 1,
    }


async def test_the_first_screen_shown_again_after_an_error_submits(hass):
    form = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(LOGIN, side_effect=TuxedoTouchConnectionError("down")):
        shown = await hass.config_entries.flow.async_configure(
            form["flow_id"], frontend_submission(form, TYPED)
        )
    assert shown["errors"] == {"base": "cannot_connect"}

    # The address, user name and Advanced come back filled in; the password
    # is never echoed, so it is typed again.
    with patch(LOGIN, return_value=None), patch(SETUP, return_value=True):
        done = await hass.config_entries.flow.async_configure(
            shown["flow_id"], frontend_submission(shown, {CONF_PASSWORD: "secret"})
        )
        await hass.async_block_till_done()
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["data"][CONF_PORT] == 443


async def test_a_discovered_panel_submits_with_advanced_collapsed(hass):
    form = await _dhcp_flow(hass, _lease("203.0.113.61"))
    assert form["step_id"] == "dhcp_confirm"
    login = {CONF_USERNAME: "installer", CONF_PASSWORD: "secret"}
    with patch(LOGIN, return_value=None), patch(SETUP, return_value=True):
        done = await hass.config_entries.flow.async_configure(
            form["flow_id"], frontend_submission(form, login)
        )
        await hass.async_block_till_done()
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["data"][CONF_HOST] == "203.0.113.61"
    assert done["data"][CONF_MAC] == MAC


async def test_the_retry_question_submits_untouched(hass, fake_panel):
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
        frontend_submission(
            result,
            {CONF_USERNAME: fake_panel.username, CONF_PASSWORD: fake_panel.password},
        ),
    )
    assert asked["step_id"] == "reauth_retry"
    done = await hass.config_entries.flow.async_configure(
        asked["flow_id"], frontend_submission(asked)
    )
    await hass.async_block_till_done()
    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reauth_successful"


async def test_what_was_typed_in_advanced_comes_back_after_an_error(hass):
    form = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    sent = frontend_submission(form, TYPED)
    sent[CONF_ADVANCED][CONF_PORT] = 8443
    with patch(LOGIN, side_effect=TuxedoTouchConnectionError("down")):
        shown = await hass.config_entries.flow.async_configure(form["flow_id"], sent)
    assert shown["errors"] == {"base": "cannot_connect"}

    # The section reopens holding the typed port, and Submit sends it back.
    again = frontend_submission(shown, {CONF_PASSWORD: "secret"})
    assert again[CONF_ADVANCED][CONF_PORT] == 8443
