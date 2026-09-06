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
import time
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
    PUSH_STABLE_AFTER,
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

# Command ids, carried in field 1 of a payload.
CMD_HOME_PARTITION = 18
CMD_PARTITION_STATUS = 21
CMD_INITIAL_DATA = 504
CMD_UNSOLICITED = -1
# Only these two carry a partition's armed state; 504 and 18 are registration
# and housekeeping, and decode to nothing because they carry no flag byte.
STATUS_CMDS = frozenset({CMD_PARTITION_STATUS, CMD_UNSOLICITED})

# What field 2 of a command-21 payload carries when the Tuxedo has lost the
# ECP link to the VISTA panel behind it. Read out of the producer,
# CReceiverThread::sltSendChangedPartitionStatus at 0x144880 in /tuxedo:
#
#     0x144a7c  bl     PanelIsTalking()
#     0x144a80  cmp    r0, #0
#     0x144a84  mvneq  r3, #0          ; link down -> r3 = -1
#     0x144a88  streq  r3, [sp, #8]    ; -1 stored into the message field
#     0x144a8c  bne    0x144ae8        ; otherwise the real status code
#     0x144aa0  bl     osal_MqSend(int, char*, int)   ; SENT EITHER WAY
#
# The frame is still sent, still carries a display text and still carries a
# state flag - all of them the last thing the Tuxedo drew before it went
# blind. So this is the one value on the stream that means "believe nothing
# else in this frame".
PANEL_STATUS_LINK_DOWN = -1

# A stream that has sent this much without a complete frame in it is not
# sending frames; the buffer is dropped rather than grown without limit.
MAX_BUFFER = 65536

# How many connections in a row may fail for a reason this module does not
# name before the stream stops rather than reconnecting. An unexpected
# exception is a bug in here, not a panel that is down: retrying it on the
# ordinary backoff is a new connection every few seconds for ever, against a
# unit that serves one at a time, for an answer that will not change.
MAX_UNEXPECTED_FAILURES = 3


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
    # Field 2 of a command-21 payload. NOT the partition - see the field map
    # in decode_status_frame. None when the frame has no such field (the
    # unsolicited record) or when it could not be read as an integer.
    panel_status_code: int | None
    armed: bool
    colour: str | None
    text: str
    seconds_remaining: int | None
    raw: str

    @property
    def link_down(self) -> bool:
        """Whether the Tuxedo has told us it cannot see the VISTA panel.

        A code that could not be read is NOT a dead link: `None` here means
        the field was absent or unparseable, and taking the alarm entity
        unavailable on a decoding fault would be a second wrong answer laid
        over the first.
        """
        return self.panel_status_code == PANEL_STATUS_LINK_DOWN


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
        |  |  +--------- panel status code; -1 when the ECP link is down
        |  +------------ command id
        +--------------- 0 in everything observed

    Field 2 was read as the partition number until 0.4.2, which was wrong and
    shipped. It is the value the panel's own /eventhandler.html page calls
    panelStatusCode: a capture of that page taken at the same instant as a
    frame answered `curStatus = "21:a1Ready To Arm:1"` while the frame read
    `0:21:1:fe:\xfe1Ready To Arm:2` - so the page's panelStatusCode is field
    2, and the frame's trailing `:2` is the colour rather than the code.
    Where the partition went is answered in TuxedoPushStream's docstring.

    The raw flag byte is what locates the display field, so the caller must
    have decoded the stream latin-1: utf-8 turns 0xFE/0xFF into U+FFFD and
    the field can no longer be found at all.
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
            panel_status_code=_status_code_of(fields[2]),
            armed=armed,
            colour=colour,
            text=text,
            seconds_remaining=int(countdown.group(1)) if countdown else None,
            raw=payload,
        )
    return None


