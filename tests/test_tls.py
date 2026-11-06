"""The client against a tuxweb panel over real TLS: first use, then verification.

The fake panel (tests/fake_panel.py) serves the chain a tuxweb panel serves -
an EC P-256 leaf naming 127.0.0.1, then the self-signed authority that issued
it - so what is exercised is the real handshake, the real chain read off the
connection and the real verifying context. The assertions are on the wire as
well as on the outcome: whether the probe reached the server at all, whether
a login page was ever asked for, and which context a request was made with.

Runs without Home Assistant, via `tests.no_ha`.
"""

import asyncio
import contextlib
import logging
import ssl

import aiohttp
import pytest

from tests.fake_panel import FakePanel, wait_until
from tests.no_ha import load
from tests.tls_certs import make_panel_tls, same_certificate

api = load("api")
push = load("push")


@pytest.fixture(autouse=True)
def _real_sockets(socket_enabled):
    """A real socket on 127.0.0.1; see tests/test_tuxweb.py."""


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    """Authorities, leaves and server contexts, built outside the event loop."""
    return make_panel_tls(str(tmp_path_factory.mktemp("tls")))


async def _panel(ctx, *, tuxweb=True):
    made = FakePanel(tuxweb=tuxweb, ssl_context=ctx)
    await made.start()
    return made


@pytest.fixture
async def session():
    made = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
    yield made
    await made.close()


def _client(panel, session, panel_ca=None, *, trust_on_first_use=True, https=True):
    return api.TuxedoTouchClient(
        session=session,
        host="127.0.0.1",
        port=panel.port,
        use_https=https,
        username=panel.username,
        password=panel.password,
        tuxweb_token=panel.token,
        panel_ca=panel_ca,
        trust_on_first_use=trust_on_first_use,
    )


def _warnings_of_the_client(caplog):
    """The client's own warnings, not asyncio's about some other connection."""
    return sum(
        record.levelno == logging.WARNING and record.name.endswith("tuxedo_touch.api")
        for record in caplog.records
    )


async def test_the_first_probe_pins_the_authority_and_the_next_request_verifies(
    tls, session
):
    panel = await _panel(tls.trusted)
    try:
        client = _client(panel, session)
        assert client.tls == "legacy"
        assert await client.async_probe_capabilities() is True

        assert same_certificate(client.panel_ca, tls.authority.pem)
        assert client.tls == "verified"
        assert client.ssl_arg is api._verified_ssl_context(client.panel_ca)
        presented = client.presented_certificate
        assert presented.fingerprint == tls.authority.fingerprint
        assert presented.subject == tls.authority.subject
        assert presented.leaf_expires == tls.leaf.expires
        # The probe, then the proof on the verifying context.
        assert panel.capability_probes == 2

        status = await client.get_status()
        assert status.status == "Ready To Arm"
        assert panel.polls == 1

        # By effect: the next request really is verified. A panel serving a
        # chain from another authority is refused before HTTP.
        await panel.use_tls(tls.other)
        with pytest.raises(api.TuxedoTouchCertificateError):
            await client.get_status()
        assert panel.polls == 1
    finally:
        await panel.close()


async def test_a_pinned_client_meeting_another_authority_never_falls_back(tls, session):
    """No legacy retry, no stock verdict, no login: a certificate error, uncached."""
    panel = await _panel(tls.other)
    try:
        client = _client(panel, session, panel_ca=tls.authority.pem)
        assert client.tls == "verified"
        with pytest.raises(api.TuxedoTouchCertificateError):
            await client.async_probe_capabilities()
        # Refused at the handshake: the server never saw a request, so
        # nothing was asked again on the legacy context.
        assert panel.capability_probes == 0
        assert client._tuxweb is None
        assert client.tls == "verified"
        with pytest.raises(api.TuxedoTouchCertificateError):
            await client.get_status()
        assert panel.login_page_requests == 0
        assert panel.login_attempts == 0
        assert panel.api_requests == 0
    finally:
        await panel.close()


async def test_a_certificate_failure_under_a_running_client_is_not_a_rollback(
    tls, session
):
    """The refusal re-check must not read a certificate failure as stock firmware."""
    panel = await _panel(tls.trusted)
    try:
        client = _client(panel, session, panel_ca=tls.authority.pem)
        assert await client.async_probe_capabilities() is True
        await panel.use_tls(tls.other)
        with pytest.raises(api.TuxedoTouchCertificateError):
            await client.async_recheck_capabilities(401)
        with pytest.raises(api.TuxedoTouchCertificateError):
            await client.arm("AWAY", "1234")
        assert client.tuxweb is True
        assert client.tls == "verified"
        assert panel.login_page_requests == 0
        assert panel.login_attempts == 0
    finally:
        await panel.close()


