# Honeywell Tuxedo Touch for Home Assistant

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/heidrickla/ha-tuxedo-touch.svg)](https://github.com/heidrickla/ha-tuxedo-touch/releases)
[![License](https://img.shields.io/github/license/heidrickla/ha-tuxedo-touch.svg)](LICENSE)
[![Validate](https://github.com/heidrickla/ha-tuxedo-touch/actions/workflows/validate.yml/badge.svg)](https://github.com/heidrickla/ha-tuxedo-touch/actions/workflows/validate.yml)

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=heidrickla&repository=ha-tuxedo-touch&category=integration)

Local (no cloud) Home Assistant custom integration for the Honeywell Tuxedo Touch WIFI
security/home automation controller (TUXWIFIS/TUXWIFIW), talking to it entirely over your
local network - no Total Connect Comfort cloud account involved. Ported from the
[heidrickla/Hubitat](https://github.com/heidrickla/Hubitat) driver of the same name.

Provides one `alarm_control_panel` entity supporting Arm Home (Stay), Arm Away, Arm Night,
and Disarm, using the same reverse-engineered login/encryption flow documented in
[docs/tuxedo_touch_api_notes.md](docs/tuxedo_touch_api_notes.md).

## Requirements

- Home Assistant 2025.3 or newer. (On 2026.3+ the integration's icon/logo are
  served locally via the Brands Proxy API from the bundled `brand/` folder; on
  older versions they simply don't display.)
- A Tuxedo Touch WIFI unit reachable on your LAN. Give it a **static IP or DHCP
  reservation** - the integration identifies the panel by its address, so a changing
  IP breaks duplicate detection and the stored connection.
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
HTTPS-only commands will silently break arming/disarming (the integration detects this and
tells you to switch). See [docs/tuxedo_touch_api_notes.md](docs/tuxedo_touch_api_notes.md)
for the full writeup on why, plus every other quirk discovered while building this.

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
- Status is polled without a partition parameter (the firmware's `GetSecurityStatus`
  doesn't take one), so on multi-partition panels the reported status is whatever the
  Tuxedo module itself reports; arm/disarm do target the configured partition.
- Verified against firmware `TUXW_V5.3.21.0_VA`. Older firmware may behave differently
  (see the docs) - not tested here.
