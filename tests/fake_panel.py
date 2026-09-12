"""A stand-in Tuxedo Touch: the real wire format, none of the hardware.

Everything the integration talks to is here - the login handshake, the
key/IV page, the encrypted API and the push stream - implemented from
docs/tuxedo_touch_api_notes.md, so a test drives the actual request
building, cookie handling, encryption and frame parsing rather than a mock
of them.

The same server stands in for a panel running tuxweb, the replacement web
server, with `tuxweb=True`: the capability endpoint answers 200, the login
and key pages are gone, the API takes a plain form body behind a bearer
token, and commands are confirmed or refused by status code. Implemented
from tuxweb's own src/api.rs and src/serve.rs, shape for shape - the same
paths, the vendor's misspelt `Sucess`, arm answering under `Response` and
disarm under `Result` - because that is what the integration is held to.

Plain HTTP on 127.0.0.1: the panel's expired 1024-bit demo certificate and
its legacy renegotiation are a property of the transport, not of anything
tested here, and tests/test_client_io.py already pins the SSLContext they
need.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from typing import Any

from aiohttp import web

KEY_HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
IV_HEX = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
KEY = bytes.fromhex(KEY_HEX)
IV = bytes.fromhex(IV_HEX)
BLOB = KEY_HEX + IV_HEX

# 31 hex characters, which is what the panel actually sends - not 32.
CHALLENGE = "a1b2c3d4e5f60718293a4b5c6d7e8f9"
SESSION_COOKIE_NAME = "z9ZAqJtI_1392221684"
BOUNDARY = "EH912ZZ"

# What tuxweb issues: 32 bytes from the CSPRNG as 64 hex characters.
TUXWEB_TOKEN = "5f4dcc3b5aa765d61d8327deb882cf99e99a18c428cb38d5f260853678922e03"
# The capabilities tuxweb 0.1.0 declares, as api.rs spells them.
TUXWEB_CAPABILITIES = ("panel_link_state", "command_result", "status_refresh")
# The stock answer to the capability endpoint: 404 with this exact 20-byte
# body, measured on TUXW_V5.3.21.0_VA and reproduced by tuxweb's not_found().
# The key is unquoted, so it is not JSON, and that is part of the measurement.
STOCK_NOT_FOUND = b'{Status:"Not Found"}'
# tuxweb's reply bodies, byte for byte from api.rs and serve.rs.
TUXWEB_ARM_OK = b'{"Status":"Sucess","Result":{"Response":"Command sent sucessfully"}}'
TUXWEB_DISARM_OK = b'{"Status":"Sucess","Result":{"Result":"Disarmed"}}'
TUXWEB_NOT_CONFIRMED = (
    b'{"Status":"Failure","Result":{"Response":"command sent but not confirmed"}}'
)
TUXWEB_CODE_REQUIRED = (
    b'{"Status":"Failure","Result":{"Response":"a user code is required"}}'
)
# The colour digit tuxweb puts in front of the display text, from the word
# the stock poll spells it with.
COLOUR_DIGITS = {"Green": "1", "Red": "2", "Yellow": "3"}

# The two frames of the live capture, byte for byte. The flag is a raw
# 0xFE/0xFF byte, so these are bytes objects and never str.
READY_FRAME = (
    b"['ud','SimpleDbgServer2ClientIntf','statusMessageText',"
    b'["0:21:1:fe:\xfe1Ready To Arm:2"]]'
)
COUNTDOWN_FRAME = (
    b"['ud','SimpleDbgServer2ClientIntf','statusMessageText',"
    b'["0:21:1:ff:\xff259  Secs Remaining:2"]]'
)


def status_frame(
    text: str, armed: bool, colour: str = "1", status_code: int = 1
) -> bytes:
    """One partition status part, in the panel's own byte layout.

    `status_code` is field 2, the panel status code - NOT the partition. The
    producer writes -1 there when PanelIsTalking() answers 0, i.e. the ECP
    link from the Tuxedo to the VISTA is down, and sends the frame anyway.
    """
    flag = 0xFF if armed else 0xFE
    payload = (
        f"0:21:{status_code}:{flag:02x}:".encode("latin-1")
        + bytes([flag])
        + f"{colour}{text}:2".encode("latin-1")
    )
    return (
        b"['ud','SimpleDbgServer2ClientIntf','statusMessageText',[\"" + payload + b'"]]'
    )


def console_frames(line_1: str, line_2: str) -> list[bytes]:
    """The FOUR parts one keypad LCD change puts on the stream, in order.

    Read out of the vendor's type-20 handler, and emitted by tuxweb in the
    same shape while it holds console mode on; the id-20 record has since
    been seen on the live stream across an arm and a disarm
    (docs/feature-spec-keypad-link-changedby.md, item 3), the -1 copies are
    still the handler's reading. `bprintf("%d%s%d%s%s", session, ":", 20,
    ":2", text)`: the session is 0 for a broadcast, which is the only kind
    sent, and the `2` after the second colon is the constant `":2"` at
    0x852f4, never a colour digit. Then the same text three more times as
    command id -1.

    The id-20 record is lossy: its first ":" past index 0 is replaced by "-".
    The three -1 copies carry the text raw. No flag byte on any of the four,
    which is what keeps them out of the partition decoder.
    """
    text = f"{line_1}|{line_2}"
    broadcast = f"0:20:2{_replace_first_colon(text)}"
    unsolicited = f"0:-1:2{text}"
    return [_part(broadcast)] + [_part(unsolicited)] * 3


def _replace_first_colon(text: str) -> str:
    """The vendor's mangling of the id-20 text: the first ':' past index 0."""
    at = text.find(":", 1)
    return text if at < 0 else text[:at] + "-" + text[at + 1 :]


def _part(payload: str) -> bytes:
    """One statusMessageText part around a payload that carries no raw byte."""
    return (
        b"['ud','SimpleDbgServer2ClientIntf','statusMessageText',[\""
        + payload.encode("latin-1")
        + b'"]]'
    )


async def wait_until(predicate: Any, timeout: float = 10.0) -> None:
    """Wait for something a real socket has to deliver.

    The stream runs on an actual TCP connection, so a frame arrives when the
    loop gets round to reading it - not when block_till_done returns.
    """
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def _encrypt(plaintext: str) -> str:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode(
        "ascii"
    )


class FakePanel:
    """One panel. Start it with `await panel.start()`, stop it with `close()`."""

    def __init__(
        self,
        *,
        password: str = "secret",
        username: str = "installer",
        status: str = "Ready To Arm",
        colour: str = "Green",
        push_status: int = 200,
        empty_command_body: bool = True,
        auto_push: bool = False,
        tuxweb: bool = False,
        token: str | None = TUXWEB_TOKEN,
        capabilities: tuple[str, ...] = TUXWEB_CAPABILITIES,
    ) -> None:
        self.username = username
        self.password = password
        self.status = status
        self.colour = colour
        # Which firmware this fake is. tuxweb serves no login page and no key
        # page, gates the API and the stream on `token` - the one token in
        # its store; None is an empty store, which authenticates nobody - and
        # confirms commands rather than merely sending them.
        self.tuxweb = tuxweb
        self.token = token
        self.capabilities = list(capabilities)
        # tuxweb's state model: the armed flag its status answer carries and
        # its commands are confirmed against. A stock panel has no such
        # thing on the API - its poll carries display text only.
        self.armed = False
        # Whether a tuxweb command is confirmed (200, the state flipped) or
        # sent and never confirmed (504, the state unchanged): a faulted zone
        # refusing an arm, or a code the panel will not take.
        self.confirm_commands = True
        # The probe, counted separately from everything else because the
        # contract is "once, at setup" - and because on stock it is the one
        # request this integration makes before knowing what it talks to.
        self.capability_probes = 0
        # Overrides for the probe's answer: a status other than the
        # firmware's own, or a 200 carrying this body instead of the
        # capability document. What is detected must be the capability list
        # and nothing else, and these are how a test shows that.
        self.capability_status: int | None = None
        self.capability_body: bytes | None = None
        # This many probes are answered by hanging up mid-request: the web
        # server restarting under a rollback, which is exactly when the
        # re-check runs. A verdict must not be drawn from a connection that
        # never happened, on the re-check as on the setup probe. Each probe
        # hung up on is still counted in capability_probes, and aiohttp
        # retries an idempotent GET once on a dropped connection, so ONE
        # hang-up never reaches the client at all - a test that wants the
        # client to see a connection failure sets 2.
        self.capability_probe_disconnects = 0
        # Reported in the capability document for people; a client must not
        # branch on either, and a test changes them to prove it does not.
        self.firmware = "tuxweb/0.1.0"
        self.contract = 1
        # Every request to the status or command endpoints, authenticated or
        # not - a refusal that was retried shows up here as two.
        self.api_requests = 0
        # A tuxweb status answer that is not the documented one: a 200 with
        # this body instead. For the shape checks on the client's side.
        self.tuxweb_status_body: bytes | None = None
        # Every GET of the login page, so a test can assert that tuxweb mode
        # never asks for one. `login_attempts` below counts credential POSTs.
        self.login_page_requests = 0
        # What the most recent API request carried, so a test can assert on
        # the wire shape - bearer token versus authtoken/identity/cookie, form
        # body versus ciphertext - rather than on the outcome alone.
        self.last_api_headers: dict[str, str] = {}
        self.last_command_body: str | None = None
        # A busy or rebooting embedded server, on the two requests that are
        # not the credential comparison. Neither is the panel judging a
        # password, and telling them apart from one that is is what several
        # of these tests are about.
        #
        # The key page: `keys_failures_left` requests answer with
        # `keys_status` and a body carrying no #readit element, so 500 covers
        # the non-200 case and 200 covers the missing-key-material case.
        self.keys_status = 200
        self.keys_failures_left = 0
        # The credential POST: `login_post_failures_left` requests answer
        # `login_post_status` INSTEAD of comparing anything. The attempt is
        # still counted, because the panel was still asked.
        self.login_post_status = 503
        self.login_post_failures_left = 0
        # What the stream endpoint answers with: 200 opens a stream, 404 is
        # firmware without one, 302/401 is a dead session cookie.
        self.push_status = push_status
        # A 200 that carries the real multipart header and the setCid part
        # and then ends the response. The failure the fixture could not
        # express before, and therefore the one nothing tested: an embedded
        # web server restarting, or a middlebox, accepts the connection and
        # hangs up. Note it still delivers a frame, so "did anything arrive"
        # is not on its own a test of whether the connection was healthy.
        self.stream_ends_at_once = False
        self.empty_command_body = empty_command_body
        # A real panel reports what a command did on the stream, seconds
        # later; with this on, so does this one.
        self.auto_push = auto_push

        # Two counters, and the gap between them is the whole point.
        # `logins` counts logins that SUCCEEDED. `login_attempts` counts every
        # credential POST the panel was asked to judge, refusals included -
        # which is what the real unit counts, and what disables its web
        # accounts at three. A test written against `logins` alone passes
        # whether or not something is hammering the panel with bad passwords.
        self.logins = 0
        self.login_attempts = 0
        self.stream_requests = 0
        self.commands: list[str] = []
        self.polls = 0
        # Set when a stream connection is open and reading.
        self.stream_open = asyncio.Event()
        self._cookie_value = "0"
        self._writers: list[web.StreamResponse] = []
        self._drop = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.port = 0
        # Headers of the most recent stream request. Recorded so a test can
        # assert what a client actually SENT, rather than only what it received
        # -- the push-source options are only meaningful if the credential
        # reaches the far end.
        self.last_push_headers: dict[str, str] = {}

    # ------------------------------------------------------------- server
    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/authenticated/index.html", self._login_page)
        app.router.add_post("/authenticated/index.html", self._login)
        app.router.add_get("/tuxedoapi.html", self._keys)
        app.router.add_get(
            "/system_http_api/API_REV01/GetCapabilities", self._capabilities
        )
        app.router.add_post(
            "/system_http_api/API_REV01/GetSecurityStatus", self._status
        )
        app.router.add_post(
            "/system_http_api/API_REV01/AdvancedSecurity/{command}", self._command
        )
        app.router.add_get("/SimpleDebugger.interface/G.", self._stream)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        sockets = self._runner.addresses
        self.port = int(sockets[0][1])

    async def close(self) -> None:
        self.drop_stream()
        if self._runner is not None:
            await self._runner.cleanup()

    # --------------------------------------------------------------- push
    def drop_stream(self) -> None:
        """Let go of every open stream, the way a panel reboot would."""
        self._drop.set()

    async def push(self, frame: bytes) -> None:
        """Send one part to every connected stream client."""
        for writer in list(self._writers):
            await writer.write(b"--" + BOUNDARY.encode() + b"\r\n" + frame)

    async def push_status_text(self, text: str, armed: bool) -> None:
        await self.push(status_frame(text, armed, "2" if armed else "1"))

    async def push_console(self, line_1: str, line_2: str) -> None:
        """One keypad LCD change: the id-20 record, then its three -1 copies.

        What tuxweb sends with console mode held on, and what the vendor sent
        while someone had /console.html open; see console_frames.
        """
        for frame in console_frames(line_1, line_2):
            await self.push(frame)

    def expire_session(self) -> None:
        """Forget the cookie, so every request with it is refused."""
        self._cookie_value = "expired"

    # ------------------------------------------------------------ handlers
    async def _capabilities(self, request: web.Request) -> web.Response:
        """The one endpoint that says which firmware this is. No auth either way."""
        self.capability_probes += 1
        if self.capability_probe_disconnects:
            self.capability_probe_disconnects -= 1
            assert request.transport is not None
            request.transport.close()
            return web.Response(status=500, body=b"")
        if self.capability_status is not None:
            return web.Response(status=self.capability_status, body=b"")
        if self.capability_body is not None:
            return web.Response(body=self.capability_body, content_type="text/html")
        if not self.tuxweb:
            return web.Response(
                status=404, body=STOCK_NOT_FOUND, content_type="application/json"
            )
        return web.json_response(
            {
                "contract": self.contract,
                "firmware": self.firmware,
                "panel_model": "TUXW",
                "capabilities": self.capabilities,
            }
        )

    def _token_ok(self, request: web.Request) -> bool:
        """tuxweb's auth.token_from_head: a bearer header or a tuxweb_token cookie."""
        if self.token is None:
            return False
        bearer = request.headers.get("Authorization", "")
        if bearer.startswith("Bearer "):
            return bearer.removeprefix("Bearer ").strip() == self.token
        for part in request.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "tuxweb_token":
                return value == self.token
        return False

    @staticmethod
    def _unauthorized() -> web.Response:
        """tuxweb's 401: a real status with an empty body, so a stream client
        sees the denial rather than reading a login page to EOF."""
        return web.Response(status=401, body=b"")

    async def _login_page(self, request: web.Request) -> web.Response:
        self.login_page_requests += 1
        if self.tuxweb:
            # tuxweb serves nothing outside the API namespace and the stream.
            return web.Response(status=404, body=b"")
        resp = web.Response(text="<html>login</html>")
        resp.headers["Random"] = CHALLENGE
        resp.headers["RandomID"] = "42"
        resp.set_cookie("_zFL", "correlate")
        return resp

    async def _login(self, request: web.Request) -> web.Response:
        # Counted BEFORE the comparison: what a panel counts is the attempt,
        # not the outcome.
        self.login_attempts += 1
        if self.tuxweb:
            return web.Response(status=404, body=b"")
        if self.login_post_failures_left:
            # The web server failing, above the login logic: the request
            # arrived, and no credential was ever compared.
            self.login_post_failures_left -= 1
            return web.Response(status=self.login_post_status, text="busy")
        form = await request.post()
        expected = hmac.new(
            CHALLENGE.encode(),
            (self.username.lower() + self.password).encode(),
            hashlib.sha512,
        ).hexdigest()
        if form.get("log1") != expected:
            return web.Response(status=200, text="denied")
        self.logins += 1
        self._cookie_value = f"{self.logins:08x}"
        resp = web.Response(status=302, text="")
        resp.set_cookie(SESSION_COOKIE_NAME, self._cookie_value)
        resp.set_cookie("_zFL", "", max_age=0)
        return resp

    def _authenticated(self, request: web.Request) -> bool:
        return request.headers.get("Cookie") == self.cookie

    @property
    def cookie(self) -> str:
        return f"{SESSION_COOKIE_NAME}={self._cookie_value}"

    async def _keys(self, request: web.Request) -> web.Response:
        if self.tuxweb:
            return web.Response(status=404, body=b"")
        if not self._authenticated(request):
            return web.Response(status=302, text="")
        if self.keys_failures_left:
            self.keys_failures_left -= 1
            return web.Response(status=self.keys_status, text="<html>busy</html>")
        return web.Response(text=f'<input id="readit" value="{BLOB}">')

    async def _status(self, request: web.Request) -> web.Response:
        self.api_requests += 1
        self.last_api_headers = dict(request.headers)
        if self.tuxweb:
            if not self._token_ok(request):
                return self._unauthorized()
            self.polls += 1
            if self.tuxweb_status_body is not None:
                return web.Response(
                    body=self.tuxweb_status_body, content_type="text/html"
                )
            # push.rs status_json: the display field after the flag byte, so
            # the colour digit leads the text; empty before any status has
            # been seen at all.
            state = ""
            if self.status:
                state = COLOUR_DIGITS.get(self.colour, "") + self.status
            return web.json_response(
                {"partition": 1, "armed": self.armed, "state": state}
            )
        if not self._authenticated(request):
            return web.Response(status=401, text="")
        self.polls += 1
        body = {"Status": self.status, "Color": self.colour}
        import json as _json

        return web.json_response({"Result": _encrypt(_json.dumps(body))})

    async def _command(self, request: web.Request) -> web.Response:
        self.api_requests += 1
        self.last_api_headers = dict(request.headers)
        if self.tuxweb:
            return await self._tuxweb_command(request)
        if not self._authenticated(request):
            return web.Response(status=401, text="")
        command = request.match_info["command"]
        self.commands.append(command)
        if self.auto_push:
            if command == "DisarmWithCode":
                self.status = "Ready To Arm"
                await self.push_status_text("Ready To Arm", armed=False)
            else:
                self.status = "59  Secs Remaining"
                await self.push(COUNTDOWN_FRAME)
        if self.empty_command_body:
            # What the firmware actually does: 200, zero bytes, and the
            # result on the push stream.
            return web.Response(status=200, body=b"")
        import json as _json

        return web.json_response(
            {"Result": _encrypt(_json.dumps({"Result": {"Response": "Sucess"}}))}
        )

    async def _tuxweb_command(self, request: web.Request) -> web.Response:
        """serve.rs handle_security, answer for answer.

        The token first, then the code, then the command: sent and confirmed
        (200, the state byte flipped) or sent and not (504). A confirmed
        command moves the state model the status answer reads from, and puts
        a frame on the stream when `auto_push` says a real panel would.
        """
        if not self._token_ok(request):
            return self._unauthorized()
        self.last_command_body = await request.text()
        form = await request.post()
        command = request.match_info["command"]
        self.commands.append(command)
        try:
            ucode = int(str(form.get("ucode", "0")))
        except ValueError:
            ucode = 0
        if ucode == 0:
            return web.Response(
                status=400, body=TUXWEB_CODE_REQUIRED, content_type="application/json"
            )
        if not self.confirm_commands:
            return web.Response(
                status=504, body=TUXWEB_NOT_CONFIRMED, content_type="application/json"
            )
        if command == "DisarmWithCode":
            self.armed = False
            self.status = "Ready To Arm"
            self.colour = "Green"
            if self.auto_push:
                await self.push_status_text("Ready To Arm", armed=False)
            return web.Response(body=TUXWEB_DISARM_OK, content_type="application/json")
        self.armed = True
        self.status = "59  Secs Remaining"
        self.colour = "Red"
        if self.auto_push:
            await self.push(COUNTDOWN_FRAME)
        return web.Response(body=TUXWEB_ARM_OK, content_type="application/json")

    async def _stream(self, request: web.Request) -> Any:
        self.stream_requests += 1
        self.last_push_headers = dict(request.headers)
        if self.push_status != 200:
            return web.Response(status=self.push_status, text="")
        if self.tuxweb:
            if not self._token_ok(request):
                return self._unauthorized()
        elif not self._authenticated(request):
            return web.Response(status=302, text="")

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": (f'multipart/x-mixed-replace; boundary="{BOUNDARY}"')
            },
        )
        await resp.prepare(request)
        await resp.write(b"--" + BOUNDARY.encode() + b"\r\n['setCid', 7]")
        if self.stream_ends_at_once:
            return resp
        self._writers.append(resp)
        self._drop.clear()
        self.stream_open.set()
        try:
            await self._drop.wait()
        finally:
            self.stream_open.clear()
            self._writers.remove(resp)
        return resp
