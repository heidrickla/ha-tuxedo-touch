"""A tuxweb panel over HTTPS, as Home Assistant sees it: trusted on first use,
verified after, and a changed certificate raised as a repair.

The fake panel serves TLS with the chain a tuxweb panel serves (leaf naming
127.0.0.1, then the self-signed authority that issued it; tests/tls_certs.py)
and can be moved to another chain under a running entry. Every request is a
new handshake there, so a certificate change is met by the next request
rather than hidden behind a pooled connection.
"""

import asyncio
import logging
from unittest.mock import patch

import pytest
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.components.repairs import repairs_flow_manager
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

from custom_components.tuxedo_touch import api, push
from custom_components.tuxedo_touch.const import (
    CONF_PANEL_CA,
    CONF_PARTITION,
    CONF_TUXWEB_TOKEN,
    CONF_USE_HTTPS,
    DOMAIN,
    ISSUE_PANEL_CERTIFICATE,
    issue_id,
)
from custom_components.tuxedo_touch.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.fake_panel import TUXWEB_TOKEN, FakePanel, wait_until
from tests.tls_certs import make_panel_tls, same_certificate

from .conftest import setup_form

PANEL = "alarm_control_panel.honeywell_tuxedo_touch_partition_1"
UNTRUSTED_LOG = "does not verify against the certificate authority this entry trusts"


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    """Authorities, leaves and server contexts, built outside the event loop."""
    return make_panel_tls(str(tmp_path_factory.mktemp("tls")))


@pytest.fixture
async def tls_tuxweb(socket_enabled, tls):
    """The fake tuxweb, over TLS with the trusted chain."""
    panel = FakePanel(tuxweb=True, ssl_context=tls.trusted)
    await panel.start()
    yield panel
    await panel.close()


@pytest.fixture(autouse=True)
def _short_reconnect(monkeypatch):
    monkeypatch.setattr(push, "PUSH_BACKOFF_INITIAL", 0.01)


@pytest.fixture(autouse=True)
async def _quiet_server_handshakes():
    """The fake's own side of a refused handshake is not the code under test.

    The fake runs in the test's event loop, and asyncio reports a server-side
    handshake that the client aborted through the loop's exception handler -
    in debug mode only. Home Assistant's test harness turns every such report
    into a failure, so debug mode is off for these tests; the client's side
    of the same handshake is what they assert on. It is not switched back on
    at teardown: a handshake still in flight then would straddle the switch,
    which asyncio does not survive, and each test has a loop of its own.
    """
    asyncio.get_running_loop().set_debug(False)


def _entry(panel, panel_ca=None, *, use_https=True):
    data = {
        CONF_HOST: "127.0.0.1",
        CONF_PORT: panel.port,
        CONF_USE_HTTPS: use_https,
        CONF_USERNAME: panel.username,
        CONF_PASSWORD: panel.password,
        CONF_CODE: "1234",
        CONF_PARTITION: 1,
        CONF_TUXWEB_TOKEN: TUXWEB_TOKEN,
    }
    if panel_ca is not None:
        data[CONF_PANEL_CA] = panel_ca
    return MockConfigEntry(
        domain=DOMAIN,
        title="Tuxedo Touch (127.0.0.1)",
        unique_id=f"127.0.0.1:{panel.port}:1",
        data=data,
    )


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _issue(hass, entry):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(ISSUE_PANEL_CERTIFICATE, entry.entry_id)
    )


def _warnings_about_the_certificate(caplog):
    """The integration's own warnings: Home Assistant logs the failed refresh
    itself as well, and that line is not this integration's to count."""
    return sum(
        record.levelno == logging.WARNING
        and record.name.startswith("custom_components.tuxedo_touch")
        and UNTRUSTED_LOG in record.getMessage()
        for record in caplog.records
    )


def _context_spy():
    """Record every context _verified_ssl_context hands out, cache intact."""
    real = api._verified_ssl_context
    handed_out = []

    def spy(pem):
        made = real(pem)
        handed_out.append(made)
        return made

    return patch.object(api, "_verified_ssl_context", spy), handed_out


