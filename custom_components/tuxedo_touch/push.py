"""The panel's push stream: the state it reports, not the state it cached.

`GetSecurityStatus` reads a cache the firmware can only fill from an ECP
message, and answers the literal `"Not available"` while that cache is empty -
the long-standing "the alarm entity goes unknown" fault. This endpoint does
not read that cache. Partition status arrives on it as the panel produces it,
so a client on the stream cannot see `"Not available"` at all.

    GET /SimpleDebugger.interface/G.     <- works
    GET /SimpleDebugger.interfaceG.      <- 404

The slash before `G.` is the whole trick; the vendor's own client appends
`G.` to a base URL that already ends in one, which is how it is easy to get
wrong. Only the session cookie is needed: no token, no query string.

The reply is `multipart/x-mixed-replace; boundary="EH912ZZ"`, one part per
event, and it MUST be decoded latin-1 - the state flag is a raw 0xFE/0xFF
byte and utf-8 replaces it with U+FFFD.

Wire format and the live capture behind it:
`iot-protocol-tools/TUXEDO-HA-ENRICHMENT.md`, section "The push frame format,
decoded byte-exact", with the reference reader in `tuxedo_push.py`; the
endpoint list it belongs to is `TUXEDO-FINDINGS.md` section "The complete
local API surface"; the cache mechanism it bypasses is `TUXEDO-FIRMWARE.md`
section 6. See also ../../../docs/tuxedo_touch_api_notes.md, "The push
stream".
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp

from .api import TuxedoTouchAuthError, TuxedoTouchClient, TuxedoTouchError
from .const import (
    COUNTDOWN_RE,
    PUSH_BACKOFF_INITIAL,
    PUSH_BACKOFF_MAX,
    PUSH_BOUNDARY,
    PUSH_CONNECT_TIMEOUT,
    PUSH_PATH,
    PUSH_READ_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

# One event, in the two shapes the stream sends them:
#   ['setCid', <connection id>]
#   ['ud','SimpleDbgServer2ClientIntf','<field>',[<value>]]
# The text is self-delimiting, which is what the live capture was parsed on.
FRAME_RE = re.compile(r"\['setCid',\s*-?\d+\]|\['ud',.*?\]\]", re.DOTALL)
STATUS_TEXT_RE = re.compile(r"'statusMessageText',\s*\[\"(.*?)\"\]\]", re.DOTALL)
SET_CID_RE = re.compile(r"\['setCid',\s*(-?\d+)\]")
CLIENT_COUNT_RE = re.compile(r"'noOfClient',\s*\[(-?\d+)\]\]")

# The state flag, carried twice in every status frame: once as hex text and
# once as this raw byte. fe = ready/disarmed, ff = arming or armed.
FLAG_READY = 0xFE
FLAG_ARMED = 0xFF
# Same codes and same meaning as the REST API's "Color" field.
COLOURS = {"1": "green", "2": "red", "3": "yellow"}

# Command ids seen in field 2 of a payload.
CMD_HOME_PARTITION = 18
CMD_PARTITION_STATUS = 21
CMD_INITIAL_DATA = 504
CMD_UNSOLICITED = -1
# Only these two carry a partition's armed state; 504 and 18 are registration
# and housekeeping, and decode to nothing because they carry no flag byte.
STATUS_CMDS = frozenset({CMD_PARTITION_STATUS, CMD_UNSOLICITED})

# A stream that has sent this much without a complete frame in it is not
# sending frames; the buffer is dropped rather than grown without limit.
MAX_BUFFER = 65536


class PushStreamUnsupported(TuxedoTouchError):
    """The panel has no push endpoint - it answered 404 rather than a stream.

    Its own class because it is a property of the firmware, not a failure:
    reconnecting cannot change the answer, so the stream stops and the poll
    carries the state on its own.
    """


class PushSessionExpired(TuxedoTouchError):
    """The panel refused the session cookie the stream opened with."""


@dataclass(frozen=True)
class PushStatus:
    """One partition status as the stream reported it."""

    cmd: int
    partition: int | None
    armed: bool
    colour: str | None
    text: str
    seconds_remaining: int | None
    raw: str


def decode_status_frame(payload: str) -> PushStatus | None:
    """Decode a statusMessageText payload, or None if it carries no status.

    The payload is colon-delimited and looks like this, latin-1 decoded:

        0:21:1:fe:\xfe1Ready To Arm:2
        0:21:1:ff:\xff259  Secs Remaining:2
        |  |  | |   |||
        |  |  | |   ||+- display text
        |  |  | |   |+-- colour: 1 green, 2 red (the REST API's "Color")
        |  |  | |   +--- the same flag again, as a RAW BYTE
        |  |  | +------- state flag as hex text: fe ready, ff arming/armed
        |  |  +--------- partition number
        |  +------------ command id
        +--------------- 0 in everything observed

    The raw flag byte is what locates the display field, so the caller must
    have decoded the stream latin-1: utf-8 turns 0xFE/0xFF into U+FFFD and
    the field can no longer be found at all.

    The trailing field (`:2` in both captures above) is not identified in the
    live capture and is deliberately not interpreted here.
    """
    fields = payload.split(":")
    if len(fields) < 3:
        return None
    try:
        cmd = int(fields[1])
    except ValueError:
        return None

    for field in fields[2:]:
        if not field or ord(field[0]) not in (FLAG_READY, FLAG_ARMED):
            continue
        armed = ord(field[0]) == FLAG_ARMED
        body = field[1:]
        colour = None
        if body[:1] in COLOURS:
            colour = COLOURS[body[0]]
            body = body[1:]
        text = body.strip()
        countdown = COUNTDOWN_RE.match(text)
        return PushStatus(
            cmd=cmd,
            partition=int(fields[2]) if fields[2].isdigit() else None,
            armed=armed,
            colour=colour,
            text=text,
            seconds_remaining=int(countdown.group(1)) if countdown else None,
            raw=payload,
        )
    return None


def next_backoff(previous: float) -> float:
    """Double the wait before the next reconnect, up to the ceiling."""
    return min(previous * 2, PUSH_BACKOFF_MAX)


class _FrameDecoder:
    """Turns stream bytes into whole frames.

    The transport is multipart, and the boundary is what marks the end of a
    part; it is used here to keep the buffer bounded and to count parts. The
    frames are emitted as soon as their own text is complete rather than when
    the closing boundary arrives, which is how the live capture was read: a
    firmware that spells the delimiter differently then still delivers state
    instead of stalling it.
    """

    def __init__(self, boundary: str = PUSH_BOUNDARY) -> None:
        self._boundary = boundary
        self._buffer = ""
        self.parts = 0

    def feed(self, text: str) -> list[str]:
        """Add what just arrived and return every frame now complete."""
        self._buffer += text
        frames: list[str] = []
        end = 0
        for match in FRAME_RE.finditer(self._buffer):
            frames.append(match.group())
            end = match.end()
        if end:
            self.parts += self._buffer.count(self._boundary, 0, end)
            self._buffer = self._buffer[end:]
        elif len(self._buffer) > MAX_BUFFER:
            # Nothing in that much text was a frame, so none of it is the
            # start of one either.
            self._buffer = ""
        return frames


class TuxedoPushStream:
    """Holds the push stream open and reports what the panel pushes.

    One task for the life of the config entry: connect, read frames, and on
    any drop reconnect with a growing backoff. Everything is aiohttp, so
    nothing here occupies the event loop between frames, and cancelling the
    task is the whole of stopping it.
    """

    def __init__(
        self,
        client: TuxedoTouchClient,
        on_status: Callable[[PushStatus], None],
        on_connection_change: Callable[[bool], None],
    ) -> None:
        self._client = client
        self._on_status = on_status
        self._on_connection_change = on_connection_change
        self.connected = False
        self.unsupported = False
        # Terminal, like `unsupported`: the panel refused the credentials and
        # the stream has stopped rather than backed off. Only a reauth and the
        # reload it brings starts a stream again.
        self.auth_failed = False
        self.connection_id: int | None = None
        self.client_count: int | None = None
        self.frames = 0
        # How long the next reconnect waits. An attribute rather than a local
        # so the reset below can be asserted, and so diagnostics can say how
        # far a failing stream has backed off.
        self.reconnect_wait = PUSH_BACKOFF_INITIAL

    async def async_run(self) -> None:
        """Keep the stream open for as long as this task is not cancelled."""
        self.reconnect_wait = PUSH_BACKOFF_INITIAL
        expiry_retried = False
        while True:
            try:
                await self._async_stream_once()
                # The connection worked, so the next expiry gets its own
                # immediate re-login rather than inheriting the last one's.
                # (The wait is reset the moment a connection comes up; see
                # _set_connected.)
                expiry_retried = False
            except asyncio.CancelledError:
                raise
            except PushStreamUnsupported:
                # A 404 is the firmware's permanent answer, so retrying it
                # would be a request every five minutes for ever, for ever
                # answered the same way - and unlike the stream itself, a
                # refused request is an ordinary one that contends for the
                # single connection the poll needs.
                self.unsupported = True
                _LOGGER.info(
                    "This panel has no push stream (it answered 404). The "
                    "alarm state comes from the status poll alone, which on "
                    "some firmware reports 'Not available' for long stretches"
                )
                return
            except PushSessionExpired:
                # The cookie died under the stream. Logging in again is the
                # fix, and it is worth doing at once - but only once per
                # outage, so a panel refusing a fresh cookie cannot turn into
                # a login loop.
                self._client.invalidate_session()
                if not expiry_retried:
                    expiry_retried = True
                    _LOGGER.debug("Push stream session expired; logging in again")
                    continue
                _LOGGER.debug("Push stream session expired again; backing off")
            except TuxedoTouchAuthError as err:
                # Terminal ONLY because this class means the panel compared a
                # credential and refused it; api.py raises it nowhere else. A
                # key page that fails behind an accepted login raises
                # TuxedoTouchSessionError and lands in the ordinary backoff
                # below, because a stream that stops for good on a transient
                # HTTP 500 loses the release's primary state source to a
                # fault that would have healed on the next reconnect.
                #
                # Terminal, exactly like the 404 above, and for a harder
                # reason. Every reconnect here re-runs the full login
                # handshake - a login GET and a credential POST - against
                # credentials the panel has already refused, and on stock
                # firmware three refused web logins disable every web account
                # permanently. Backing off does not help: it only spaces out
                # the attempts that reach three. The poll raises
                # ConfigEntryAuthFailed on the same credentials and starts the
                # reauth flow, which is the only thing that can fix this, and
                # the reload it ends with builds a new stream.
                self.auth_failed = True
                _LOGGER.warning(
                    "The panel rejected the web login credentials, so its push "
                    "stream has stopped and Home Assistant will not try them "
                    "again on its own (%s). Repeated failed web logins can "
                    "disable the panel's web accounts - permanently, on "
                    "unpatched firmware - so the credentials have to come from "
                    "you: enter the current ones on the integration's "
                    "re-authentication card",
                    err,
                )
                return
            except (TuxedoTouchError, aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug("Push stream dropped (%s); reconnecting", err)

            await asyncio.sleep(self.reconnect_wait)
            self.reconnect_wait = next_backoff(self.reconnect_wait)

    async def _async_stream_once(self) -> None:
        """One connection, from login to the moment the panel stops talking."""
        cookie = await self._client.async_session_cookie()
        url = f"{self._client.base_url}{PUSH_PATH}"
        # No total timeout: the point of the request is to stay open. A read
        # timeout still applies, and it is meaningful here rather than a
        # guess: the panel repeats the partition status on its own timer
        # roughly every 33 s, so silence for PUSH_READ_TIMEOUT is a socket
        # that has gone half-open - after a panel reboot, say - and not a
        # quiet house.
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=PUSH_CONNECT_TIMEOUT,
            sock_connect=PUSH_CONNECT_TIMEOUT,
            sock_read=PUSH_READ_TIMEOUT,
        )
        async with self._client.session.get(
            url,
            headers={"Cookie": cookie},
            ssl=self._client.ssl_arg,
            timeout=timeout,
            allow_redirects=False,
        ) as resp:
            if resp.status in (401, 302):
                raise PushSessionExpired(f"push stream refused: HTTP {resp.status}")
            if resp.status == 404:
                raise PushStreamUnsupported("no push endpoint on this firmware")
            if resp.status != 200:
                raise TuxedoTouchError(f"push stream returned HTTP {resp.status}")

            decoder = _FrameDecoder(_boundary_of(resp.headers.get("Content-Type")))
            self._set_connected(True)
            try:
                async for chunk in resp.content.iter_any():
                    # latin-1, never utf-8: the state flag is a raw byte.
                    for frame in decoder.feed(chunk.decode("latin-1")):
                        self._handle_frame(frame)
            finally:
                self._set_connected(False)

    def _set_connected(self, connected: bool) -> None:
        if connected == self.connected:
            return
        self.connected = connected
        if connected:
            # A connection that came up starts the next outage from scratch.
            # Without this, a night of occasional blips ratchets the wait to
            # the five-minute ceiling and leaves it there, so a stream that
            # had been healthy for hours would come back slowly for a reason
            # that has nothing to do with the panel.
            self.reconnect_wait = PUSH_BACKOFF_INITIAL
        else:
            self.connection_id = None
        self._on_connection_change(connected)

    def _handle_frame(self, frame: str) -> None:
        self.frames += 1
        if (cid := SET_CID_RE.match(frame)) is not None:
            self.connection_id = int(cid.group(1))
            _LOGGER.debug("Push stream connected, connection id %s", cid.group(1))
            return
        if (count := CLIENT_COUNT_RE.search(frame)) is not None:
            self.client_count = int(count.group(1))
            return
        if (payload := STATUS_TEXT_RE.search(frame)) is None:
            return
        status = decode_status_frame(payload.group(1))
        if status is None or status.cmd not in STATUS_CMDS:
            _LOGGER.debug("Push frame carries no partition status: %r", frame)
            return
        self._on_status(status)


def _boundary_of(content_type: str | None) -> str:
    """The multipart boundary the panel declared, or the one it always uses."""
    if content_type:
        match = re.search(r'boundary="?([^";]+)"?', content_type)
        if match:
            return match.group(1)
    return PUSH_BOUNDARY
