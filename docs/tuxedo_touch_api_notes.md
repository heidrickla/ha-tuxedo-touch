# Honeywell Tuxedo Touch WIFI - Local API Notes

Reverse-engineering notes behind `custom_components/tuxedo_touch`. Verified against
firmware `TUXW_V5.3.21.0_VA` (visible on the unit under Settings -> System Information).
Findings below are specific to that firmware line unless noted. A companion Hubitat
driver with an earlier version of this writeup lives in
[heidrickla/Hubitat](https://github.com/heidrickla/Hubitat/blob/main/Drivers/HoneywellTuxedoTouchAPI/HoneywellTuxedoTouchAPINotes.md);
this doc supersedes it with additional findings from building the Home Assistant
integration (notably the HTTP-vs-HTTPS split described below).

## Firmware matters more than the endpoint list suggests

There are (at least) two generations of behavior in the wild, and community threads
(Hubitat forum, Home Assistant forum) mix reports from both without saying which:

- **Older firmware (~V4.x)**: the `/tuxedoapi.html` page that hands out the AES key/IV is
  reachable **unauthenticated** on the LAN. This is the assumption baked into
  [homebridge-honeywell-tuxedo-touch](https://github.com/lockpicker/homebridge-honeywell-tuxedo-touch)
  and [homebridge-honeywell-vam](https://github.com/sparsematrix/homebridge-honeywell-vam) (an
  even older no-crypto GET-based variant).
- **Newer firmware (V5.3.21.0+, confirmed here)**: **every** local page, including
  `/tuxedoapi.html` and the raw `/system_http_api/API_REV01/...` endpoints, redirects to a
  login page unless you present a valid session cookie. This is true regardless of the
  "Authentication for Web Server Local Access" checkbox in the unit's settings - unchecking
  that setting and rebooting did **not** change this behavior in testing. That setting
  appears to control something else (likely remote/WAN access), not local LAN access.

The `Registration/AddDeviceMAC` + `Registration/Register` MAC-enrollment flow described in
Honeywell's own API reference doc is a separate, more heavyweight mechanism that was never
gotten working reliably by the Hubitat community. This integration uses the session-login
flow instead, which is what the device's own web UI uses and which was confirmed to work
end-to-end, including live arm/disarm against real hardware.

## Transport layer: HTTP vs HTTPS is not all-or-nothing

The unit has a **"Secured Web Server Access (HTTPS)"** toggle in its settings. With it
enabled (the common/likely-default state):

- The **login page and `/tuxedoapi.html`** tolerate plain HTTP requests directly (no
  redirect to HTTPS observed for these specific pages).
- The **actual command endpoints** (`/system_http_api/API_REV01/GetSecurityStatus`,
  `.../AdvancedSecurity/ArmWithCode`, etc.) **302-redirect to HTTPS** even when requested
  over plain HTTP. This means a client that logs in over HTTP and then tries to send
  commands over HTTP will authenticate fine and then mysteriously fail every actual API
  call with a 302. This integration defaults to HTTPS end-to-end to avoid the split
  entirely; if you disable "Secured Web Server Access" on the unit, plain HTTP for
  everything may work too, but that combination hasn't been tested.

HTTPS uses a **self-signed demo certificate from ~2009** (`SharkSSL`, `CN=server demo 1024
bits`, 1024-bit RSA, MD5 signature, expired since 2019) and requires **legacy/unsafe TLS
renegotiation**. Modern TLS stacks reject this by default:

- curl on Windows (Schannel backend) fails outright with `SEC_E_INVALID_TOKEN`.
- OpenSSL 3.x's CLI fails with `unsafe legacy renegotiation disabled` unless explicitly
  overridden via config.
- **Python's `ssl` module handles it natively** once you set
  `ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT` and `ctx.set_ciphers("DEFAULT@SECLEVEL=0")`
  on a `SSLContext` with `verify_mode = ssl.CERT_NONE` - no external config file needed.
  This is exactly what `api.py`'s `_legacy_ssl_context()` does, and it's been confirmed
  against real hardware with CPython 3.13 / OpenSSL 3.x. The custom `SSLContext` is
  attached per-request (`ssl=` on each call), which aiohttp supports on any session. The
  integration still opens its **own dedicated `aiohttp.ClientSession`** per config entry
  rather than reusing Home Assistant's shared session, but for different reasons: cookie
  isolation (the panel sets a session cookie with a random name per login) and so the
  connector's keep-alive can be tuned to outlive the poll interval (aiohttp's default 15s
  keep-alive would otherwise force a fresh TCP + legacy-TLS handshake on every 30s poll).

## Login flow (session-cookie auth)

This is what `TuxedoTouchClient.login()` in `api.py` implements. All values below are
illustrative - actual challenge/cookie/key values are per-session/per-boot and differ
every time.

1. **GET** `/authenticated/index.html?url=tuxedoapi.html` (no auth). The response includes:
   - Response header `Random: <32-hex-char challenge>` - also embedded as a JS variable
     `login` in the page itself (`var login="...";`). Changes every request.
   - Response header `RandomID: <small integer>` - embedded as JS `myID`.
   - `Set-Cookie: _zFL=...` - a short-lived correlation cookie, required on the next request.
2. Compute (mirrors `validateCredentails()` in `/script/validatelogin.js` on the unit):
   - `log  = HMAC-SHA512(message = username.toLowerCase(), key = challenge)`
   - `log1 = HMAC-SHA512(message = username.toLowerCase() + password, key = challenge)`
   - Both encoded as lowercase hex.
   - **The challenge is used as the literal UTF-8 text of the hex string, not as
     hex-decoded bytes.** This same "hex-string-as-literal-text" quirk shows up again in
     the API authtoken signing below.
   - The raw `j_username`/`j_password` `<input>` elements in the real login page HTML live
     *outside* the `<form>` element that actually gets submitted. Only `log`, `log1`, and
     `identity` (=`RandomID`) are POSTed - the plaintext password never goes over the wire,
     even before TLS is considered.
3. **POST** to the same URL, body `log=<log>&log1=<log1>&identity=<RandomID>` as
   `application/x-www-form-urlencoded`, with the `_zFL` cookie from step 1 attached.
4. On success (HTTP 200/302), the response sets **two** `Set-Cookie` headers: a new session
   cookie with an unpredictable name (e.g. `z9ZAqJtI_1387622758=<hex>` - don't hardcode the
   name, parse it), and an **expiring** `_zFL` cookie clearing the correlation cookie. Skip
   any cookie whose name starts with `_zFL` when picking the session cookie.
5. Attach that session cookie to every subsequent request - both the `/tuxedoapi.html` key
   fetch and the actual API calls. In testing, the session stayed valid across several
   minutes and multiple commands without needing to re-login.

## Key/IV retrieval

**GET** `/tuxedoapi.html` **with the session cookie attached**. The page HTML contains
`<input id="readit" ... value="<hex blob>" />`. On this firmware/unit the blob was
consistently **96 hex characters**: first 64 (32 bytes) = AES-256 key, remaining 32 (16
bytes) = AES-CBC IV.

## API call signing/encryption

For every `/system_http_api/API_REV01/<endpoint>` call:

- **Body**, `application/x-www-form-urlencoded`:
  `param=<url-encoded base64 AES ciphertext>&len=<ciphertext length before url-encoding>&tstamp=<ms epoch>`.
  Nothing is appended to the URL/query string.
- **Plaintext parameters** are themselves query-string-shaped, e.g.
  `arming=STAY&pID=1&ucode=<code>&operation=set` for arming, or `operation=get` for
  `GetSecurityStatus`.
- **Encryption**: AES-256-CBC, PKCS7 padding, using the raw hex-decoded key/IV bytes from
  the `#readit` blob. Result is base64-encoded.
- **`authtoken` header**: `HMAC-SHA1(message = header, key = keyHex)` where
  `header = "MACID:Browser,Path:API_REV01<endpoint-path>"`. Note this **omits** the
  `/system_http_api` prefix even though that prefix **is** part of the actual request URL,
  and uses the **literal string `"Browser"`** as the device identifier. Just like the login
  HMAC, the key here is the **hex string used as literal UTF-8 text**, not hex-decoded
  bytes - signing with the decoded raw key bytes produces a silently-invalid authtoken with
  no clear error from the device.
- **`identity` header**: the IV hex string, as-is.
- **`Cookie` header**: the session cookie from login.
- **Response**: `{"Result": "<base64 ciphertext>"}`. Decrypt with the same key/IV to get
  JSON, e.g. `{"Status":"Ready To Arm","Color":"Green"}` for a status query, or
  `{"Status":"Sucess","Result":{"Response":"Command sent sucessfully"}}` for arm/disarm
  (that's the device's own spelling - don't "fix" it when matching response text).
  Confirmed live: disarm nests its message under `Result.Result` while arm uses
  `Result.Response` - don't rely on the inner key name. During the exit delay after an
  arm command, `GetSecurityStatus` reports a countdown like `"59  Secs Remaining"`
  (double space, `Color: "Red"`), then the final armed status once the delay ends. The
  login challenge's `Random` header was also observed at 31 hex chars (not always 32) -
  treat it as opaque text.

## The push stream

This is where the alarm state comes from as of 0.4.0, and it is the answer to the
`"Not available"` bug described under Known device quirks: it does not read the cache
that produces that placeholder, so a client on this stream cannot see it at all.

```
GET /SimpleDebugger.interface/G.     <- works
GET /SimpleDebugger.interfaceG.      <- 404
```

**The slash before `G.` is the whole trick.** The vendor's own client appends `G.` to a
base URL that already ends in one, which is why it is easy to get wrong and why this
endpoint was written off as absent for a long time. Authentication is the **session
cookie alone**: no `authtoken`, no `identity` header, no encrypted body, no query string.

The reply is `multipart/x-mixed-replace; boundary="EH912ZZ"`, one part per event, and
the request never ends by itself.

**Decode it latin-1, never utf-8.** The state flag is a raw `0xFE`/`0xFF` byte; utf-8
turns it into U+FFFD and the field carrying the display text can no longer be located at
all, so the frame decodes to nothing rather than decoding wrongly.

Three frame shapes:

```
['setCid', <connection id>]
['ud','SimpleDbgServer2ClientIntf','noOfClient',[<n>]]
['ud','SimpleDbgServer2ClientIntf','statusMessageText',["<payload>"]]
```

The payload is colon-delimited:

```
0:21:1:fe:\xfe1Ready To Arm:2
0:21:1:ff:\xff259  Secs Remaining:2
|  |  | |   |  ||
|  |  | |   |  |+- display text
|  |  | |   |  +-- colour: 1 green, 2 red, 3 yellow (the REST API's "Color")
|  |  | |   +----- the same flag again, as a RAW BYTE
|  |  | +--------- state flag as hex TEXT: fe ready/disarmed, ff arming/armed
|  |  +----------- panel status code; -1 means the ECP link is down
|  +-------------- command id
+----------------- 0 in everything observed
```

Command ids in field 1: **21** partition status (the useful one), **18** home partition,
**504** initial/registration data on connect, **-1** unsolicited status update. Only 21
and -1 carry the flag byte, so only those decode to a partition status here.

**Scope of the producer evidence, stated because the integration relies on it.**
The disassembly proving the stream is scoped to the panel's current partition is
of `sltSendChangedPartitionStatus` at 0x144880, and that function produces
**command 21 only**. Nothing yet establishes which producer emits the
unsolicited **-1** record, or whether it applies the same partition filter, so
applying those frames rests on the command-21 filter generalising to them. On a
single-partition panel the distinction cannot matter; on a multi-partition one
it is the same uncertainty as the caveat below, and settling it means finding
the -1 producer rather than assuming it. A -1 frame carries no status code, so
it can never clear the dead-link latch nor speak while the link is down.

### Field 2 is not the partition

It was read as the partition number up to and including 0.4.1, and that was wrong. It is
the value the panel's own `/eventhandler.html` calls **`panelStatusCode`**: an
authenticated GET of that page taken at the same instant as a frame answered
`curStatus = "21:a1Ready To Arm:1"` while the frame read `0:21:1:fe:\xfe1Ready To Arm:2`,
so the page's trailing `panelStatusCode` is the frame's *field 2*, and the frame's own
trailing `:2` is the colour rather than the code. Two other frames captured at that
instant carry **`P1` as literal text** - `0:504:1:P1  H:1:0:3:3` and `0:18:1 P1  H:2` -
which is the second reason to doubt field 2 was ever duplicating a partition.

**`-1` in this field means the Tuxedo has lost the ECP link to the VISTA panel.** From
the producer, `CReceiverThread::sltSendChangedPartitionStatus` at `0x144880` in
`/tuxedo`:

```
0x144a7c  bl     PanelIsTalking()
0x144a80  cmp    r0, #0
0x144a84  mvneq  r3, #0          ; link down -> r3 = -1
0x144a88  streq  r3, [sp, #8]    ; -1 stored into the message field
0x144a8c  bne    0x144ae8        ; otherwise the real status code
0x144aa0  bl     osal_MqSend(int, char*, int)   ; the frame is SENT EITHER WAY
```

The frame still arrives, still carries a display text and still carries a state flag, and
every bit of that is the last thing the Tuxedo drew before it went blind. Nothing about
the connection changes, so no liveness or freshness check on the stream can see this -
and `GetSecurityStatus` reads a cache those same ECP messages are what fill, so the poll
cannot see it either. The integration takes the alarm entity **unavailable** for as long
as field 2 reads -1, and a frame carrying a real status code is the only thing that
clears it.

### There is no partition field, and the stream does not need one

The head of the same producer:

```
0x144884  mov  r3, #0x15            ; 21, the command id
0x1448a0  bl   GetCurrentPartition()
0x1448a4  cmp  r0, r4               ; r4 = the partition that changed
0x1448a8  beq  0x1448b4             ; equal -> build and send
0x1448ac  add  sp, sp, #0x26c       ; NOT equal -> return, send NOTHING
```

**A frame is emitted only when the partition that changed is the panel's currently
selected partition.** Every frame that arrives is therefore about the current partition by
construction: the stream is implicitly scoped by the producer, which is why no partition
field exists. Guarding again on the receiver can only reject valid frames.

The caveat, worth writing down and not worth building for: the scoping is *the current
partition*, not *partition N*, so the stream **follows the panel**. Change the displayed
partition at the touchscreen or through the web UI (the firmware ships
`script/changePartitionScript.js` for exactly that) and the stream begins delivering a
different partition's status with no marker in the frame to say so. On a single-partition
system that is theoretical. On a multi-partition one it is a real mis-attribution and it
is invisible in the data; the fix would be to consult `GetCurrentPartition` over REST
rather than assume, and it is not attempted here. The configured partition still governs
the `GetSecurityStatus` poll and every arm/disarm command, which do take a partition id.

Read the colour digit carefully: it sits between the flag byte and the text, so
`\xff259  Secs Remaining` is *colour 2* and *59 seconds*, not 259 seconds.

The two sources spell the colour differently: the stream sends the digit above, while
`GetSecurityStatus` sends the panel's own capitalised word (`"Color":"Green"`). The
integration lower-cases the REST word as it reads it, so the `tuxedo_color` attribute
carries one vocabulary - `green`, `red`, `yellow` - no matter which source produced the
status.

### Which display texts have actually been seen on the stream

Two, and only two: **`Ready To Arm`** and the exit-delay countdown
(**`NN  Secs Remaining`**, double space). The capture behind this section is a single
arm/disarm cycle, and it ends with the panel disarmed, so no armed status was ever
watched arriving on the stream.

The armed spellings in the status table - `Armed Stay`, `Armed Away`, `Armed Night`,
`Armed Instant` and their `Fault` and `Alarm` variants - are what `GetSecurityStatus`
returns. The integration **assumes** the stream spells them identically, which is why
one status map serves both sources. That assumption is unconfirmed and stays unconfirmed
until someone holds the stream open while the panel sits armed.

Nothing rests on it being right. The stream's `0xFE`/`0xFF` flag says whether the
partition is armed without reference to the display text, and a text the status map does
not recognise settles nothing: the coordinator then stops suppressing the
`GetSecurityStatus` poll and lets it name the mode instead of guessing. If the streamed
spellings do turn out to differ, that fallback is the designed answer, the entity is
correct throughout on the poll's 30-second granularity, and the fix is to add the
streamed spellings to `STATUS_STATES` in `const.py`.

Behaviour worth relying on, all measured on the reference unit:

- **An idle panel is not a silent stream.** It repeats the partition status on its own
  timer roughly every 33 seconds - 81 frames over a five-minute hold, the last at
  t+296 s. So silence much longer than that means the socket is dead, which is what
  `PUSH_READ_TIMEOUT` is set from rather than guessed at.
- **Reconnecting is cheap.** Six connect-disconnect cycles on one session held
  `noOfClient` at 1 every time and left the session usable; slots are reclaimed on
  disconnect.
- **Streams coexist with everything else.** Two clients each held a stream while
  commands went out on a separate request, `noOfClient` peaking at 2 with neither
  starved. This is the one place the unit's usual one-connection-at-a-time behaviour
  (below) does not apply, which is what makes holding a stream open for the life of an
  entry safe.
- **It is not faster than a tight poll** - push saw an arm at t+1.70 s against t+1.92 s
  for a poll in a loop; the panel itself takes about 1.7 s. The case for it is that it
  cannot hit the `"Not available"` fault, it needs no polling loop, and it reports
  transitional states such as the exit-delay countdown that a 30-second poll misses.
- **It carries partition/alarm state and nothing else.** Commands 12 (all zone status),
  17 (event log), 22, 51, 134, 155 and 500 were issued while listening and produced
  nothing, on the stream or inline. Zone data is not reachable over HTTP on this
  firmware by any route tested.

Wire format, the live capture and the reference reader:
`iot-protocol-tools/TUXEDO-HA-ENRICHMENT.md` section "The push frame format, decoded
byte-exact" and `tuxedo_push.py` in the same repository. The endpoint list this belongs
to is `TUXEDO-FINDINGS.md` section "The complete local API surface", and the cache
mechanism the stream bypasses is `TUXEDO-FIRMWARE.md` section 6. Those documents are the
source; this section summarises rather than duplicates them.

## What the REST surface actually answers

Measured live against the unit, and it is much smaller than the vendor's own API
reference suggests. Where static documentation and this section disagree, believe this
section. Full enumeration in `iot-protocol-tools/TUXEDO-FINDINGS.md` section "The
complete local API surface", taken from `script/tuxapi.js` inside the panel's own
firmware image.

- **Only about six endpoints answer with data**: `GetSecurityStatus`, `GetSceneList`,
  `GetOccupancyMode`, `AdvancedMultimedia/GetCameraList`,
  `AdvancedAutomation/DoorBell/getDoorBell`, and the two `Administration/View*` calls,
  which refuse with `"This services are accessable local only"`.
- **`GetOccupancyMode` is misrouted.** It returns the security status verbatim -
  byte-identical to `GetSecurityStatus` - whatever parameters it is given. The handler is
  wired to the wrong function.
- **Most documented endpoints are not implemented.** They return the built-in test
  console's *input form* for that endpoint - its own documentation page, not a handler.
  Supplying the parameter changes nothing, in the encrypted body or the query string.
  A few (`GetThermostatClock`, `GetThermostatSchedule`, `GetVideoEvents`) answer HTTP 200
  with an HTML page containing `{"ErrorCode":"404"}`.
- **There is no version, model or firmware endpoint, and no status-refresh endpoint** -
  established by enumerating the vendor's own client, not by probing. This is why
  `sw_version` and `model_id` are left blank on the device rather than synthesised.
- **`GetSecurityStatus` is POST-only.** A GET returns 405. An **empty body** works: the
  vendor's client sends nothing, this integration sends `operation=get`, and both are
  accepted, so the parameter is ignored for that endpoint.
- **Commands return nothing inline.** `ArmWithCode` and `DisarmWithCode` answer HTTP 200
  with a **zero-byte body**; the result arrives on the push stream. Note that `aiohttp`
  returns `None` from `resp.json()` for an empty body rather than raising, so code that
  only catches a decode error never sees this case.
- **`tokenkey` is not required.** Commands are accepted with an empty token, and it was
  absent from every page checked.
- **`/Config/` is not reachable.** `panelinfo.txt` and `P<N>Info.txt` are 404 on every
  casing - no auth challenge, nothing served.
- **The key blob is served on both** `/tuxedoapi.html` and
  `/authenticated/index.html?url=tuxedoapi.html`, with the session cookie, on this
  firmware.
- **`Random` is 31 hex characters, reproducibly** - not 32, and not an opaque
  fixed-width field. **`RandomID` increments on every fetch of the login page**, so its
  meaning is not established; it is echoed back as `identity` in the login POST and
  nothing here depends on its shape.
- **The session cookie's name encodes the panel's boot time** (`z9ZAqJtI_<timestamp>`,
  observed February 2014 on a unit whose clock starts in the past). For
  `/handlerequest.html`, the `sessionid` that page expects is **the first 8 hex
  characters of the cookie value read as a SIGNED 32-bit integer** - computed unsigned it
  is a value the panel does not recognise. This integration does not use that endpoint;
  it is recorded because it is the trap anything reaching for the second API will hit.
- **The same web application is served on ports 80, 443 and 6280.** All three are gated
  by the same session cookie, so the plaintext ports are a confidentiality problem
  (the alarm user code travels in a query string on the second API), not an access one.

## Known device quirks

- **`"Status":"Not available"`**: an intermittent, documented bug (also called out in the
  `lockpicker` plugin's source comments) where a structurally-valid, successfully-decrypted
  response nonetheless reports this status instead of the real one. Reproduced repeatedly
  in the minutes after rebooting the unit, and also observed well after that in later
  testing sessions - it's not purely a post-reboot settling issue. Treat it as a signal to
  re-authenticate (re-login + re-fetch keys) rather than a fatal error; the integration's
  `_call()` does this automatically on a 401/302, but a persistent "Not available" after
  that likely means the Tuxedo module itself has lost sync with the Vista panel and needs
  attention outside of software (check the panel's own touchscreen for its actual status).

  **On at least one unit this is not intermittent - it's permanent.** `GetSecurityStatus`
  returned `"Not available"` on every single poll across an entire testing session,
  including immediately after successful arm and disarm commands. This was confirmed to be
  a status-reporting problem specific to the Tuxedo module, not a failure of the commands
  themselves: a separate ECP-bus-based alarm integration on the same physical panel (e.g.
  Envisalink) correctly tracked the panel flipping between armed/disarmed in real time, at
  the same moments `GetSecurityStatus` kept reporting "Not available". In other words, the
  Tuxedo Touch's command path and its status-reporting path can be independently broken -
  don't assume a stuck "Not available" means arm/disarm aren't working, and don't assume
  arm/disarm working means status will start reporting correctly.

  **This is a property of `GetSecurityStatus` only.** The firmware fills that cache from
  a message on the ECP bus and has no on-demand refresh, so the endpoint answers its
  compiled-in default whenever nothing has filled it; polling cannot wake it. The push
  stream described above does not read that cache, so from 0.4.0 the condition does not
  reach the entity on firmware that serves the stream. See
  `iot-protocol-tools/TUXEDO-FIRMWARE.md` section 6 for the mechanism in the binary.

  **Integration handling** (see `TuxedoTouchCoordinator._async_poll` in `coordinator.py`):
  a polled `"Not available"` is a *failed read* - `UpdateFailed` on every poll that
  returns it, the first one after a load included - so the last real status is kept
  underneath rather than overwritten. Up to 0.3.1 the placeholder was instead stored as
  data when there was nothing earlier to keep, which latched: every later
  `"Not available"` then preserved that stored placeholder and the entity read `unknown`
  until a command replaced it. The entity is unavailable only when the stream is down
  too; while the stream is connected, a poll answering the placeholder changes nothing.

  Commands are confirmed rather than assumed (`async_send_command`): arm and disarm
  return a zero-byte body, so the coordinator waits for the panel's own report on the
  stream, falls back to a poll, and only then shows the requested status marked
  `assumed`. A poll already in flight when the command goes out carries an answer that
  predates it and is discarded rather than allowed to flip the entity back
  (`tests/ha/test_init.py::test_a_poll_in_flight_when_a_command_lands_is_discarded`).
  On firmware with no push stream this remains the pre-0.4.0 situation: while the feed
  is answering `"Not available"` the entity is `unavailable`, Home Assistant skips
  unavailable entities in service calls, and no arm or disarm can be sent from Home
  Assistant until a real status comes back. The panel's own touchscreen is unaffected.
  If you have a working ECP-bus alarm integration (Envisalink, esphome-vistaECP, etc.) on
  the same panel, prefer that one for zone-level data, which the Tuxedo's web interface
  cannot supply at all.
- A GET to any endpoint (including the raw API endpoints, unauthenticated) redirects to
  `https://<ip>:443/tuxedoapi.html` regardless of the port/scheme requested when HTTPS
  access is enabled on the unit.

- **The unit serves ONE connection at a time.** This is the single most misleading property
  of the device, because contention presents as a **hang, not a refusal**: a second client
  gets a completed TCP handshake and then silence, indefinitely. With the integration
  polling every 30s, anything else touching the panel - a browser tab left open on its web
  UI, a `curl` from a shell, another HA instance - will intermittently starve the poll and
  make a perfectly healthy panel look dead.

  Observed signature while contended (from a machine that was *not* the one holding the
  slot):

  ```
  ICMP            replies normally, <1ms
  TCP 80 / 443    connection ACCEPTED
  TCP 8080/8443   cleanly refused        <- the TCP stack is alive and selective
  HTTP request    "Request completely sent off", then nothing, until timeout
  TLS ClientHello sent, no ServerHello, handshake times out
  ```

  Every layer looks healthy except the application, which is exactly what a crashed web
  server looks like too - so this is easy to misdiagnose as a dead unit. Repeated retries
  make it worse rather than better, and prolonged contention can leave the unit refusing
  new connections with RST until it is reset. **Before concluding the panel is broken,
  close every other client and test once.**

  The limit is counted in *connections*, not logins, which makes it an implementation
  constraint rather than a caveat: two clients of ours can be two connections. Home
  Assistant pools connections and keys that pool on the ssl argument as well as the
  address, and two `SSLContext` objects never compare equal - so the permissive context
  this unit's certificate needs is built once and shared (`api._legacy_ssl_context`,
  cached). With one key, a config flow's login check reuses the socket an entry's poller
  left idle in the pool rather than opening its own beside it. Anything added here that
  talks to the panel should take that same context.

  **The push stream is the exception, measured.** Two clients each holding a stream while
  commands went out on a third request were all served at once (`noOfClient` peaked at
  2, neither starved), and disconnecting reclaimed the slot every time. So holding a
  stream open for the life of an entry does not starve the poll, the config flow's check
  or the panel's own web UI - which is what makes the 0.4.0 design possible at all.

- **A panel reset disables web access per user; it does not delete the accounts.** After
  resetting the unit, existing users survive but each one's web-access flag comes back
  **off**, and the web UI shows: *"Web access has been deactivated. Go to your Tuxedo's
  login setup to create an user account or reactivate an account."* Re-enable it on the
  touchscreen under **Setup -> Account**, pressing `Enable` for each user that needs it.
  Note the login *page* still serves normally in this state, complete with valid
  `Random`/`RandomID` headers - only authentication fails - so this looks like a credential
  problem rather than a settings one.

## Manual testing recipe (outside Home Assistant)

```python
import asyncio, aiohttp
from custom_components.tuxedo_touch.api import TuxedoTouchClient


async def main():
    async with aiohttp.ClientSession() as session:
        client = TuxedoTouchClient(
            session, "<ip>", 443, True, "<username>", "<password>"
        )
        await client.login()
        print(await client.get_status())


asyncio.run(main())
```

For raw protocol-level debugging without any Python dependencies, `openssl s_client` can be
coaxed into the legacy handshake with a temp config enabling `UnsafeLegacyRenegotiation`
plus `-cipher 'DEFAULT@SECLEVEL=0'` - see the Hubitat repo's notes for the exact recipe used
during initial reverse engineering.

## Other endpoints in the official API

Honeywell's own API reference (not reproduced here) documents a much larger surface than
security arm/disarm/status - lighting, thermostats, door locks, scenes, garage doors and
water valves, all addressed by a Z-Wave `nodeID` rather than a partition ID. They would
follow the same request-signing/encryption/session pattern documented above; only the
endpoint path and the plaintext parameter shape change.

Earlier revisions of this document called them "untested here". They have since been
tested, and most of them **are not implemented on this firmware at all** - see "What the
REST surface actually answers" above. Nothing in that group is worth building against
without checking first that the endpoint answers with data rather than its own
documentation form.

## References

- `iot-protocol-tools` (private, not in this repository) - the reverse-engineering
  record this document cites rather than duplicates. `TUXEDO-HA-ENRICHMENT.md` section
  "The push frame format, decoded byte-exact" and its reference reader `tuxedo_push.py`
  for the stream; `TUXEDO-FINDINGS.md` section "The complete local API surface" for the
  endpoint enumeration taken from the panel's own client; `TUXEDO-FIRMWARE.md` section 6
  for the status cache in the binary, with every claim tagged CONFIRMED, LIKELY or
  UNKNOWN.
- [homebridge-honeywell-tuxedo-touch](https://github.com/lockpicker/homebridge-honeywell-tuxedo-touch) -
  working reference for the unauthenticated-`/tuxedoapi.html` (older firmware) flow; where
  the "HMAC key = literal hex text" quirk and the `MACID:Browser,Path:API_REV01<endpoint>`
  signed-header format were first confirmed.
- [homebridge-honeywell-vam](https://github.com/sparsematrix/homebridge-honeywell-vam) -
  even older, no-crypto plain-GET variant for VAM-era units.
- [Dilbert66/esphome-vistaECP](https://github.com/Dilbert66/esphome-vistaECP) - an
  entirely different, more robust approach: bypass the Tuxedo Touch's web stack altogether
  and talk to the Vista panel's keypad (ECP) bus directly via ESP8266/ESP32. Worth
  considering if this web API keeps being fragile across firmware updates.
- [heidrickla/Hubitat](https://github.com/heidrickla/Hubitat/tree/main/Drivers/HoneywellTuxedoTouchAPI) -
  the original Groovy/Hubitat driver this Home Assistant integration was ported from.