# ------------------------------------------------------------ first use


async def test_a_new_entry_is_created_pinned_and_shares_the_flows_context(
    hass, tls, tls_tuxweb
):
    """The flow's probe pins; the entry it creates carries the pin; and the
    coordinator is handed the very context object the flow's proof used, so
    the two share one pool key to the panel."""
    spy, handed_out = _context_spy()
    with spy:
        form = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            setup_form(
                {
                    CONF_HOST: "127.0.0.1",
                    CONF_PORT: tls_tuxweb.port,
                    CONF_USE_HTTPS: True,
                    CONF_USERNAME: tls_tuxweb.username,
                    CONF_PASSWORD: tls_tuxweb.password,
                    CONF_CODE: "1234",
                    CONF_PARTITION: 1,
                    CONF_TUXWEB_TOKEN: TUXWEB_TOKEN,
                }
            ),
        )
        await hass.async_block_till_done()
        entry = result["result"]
        coordinator = entry.runtime_data
        await wait_until(lambda: coordinator.push.connected)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert same_certificate(entry.data[CONF_PANEL_CA], tls.authority.pem)
    assert entry.state is ConfigEntryState.LOADED
    assert coordinator.client.tls == "verified"
    assert handed_out, "nothing was verified"
    assert all(made is handed_out[0] for made in handed_out)
    assert coordinator.client.ssl_arg is handed_out[0]
    assert tls_tuxweb.login_attempts == 0


async def test_an_existing_entry_pins_on_its_first_setup_and_writes_once(
    hass, tls, tls_tuxweb
):
    """The live install's path: no pin stored, so the coordinator's probe
    learns it and writes it to the entry, once, without a reload loop."""
    entry = _entry(tls_tuxweb)
    entry.add_to_hass(hass)
    real_update = hass.config_entries.async_update_entry
    with patch.object(
        hass.config_entries, "async_update_entry", wraps=real_update
    ) as update:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert same_certificate(entry.data.get(CONF_PANEL_CA), tls.authority.pem)
        assert entry.runtime_data.client.tls == "verified"
        await wait_until(lambda: entry.runtime_data.push.connected)

        # The fake holds a stream open until told to let go; let this one go
        # so only the reloaded entry's stream is open at the end.
        await hass.config_entries.async_unload(entry.entry_id)
        tls_tuxweb.drop_stream()
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        # Connected before the test ends, or the fake's teardown can let go
        # of streams before this one has started waiting to be let go of.
        await wait_until(lambda: entry.runtime_data.push.connected)

    pin_writes = [
        call
        for call in update.call_args_list
        if CONF_PANEL_CA in (call.kwargs.get("data") or {})
    ]
    assert len(pin_writes) == 1
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.client.tls == "verified"


async def test_an_unverifiable_chain_pins_nothing_and_loads_as_before(
    hass, tls, tls_tuxweb
):
    await tls_tuxweb.use_tls(tls.broken)
    entry = _entry(tls_tuxweb)
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert CONF_PANEL_CA not in entry.data
    assert entry.runtime_data.client.tls == "legacy"
    await wait_until(lambda: entry.runtime_data.push.connected)
    report = await async_get_config_entry_diagnostics(hass, entry)
    assert report["tls"] == "legacy"
    assert report["panel_ca"] is None


# -------------------------------------------------- a changed certificate


