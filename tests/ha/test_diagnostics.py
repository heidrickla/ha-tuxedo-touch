"""The diagnostics download: useful about the panel, quiet about the user."""

from unittest.mock import patch

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME

from custom_components.tuxedo_touch.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import HOST

STATUS = "custom_components.tuxedo_touch.api.TuxedoTouchClient.get_status"


async def _setup(hass, entry, status):
    entry.add_to_hass(hass)
    with patch(STATUS, return_value=status):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_the_report_carries_the_panel_strings_and_no_secrets(
    hass, config_entry, ready
):
    """`Not available` is the usual reason the entity is unavailable while the
    panel is up, so the raw strings and whether the last poll succeeded are the
    first things worth seeing in a bug report."""
    await _setup(hass, config_entry, ready)

    report = await async_get_config_entry_diagnostics(hass, config_entry)

    assert report["panel_status"] == "Ready To Arm"
    assert report["panel_color"] == "green"
    assert report["partition"] == 1
    assert report["update_interval"] == "0:00:30"
    assert report["last_update_success"] is True
    config = report["config"]
    for key in (CONF_HOST, CONF_USERNAME, CONF_PASSWORD, "code"):
        assert config[key] == "**REDACTED**"
    assert HOST not in str(report)
    assert "secret" not in str(report)


async def test_a_panel_that_never_answered_reports_no_status(hass, config_entry, ready):
    """A report is most wanted when nothing is working, so it must not
    depend on there being data. A panel stuck on `Not available` stores none:
    the poll fails, so the report has to say so and carry no status."""
    await _setup(hass, config_entry, ready)
    coordinator = config_entry.runtime_data
    coordinator.data = None
    coordinator.last_update_success = False

    report = await async_get_config_entry_diagnostics(hass, config_entry)

    assert report["last_update_success"] is False
    assert report["panel_status"] is None
    assert report["panel_color"] is None