def _connection_key():
    from aiohttp.client_reqrep import ConnectionKey

    return ConnectionKey(
        **{
            **dict.fromkeys(ConnectionKey._fields),
            "host": "127.0.0.1",
            "port": 443,
            "is_ssl": True,
        }
    )


class _HandshakeRefused:
    """A session whose every request fails in the TLS handshake, not on a certificate.

    What a pinned client meets when the panel is back on stock firmware: the
    verifying context refuses stock's handshake (legacy renegotiation) before
    any certificate is compared, so aiohttp raises ClientConnectorSSLError
    rather than its certificate error.
    """

    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        return self

    def post(self, url, **kwargs):
        self.calls.append(kwargs)
        return self

    async def __aenter__(self):
        raise aiohttp.ClientConnectorSSLError(
            _connection_key(), ssl.SSLError(1, "unsafe legacy renegotiation disabled")
        )

    async def __aexit__(self, *exc_info):
        return False


async def test_a_handshake_the_verifying_context_refuses_is_a_certificate_error(
    tls,
):
    """Still a certificate error on a pinned client: the repair, not a retry loop."""
    pinned = api.TuxedoTouchClient(
        session=_HandshakeRefused(),
        host="127.0.0.1",
        port=443,
        use_https=True,
        username="u",
        password="p",
        tuxweb_token="t",
        panel_ca=tls.authority.pem,
    )
    with pytest.raises(api.TuxedoTouchCertificateError):
        await pinned.async_probe_capabilities()
    assert pinned._tuxweb is None

    # The control: the same failure on the legacy context is a connection
    # error, as it always was.
    unpinned = api.TuxedoTouchClient(
        session=_HandshakeRefused(),
        host="127.0.0.1",
        port=443,
        use_https=True,
        username="u",
        password="p",
    )
    with pytest.raises(api.TuxedoTouchConnectionError):
        await unpinned.async_probe_capabilities()


async def test_an_anchor_that_does_not_verify_the_leaf_pins_nothing(
    tls, session, caplog
):
    """The proof by effect: without it this chain would pin an authority that
    verifies nothing, and every later request would fail."""
    panel = await _panel(tls.broken)
    try:
        client = _client(panel, session)
        with caplog.at_level(logging.WARNING):
            assert await client.async_probe_capabilities() is True
        assert client.panel_ca is None
        assert client.tls == "legacy"
        assert client.ssl_arg is api._legacy_ssl_context()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert tls.unrelated.fingerprint in warnings[0].getMessage()

        # And it keeps working exactly as before, on the legacy context.
        assert (await client.get_status()).status == "Ready To Arm"
    finally:
        await panel.close()


async def test_a_proof_that_cannot_connect_pins_nothing(tls, session, monkeypatch):
    panel = await _panel(tls.trusted)
    try:
        client = _client(panel, session)
        real = client._async_ask_capabilities

        async def proof_fails(**kwargs):
            if kwargs.get("ssl_ctx") is not None:
                raise api.TuxedoTouchConnectionError("gone")
            return await real(**kwargs)

        monkeypatch.setattr(client, "_async_ask_capabilities", proof_fails)
        assert await client.async_probe_capabilities() is True
        assert client.panel_ca is None
        assert client.presented_certificate is not None
    finally:
        await panel.close()


async def test_a_proof_that_declares_no_capabilities_pins_nothing(tls, session):
    panel = await _panel(tls.trusted)
    try:
        client = _client(panel, session)
        real = client._async_ask_capabilities

        async def proof_says_stock(**kwargs):
            status, capabilities = await real(**kwargs)
            if kwargs.get("ssl_ctx") is not None:
                return status, None
            return status, capabilities

        client._async_ask_capabilities = proof_says_stock
        assert await client.async_probe_capabilities() is True
        assert client.panel_ca is None
    finally:
        await panel.close()


async def test_a_chain_that_cannot_be_read_pins_nothing(tls, session, monkeypatch):
    panel = await _panel(tls.trusted)
    try:
        client = _client(panel, session)
        monkeypatch.setattr(api, "_chain_of", lambda ssl_object: [])
        assert await client.async_probe_capabilities() is True
        assert client.panel_ca is None
        assert client.presented_certificate is None
    finally:
        await panel.close()