async def test_a_pinned_entry_meeting_another_authority_raises_the_repair(
    hass, tls, tls_tuxweb, caplog
):
    """Setup fails without a login and without a request on the legacy
    context; the repair goes when the trusted certificate is back, and with
    the entry when it is removed."""
    await tls_tuxweb.use_tls(tls.other)
    entry = _entry(tls_tuxweb, tls.authority.pem)
    with caplog.at_level(logging.WARNING):
        await _setup(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.is_fixable
    assert issue.severity is ir.IssueSeverity.ERROR
    assert tls_tuxweb.capability_probes == 0
    assert tls_tuxweb.login_page_requests == 0
    assert tls_tuxweb.login_attempts == 0
    assert tls_tuxweb.api_requests == 0
    assert _warnings_about_the_certificate(caplog) == 1
    assert entry.data[CONF_PANEL_CA] == tls.authority.pem

    await tls_tuxweb.use_tls(tls.trusted)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert _issue(hass, entry) is None
    await wait_until(lambda: entry.runtime_data.push.connected)

    await tls_tuxweb.use_tls(tls.other)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is not None
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None


async def test_a_running_entry_fails_its_poll_and_stops_its_stream(
    hass, tls, tls_tuxweb, caplog
):
    """The poll becomes UpdateFailed with the repair raised, warned once; the
    stream stops rather than reconnecting; a command fails; and the repair
    goes the moment a verified poll succeeds again."""
    entry = _entry(tls_tuxweb, tls.authority.pem)
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    await wait_until(lambda: coordinator.push.connected)
    assert coordinator.last_update_success

    await tls_tuxweb.use_tls(tls.other)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        # While the entity is still available on the stream, a command fails
        # on the certificate and raises the repair itself.
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                ALARM_DOMAIN,
                "alarm_arm_away",
                {ATTR_ENTITY_ID: PANEL, "code": "1234"},
                blocking=True,
            )
        assert _issue(hass, entry) is not None

        await coordinator.async_refresh()
        assert not coordinator.last_update_success

        tls_tuxweb.drop_stream()
        await wait_until(lambda: coordinator.push.certificate_failed)
        await hass.async_block_till_done()
        assert not coordinator.push.connected
        assert tls_tuxweb.stream_requests == 1
        assert hass.states.get(PANEL).state == "unavailable"
        await coordinator.async_refresh()
    assert _warnings_about_the_certificate(caplog) == 1
    assert tls_tuxweb.login_page_requests == 0
    assert tls_tuxweb.login_attempts == 0
    assert tls_tuxweb.stream_requests == 1

    await tls_tuxweb.use_tls(tls.trusted)
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert _issue(hass, entry) is None


async def test_a_failed_poll_raises_the_repair_on_its_own(hass, tls, tls_tuxweb):
    entry = _entry(tls_tuxweb, tls.authority.pem)
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    await wait_until(lambda: coordinator.push.connected)

    await tls_tuxweb.use_tls(tls.other)
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert _issue(hass, entry) is not None
    assert tls_tuxweb.login_attempts == 0


# ------------------------------------------------------------ the fix flow


async def _fix_flow(hass, entry):
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return manager, await manager.async_init(
        DOMAIN,
        data={"issue_id": issue_id(ISSUE_PANEL_CERTIFICATE, entry.entry_id)},
    )


async def test_the_fix_shows_the_new_certificate_and_pins_it(hass, tls, tls_tuxweb):
    await tls_tuxweb.use_tls(tls.other)
    entry = _entry(tls_tuxweb, tls.authority.pem)
    await _setup(hass, entry)
    assert _issue(hass, entry) is not None

    manager, form = await _fix_flow(hass, entry)
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "accept"
    shown = form["description_placeholders"]
    assert shown["fingerprint"] == tls.other_authority.fingerprint
    assert shown["subject"] == tls.other_authority.subject
    assert shown["issuer"] == tls.other_authority.subject
    assert shown["expires"] == tls.other_leaf.expires
    assert tls_tuxweb.login_attempts == 0

    done = await manager.async_configure(form["flow_id"], {})
    await hass.async_block_till_done()
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert same_certificate(entry.data[CONF_PANEL_CA], tls.other_authority.pem)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.client.tls == "verified"
    assert _issue(hass, entry) is None
    await wait_until(lambda: entry.runtime_data.push.connected)


async def test_the_fix_removes_the_pin_from_a_panel_back_on_stock(
    hass, tls, tls_tuxweb
):
    await tls_tuxweb.use_tls(tls.other)
    tls_tuxweb.tuxweb = False
    entry = _entry(tls_tuxweb, tls.authority.pem)
    await _setup(hass, entry)
    assert _issue(hass, entry) is not None

    manager, form = await _fix_flow(hass, entry)
    assert form["step_id"] == "remove_pin"
    assert tls_tuxweb.login_attempts == 0
    done = await manager.async_configure(form["flow_id"], {})
    await hass.async_block_till_done()

    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_PANEL_CA not in entry.data
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.client.tuxweb is False
    assert entry.runtime_data.client.tls == "legacy"
    assert _issue(hass, entry) is None
    await wait_until(lambda: entry.runtime_data.push.connected)


