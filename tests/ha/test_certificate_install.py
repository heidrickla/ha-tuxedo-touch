"""Home Assistant issuing a tuxweb panel its certificate and installing it.

The fake panel serves TLS and takes InstallCertificate the way the firmware
contract says (tests/fake_panel.py), serving an accepted pair from the next
handshake. Each request is a new handshake there, so a test sees what a new
connection verifies against.
"""

import asyncio
import datetime
import hashlib
import ipaddress
import logging
import os
import ssl
from http import HTTPStatus
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_CODE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.tuxedo_touch import api, certificate, push
from custom_components.tuxedo_touch.ca_view import download_name
from custom_components.tuxedo_touch.const import (
    CONF_CA_CERT,
    CONF_CA_KEY,
    CONF_CERT_NOT_AFTER,
    CONF_PANEL_CA,
    CONF_PARTITION,
    CONF_TUXWEB_TOKEN,
    CONF_USE_HTTPS,
    DOMAIN,
    ISSUE_CERTIFICATE_RENEWAL_FAILED,
    issue_id,
)
from custom_components.tuxedo_touch.diagnostics import (
    REDACT,
    async_get_config_entry_diagnostics,
)
from tests.fake_panel import (
    CERTIFICATE_INSTALL,
    TUXWEB_CAPABILITIES,
    TUXWEB_TOKEN,
    FakePanel,
    wait_until,
)
from tests.tls_certs import make_leaf, make_panel_tls, server_context

from .conftest import ENTRY_DATA

BUTTON = "button.honeywell_tuxedo_touch_renew_certificate"
ENTRY_ID = "01JTUXEDOTESTENTRYABCDEFGH"
CAPABLE = (*TUXWEB_CAPABILITIES, CERTIFICATE_INSTALL)


def _context_from_pem(directory: str, chain_pem: str, key_pem: str) -> ssl.SSLContext:
    os.makedirs(directory, exist_ok=True)
    name = hashlib.sha256(chain_pem.encode()).hexdigest()[:16]
    chain_path = os.path.join(directory, f"{name}.chain.pem")
    key_path = os.path.join(directory, f"{name}.key.pem")
    with open(chain_path, "w", encoding="ascii") as handle:
        handle.write(chain_pem)
    with open(key_path, "w", encoding="ascii") as handle:
        handle.write(key_pem)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(chain_path, key_path)
    return ctx


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    """Built outside the event loop: the owner's chains, and the entry's own CA."""
    directory = str(tmp_path_factory.mktemp("tls"))
    panel = make_panel_tls(directory)
    own_cert, own_key = certificate.generate_authority(ENTRY_ID)
    moved = certificate.issue_leaf(own_cert, own_key, "127.0.0.2")
    soon = make_leaf(panel.authority, days=10)
    return {
        "panel": panel,
        "own_cert": own_cert,
        "own_key": own_key,
        # A certificate of the entry's own CA for the address the panel had.
        "moved": _context_from_pem(directory, moved.chain_pem, moved.key_pem),
        # The owner's certificate, ten days from expiry.
        "expiring": server_context(
            os.path.join(directory, "soon"), soon, [panel.authority.cert]
        ),
    }


@pytest.fixture
async def tls_tuxweb(socket_enabled, tls, tmp_path):
    """A tuxweb over TLS declaring certificate_install, owner chain served."""
    panel = FakePanel(
        tuxweb=True,
        capabilities=CAPABLE,
        ssl_context=tls["panel"].trusted,
        tls_dir=str(tmp_path / "installed"),
    )
    await panel.start()
    yield panel
    await panel.close()


@pytest.fixture
async def tls_stock(socket_enabled, tls):
    panel = FakePanel(tuxweb=False, ssl_context=tls["panel"].trusted)
    await panel.start()
    yield panel
    await panel.close()


@pytest.fixture(autouse=True)
def _short_reconnect(monkeypatch):
    monkeypatch.setattr(push, "PUSH_BACKOFF_INITIAL", 0.01)


@pytest.fixture(autouse=True)
async def _quiet_server_handshakes():
    """See tests/ha/test_tls.py: the fake's own refused handshakes are not
    the code under test, and debug mode would turn them into failures."""
    asyncio.get_running_loop().set_debug(False)


def _entry(panel, *, panel_ca=None, own=None, use_https=True, **extra):
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
    if own is not None:
        data[CONF_CA_CERT], data[CONF_CA_KEY] = own
    data.update(extra)
    return MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        title="Tuxedo Touch (127.0.0.1)",
        unique_id=f"127.0.0.1:{panel.port}:1",
        data=data,
    )


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _settled(hass, entry)


async def _settled(hass, entry):
    """Wait out a certificate check and any reload it caused, then the stream.

    The check runs as a background task, which async_block_till_done does not
    wait for, and a renewal it makes reloads the entry.
    """
    for _ in range(3):
        if entry.state is not ConfigEntryState.LOADED:
            return
        coordinator = entry.runtime_data
        if coordinator._certificate_task is not None:
            await coordinator._certificate_task
        await hass.async_block_till_done()
        if entry.runtime_data is coordinator:
            break
    if entry.state is not ConfigEntryState.LOADED:
        return
    await wait_until(lambda: entry.runtime_data.push.connected)


async def _press(hass):
    await hass.services.async_call(
        "button", "press", {"entity_id": BUTTON}, blocking=True
    )


def _renewal_issue(hass):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(ISSUE_CERTIFICATE_RENEWAL_FAILED, ENTRY_ID)
    )


def _own(tls):
    return tls["own_cert"], tls["own_key"]


def _not_after(days):
    return (dt_util.utcnow() + datetime.timedelta(days=days)).isoformat()


