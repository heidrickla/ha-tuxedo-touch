# Honeywell Tuxedo Touch (local API)

A Home Assistant custom integration for the Honeywell Tuxedo Touch WIFI security/home
automation controller (TUXWIFIS/TUXWIFIW), talking to it entirely over your local network -
no Total Connect Comfort cloud account involved.

Provides one `alarm_control_panel` entity supporting Arm Home (Stay), Arm Away, Arm Night,
and Disarm.

## Requirements

- A Tuxedo Touch WIFI unit reachable on your LAN.
- Its **web login username and password** (Settings on the touchscreen -> Login settings).
  This is different from the 4-digit keypad user code used to arm/disarm.
- The 4-digit keypad user code, entered either at setup (used as the default arm/disarm
  code) or each time from the Home Assistant UI/automations.

## Installation

1. Copy `custom_components/tuxedo_touch` into your Home Assistant `custom_components`
   directory (or install via HACS as a custom repository pointing at
   `heidrickla/HomeAssistant`).
2. Restart Home Assistant.
3. Settings -> Devices & Services -> Add Integration -> "Honeywell Tuxedo Touch".
4. Enter the panel's IP, port, whether to use HTTPS, the web login username/password, and
   optionally the keypad code and partition number.

**On HTTPS**: leave it enabled unless you've specifically disabled "Secured Web Server
Access" in the unit's settings. The unit's actual command endpoints redirect to HTTPS
regardless of the scheme you request whenever that setting is on, so mixing HTTP login with
HTTPS-only commands will silently break arming/disarming. See
[../../docs/tuxedo_touch_api_notes.md](../../docs/tuxedo_touch_api_notes.md) for the full
writeup on why, plus every other quirk discovered while building this.

## Known limitations

- Only security arm/disarm/status is implemented. The panel's API also exposes lighting,
  thermostat, door lock, scene, and garage door control - untested and unimplemented here,
  though they should follow the same request-signing pattern.
- The panel intermittently reports `"Not available"` as its status even on a
  successfully-decrypted response. The integration automatically re-authenticates when this
  happens; if it persists, check the panel's own touchscreen - it usually means the Tuxedo
  module has lost sync with the Vista panel, not a problem with this integration.
- Verified against firmware `TUXW_V5.3.21.0_VA`. Older firmware may behave differently
  (see the docs) - not tested here.
