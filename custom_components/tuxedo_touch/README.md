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
   directory (or install via HACS - search for "Honeywell Tuxedo Touch", or add
   `heidrickla/ha-tuxedo-touch` as a custom repository if it's not yet in the default list).
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
  successfully-decrypted response - on at least one unit this was observed to be
  persistent, not intermittent, while arm/disarm commands kept working correctly. The
  integration works around this by treating `"Not available"` as "no new information"
  (ignoring it rather than overwriting known-good status) and by optimistically updating
  the entity's state immediately after a successful arm/disarm rather than waiting on the
  next poll. This means the entity reflects the last command *you* sent, but can't detect
  state changes from the physical keypad or another integration while the panel's status
  feed is down. If you have a working ECP-bus alarm integration (Envisalink,
  esphome-vistaECP, etc.) on the same panel, prefer that one for status.
- Verified against firmware `TUXW_V5.3.21.0_VA`. Older firmware may behave differently
  (see the docs) - not tested here.