# ------------------------------------------------------------------ the gate


@pytest.mark.parametrize("panel_kind", ["stock", "plain", "no_capability"])
async def test_no_button_and_no_authority_outside_the_gate(
    hass, tls, tls_tuxweb, tls_stock, fake_tuxweb, panel_kind
):
    """Stock firmware, plain HTTP and a tuxweb that does not declare the
    capability: no button, no authority generated, nothing watched."""
    if panel_kind == "stock":
        entry = _entry(tls_stock)
    elif panel_kind == "plain":
        fake_tuxweb.capabilities = list(CAPABLE)
        entry = _entry(fake_tuxweb, use_https=False)
    else:
        tls_tuxweb.capabilities = list(TUXWEB_CAPABILITIES)
        entry = _entry(tls_tuxweb, panel_ca=tls["panel"].authority.pem)
    # A button left from when the panel did declare it is removed.
    er.async_get(hass).async_get_or_create(
        "button",
        DOMAIN,
        f"{ENTRY_ID}_renew_certificate",
        suggested_object_id="honeywell_tuxedo_touch_renew_certificate",
    )
    with patch.object(
        api.TuxedoTouchClient, "async_read_presented_chain"
    ) as chain_reads:
        await _setup(hass, entry)
        assert entry.state is ConfigEntryState.LOADED
        await hass.async_block_till_done()

    assert hass.states.get(BUTTON) is None
    assert er.async_get(hass).async_get(BUTTON) is None
    assert CONF_CA_CERT not in entry.data
    assert entry.runtime_data._certificate_task is None
    chain_reads.assert_not_called()
    assert entry.runtime_data.client.certificate_install is False


async def test_the_button_exists_under_the_gate(hass, tls, tls_tuxweb):
    entry = _entry(tls_tuxweb, panel_ca=tls["panel"].authority.pem)
    await _setup(hass, entry)
    state = hass.states.get(BUTTON)
    assert state is not None
    registered = er.async_get(hass).async_get(BUTTON)
    assert registered.entity_category is er.EntityCategory.CONFIG
    assert registered.translation_key == "renew_certificate"
    assert entry.runtime_data.client.certificate_install is True
    # Setting up generates nothing: the authority comes with the first issue.
    assert CONF_CA_CERT not in entry.data
    assert tls_tuxweb.install_requests == 0


# ------------------------------------------------------- the certificates


def test_the_authority_is_what_the_design_says():
    cert_pem, key_pem = certificate.generate_authority(ENTRY_ID)
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    key = serialization.load_pem_private_key(key_pem.encode(), None)
    assert key_pem.startswith("-----BEGIN PRIVATE KEY-----")
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    assert key.curve.name == "secp256r1"
    assert cert.subject == cert.issuer
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == (
        f"Tuxedo Touch CA {ENTRY_ID[-8:]}"
    )
    assert (
        cert.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)[0].value
        == "Home Assistant"
    )
    constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert constraints.critical
    assert constraints.value.ca is True
    assert constraints.value.path_length == 0
    usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    assert usage.key_cert_sign
    assert not usage.digital_signature
    assert not usage.crl_sign
    now = datetime.datetime.now(datetime.UTC)
    assert cert.not_valid_before_utc < now
    assert now - cert.not_valid_before_utc >= datetime.timedelta(minutes=4)
    span = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert span == datetime.timedelta(days=3650)
    cert.verify_directly_issued_by(cert)