async def test_stock_over_tls_is_never_pinned(tls, session):
    """Stock is reached as it always was: legacy context, no chain read, no pin."""
    panel = await _panel(tls.trusted, tuxweb=False)
    try:
        client = _client(panel, session)
        assert await client.async_probe_capabilities() is False
        assert client.panel_ca is None
        assert client.tls == "legacy"
        assert panel.capability_probes == 1
    finally:
        await panel.close()


async def test_a_client_that_only_offers_keeps_the_proven_authority_unpinned(
    tls, session
):
    """An existing entry's client: the same proof, nothing pinned, legacy."""
    panel = await _panel(tls.trusted)
    try:
        client = _client(panel, session, trust_on_first_use=False)
        assert await client.async_probe_capabilities() is True
        assert client.panel_ca is None
        assert client.tls == "legacy"
        assert client.ssl_arg is api._legacy_ssl_context()
        offered = client.trustable_certificate
        assert offered is not None
        assert offered.fingerprint == tls.authority.fingerprint
        assert same_certificate(offered.anchor_pem, tls.authority.pem)
        assert panel.capability_probes == 2
        assert panel.login_attempts == 0
    finally:
        await panel.close()


async def test_a_client_that_only_offers_offers_nothing_unverifiable(
    tls, session, caplog
):
    panel = await _panel(tls.broken)
    try:
        client = _client(panel, session, trust_on_first_use=False)
        with caplog.at_level(logging.WARNING):
            assert await client.async_probe_capabilities() is True
        assert client.trustable_certificate is None
        assert client.panel_ca is None
        assert client.tls == "legacy"
        assert _warnings_of_the_client(caplog) == 1
    finally:
        await panel.close()


async def test_a_stock_client_looks_again_and_finds_tuxweb_without_logging_in(
    tls, session
):
    """The look after a recovery: one GET, the proof, nothing pinned, and the
    client goes on as stock until the entry is reloaded."""
    panel = await _panel(tls.trusted, tuxweb=False)
    try:
        client = _client(panel, session, trust_on_first_use=False)
        assert await client.async_probe_capabilities() is False
        assert await client.async_look_for_tuxweb() is None
        assert panel.capability_probes == 2

        panel.tuxweb = True
        found = await client.async_look_for_tuxweb()
        assert found is not None
        assert found.fingerprint == tls.authority.fingerprint
        assert client.trustable_certificate is found
        assert client.tuxweb is False
        assert client.capabilities == frozenset()
        assert client.panel_ca is None
        assert client.tls == "legacy"
        # The look's GET, then the proof's.
        assert panel.capability_probes == 4
        assert panel.login_page_requests == 0
        assert panel.login_attempts == 0
    finally:
        await panel.close()


async def test_a_look_at_an_unverifiable_panel_finds_nothing(tls, session, caplog):
    panel = await _panel(tls.broken, tuxweb=False)
    try:
        client = _client(panel, session, trust_on_first_use=False)
        assert await client.async_probe_capabilities() is False
        panel.tuxweb = True
        with caplog.at_level(logging.WARNING):
            assert await client.async_look_for_tuxweb() is None
        assert client.trustable_certificate is None
        assert _warnings_of_the_client(caplog) == 1
    finally:
        await panel.close()


async def test_the_look_asks_nothing_of_a_client_that_is_not_stock_over_https(
    tls, session
):
    panel = await _panel(tls.trusted)
    try:
        unasked = _client(panel, session, trust_on_first_use=False)
        assert await unasked.async_look_for_tuxweb() is None
        tuxweb = _client(panel, session, trust_on_first_use=False)
        assert await tuxweb.async_probe_capabilities() is True
        probes = panel.capability_probes
        assert await tuxweb.async_look_for_tuxweb() is None
        assert panel.capability_probes == probes

        panel.tuxweb = False
        pinned = _client(panel, session, panel_ca=tls.authority.pem)
        assert await pinned.async_probe_capabilities() is False
        probes = panel.capability_probes
        assert await pinned.async_look_for_tuxweb() is None
        assert panel.capability_probes == probes
    finally:
        await panel.close()

    plain = await _panel(None, tuxweb=False)
    try:
        client = _client(plain, session, https=False)
        assert await client.async_probe_capabilities() is False
        plain.tuxweb = True
        assert await client.async_look_for_tuxweb() is None
        assert plain.capability_probes == 1
    finally:
        await plain.close()


async def test_a_stock_client_tells_an_answer_from_nothing_answering(tls, session):
    """The coordinator's recovery signal: a stock client meeting tuxweb fails
    on an answer (no login page), a closed panel on nothing answering."""
    panel = await _panel(tls.trusted, tuxweb=False)
    client = _client(panel, session, trust_on_first_use=False)
    try:
        assert await client.async_probe_capabilities() is False
        await client.get_status()
        panel.tuxweb = True
        with pytest.raises(api.TuxedoTouchConnectionError) as answered:
            await client.get_status()
        assert not isinstance(answered.value, api.TuxedoTouchUnreachable)
    finally:
        await panel.close()
    with pytest.raises(api.TuxedoTouchUnreachable):
        await client.get_status()


