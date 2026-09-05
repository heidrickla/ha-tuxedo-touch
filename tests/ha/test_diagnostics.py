"""The diagnostics download: useful about the panel, quiet about the user."""

from unittest.mock import patch

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME

from custom_components.tuxedo_touch.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.fake_panel import wait_until

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
    """The raw strings, which source spelled them, and whether the fallback
    poll worked. `Not available` is the reason a poll-only install sits
    unavailable while the panel is up, so those are the first things worth
    seeing in a bug report."""
    await _setup(hass, config_entry, ready)

    report = await async_get_config_entry_diagnostics(hass, config_entry)

    assert report["panel_status"] == "Ready To Arm"
    assert report["panel_color"] == "green"
    assert report["status_source"] == "poll"
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
    assert report["status_source"] is None


async def test_the_report_says_what_the_stream_is_doing(hass, config_entry, ready):
    """The stream is the source the state comes from, so a report that cannot
    say whether it is up says nothing about why the entity looks wrong. This
    entry's stream never connected, which the block has to show as plainly as
    a connected one."""
    await _setup(hass, config_entry, ready)

    report = await async_get_config_entry_diagnostics(hass, config_entry)

    push = report["push"]
    assert push["connected"] is False
    assert push["unsupported"] is False
    assert push["connection_id"] is None
    assert push["client_count"] is None
    assert push["frames"] == 0
    assert push["reconnect_wait"] > 0


async def test_a_connected_stream_shows_in_the_report(hass, fake_panel, panel_entry):
    """Against a panel that really streams: the report names the stream as the
    source of the state on screen, and carries the stream's own account of
    itself - the connection the panel handed out, how many clients it thinks
    it has, and that frames have actually arrived."""
    panel_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(panel_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = panel_entry.runtime_data
    await wait_until(lambda: coordinator.push.connected)
    await fake_panel.push(b"['ud','SimpleDbgServer2ClientIntf','noOfClient',[1]]")
    await fake_panel.push_status_text("Armed Away", armed=True)
    await wait_until(lambda: coordinator.data.source == "stream")

    report = await async_get_config_entry_diagnostics(hass, panel_entry)

    assert report["status_source"] == "stream"
    assert report["panel_status"] == "Armed Away"
    push = report["push"]
    assert push["connected"] is True
    assert push["unsupported"] is False
    assert push["connection_id"] == 7
    assert push["client_count"] == 1
    assert push["frames"] > 0