@pytest.mark.parametrize(
    ("host", "kind"),
    [("192.0.2.15", x509.IPAddress), ("panel.example", x509.DNSName)],
)
def test_the_leaf_names_the_host_in_its_san(host, kind):
    ca_pem, ca_key = certificate.generate_authority(ENTRY_ID)
    issued = certificate.issue_leaf(ca_pem, ca_key, host)
    certs = x509.load_pem_x509_certificates(issued.chain_pem.encode())
    leaf, authority = certs
    assert authority == x509.load_pem_x509_certificate(ca_pem.encode())
    leaf.verify_directly_issued_by(authority)
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    names = san.get_values_for_type(kind)
    expected = ipaddress.ip_address(host) if kind is x509.IPAddress else host
    assert names == [expected]
    other = x509.DNSName if kind is x509.IPAddress else x509.IPAddress
    assert san.get_values_for_type(other) == []
    assert leaf.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == host
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert (leaf.not_valid_after_utc - leaf.not_valid_before_utc) == datetime.timedelta(
        days=397
    )
    key = serialization.load_pem_private_key(issued.key_pem.encode(), None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    assert key.curve.name == "secp256r1"
    assert issued.sha256 == leaf.fingerprint(hashes.SHA256()).hex()
    assert certificate.leaf_names_host(leaf, host)
    assert not certificate.leaf_names_host(leaf, "198.51.100.1")
    assert not certificate.leaf_names_host(leaf, "other.example")


def test_the_leaf_starts_before_now():
    """The panel refuses a notBefore after its own clock, with no allowance."""
    ca_pem, ca_key = certificate.generate_authority(ENTRY_ID)
    issued = certificate.issue_leaf(ca_pem, ca_key, "192.0.2.15")
    leaf = x509.load_pem_x509_certificates(issued.chain_pem.encode())[0]
    now = datetime.datetime.now(datetime.UTC)
    assert leaf.not_valid_before_utc < now
    assert now - leaf.not_valid_before_utc >= datetime.timedelta(minutes=4)
    assert now - leaf.not_valid_before_utc <= datetime.timedelta(minutes=6)


def test_a_long_host_goes_in_the_san_only():
    ca_pem, ca_key = certificate.generate_authority(ENTRY_ID)
    host = "a" * 60 + ".example"
    leaf = x509.load_pem_x509_certificates(
        certificate.issue_leaf(ca_pem, ca_key, host).chain_pem.encode()
    )[0]
    assert leaf.subject.get_attributes_for_oid(NameOID.COMMON_NAME) == []
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert san.critical
    assert san.value.get_values_for_type(x509.DNSName) == [host]


def test_a_chain_is_read_for_its_expiry_and_its_move(tls):
    assert certificate.leaf_expiry([]) is None
    assert certificate.leaf_expiry([b"not der"]) is None
    assert certificate.moved_from([], tls["own_cert"], "127.0.0.1") is False
    own_leaf = certificate.issue_leaf(tls["own_cert"], tls["own_key"], "127.0.0.2")
    der = [
        cert.public_bytes(serialization.Encoding.DER)
        for cert in x509.load_pem_x509_certificates(own_leaf.chain_pem.encode())
    ]
    assert certificate.moved_from(der, tls["own_cert"], "127.0.0.1") is True
    assert certificate.moved_from(der, tls["own_cert"], "127.0.0.2") is False
    # A certificate another authority issued is no move of ours.
    owner = tls["panel"].leaf.cert.public_bytes(serialization.Encoding.DER)
    assert certificate.moved_from([owner], tls["own_cert"], "127.0.0.9") is False
    leaf = x509.load_der_x509_certificate(owner)
    no_san = x509.CertificateBuilder(
        issuer_name=leaf.issuer,
        subject_name=leaf.subject,
        public_key=leaf.public_key(),
        serial_number=1,
        not_valid_before=leaf.not_valid_before_utc,
        not_valid_after=leaf.not_valid_after_utc,
    ).sign(tls["panel"].authority.key, hashes.SHA256())
    assert certificate.leaf_names_host(no_san, "127.0.0.1") is False


# ------------------------------------------------------------- the button


async def test_pressing_installs_moves_the_pin_and_reloads(hass, tls, tls_tuxweb):
    """Issued from a new authority of the entry's own, installed over the
    verifying context, the pin moved to it, and the reload verifying the new
    certificate on its next connection. No login anywhere."""
    owner = tls["panel"].authority.pem
    entry = _entry(tls_tuxweb, panel_ca=owner)
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    assert coordinator.client.tls == "verified"

    await _press(hass)
    await hass.async_block_till_done()
    assert tls_tuxweb.install_requests == 1
    assert len(tls_tuxweb.installed) == 1
    assert tls_tuxweb.last_install_headers["Authorization"] == f"Bearer {TUXWEB_TOKEN}"
    assert tls_tuxweb.last_install_headers["Content-Type"] == "application/json"
    own = entry.data[CONF_CA_CERT]
    assert entry.data[CONF_PANEL_CA] == own
    assert own != owner
    assert entry.data[CONF_CA_KEY].startswith("-----BEGIN PRIVATE KEY-----")
    installed = tls_tuxweb.installed[0]
    assert entry.data[CONF_CERT_NOT_AFTER] == installed.not_valid_after_utc.isoformat()
    installed.verify_directly_issued_by(x509.load_pem_x509_certificate(own.encode()))
    # The leaf's key went to the panel and is not stored.
    assert [k for k, v in entry.data.items() if "PRIVATE KEY" in str(v)] == [
        CONF_CA_KEY
    ]

    await _settled(hass, entry)
    reloaded = entry.runtime_data
    assert reloaded is not coordinator
    assert entry.state is ConfigEntryState.LOADED
    assert reloaded.client.tls == "verified"
    assert reloaded.client.ssl_arg is api._verified_ssl_context(own)
    await reloaded.async_refresh()
    assert reloaded.last_update_success
    assert tls_tuxweb.login_attempts == 0
    assert tls_tuxweb.login_page_requests == 0


async def test_connections_verify_while_the_panel_switches(hass, tls, tls_tuxweb):
    """Between the panel's 200 and the reload, a new connection meets the new
    certificate and still verifies; the stream opened before stays up."""
    entry = _entry(tls_tuxweb, panel_ca=tls["panel"].authority.pem)
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await _press(hass)
    reload.assert_called_once_with(ENTRY_ID)
    assert coordinator.push.connected
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.client.panel_ca == entry.data[CONF_CA_CERT]


async def test_the_authority_is_generated_once_only(hass, tls, tls_tuxweb):
    entry = _entry(tls_tuxweb, panel_ca=tls["panel"].authority.pem)
    with patch.object(
        certificate, "generate_authority", wraps=certificate.generate_authority
    ) as generated:
        await _setup(hass, entry)
        await _press(hass)
        await hass.async_block_till_done()
        await _settled(hass, entry)
        first = entry.data[CONF_CA_CERT], entry.data[CONF_CA_KEY]
        await _press(hass)
        await hass.async_block_till_done()
        await _settled(hass, entry)
    assert generated.call_count == 1
    assert (entry.data[CONF_CA_CERT], entry.data[CONF_CA_KEY]) == first
    assert tls_tuxweb.install_requests == 2
    second = tls_tuxweb.installed[1]
    second.verify_directly_issued_by(x509.load_pem_x509_certificate(first[0].encode()))


@pytest.mark.parametrize(
    ("status", "body_reason", "reason"),
    [
        (400, "bad_json", "bad_json"),
        (400, "bad_chain", "bad_chain"),
        (400, "bad_key", "bad_key"),
        (400, "key_mismatch", "key_mismatch"),
        (400, "not_yet_valid", "not_yet_valid"),
        (400, "expired", "expired"),
        (400, "no_san", "no_san"),
        (401, "", "token_rejected"),
        (403, "tls_required", "tls_required"),
        (405, "", "not_supported"),
        (409, "tls_not_configured", "tls_not_configured"),
        (413, "too_large", "too_large"),
        (500, "write_failed", "write_failed"),
        (502, "", "http_502"),
    ],
)
async def test_each_refusal_is_named_and_changes_nothing(
    hass, tls, tls_tuxweb, status, body_reason, reason
):
    owner = tls["panel"].authority.pem
    entry = _entry(tls_tuxweb, panel_ca=owner)
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    before = dict(entry.data)
    tls_tuxweb.install_error = (status, body_reason)

    with (
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
        pytest.raises(HomeAssistantError) as raised,
    ):
        await _press(hass)
    assert raised.value.translation_key == "certificate_renewal_failed"
    assert raised.value.translation_placeholders == {"reason": reason}
    assert dict(entry.data) == before
    reload.assert_not_called()
    # One request, never retried, no login, and the client trusts what it did.
    assert tls_tuxweb.install_requests == 1
    assert tls_tuxweb.login_attempts == 0
    assert coordinator.client.own_ca is None
    assert coordinator.client.ssl_arg is api._verified_ssl_context(owner)
    # A button press raises no repair.
    assert _renewal_issue(hass) is None


async def test_an_answer_naming_another_certificate_is_refused(hass, tls, tls_tuxweb):
    entry = _entry(tls_tuxweb, panel_ca=tls["panel"].authority.pem)
    await _setup(hass, entry)
    before = dict(entry.data)
    real = api.TuxedoTouchClient.async_install_certificate

    async def lying(self, chain, key, **kwargs):
        answer = await real(self, chain, key, **kwargs)
        return api.InstalledCertificate(answer.not_after, "00" * 32)

    with (
        patch.object(api.TuxedoTouchClient, "async_install_certificate", lying),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await _press(hass)
    assert raised.value.translation_placeholders == {"reason": "unexpected_answer"}
    assert dict(entry.data) == before


async def test_a_panel_that_cannot_be_reached_is_named(hass, tls, tls_tuxweb):
    entry = _entry(tls_tuxweb, panel_ca=tls["panel"].authority.pem)
    await _setup(hass, entry)
    with (
        patch.object(
            api.TuxedoTouchClient,
            "async_install_certificate",
            side_effect=api.TuxedoTouchUnreachable("gone"),
        ),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await _press(hass)
    assert raised.value.translation_placeholders == {"reason": "unreachable"}


async def test_an_unverified_client_sends_no_key(hass, tls, tls_tuxweb):
    """An entry whose trust repair is unanswered connects unverified, and the
    new private key never goes out on that connection."""
    entry = _entry(tls_tuxweb)
    await _setup(hass, entry)
    assert entry.runtime_data.client.tls == "legacy"
    with pytest.raises(HomeAssistantError) as raised:
        await _press(hass)
    assert raised.value.translation_placeholders == {"reason": "not_verified"}
    assert tls_tuxweb.install_requests == 0
    assert CONF_CA_CERT not in entry.data


async def test_a_panel_that_stops_declaring_it_is_refused(hass, tls, tls_tuxweb):
    entry = _entry(tls_tuxweb, panel_ca=tls["panel"].authority.pem)
    await _setup(hass, entry)
    entry.runtime_data.client._capabilities = frozenset(TUXWEB_CAPABILITIES)
    with pytest.raises(HomeAssistantError) as raised:
        await _press(hass)
    assert raised.value.translation_placeholders == {"reason": "not_supported"}
    assert tls_tuxweb.install_requests == 0


# ------------------------------------------------------- a moved address


async def test_a_moved_panel_is_reissued_on_the_no_hostname_context(
    hass, tls, tls_tuxweb
):
    """The panel serves our certificate for the address it had; setup issues
    one for the new address over the context that skips only the name check,
    then verifies the panel by name as always."""
    await tls_tuxweb.use_tls(tls["moved"])
    entry = _entry(
        tls_tuxweb,
        panel_ca=tls["own_cert"],
        own=_own(tls),
        **{CONF_CERT_NOT_AFTER: _not_after(300)},
    )
    real = api._install_ssl_context
    handed_out = []

    def spy(pem):
        made = real(pem)
        handed_out.append(made)
        return made

    with patch.object(api, "_install_ssl_context", spy):
        await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert handed_out == [real(tls["own_cert"])]
    assert tls_tuxweb.install_requests == 1
    installed = tls_tuxweb.installed[0]
    assert certificate.leaf_names_host(installed, "127.0.0.1")
    assert entry.data[CONF_CA_CERT] == tls["own_cert"]
    assert entry.data[CONF_PANEL_CA] == tls["own_cert"]
    assert entry.data[CONF_CERT_NOT_AFTER] == installed.not_valid_after_utc.isoformat()
    assert entry.runtime_data.client.tls == "verified"
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, issue_id("panel_certificate", ENTRY_ID)
        )
        is None
    )


async def test_a_refused_move_fails_setup_without_a_retry(hass, tls, tls_tuxweb):
    await tls_tuxweb.use_tls(tls["moved"])
    tls_tuxweb.install_error = (409, "tls_not_configured")
    entry = _entry(tls_tuxweb, panel_ca=tls["own_cert"], own=_own(tls))
    before = dict(entry.data)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert tls_tuxweb.install_requests == 1
    assert dict(entry.data) == before
    issue = _renewal_issue(hass)
    assert issue is not None
    assert issue.translation_placeholders["reason"] == "tls_not_configured"
    assert not issue.is_fixable


async def test_an_unreachable_move_is_retried_with_setup(hass, tls, tls_tuxweb):
    await tls_tuxweb.use_tls(tls["moved"])
    entry = _entry(tls_tuxweb, panel_ca=tls["own_cert"], own=_own(tls))
    with patch.object(
        api.TuxedoTouchClient,
        "async_install_certificate",
        side_effect=api.TuxedoTouchUnreachable("rebooting"),
    ):
        await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert _renewal_issue(hass) is not None


async def test_a_foreign_certificate_is_not_taken_for_a_move(hass, tls, tls_tuxweb):
    """Pinned to our own CA and served the owner's: the certificate repair,
    not an install."""
    entry = _entry(tls_tuxweb, panel_ca=tls["own_cert"], own=_own(tls))
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert tls_tuxweb.install_requests == 0
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id("panel_certificate", ENTRY_ID)
    )


