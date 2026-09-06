"""The push stream against a fake panel: connect, drop, expire, reconnect.

A real HTTP server on 127.0.0.1 serving the panel's own wire format, so what
is exercised is the actual request, the actual multipart reading and the
actual reconnect loop. Nothing here talks to a panel.
"""

import asyncio

import pytest

from tests.fake_panel import COUNTDOWN_FRAME, FakePanel, wait_until
from tests.no_ha import load

api = load("api")
push = load("push")


@pytest.fixture(autouse=True)
def _real_sockets(socket_enabled):
    """These tests need a real socket, and pytest-socket blocks them.

    pytest-homeassistant-custom-component disables socket creation for every
    test in the session, whether or not it uses Home Assistant. Asking for
    socket_enabled is how a test says it is one of the exceptions; the
    connect() guard the plugin installs alongside it still allows only
    127.0.0.1, which is where the fake panel is.
    """


@pytest.fixture
async def panel():
    made = FakePanel()
    await made.start()
    yield made
    await made.close()


class Collector:
    """Stands in for the coordinator's two callbacks."""

    def __init__(self):
        self.statuses = []
        self.connections = []

    def status(self, status):
        self.statuses.append(status)

    def connection(self, connected):
        self.connections.append(connected)


async def _client(panel, session):
    return api.TuxedoTouchClient(
        session=session,
        host="127.0.0.1",
        port=panel.port,
        use_https=False,
        username=panel.username,
        password=panel.password,
    )


@pytest.fixture
async def session():
    import aiohttp

    made = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
    yield made
    await made.close()


async def _running(panel, session, collector):
    """Start the stream task and wait until it is connected."""
    client = await _client(panel, session)
    stream = push.TuxedoPushStream(client, collector.status, collector.connection)
    task = asyncio.create_task(stream.async_run())
    await wait_until(lambda: stream.connected)
    return stream, task