def _status_code_of(field: str) -> int | None:
    """The panel status code, or None when the field does not hold one.

    Field 2 is the display text on the unsolicited record (command id -1),
    which is how "no code" happens on a well-formed frame.

    The conversion IS the guard, rather than a different question asked
    alongside it. str.isdigit() is not int(): the latin-1 superscripts
    (0xB9/0xB2/0xB3) satisfy the first and raise ValueError on the second,
    and latin-1 is how this stream must be decoded, so those bytes are
    exactly what this decoder can see. That ValueError was raised inside the
    read loop, where nothing in async_run's except list catches it.
    """
    try:
        return int(field)
    except ValueError:
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

    THE STREAM CARRIES NO PARTITION FIELD, and does not need one. The head of
    the producer, CReceiverThread::sltSendChangedPartitionStatus at 0x144880:

        0x144884  mov  r3, #0x15            ; 21, the command id
        0x1448a0  bl   GetCurrentPartition()
        0x1448a4  cmp  r0, r4               ; r4 = the partition that changed
        0x1448a8  beq  0x1448b4             ; equal -> build and send
        0x1448ac  add  sp, sp, #0x26c       ; NOT equal -> return, send nothing

    A frame is emitted only when the partition that changed IS the panel's
    currently selected partition, so every frame that arrives is about the
    current partition by construction - the producer has already applied the
    filter. Nothing here re-applies it: a receiver-side partition guard can
    only reject valid frames, which is the 0.4.1 defect. It does not exist
    because the field is not there, not because the field was not found.

    The caveat, written down and deliberately NOT built for: the scoping is
    "whichever partition the panel is currently showing", not "partition N".
    So the stream FOLLOWS the panel. If someone changes the displayed
    partition at the touchscreen or through the web UI (the firmware ships
    script/changePartitionScript.js for exactly that), this stream begins
    delivering a different partition's status with no marker in the frame to
    say so. On a single-partition system - what this integration has been
    built and tested against - that cannot happen. On a multi-partition one
    it is a real mis-attribution and it is invisible in the data: the fix is
    to consult GetCurrentPartition over REST rather than to guess, and it is
    not attempted here. CONF_PARTITION still governs the REST status poll and
    every arm/disarm command, which do take a partition id.
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
        # The third terminal state, and the one that says a bug rather than a
        # panel: the loop failed repeatedly for a reason this module does not
        # name. Reported in diagnostics beside the other two, so a downloaded
        # report stops claiming a reconnect is pending for a task that has
        # stopped.
        self.stopped = False
        self.last_error: str | None = None
        self.connection_id: int | None = None
        self.client_count: int | None = None
        self.frames = 0
        # How long the next reconnect waits. An attribute rather than a local
        # so the reset can be asserted, and so diagnostics can say how far a
        # failing stream has backed off.
        self.reconnect_wait = PUSH_BACKOFF_INITIAL

    async def async_run(self) -> None:
        """Keep the stream open for as long as this task is not cancelled."""
        self.reconnect_wait = PUSH_BACKOFF_INITIAL
        expiry_retried = False
        unexpected_failures = 0
        while True:
            try:
                await self._async_stream_once()
                # The connection worked, so the next expiry gets its own
                # immediate re-login rather than inheriting the last one's.
                # (Whether the wait is reset is a higher bar, and is decided
                # in _async_stream_once: see PUSH_STABLE_AFTER.)
                expiry_retried = False
                unexpected_failures = 0
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
                unexpected_failures = 0
            except Exception as err:
                # Anything this module does not name is a bug in here, and it
                # used to end the task by propagating - permanently, with no
                # reconnect, while the finally in _async_stream_once had
                # already cleared `connected` so the coordinator logged
                # "reconnecting" and diagnostics reported "backing off" about
                # a task that was dead. Home Assistant attaches no
                # error-logging callback to a background task and the
                # coordinator holds a reference, so even the traceback did
                # not reach the log until garbage collection.
                #
                # CancelledError is re-raised above and is a BaseException, so
                # this cannot swallow cancellation.
                unexpected_failures += 1
                self.last_error = f"{type(err).__name__}: {err}"
                _LOGGER.exception("The push stream failed unexpectedly")
                if unexpected_failures >= MAX_UNEXPECTED_FAILURES:
                    # Terminal, like the 404 and the refused credentials: a
                    # deterministic fault would otherwise take a new
                    # connection every few seconds for ever from a panel that
                    # serves one at a time, and the poll would be contending
                    # with it for exactly that connection.
                    self.stopped = True
                    _LOGGER.warning(
                        "The panel's push stream has stopped after %s "
                        "unexpected failures (%s) and will not reconnect on "
                        "its own. The status poll carries the alarm state by "
                        "itself from here, which on some firmware reports "
                        "'Not available' for long stretches; reloading the "
                        "integration starts a new stream",
                        unexpected_failures,
                        self.last_error,
                    )
                    return

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
            started = time.monotonic()
            frames_before = self.frames
            try:
                async for chunk in resp.content.iter_any():
                    # latin-1, never utf-8: the state flag is a raw byte.
                    for frame in decoder.feed(chunk.decode("latin-1")):
                        try:
                            self._handle_frame(frame)
                        except Exception:
                            # One frame the panel spelled in a way nothing
                            # here expected costs that frame, not the stream.
                            # Contained at the frame rather than in async_run
                            # deliberately: reconnecting on a decode bug tears
                            # down a healthy socket and takes another
                            # connection from a panel that serves one at a
                            # time, for a fault the next frame is unaffected
                            # by. The dispatch into the coordinator is inside
                            # this guard too, because that is where a listener
                            # can raise.
                            _LOGGER.exception(
                                "Dropping a push frame that could not be handled: %r",
                                frame,
                            )
            finally:
                self._set_connected(False)
                # Whether this connection earned the next outage a fresh
                # start. Both halves are needed: the panel sends its setCid
                # part before a body that ends at once, so a frame having
                # arrived proves nothing on its own, and a half-open socket
                # can sit there past the threshold delivering nothing.
                held = time.monotonic() - started >= PUSH_STABLE_AFTER
                if held and self.frames > frames_before:
                    self.reconnect_wait = PUSH_BACKOFF_INITIAL

    def _set_connected(self, connected: bool) -> None:
        if connected == self.connected:
            return
        self.connected = connected
        if not connected:
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