async def test_a_reconfigure_to_the_new_address_reissues(hass, tls, tls_tuxweb):
    """The entry still names the address the panel had; reconfiguring it to
    the new one issues the certificate for it before the check."""
    await tls_tuxweb.use_tls(tls["moved"])
    entry = _entry(
        tls_tuxweb,
        panel_ca=tls["own_cert"],
        own=_own(tls),
        **{CONF_HOST: "192.0.2.77", CONF_CERT_NOT_AFTER: _not_after(300)},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": ENTRY_ID},
        data={
            **{k: v for k, v in ENTRY_DATA.items() if k != CONF_CODE},
            CONF_HOST: "127.0.0.1",
            CONF_PORT: tls_tuxweb.port,
            CONF_USERNAME: tls_tuxweb.username,
            CONF_PASSWORD: tls_tuxweb.password,
        },
    )
    await hass.async_block_till_done()
    await _settled(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert tls_tuxweb.install_requests == 1
    assert entry.data[CONF_HOST] == "127.0.0.1"
    assert entry.data[CONF_CA_KEY] == tls["own_key"]
    assert entry.data[CONF_CA_CERT] == tls["own_cert"]
    assert (
        entry.data[CONF_CERT_NOT_AFTER]
        == tls_tuxweb.installed[0].not_valid_after_utc.isoformat()
    )
    assert entry.state is ConfigEntryState.LOADED


async def test_a_refused_reconfigure_reissue_is_a_form_error(hass, tls, tls_tuxweb):
    await tls_tuxweb.use_tls(tls["moved"])
    tls_tuxweb.install_error = (400, "bad_chain")
    entry = _entry(
        tls_tuxweb, panel_ca=tls["own_cert"], own=_own(tls), **{CONF_HOST: "192.0.2.77"}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": ENTRY_ID},
        data={
            **{k: v for k, v in ENTRY_DATA.items() if k != CONF_CODE},
            CONF_HOST: "127.0.0.1",
            CONF_PORT: tls_tuxweb.port,
            CONF_USERNAME: tls_tuxweb.username,
            CONF_PASSWORD: tls_tuxweb.password,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "certificate_renewal_failed"}
    assert entry.data[CONF_HOST] == "192.0.2.77"


async def test_a_reauthentication_keeps_a_reissued_expiry(hass, tls, tls_tuxweb):
    """A reauthentication that meets a moved panel stores the new expiry."""
    await tls_tuxweb.use_tls(tls["moved"])
    entry = _entry(
        tls_tuxweb,
        panel_ca=tls["own_cert"],
        own=_own(tls),
        **{CONF_TUXWEB_TOKEN: "0" * 64, CONF_CERT_NOT_AFTER: _not_after(300)},
    )
    entry.add_to_hass(hass)
    flow = await entry.start_reauth_flow(hass)
    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"],
            {
                CONF_USERNAME: tls_tuxweb.username,
                CONF_PASSWORD: tls_tuxweb.password,
                CONF_TUXWEB_TOKEN: TUXWEB_TOKEN,
            },
        )
    assert result["reason"] == "reauth_successful"
    assert (
        entry.data[CONF_CERT_NOT_AFTER]
        == tls_tuxweb.installed[0].not_valid_after_utc.isoformat()
    )


# ------------------------------------------------------------ expiry


async def test_an_expiring_certificate_is_renewed_at_setup_once(hass, tls, tls_tuxweb):
    """The owner's certificate ten days from expiry: renewed at setup, and the
    reloaded entry reads the stored expiry and leaves it be."""
    await tls_tuxweb.use_tls(tls["expiring"])
    entry = _entry(tls_tuxweb, panel_ca=tls["panel"].authority.pem)
    await _setup(hass, entry)
    await hass.async_block_till_done()
    await _settled(hass, entry)
    assert tls_tuxweb.install_requests == 1
    assert entry.data[CONF_PANEL_CA] == entry.data[CONF_CA_CERT]
    for _ in range(3):
        await entry.runtime_data.async_refresh()
    await _settled(hass, entry)
    assert tls_tuxweb.install_requests == 1


async def test_expiry_is_checked_once_a_day_and_never_per_poll(hass, tls, tls_tuxweb):
    entry = _entry(
        tls_tuxweb,
        panel_ca=tls["panel"].authority.pem,
    )
    real = api.TuxedoTouchClient.async_read_presented_chain
    reads = 0

    async def counted(self):
        nonlocal reads
        reads += 1
        return await real(self)

    with patch.object(api.TuxedoTouchClient, "async_read_presented_chain", counted):
        await _setup(hass, entry)
        coordinator = entry.runtime_data
        assert reads == 1
        for _ in range(5):
            await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert reads == 1
        assert tls_tuxweb.install_requests == 0

        await tls_tuxweb.use_tls(tls["expiring"])
        for _ in range(5):
            await coordinator.async_refresh()
        await _settled(hass, entry)
        assert reads == 1
        assert tls_tuxweb.install_requests == 0

        async_fire_time_changed(hass, dt_util.utcnow() + datetime.timedelta(days=1))
        await hass.async_block_till_done()
        if coordinator._certificate_task is not None:
            await coordinator._certificate_task
        await hass.async_block_till_done()
    assert reads == 2
    assert tls_tuxweb.install_requests == 1
    await _settled(hass, entry)


async def test_the_stored_expiry_is_read_for_our_own_certificate(hass, tls, tls_tuxweb):
    """While the panel serves our certificate, the stored expiry is the one
    read: no handshake for it, and a stored date inside 30 days renews."""
    await tls_tuxweb.use_tls(tls["moved"])
    entry = _entry(tls_tuxweb, panel_ca=tls["own_cert"], own=_own(tls))
    await _setup(hass, entry)
    tls_tuxweb.install_requests = 0
    coordinator = entry.runtime_data
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_CERT_NOT_AFTER: _not_after(31)}
    )
    with patch.object(
        api.TuxedoTouchClient, "async_read_presented_chain"
    ) as chain_reads:
        coordinator._async_certificate_tick()
        await coordinator._certificate_task
        assert tls_tuxweb.install_requests == 0
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_CERT_NOT_AFTER: _not_after(29)}
        )
        coordinator._async_certificate_tick()
        await coordinator._certificate_task
    chain_reads.assert_not_called()
    assert tls_tuxweb.install_requests == 1
    await hass.async_block_till_done()
    await _settled(hass, entry)


