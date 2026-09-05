"""A stand-in Tuxedo Touch: the real wire format, none of the hardware.

Everything the integration talks to is here - the login handshake, the
key/IV page, the encrypted API and the push stream - implemented from
docs/tuxedo_touch_api_notes.md, so a test drives the actual request
building, cookie handling, encryption and frame parsing rather than a mock
of them.

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
    text: str, armed: bool, colour: str = "1", partition: int = 1
) -> bytes:
    """One partition-status part, in the panel's own byte layout."""
    flag = 0xFF if armed else 0xFE
    payload = (
        f"0:21:{partition}:{flag:02x}:".encode("latin-1")
        + bytes([flag])
        + f"{colour}{text}:2".encode("latin-1")
    )
    return (
        b"['ud','SimpleDbgServer2ClientIntf','statusMessageText',[\"" + payload + b'"]]'
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
    ) -> None:
        self.username = username
        self.password = password
        self.status = status
        self.colour = colour
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

    # ------------------------------------------------------------- server
    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/authenticated/index.html", self._login_page)
        app.router.add_post("/authenticated/index.html", self._login)
        app.router.add_get("/tuxedoapi.html", self._keys)
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

    def expire_session(self) -> None:
        """Forget the cookie, so every request with it is refused."""
        self._cookie_value = "expired"

    # ------------------------------------------------------------ handlers
    async def _login_page(self, request: web.Request) -> web.Response:
        resp = web.Response(text="<html>login</html>")
        resp.headers["Random"] = CHALLENGE
        resp.headers["RandomID"] = "42"
        resp.set_cookie("_zFL", "correlate")
        return resp

    async def _login(self, request: web.Request) -> web.Response:
        # Counted BEFORE the comparison: what a panel counts is the attempt,
        # not the outcome.
        self.login_attempts += 1
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
        if not self._authenticated(request):
            return web.Response(status=302, text="")
        if self.keys_failures_left:
            self.keys_failures_left -= 1
            return web.Response(status=self.keys_status, text="<html>busy</html>")
        return web.Response(text=f'<input id="readit" value="{BLOB}">')

    async def _status(self, request: web.Request) -> web.Response:
        if not self._authenticated(request):
            return web.Response(status=401, text="")
        self.polls += 1
        body = {"Status": self.status, "Color": self.colour}
        import json as _json

        return web.json_response({"Result": _encrypt(_json.dumps(body))})

    async def _command(self, request: web.Request) -> web.Response:
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

    async def _stream(self, request: web.Request) -> Any:
        self.stream_requests += 1
        if self.push_status != 200:
            return web.Response(status=self.push_status, text="")
        if not self._authenticated(request):
            return web.Response(status=302, text="")

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": (f'multipart/x-mixed-replace; boundary="{BOUNDARY}"')
            },
        )
        await resp.prepare(request)
        await resp.write(b"--" + BOUNDARY.encode() + b"\r\n['setCid', 7]")
        self._writers.append(resp)
        self._drop.clear()
        self.stream_open.set()
        try:
            await self._drop.wait()
        finally:
            self.stream_open.clear()
            self._writers.remove(resp)
        return resp
