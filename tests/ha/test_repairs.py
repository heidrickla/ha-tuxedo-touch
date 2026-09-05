"""The repair issues and the one flow that fixes a condition for the user.

Driven through Home Assistant's own repairs flow manager rather than by
calling the flow object, so the platform really is discovered and the issue
really is cleared when the flow finishes.
"""

from unittest.mock import patch

from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PORT
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuxedo_touch.api import TuxedoTouchHttpsRequiredError
from custom_components.tuxedo_touch.const import (
    CONF_USE_HTTPS,
    DOMAIN,
    ISSUE_HTTPS_REDIRECT,
    issue_id,
)

from .conftest import ENTRY_DATA, HOST

STATUS = "custom_components.tuxedo_touch.api.TuxedoTouchClient.get_status"


def _plain_http_entry():
    """An entry set up for HTTP against a panel that insists on HTTPS."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Tuxedo Touch ({HOST})",
        unique_id=f"{HOST}:80:1",
        data={**ENTRY_DATA, CONF_USE_HTTPS: False, CONF_PORT: 80},
    )


async def _setup_redirecting(hass, entry):
    entry.add_to_hass(hass)
    with patch(STATUS, side_effect=TuxedoTouchHttpsRequiredError("redirected")):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_the_redirect_raises_a_fixable_issue(hass):
    """Retrying can never clear this, so it must not look like a bad link."""
    entry = _plain_http_entry()
    await _setup_redirecting(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(ISSUE_HTTPS_REDIRECT, entry.entry_id)
    )
    assert issue is not None
    assert issue.is_fixable
    assert issue.severity is ir.IssueSeverity.ERROR


async def test_the_fix_switches_the_entry_to_https_and_clears_the_issue(hass, ready):
    entry = _plain_http_entry()
    await _setup_redirecting(hass, entry)
    this_issue = issue_id(ISSUE_HTTPS_REDIRECT, entry.entry_id)

    manager = repairs_flow_manager(hass)
    assert manager is not None
    with patch(STATUS, return_value=ready):
        form = await manager.async_init(DOMAIN, data={"issue_id": this_issue})
        assert form["type"] is FlowResultType.FORM
        assert form["step_id"] == "confirm"
        done = await manager.async_configure(form["flow_id"], {})
        await hass.async_block_till_done()

    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_USE_HTTPS] is True
    # Port 80 is the pairing the scheme change implies; anything else is the
    # user's own choice and is left alone.
    assert entry.data[CONF_PORT] == 443
    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, this_issue) is None


async def test_a_non_default_port_survives_the_fix(hass, ready):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Tuxedo Touch ({HOST})",
        unique_id=f"{HOST}:8080:1",
        data={**ENTRY_DATA, CONF_USE_HTTPS: False, CONF_PORT: 8080},
    )
    await _setup_redirecting(hass, entry)

    manager = repairs_flow_manager(hass)
    with patch(STATUS, return_value=ready):
        form = await manager.async_init(
            DOMAIN,
            data={"issue_id": issue_id(ISSUE_HTTPS_REDIRECT, entry.entry_id)},
        )
        await manager.async_configure(form["flow_id"], {})
        await hass.async_block_till_done()

    assert entry.data[CONF_USE_HTTPS] is True
    assert entry.data[CONF_PORT] == 8080


async def test_a_recovered_poll_clears_a_standing_issue(hass, ready):
    """The scheme can also be fixed on the panel, and the issue must not
    outlive the condition when it is."""
    entry = _plain_http_entry()
    await _setup_redirecting(hass, entry)
    this_issue = issue_id(ISSUE_HTTPS_REDIRECT, entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, this_issue) is not None

    with patch(STATUS, return_value=ready):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, this_issue) is None


async def test_the_issue_goes_when_the_entry_does(hass):
    entry = _plain_http_entry()
    await _setup_redirecting(hass, entry)
    this_issue = issue_id(ISSUE_HTTPS_REDIRECT, entry.entry_id)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, this_issue) is None


async def test_the_flow_gives_up_when_the_entry_it_names_is_gone(hass, config_entry):
    """An issue can outlive its entry by a moment; writing to a stale id
    would be worse than saying so."""
    config_entry.add_to_hass(hass)
    with patch(STATUS, side_effect=TuxedoTouchHttpsRequiredError("redirected")):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    manager = repairs_flow_manager(hass)
    for stale, data in (
        ("https_redirect_removed", {"entry_id": "does_not_exist"}),
        ("https_redirect_dataless", None),
    ):
        ir.async_create_issue(
            hass,
            DOMAIN,
            stale,
            data=data,
            is_fixable=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_HTTPS_REDIRECT,
            translation_placeholders={"title": "Tuxedo Touch"},
        )
        result = await manager.async_init(DOMAIN, data={"issue_id": stale})
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "entry_gone"