async def test_an_unreadable_expiry_renews_nothing(hass, tls, tls_tuxweb):
    entry = _entry(tls_tuxweb, panel_ca=tls["panel"].authority.pem)
    with patch.object(
        api.TuxedoTouchClient, "async_read_presented_chain", return_value=[]
    ):
        await _setup(hass, entry)
    assert tls_tuxweb.install_requests == 0


# ------------------------------------------------- the renewal-failed issue


async def test_a_failed_renewal_raises_the_issue_once_and_success_clears_it(
    hass, tls, tls_tuxweb, caplog
):
    """A write failure: warned once, the issue raised, retried the next day
    (it was not a refusal), and the issue deleted when that succeeds."""
    await tls_tuxweb.use_tls(tls["expiring"])
    tls_tuxweb.install_error = (500, "write_failed")
    entry = _entry(tls_tuxweb, panel_ca=tls["panel"].authority.pem)
    with caplog.at_level(logging.WARNING):
        await _setup(hass, entry)
        coordinator = entry.runtime_data
        coordinator._async_certificate_tick()
        await coordinator._certificate_task
    issue = _renewal_issue(hass)
    assert issue is not None
    assert not issue.is_fixable
    assert issue.translation_key == ISSUE_CERTIFICATE_RENEWAL_FAILED
    assert issue.translation_placeholders["reason"] == "write_failed"
    assert CONF_CA_CERT not in entry.data
    assert tls_tuxweb.install_requests == 2
    warned = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name.startswith("custom_components.tuxedo_touch")
        and "could not renew the certificate" in r.getMessage()
    ]
    assert len(warned) == 1

    tls_tuxweb.install_error = None
    async_fire_time_changed(hass, dt_util.utcnow() + datetime.timedelta(days=1))
    await hass.async_block_till_done()
    await coordinator._certificate_task
    await hass.async_block_till_done()
    assert tls_tuxweb.install_requests == 3
    assert _renewal_issue(hass) is None
    assert entry.data[CONF_PANEL_CA] == entry.data[CONF_CA_CERT]
    await _settled(hass, entry)