async def _stop(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_stream_logs_in_opens_and_reports_what_the_panel_pushes(
    panel, session
):
    collector = Collector()
    stream, task = await _running(panel, session, collector)
    try:
        await wait_until(lambda: stream.connection_id is not None)
        assert stream.connection_id == 7
        assert panel.logins == 1

        await panel.push_status_text("Ready To Arm", armed=False)
        await wait_until(lambda: collector.statuses)
        status = collector.statuses[0]
        assert status.text == "Ready To Arm"
        assert status.armed is False
        assert status.colour == "green"
        assert collector.connections == [True]
    finally:
        await _stop(task)


async def test_the_exit_delay_countdown_arrives_as_seconds(panel, session):
    collector = Collector()
    _stream, task = await _running(panel, session, collector)
    try:
        await panel.push(COUNTDOWN_FRAME)
        await wait_until(lambda: collector.statuses)
        assert collector.statuses[0].seconds_remaining == 59
        assert collector.statuses[0].armed is True
    finally:
        await _stop(task)


async def test_a_dropped_stream_is_reconnected(panel, session, monkeypatch):
    """The panel rebooting, or anything else closing the connection: the
    task reconnects on its own and the state keeps arriving."""
    monkeypatch.setattr(push, "PUSH_BACKOFF_INITIAL", 0.01)
    collector = Collector()
    stream, task = await _running(panel, session, collector)
    try:
        panel.drop_stream()
        await wait_until(lambda: not stream.connected)
        await wait_until(lambda: panel.stream_requests == 2)
        await wait_until(lambda: stream.connected)
        assert collector.connections == [True, False, True]

        await panel.push_status_text("Armed Stay", armed=True)
        await wait_until(lambda: collector.statuses)
        assert collector.statuses[0].text == "Armed Stay"
        # Reconnecting did not need another login: the session is still good.
        assert panel.logins == 1
    finally:
        await _stop(task)


async def test_an_expired_session_is_logged_in_again_and_the_stream_reopens(
    panel, session, monkeypatch
):
    """The panel refuses the cookie the stream is holding: logging in again
    is the fix, and the stream comes back on the new session."""
    monkeypatch.setattr(push, "PUSH_BACKOFF_INITIAL", 0.01)
    collector = Collector()
    stream, task = await _running(panel, session, collector)
    try:
        panel.expire_session()
        panel.drop_stream()
        await wait_until(lambda: panel.logins == 2)
        await wait_until(lambda: stream.connected)
        await panel.push_status_text("Ready To Arm", armed=False)
        await wait_until(lambda: collector.statuses)
    finally:
        await _stop(task)


async def test_a_second_refusal_backs_off_instead_of_logging_in_forever(
    panel, session, monkeypatch
):
    """A cookie the panel refuses however fresh it is must not become a
    login loop against a unit that serves one connection at a time."""
    monkeypatch.setattr(push, "PUSH_BACKOFF_INITIAL", 0.02)
    panel.push_status = 302
    client = await _client(panel, session)
    stream = push.TuxedoPushStream(client, lambda status: None, lambda up: None)
    task = asyncio.create_task(stream.async_run())
    try:
        # One immediate retry after the first refusal, and then the wait
        # between attempts doubles: a tenth of a second is a handful of
        # attempts, not a hundred.
        await wait_until(lambda: panel.stream_requests >= 3)
        await asyncio.sleep(0.2)
        assert panel.stream_requests <= 8
    finally:
        await _stop(task)


async def test_firmware_without_a_push_endpoint_stops_asking(panel, session):
    """404 is the firmware's permanent answer; retrying it forever would be
    a request every few minutes to a panel that answers one at a time."""
    panel.push_status = 404
    client = await _client(panel, session)
    stream = push.TuxedoPushStream(client, lambda status: None, lambda up: None)
    await stream.async_run()
    assert stream.unsupported is True
    assert panel.stream_requests == 1
    assert stream.connected is False


async def test_an_unexpected_status_is_retried(panel, session, monkeypatch):
    monkeypatch.setattr(push, "PUSH_BACKOFF_INITIAL", 0.01)
    panel.push_status = 500
    client = await _client(panel, session)
    stream = push.TuxedoPushStream(client, lambda status: None, lambda up: None)
    task = asyncio.create_task(stream.async_run())
    try:
        await wait_until(lambda: panel.stream_requests >= 2)
    finally:
        await _stop(task)


async def test_a_connection_that_held_resets_the_reconnect_wait(
    panel, session, monkeypatch
):
    """Otherwise a night of occasional blips ratchets the wait to the ceiling
    and leaves it there, and a stream healthy for hours comes back slowly for
    a reason that has nothing to do with the panel.

    What earns the reset is the connection having HELD, not having opened:
    it has to outlive PUSH_STABLE_AFTER, which is comfortably longer than the
    panel's own ~33 s status repeat, and to have carried a frame."""
    monkeypatch.setattr(push, "PUSH_BACKOFF_INITIAL", 0.01)
    monkeypatch.setattr(push, "PUSH_STABLE_AFTER", 0.05)
    collector = Collector()
    stream, task = await _running(panel, session, collector)
    try:
        # A run of refusals with nothing coming up: the wait grows.
        panel.push_status = 500
        panel.drop_stream()
        await wait_until(lambda: stream.reconnect_wait > 0.16)

        # The panel answers again and this time the stream stays up, so the
        # next outage starts from scratch.
        panel.push_status = 200
        await wait_until(lambda: stream.connected)
        await asyncio.sleep(0.1)
        panel.drop_stream()
        await wait_until(lambda: not stream.connected)
        # The floor, or the floor after one doubling if the loop got round to
        # its sleep first. Either way it is back at the bottom.
        assert stream.reconnect_wait <= 0.02
    finally:
        await _stop(task)


async def test_a_stream_accepted_and_dropped_at_once_is_not_treated_as_healthy(
    panel, session, monkeypatch
):
    """The failure that turned the ceiling off.

    The backoff used to be reset the instant the response status was 200 -
    before a byte of the body had been read - so every failure AFTER the
    headers returned the wait to its floor: a body that ends at once, an HTML
    error page, an RST mid-body, a read timeout on a half-open socket. At the
    shipped five-second floor that is a connection every five seconds, about
    720 an hour against an intended worst case of twelve, for the life of the
    config entry, on a unit whose own notes say repeated retries make it
    worse and prolonged contention can leave it refusing new connections
    until it is reset.

    Note the panel here still sends its setCid part, so a frame does arrive.
    Resetting on "something came through" would not have closed this.
    """
    monkeypatch.setattr(push, "PUSH_BACKOFF_INITIAL", 0.02)
    monkeypatch.setattr(push, "PUSH_BACKOFF_MAX", 1.0)
    panel.stream_ends_at_once = True
    client = await _client(panel, session)
    stream = push.TuxedoPushStream(client, lambda status: None, lambda up: None)
    task = asyncio.create_task(stream.async_run())
    try:
        await wait_until(lambda: panel.stream_requests >= 4)
        # Every one of those connections delivered its setCid part.
        assert stream.frames >= 3
        # Four connections in, the wait has doubled its way up rather than
        # being pinned at the floor.
        assert stream.reconnect_wait >= 0.08
        # And it keeps growing rather than settling: a fixed window buys a
        # handful of connections, not one every floor-length.
        requests = panel.stream_requests
        await asyncio.sleep(0.3)
        assert panel.stream_requests - requests <= 4
    finally:
        await _stop(task)


async def test_a_frame_that_cannot_be_handled_costs_a_frame_not_the_stream(
    panel, session
):
    """One bad frame must not end the task that carries the alarm state.

    Nothing in async_run's except list catches an arbitrary exception, so one
    raised while decoding or dispatching escaped and the background task
    ended - permanently, with no reconnect, while _set_connected(False) in
    the finally left the two terminal flags clear so the log said
    "reconnecting" and the diagnostics download said "backing off" about a
    task that was dead. The frame is dropped, the connection is kept, and the
    next frame is read as normal.
    """
    collector = Collector()
    client = await _client(panel, session)
    raised: list[str] = []

    def explode(status):
        raised.append(status.text)
        raise ValueError("something in a callback went wrong")

    stream = push.TuxedoPushStream(client, explode, collector.connection)
    task = asyncio.create_task(stream.async_run())
    try:
        await wait_until(lambda: stream.connected)
        await panel.push_status_text("Armed Stay", armed=True)
        await wait_until(lambda: raised)

        await asyncio.sleep(0.1)
        assert not task.done()
        assert stream.connected is True
        assert panel.stream_requests == 1

        # And the connection is still reading.
        stream._on_status = collector.status
        await panel.push_status_text("Ready To Arm", armed=False)
        await wait_until(lambda: collector.statuses)
    finally:
        await _stop(task)


async def test_a_stream_that_keeps_failing_unexpectedly_stops_and_says_so(
    panel, session, monkeypatch
):
    """An escape has to end somewhere visible rather than in silence.

    Falling into the ordinary backoff would open a new connection every few
    seconds for ever on a deterministic bug, against a panel that serves one
    connection at a time; ending without a flag leaves the log and the
    diagnostics asserting a reconnect that is not coming. So: a few attempts,
    then stop, with a terminal flag the diagnostics report carries.
    """
    monkeypatch.setattr(push, "PUSH_BACKOFF_INITIAL", 0.01)
    client = await _client(panel, session)
    stream = push.TuxedoPushStream(client, lambda status: None, lambda up: None)

    async def broken():
        raise ValueError("a bug, not a panel")

    monkeypatch.setattr(stream, "_async_stream_once", broken)
    await asyncio.wait_for(stream.async_run(), timeout=5)

    assert stream.stopped is True
    assert stream.last_error is not None
    assert "a bug, not a panel" in stream.last_error
    assert stream.connected is False


async def test_cancelling_the_task_closes_the_stream(panel, session):
    """Unloading an entry is exactly this: the request never ends by itself."""
    collector = Collector()
    stream, task = await _running(panel, session, collector)
    await _stop(task)
    assert task.done()
    assert stream.connected is False
    assert collector.connections == [True, False]


async def test_a_dead_ecp_link_reaches_the_listener_over_a_real_socket(panel, session):
    """The reader reports the dead link rather than filtering it out.

    Over the actual multipart transport, because the -1 is the whole content
    of the frame that matters and the coordinator can only act on what the
    reader hands it. The connection stays up throughout, which is exactly why
    nothing else in the stream's own health can surface this.
    """
    collector = Collector()
    stream, task = await _running(panel, session, collector)
    try:
        await panel.push(
            b"['ud','SimpleDbgServer2ClientIntf','statusMessageText',"
            b'["0:21:-1:fe:\xfe1Ready To Arm:2"]]'
        )
        await wait_until(lambda: collector.statuses)
        assert collector.statuses[0].panel_status_code == -1
        assert collector.statuses[0].link_down is True
        assert stream.connected is True
    finally:
        await _stop(task)


async def test_the_client_count_frame_is_recorded_and_not_a_status(panel, session):
    collector = Collector()
    stream, task = await _running(panel, session, collector)
    try:
        await panel.push(b"['ud','SimpleDbgServer2ClientIntf','noOfClient',[2]]")
        await wait_until(lambda: stream.client_count == 2)
        assert collector.statuses == []
    finally:
        await _stop(task)


async def test_a_frame_that_is_not_a_partition_status_is_ignored(panel, session):
    collector = Collector()
    stream, task = await _running(panel, session, collector)
    try:
        await panel.push(
            b"['ud','SimpleDbgServer2ClientIntf','statusMessageText',"
            b'["0:504:1:P1  H:1:0:3:3"]]'
        )
        await wait_until(lambda: stream.frames >= 2)
        assert collector.statuses == []
    finally:
        await _stop(task)


async def test_bad_credentials_do_not_hammer_the_panel(panel, session, monkeypatch):
    """ONE credential POST, and then the task ENDS. Not a slower loop.

    This is the failure the login budget exists for. The panel counts failed
    web logins and disables every web account at three - permanently, on
    unpatched firmware, recoverable only at the touchscreen - so a stream
    that reconnects on a rejected password walks the unit off that cliff in
    minutes and then goes on knocking at a door it has bricked shut. A longer
    backoff is not a fix: it only spaces out the attempts that reach three.

    Both waits are shrunk to a hundredth of a second and then a full second
    is allowed to pass: a hundred backoff periods, against a loop whose first
    iteration would already have been the second attempt. What is counted is
    what the PANEL counts - `panel.logins` records only successful logins and
    stays at zero however hard a wrong password is hammered, so a test
    written against it would pass while the panel was being disabled.
    """
    monkeypatch.setattr(push, "PUSH_BACKOFF_INITIAL", 0.01)
    monkeypatch.setattr(push, "PUSH_BACKOFF_MAX", 0.01)
    client = api.TuxedoTouchClient(
        session=session,
        host="127.0.0.1",
        port=panel.port,
        use_https=False,
        username=panel.username,
        password="wrong",
    )
    stream = push.TuxedoPushStream(client, lambda status: None, lambda up: None)
    task = asyncio.create_task(stream.async_run())
    # The task FINISHES rather than idling, so `_stop` is not usable here -
    # it requires a task still running, and that assertion inverts with the
    # fix. Waiting on task.done() is the fix's own claim.
    await wait_until(lambda: task.done())
    await asyncio.sleep(1.0)

    assert panel.login_attempts == 1
    assert task.done()
    assert stream.auth_failed is True
    # It never reached the stream endpoint at all: the refusal is at the login.
    assert panel.stream_requests == 0
    assert stream.connected is False