async def test_the_fix_refuses_an_authority_that_does_not_verify_the_panel(
    hass, tls, tls_tuxweb
):
    await tls_tuxweb.use_tls(tls.broken)
    entry = _entry(tls_tuxweb, tls.other_authority.pem)
    await _setup(hass, entry)
    assert _issue(hass, entry) is not None

    _, result = await _fix_flow(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "certificate_unverifiable"
    assert entry.data[CONF_PANEL_CA] == tls.other_authority.pem


async def test_the_fix_aborts_when_the_panel_cannot_be_reached(hass, tls, tls_tuxweb):
    await tls_tuxweb.use_tls(tls.other)
    entry = _entry(tls_tuxweb, tls.authority.pem)
    await _setup(hass, entry)
    tls_tuxweb.capability_probe_disconnects = 4

    _, result = await _fix_flow(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_the_fix_aborts_for_an_entry_that_is_gone(hass, tls, tls_tuxweb):
    await tls_tuxweb.use_tls(tls.other)
    entry = _entry(tls_tuxweb, tls.authority.pem)
    await _setup(hass, entry)
    manager = repairs_flow_manager(hass)
    flow_id = issue_id(ISSUE_PANEL_CERTIFICATE, entry.entry_id)
    with patch.object(hass.config_entries, "async_get_entry", return_value=None):
        result = await manager.async_init(DOMAIN, data={"issue_id": flow_id})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_gone"


# ------------------------------------------------- reconfigure, diagnostics


async def test_reconfigure_keeps_the_pin_and_names_a_certificate_failure(
    hass, tls, tls_tuxweb
):
    entry = _entry(tls_tuxweb, tls.authority.pem)
    await _setup(hass, entry)
    await wait_until(lambda: entry.runtime_data.push.connected)
    form_data = {
        CONF_HOST: "127.0.0.1",
        CONF_PORT: tls_tuxweb.port,
        CONF_USE_HTTPS: True,
        CONF_USERNAME: tls_tuxweb.username,
        CONF_PARTITION: 1,
    }

    # Nothing the probe depends on changed: saved without one, pin kept.
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
        data={**form_data, CONF_CODE: "4321"},
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PANEL_CA] == tls.authority.pem
    assert entry.data[CONF_CODE] == "4321"

    # A probe against a certificate the pin does not verify: a form error.
    await tls_tuxweb.use_tls(tls.other)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
        data={**form_data, CONF_PASSWORD: "changed"},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "certificate_untrusted"}
    assert entry.data[CONF_PANEL_CA] == tls.authority.pem
    assert tls_tuxweb.login_attempts == 0


async def test_diagnostics_report_a_verified_connection_and_its_authority(
    hass, tls, tls_tuxweb
):
    entry = _entry(tls_tuxweb, tls.authority.pem)
    await _setup(hass, entry)
    await wait_until(lambda: entry.runtime_data.push.connected)

    report = await async_get_config_entry_diagnostics(hass, entry)
    assert report["tls"] == "verified"
    assert report["panel_ca"] == {
        "subject": tls.authority.subject,
        "sha256": tls.authority.fingerprint,
    }
    assert CONF_PANEL_CA not in report["config"]
    assert "BEGIN CERTIFICATE" not in str(report)
    assert report["push"]["certificate_failed"] is False


async def test_diagnostics_report_plain_http(hass, fake_tuxweb, tuxweb_entry):
    tuxweb_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(tuxweb_entry.entry_id)
    await hass.async_block_till_done()
    await wait_until(lambda: tuxweb_entry.runtime_data.push.connected)

    report = await async_get_config_entry_diagnostics(hass, tuxweb_entry)
    assert report["tls"] == "plain"
    assert report["panel_ca"] is None