async def test_a_refused_renewal_is_not_retried_until_pressed(hass, tls, tls_tuxweb):
    await tls_tuxweb.use_tls(tls["expiring"])
    tls_tuxweb.install_error = (409, "tls_not_configured")
    entry = _entry(tls_tuxweb, panel_ca=tls["panel"].authority.pem)
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    assert tls_tuxweb.install_requests == 1
    assert _renewal_issue(hass).data["refused"] is True

    async_fire_time_changed(hass, dt_util.utcnow() + datetime.timedelta(days=1))
    await hass.async_block_till_done()
    await coordinator._certificate_task
    assert tls_tuxweb.install_requests == 1

    tls_tuxweb.install_error = None
    await _press(hass)
    await hass.async_block_till_done()
    assert tls_tuxweb.install_requests == 2
    assert _renewal_issue(hass) is None
    await _settled(hass, entry)

    await hass.config_entries.async_remove(ENTRY_ID)
    await hass.async_block_till_done()


async def test_the_issue_goes_with_the_entry(hass, tls, tls_tuxweb):
    await tls_tuxweb.use_tls(tls["expiring"])
    tls_tuxweb.install_error = (409, "tls_not_configured")
    entry = _entry(tls_tuxweb, panel_ca=tls["panel"].authority.pem)
    await _setup(hass, entry)
    assert _renewal_issue(hass) is not None
    await hass.config_entries.async_remove(ENTRY_ID)
    await hass.async_block_till_done()
    assert _renewal_issue(hass) is None


# ------------------------------------------------- the shared context


