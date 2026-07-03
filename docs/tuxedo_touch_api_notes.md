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
  against real hardware with CPython 3.13 / OpenSSL 3.x. Since this differs from the
  connection's default SSL handling, the integration opens its **own dedicated
  `aiohttp.ClientSession`** per config entry rather than reusing Home Assistant's shared
  session, so this custom `SSLContext` can be attached per-request.

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

  **Integration workaround** (see `TuxedoTouchCoordinator._async_update_data` in
  `__init__.py` and `TuxedoAlarmPanel._set_optimistic_status` in
  `alarm_control_panel.py`): the coordinator now treats a polled `"Not available"` as "no
  new information" and keeps whatever status it last knew, instead of overwriting good data
  with the placeholder every 30-second poll. Separately, each arm/disarm call immediately
  pushes the *requested* status into the coordinator via `async_set_updated_data()` right
  after the command succeeds, rather than waiting on (and trusting) the next poll. If the
  panel's status feed is genuinely dead, the entity will now reflect the last command you
  sent rather than sitting on "Not available"/`Unknown` forever - it just can't detect
  arming/disarming triggered from the physical keypad or another integration while the feed
  is down. If a real status ever does come back, it overrides the optimistic value normally.
  If you have a working ECP-bus alarm integration (Envisalink, esphome-vistaECP, etc.) on
  the same panel, prefer that one for status - this integration's status reporting is only
  as good as the Tuxedo module's own connection to the panel.
- A GET to any endpoint (including the raw API endpoints, unauthenticated) redirects to
  `https://<ip>:443/tuxedoapi.html` regardless of the port/scheme requested when HTTPS
  access is enabled on the unit.

## Manual testing recipe (outside Home Assistant)

```python
import asyncio, aiohttp
from custom_components.tuxedo_touch.api import TuxedoTouchClient

async def main():
    async with aiohttp.ClientSession() as session:
        client = TuxedoTouchClient(session, "<ip>", 443, True, "<username>", "<password>")
        await client.login()
        print(await client.get_status())

asyncio.run(main())
```

For raw protocol-level debugging without any Python dependencies, `openssl s_client` can be
coaxed into the legacy handshake with a temp config enabling `UnsafeLegacyRenegotiation`
plus `-cipher 'DEFAULT@SECLEVEL=0'` - see the Hubitat repo's notes for the exact recipe used
during initial reverse engineering.

## Other endpoints in the official API (untested here)

Honeywell's own API reference (not reproduced here) documents a much larger surface than
security arm/disarm/status - lighting, thermostats, door locks, scenes, garage doors, and
water valves, all addressed by a Z-Wave `nodeID` rather than partition ID. These should
follow the exact same request-signing/encryption/session pattern documented above - only
the endpoint path and plaintext parameter shape change. Not implemented in this integration
(security-only) and not verified against real hardware.

## References

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