async def test_a_pinned_client_answered_stock_through_its_pin_uses_legacy(tls, session):
    """Only a verified answer can say stock; then stock is spoken as stock."""
    panel = await _panel(tls.trusted, tuxweb=False)
    try:
        client = _client(panel, session, panel_ca=tls.authority.pem)
        assert await client.async_probe_capabilities() is False
        assert client.tls == "legacy"
    finally:
        await panel.close()


async def test_the_push_stream_stops_on_a_certificate_failure(tls, session):
    """Terminal, like a refused credential: one attempt, no loop, no login."""
    panel = await _panel(tls.trusted)
    try:
        client = _client(panel, session, panel_ca=tls.authority.pem)
        assert await client.async_probe_capabilities() is True
        await panel.use_tls(tls.other)
        failures = []
        stream = push.TuxedoPushStream(
            client,
            lambda _status: None,
            lambda _connected: None,
            on_certificate_failed=failures.append,
        )
        async with asyncio.timeout(5):
            await stream.async_run()
        assert stream.certificate_failed is True
        assert stream.auth_failed is False
        assert len(failures) == 1
        assert isinstance(failures[0], api.TuxedoTouchCertificateError)
        assert panel.stream_requests == 0
        assert panel.login_page_requests == 0
        assert panel.login_attempts == 0
    finally:
        await panel.close()


async def test_the_push_stream_opens_on_the_pinned_context(tls, session):
    panel = await _panel(tls.trusted)
    try:
        client = _client(panel, session)
        assert await client.async_probe_capabilities() is True
        stream = push.TuxedoPushStream(client, lambda _s: None, lambda _c: None)
        task = asyncio.create_task(stream.async_run())
        try:
            await wait_until(lambda: stream.connected, timeout=5)
            assert client.tls == "verified"
            assert panel.stream_requests == 1
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        await panel.close()


def test_one_context_per_authority():
    """One object per PEM, so every client for one panel shares a pool key."""
    tls = make_panel_tls_in_memory()
    first = api._verified_ssl_context(tls.authority.pem)
    assert api._verified_ssl_context(tls.authority.pem) is first
    assert api._verified_ssl_context(tls.other_authority.pem) is not first
    assert first.verify_mode == ssl.CERT_REQUIRED
    assert first.check_hostname is True
    assert first.minimum_version == ssl.TLSVersion.TLSv1_2
    assert not first.options & getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0)


def make_panel_tls_in_memory():
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        return make_panel_tls(directory)


def test_chain_items_are_taken_as_der_or_certificate_objects():
    tls = make_panel_tls_in_memory()
    der = ssl.PEM_cert_to_DER_cert(tls.authority.pem)

    class _Cert:
        def __init__(self, encoded):
            self._encoded = encoded

        def public_bytes(self):
            return self._encoded

    assert api._der_of(der) == der
    assert api._der_of(bytearray(der)) == der
    assert api._der_of(_Cert(tls.authority.pem)) == der
    assert api._der_of(_Cert(tls.authority.pem.encode("ascii"))) == der
    assert api._der_of(_Cert(der)) == der
    assert api._der_of(_Cert(None)) is None
    assert api._der_of(object()) is None


def test_unreadable_certificates_describe_as_nothing():
    assert api.presented_certificate([]) is None
    assert api.presented_certificate([b"not a certificate"]) is None
    assert api.describe_certificate("not a certificate") is None
    tls = make_panel_tls_in_memory()
    described = api.describe_certificate(tls.authority.pem)
    assert described.fingerprint == tls.authority.fingerprint
    assert described.leaf_expires is None


def test_only_a_verifying_client_turns_a_tls_failure_into_a_certificate_error():
    """The legacy context's TLS failures stay connection errors, as before."""
    err = aiohttp.ClientSSLError(_connection_key(), OSError(1, "handshake"))
    tls = make_panel_tls_in_memory()

    def client(panel_ca):
        return api.TuxedoTouchClient(
            session=None,
            host="127.0.0.1",
            port=443,
            use_https=True,
            username="u",
            password="p",
            panel_ca=panel_ca,
        )

    assert client(None).certificate_error(err) is None
    assert isinstance(
        client(tls.authority.pem).certificate_error(err),
        api.TuxedoTouchCertificateError,
    )