def test_one_context_per_cadata(tls):
    owner = tls["panel"].authority.pem
    own = tls["own_cert"]
    both = api.trust_anchors(owner, own)
    assert both == api.trust_anchors(owner, own)
    assert both.startswith(owner.rstrip())
    assert both.endswith(own)
    assert api.trust_anchors(owner, None) == owner
    assert api.trust_anchors(own, own) == own
    assert api.trust_anchors(None, own) is None
    assert api._verified_ssl_context(both) is api._verified_ssl_context(
        api.trust_anchors(owner, own)
    )
    assert api._verified_ssl_context(both) is not api._verified_ssl_context(owner)
    assert api._install_ssl_context(both) is api._install_ssl_context(both)
    assert api._install_ssl_context(both) is not api._verified_ssl_context(both)
    assert api._install_ssl_context(both).check_hostname is False
    assert api._install_ssl_context(both).verify_mode is ssl.CERT_REQUIRED


async def test_every_client_of_the_entry_shares_the_context(hass, tls):
    owner = tls["panel"].authority.pem
    own = tls["own_cert"]
    clients = [
        api.TuxedoTouchClient(
            session=None,
            host="127.0.0.1",
            port=443,
            use_https=True,
            username="u",
            password="p",
            panel_ca=owner,
            own_ca=own,
        )
        for _ in range(2)
    ]
    assert clients[0].ssl_arg is clients[1].ssl_arg
    assert clients[0].ssl_arg is api._verified_ssl_context(
        api.trust_anchors(owner, own)
    )
    clients[0].trust_own_ca(None)
    assert clients[0].ssl_arg is api._verified_ssl_context(owner)
    clients[1].pin_own_ca()
    assert clients[1].ssl_arg is api._verified_ssl_context(own)


async def test_the_client_installs_only_verified_over_https(hass, tls):
    def client(**kwargs):
        return api.TuxedoTouchClient(
            session=None,
            host="127.0.0.1",
            port=443,
            username="u",
            password="p",
            tuxweb_token=TUXWEB_TOKEN,
            **kwargs,
        )

    with pytest.raises(api.TuxedoTouchError):
        await client(
            use_https=False, panel_ca=tls["own_cert"]
        ).async_install_certificate("c", "k")
    with pytest.raises(api.TuxedoTouchError):
        await client(use_https=True).async_install_certificate("c", "k")
    with pytest.raises(api.TuxedoTouchInstallRefused) as refused:
        await client(
            use_https=True, panel_ca=tls["own_cert"]
        ).async_install_certificate("c" * 20000, "k")
    assert refused.value.reason == "too_large"


# -------------------------------------------- trusting it on a computer


async def _issued_entry(hass, tls):
    """A loaded-or-not entry holding an authority, with no panel behind it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        title="Tuxedo Touch (203.0.113.60)",
        unique_id="203.0.113.60:443:1",
        data={**ENTRY_DATA, CONF_CA_CERT: tls["own_cert"], CONF_CA_KEY: tls["own_key"]},
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    return entry


async def test_the_trust_step_shows_the_authority_and_a_signed_link(
    hass, tls, hass_client, hass_client_no_auth
):
    """Driven through the frontend's own HTTP API: the link is signed for
    the administrator whose request ran the step, and downloads without a
    bearer header. Submitting stores nothing and does not reload."""
    entry = await _issued_entry(hass, tls)
    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    started = await client.post(
        "/api/config/config_entries/options/flow", json={"handler": ENTRY_ID}
    )
    menu = await started.json()
    assert menu["type"] == "menu"
    assert menu["menu_options"] == ["push_source", "trust_on_computer"]
    shown = await (
        await client.post(
            f"/api/config/config_entries/options/flow/{menu['flow_id']}",
            json={"next_step_id": "trust_on_computer"},
        )
    ).json()
    assert shown["type"] == "form"
    assert shown["step_id"] == "trust_on_computer"
    assert shown["data_schema"] == []
    placeholders = shown["description_placeholders"]
    authority = x509.load_pem_x509_certificate(tls["own_cert"].encode())
    assert placeholders["subject"] == authority.subject.rfc4514_string()
    assert placeholders["fingerprint"] == (
        authority.fingerprint(hashes.SHA256()).hex(":").upper()
    )
    url = placeholders["download_url"]
    assert url.startswith(f"/api/tuxedo_touch/{ENTRY_ID}/ca.crt?authSig=")

    anonymous = await hass_client_no_auth()
    download = await anonymous.get(url)
    assert download.status == HTTPStatus.OK
    assert await download.text() == tls["own_cert"]
    assert download.headers["Content-Type"] == "application/x-x509-ca-cert"
    assert download.headers["Content-Disposition"] == (
        'attachment; filename="tuxedo-touch-ca-203-0-113-60.crt"'
    )
    options_before = dict(entry.options)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        done = await (
            await client.post(
                f"/api/config/config_entries/options/flow/{menu['flow_id']}", json={}
            )
        ).json()
        await hass.async_block_till_done()
    assert done["type"] == "create_entry"
    reload.assert_not_called()
    assert dict(entry.options) == options_before


async def test_the_trust_step_aborts_before_an_authority_exists(hass, config_entry):
    config_entry.add_to_hass(hass)
    menu = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "trust_on_computer"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "certificate_not_issued"


async def test_the_trust_step_aborts_outside_a_users_request(hass, tls):
    """No user's request, no signed link: never one signed for nobody."""
    entry = await _issued_entry(hass, tls)
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "trust_on_computer"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_signed_link"


# ------------------------------------------------------------- the view


async def test_the_view_serves_an_administrator(hass, tls, hass_client):
    await _issued_entry(hass, tls)
    client = await hass_client()
    response = await client.get(f"/api/tuxedo_touch/{ENTRY_ID}/ca.crt")
    assert response.status == HTTPStatus.OK
    assert await response.text() == tls["own_cert"]


async def test_the_view_refuses_a_user_who_is_not_an_administrator(
    hass, tls, hass_client, hass_read_only_access_token
):
    await _issued_entry(hass, tls)
    client = await hass_client(hass_read_only_access_token)
    response = await client.get(f"/api/tuxedo_touch/{ENTRY_ID}/ca.crt")
    assert response.status == HTTPStatus.UNAUTHORIZED
    # Nor through a path signed for that user.
    token = hass.auth.async_validate_access_token(hass_read_only_access_token)
    signed = async_sign_path(
        hass,
        f"/api/tuxedo_touch/{ENTRY_ID}/ca.crt",
        datetime.timedelta(minutes=5),
        refresh_token_id=token.id,
    )
    response = await client.get(signed)
    assert response.status == HTTPStatus.UNAUTHORIZED


async def test_the_view_has_nothing_for_an_unknown_entry_or_no_authority(
    hass, tls, hass_client, hass_client_no_auth
):
    await _issued_entry(hass, tls)
    other = MockConfigEntry(domain="other_domain", data={})
    other.add_to_hass(hass)
    bare = MockConfigEntry(
        domain=DOMAIN, unique_id="bare", data={**ENTRY_DATA, CONF_HOST: "192.0.2.3"}
    )
    bare.add_to_hass(hass)
    client = await hass_client()
    for entry_id in ("nonexistent", other.entry_id, bare.entry_id):
        response = await client.get(f"/api/tuxedo_touch/{entry_id}/ca.crt")
        assert response.status == HTTPStatus.NOT_FOUND
    anonymous = await hass_client_no_auth()
    response = await anonymous.get(f"/api/tuxedo_touch/{ENTRY_ID}/ca.crt")
    assert response.status == HTTPStatus.UNAUTHORIZED


def test_the_download_name_takes_dashes():
    assert download_name("10.0.0.5") == "tuxedo-touch-ca-10-0-0-5.crt"
    assert download_name("panel.example") == "tuxedo-touch-ca-panel-example.crt"
    assert download_name("fe80::1") == "tuxedo-touch-ca-fe80--1.crt"


# ------------------------------------------------------------ diagnostics


async def test_diagnostics_never_carry_the_authority_key(hass, tls, tls_tuxweb):
    entry = _entry(
        tls_tuxweb,
        panel_ca=tls["panel"].authority.pem,
    )
    await _setup(hass, entry)
    report = await async_get_config_entry_diagnostics(hass, entry)
    assert report["certificate_install"] is True
    assert report["ca_issued_by_home_assistant"] is False
    assert report["home_assistant_ca"] is None

    await _press(hass)
    await hass.async_block_till_done()
    await _settled(hass, entry)
    report = await async_get_config_entry_diagnostics(hass, entry)
    flat = repr(report)
    assert "PRIVATE KEY" not in flat
    assert entry.data[CONF_CA_KEY] not in flat
    assert CONF_CA_KEY not in report["config"]
    assert CONF_CA_CERT not in report["config"]
    assert report["ca_issued_by_home_assistant"] is True
    authority = x509.load_pem_x509_certificate(entry.data[CONF_CA_CERT].encode())
    assert report["home_assistant_ca"] == {
        "subject": authority.subject.rfc4514_string(),
        "sha256": authority.fingerprint(hashes.SHA256()).hex(":").upper(),
    }
    assert report["cert_not_after"] == entry.data[CONF_CERT_NOT_AFTER]
    assert report["tls"] == "verified"


def test_diagnostics_redact_a_key_wherever_it_appears():
    """The redaction set holds the key too, not only the filter above it."""
    assert CONF_CA_KEY in REDACT


# ------------------------------------------------------------ the edges


@pytest.mark.parametrize(
    ("err", "reason", "refused"),
    [
        (api.TuxedoTouchInstallRefused("bad_key", 400), "bad_key", True),
        (api.TuxedoTouchInstallRefused("write_failed", 500), "write_failed", False),
        (api.TuxedoTouchInstallRefused("too_large"), "too_large", True),
        (api.TuxedoTouchTokenRejected("none"), "token_rejected", True),
        (api.TuxedoTouchCertificateError("no"), "certificate_untrusted", False),
        (api.TuxedoTouchUnreachable("down"), "unreachable", False),
        (api.TuxedoTouchError("odd"), "panel_error", False),
    ],
)
def test_failures_are_named_and_refusals_told_apart(err, reason, refused):
    assert certificate.failure_reason(err) == reason
    assert certificate._refused(err) is refused


async def test_a_corrupt_stored_authority_is_named(hass, tls, tls_tuxweb):
    entry = _entry(
        tls_tuxweb,
        panel_ca=tls["panel"].authority.pem,
        own=(tls["own_cert"], "not a key"),
    )
    await _setup(hass, entry)
    with pytest.raises(HomeAssistantError) as raised:
        await _press(hass)
    assert raised.value.translation_placeholders == {"reason": "bad_authority"}
    assert tls_tuxweb.install_requests == 0


async def test_a_success_that_does_not_say_installed_is_refused(hass, tls, tls_tuxweb):
    entry = _entry(tls_tuxweb, panel_ca=tls["panel"].authority.pem)
    await _setup(hass, entry)
    before = dict(entry.data)
    tls_tuxweb.install_error = (200, "installed")
    with pytest.raises(HomeAssistantError) as raised:
        await _press(hass)
    assert raised.value.translation_placeholders == {"reason": "unexpected_answer"}
    assert dict(entry.data) == before


async def test_a_check_in_flight_is_not_started_twice(hass, tls, tls_tuxweb):
    entry = _entry(tls_tuxweb, panel_ca=tls["panel"].authority.pem)
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    coordinator._async_certificate_tick()
    first = coordinator._certificate_task
    coordinator._async_certificate_tick()
    assert coordinator._certificate_task is first
    await first
