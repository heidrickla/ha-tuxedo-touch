# Honeywell Tuxedo Touch for Home Assistant

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/heidrickla/ha-tuxedo-touch.svg)](https://github.com/heidrickla/ha-tuxedo-touch/releases)
[![License](https://img.shields.io/github/license/heidrickla/ha-tuxedo-touch.svg)](LICENSE)
[![Validate](https://github.com/heidrickla/ha-tuxedo-touch/actions/workflows/validate.yml/badge.svg)](https://github.com/heidrickla/ha-tuxedo-touch/actions/workflows/validate.yml)
[![Tests](https://github.com/heidrickla/ha-tuxedo-touch/actions/workflows/tests.yml/badge.svg)](https://github.com/heidrickla/ha-tuxedo-touch/actions/workflows/tests.yml)

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=heidrickla&repository=ha-tuxedo-touch&category=integration)

Local (no cloud) Home Assistant custom integration for the Honeywell Tuxedo Touch WIFI
security/home automation controller (TUXWIFIS/TUXWIFIW), talking to it entirely over your
local network - no Total Connect Comfort cloud account involved. Ported from the
[heidrickla/Hubitat](https://github.com/heidrickla/Hubitat) driver of the same name.

It gives you one `alarm_control_panel` entity per partition, with Arm Home (Stay), Arm
Away, Arm Night and Disarm, using the reverse-engineered login and encryption flow
documented in [docs/tuxedo_touch_api_notes.md](docs/tuxedo_touch_api_notes.md).

## Supported devices

| Device | Notes |
|---|---|
| Honeywell Tuxedo Touch WIFI (TUXWIFIS, TUXWIFIW) | Verified against firmware `TUXW_V5.3.21.0_VA`. Other V5.x releases are expected to behave the same. |
| Tuxedo Touch on ~V4.x firmware | Untested. Older firmware is reported to allow unauthenticated access to the key page; this client always logs in first, which should be harmless. |

The panel behind the Tuxedo (a VISTA-series control) is not addressed directly; the
Tuxedo's own web API is the only thing spoken to.

## Supported functions

One `alarm_control_panel` entity per configured partition, named **Partition N** under
the device **Honeywell Tuxedo Touch**. A fresh install gets the entity id
`alarm_control_panel.honeywell_tuxedo_touch_partition_1`; an install from before the
name carried the partition keeps whatever entity id it already had.

| Panel status (`tuxedo_status` attribute) | Entity state |
|---|---|
| Ready To Arm, Ready Fault, Not Ready, Not Ready Fault | `disarmed` |
| Armed Stay, Armed Stay Fault | `armed_home` |
| Armed Away, Armed Away Fault | `armed_away` |
| Armed Night, Armed Night Fault, Armed Instant, Armed Instant Fault | `armed_night` |
| `NN  Secs Remaining` (exit delay) | `arming` |
| Entry Delay Active | `pending` |
| Not Ready Alarm, Armed Stay Alarm, Armed Away Alarm, Armed Night Alarm, Armed Instant Alarm | `triggered` |
| Anything else | `unknown` |

Commands are the alarm panel domain's own actions: `alarm_control_panel.alarm_arm_home`
(Stay), `alarm_arm_away`, `alarm_arm_night` and `alarm_disarm`. Each takes an optional
`code`; without one the keypad code stored at setup is used. The raw strings the panel
returned are exposed as the `tuxedo_status` and `tuxedo_color` attributes. There are no
actions, triggers or conditions of this integration's own.

## Use cases

- Arm away when the last person leaves and disarm when the first arrives, with the
  keypad code stored so automations need not carry it.
- Arm home at bedtime from a dashboard tile or a voice assistant, and be told if the
  panel refused because a zone was faulted.
- Notify a phone when the panel goes into alarm, from a Tuxedo that has no keypad bus
  interface (Envisalink or similar) attached.
- Keep the Tuxedo's lighting, thermostat and lock features out of Home Assistant while
  still owning the alarm from it - only security is implemented here.

## Requirements

- Home Assistant 2026.3 or newer. The integration's icon and logo ship in the repository
  and are served by Home Assistant from that release on.
- A Tuxedo Touch WIFI unit reachable on your LAN. A **static IP or DHCP reservation** is
  still worth setting: the integration reaches the panel at the address you give it, so a
  changed lease means no polling until you correct it under Settings -> Devices & Services
  -> Configure. It is not an *identity* problem, though - see "How the panel is
  identified" below.
- Its **web login username and password** (Settings on the touchscreen -> Login settings).
  This is different from the 4-digit keypad user code used to arm/disarm.
- The 4-digit keypad user code, entered either at setup (used as the default arm/disarm
  code) or each time from the Home Assistant UI/automations.

## Installation

HACS: search for "Honeywell Tuxedo Touch", or add `heidrickla/ha-tuxedo-touch` as a
custom repository of type Integration if it is not yet in the default list. Install and
restart Home Assistant.

Manual: copy `custom_components/tuxedo_touch` into your Home Assistant
`config/custom_components/` directory and restart.

Then Settings -> Devices & Services -> Add Integration -> "Honeywell Tuxedo Touch".

### Installation parameters

| Field | Meaning |
|---|---|
| IP address | IP address or hostname of the Tuxedo Touch unit on your LAN. |
| Port | The web server port on the unit. 443 with HTTPS enabled (the default state), 80 without. |
| Use HTTPS | On by default. Required whenever "Secured Web Server Access" is enabled on the unit, which it is unless you turned it off. |
| Web login username | The unit's web login username (touchscreen: Settings -> Login settings), not the keypad code. |
| Web login password | The unit's web login password. Masked; never shown again once stored. |
| Keypad user code | Optional. The 4-digit user code used as the default for arm and disarm so automations and dashboards need not supply one. Masked; never shown again once stored. Leave it empty to be asked for a code on every arm and disarm. |
| Partition number | The panel partition this entry controls. Default 1. Add the integration once per partition for a multi-partition panel. |

Setup performs a real login against the panel before the entry is created, so a wrong
password, an unreachable address or a panel that answered oddly is caught on the form
with a message that says which.

**On HTTPS**: leave it enabled unless you have specifically disabled "Secured Web Server
Access" in the unit's settings. The unit's command endpoints redirect to HTTPS regardless
of the scheme you request whenever that setting is on, so mixing an HTTP login with
HTTPS-only commands would silently break arming and disarming. The integration detects
the redirect and raises a repair notification that switches the entry over for you. See
[docs/tuxedo_touch_api_notes.md](docs/tuxedo_touch_api_notes.md) for the full writeup.

### Reconfiguring

Settings -> Devices & Services -> Honeywell Tuxedo Touch -> Reconfigure changes the
address, port, scheme, credentials, keypad code or partition. The password and keypad
code fields come up empty; leaving them empty keeps the stored values. The address must
still answer as the same panel where the panel's MAC is known (see below); an address
that answers as a different panel is refused. The entry's title follows the new address
unless you renamed the entry. There are no options beyond these.

When the panel starts refusing the stored credentials, Home Assistant stops polling and
asks for them again rather than re-running the login handshake against doomed
credentials every thirty seconds.

### Removing it

Settings -> Devices & Services -> Honeywell Tuxedo Touch -> the entry's menu -> Delete.
That removes the entry, its device, its entity and any repair notification it raised.
Nothing is written to the panel at any point, so there is nothing to undo on the unit;
the web login account you used stays as it was.

## How it updates

Every 30 seconds the integration reads the panel's security status. A successful arm or
disarm updates the entity immediately with the state just commanded, and a poll that was
already in flight when the command went out is discarded rather than allowed to flip the
entity back. When the panel is unreachable the entity becomes unavailable; the log has
one line when that happens and one when it recovers.

The panel's status endpoint intermittently answers `Not available` on a unit that is
otherwise fine (on at least one unit, persistently). That answer is treated as no new
information: the last known state is kept rather than overwritten. On a restart with no
prior state to keep, the entity reads `unknown` until the panel says something else.

Requests go out on Home Assistant's own HTTP session pool rather than a connection pool
of this integration's own. That pool keeps an idle connection for fifteen seconds, so a
thirty-second poll opens a fresh connection each time and pays for the panel's slow
legacy TLS handshake once per poll. The unit serves one web session at a time, and
nothing of ours is holding one open between polls.

## Repairs

Two conditions cannot be cleared by retrying, so they arrive as repair notifications
under Settings -> System -> Repairs rather than as log lines.

| Notification | What it does |
|---|---|
| **Tuxedo Touch needs HTTPS** | The panel redirected the API to HTTPS because "Secured Web Server Access" is on. Opening the notification and submitting turns on Use HTTPS for that entry, moves port 80 to 443 (any other port is left as you set it) and reloads the integration. |
| **Two Tuxedo Touch entries reach one panel** | Two entries reach the same panel and partition by different addresses, so only one of them can hold the panel's identity. The notification names both entries; remove whichever you do not want and the other adopts the panel's MAC on its next start. |

Both disappear on their own when the condition goes - including when you fix the panel's
HTTPS setting from the touchscreen instead - and when the entry is deleted.

## Examples

Arm away when everyone has left, disarm when the first person is home:

```yaml
automation:
  - alias: Arm the house when it empties
    triggers:
      - trigger: state
        entity_id: zone.home
        to: "0"
        for: "00:10:00"
    actions:
      - action: alarm_control_panel.alarm_arm_away
        target:
          entity_id: alarm_control_panel.honeywell_tuxedo_touch_partition_1
  - alias: Disarm when someone comes home
    triggers:
      - trigger: numeric_state
        entity_id: zone.home
        above: 0
    conditions:
      - condition: state
        entity_id: alarm_control_panel.honeywell_tuxedo_touch_partition_1
        state: armed_away
    actions:
      - action: alarm_control_panel.alarm_disarm
        target:
          entity_id: alarm_control_panel.honeywell_tuxedo_touch_partition_1
```

Notify when the alarm goes off, with the panel's own words:

```yaml
automation:
  - alias: Alarm triggered
    triggers:
      - trigger: state
        entity_id: alarm_control_panel.honeywell_tuxedo_touch_partition_1
        to: triggered
    actions:
      - action: notify.mobile_app_phone
        data:
          message: >
            The alarm is sounding. Panel says:
            {{ state_attr('alarm_control_panel.honeywell_tuxedo_touch_partition_1', 'tuxedo_status') }}
```

Arm home from a dashboard without storing the code, passing it in the call instead:

```yaml
script:
  arm_stay_with_code:
    fields:
      code:
        selector:
          text:
            type: password
    sequence:
      - action: alarm_control_panel.alarm_arm_home
        target:
          entity_id: alarm_control_panel.honeywell_tuxedo_touch_partition_1
        data:
          code: "{{ code }}"
```

## Known limitations

- Only security arm/disarm/status is implemented. The panel's API also exposes lighting,
  thermostat, door lock, scene, and garage door control - untested and unimplemented here,
  though they should follow the same request-signing pattern.
- **The status feed can go quiet.** While the panel answers `Not available` the entity
  reflects the last command *you* sent and cannot see changes made at the physical keypad
  or by another integration. If you have a working ECP-bus alarm integration (Envisalink,
  esphome-vistaECP, etc.) on the same panel, prefer that one for status.
- Status is polled without a partition parameter (the firmware's `GetSecurityStatus`
  doesn't take one), so on multi-partition panels the reported status is whatever the
  Tuxedo module itself reports; arm/disarm do target the configured partition.
- **The panel serves one web session at a time.** A browser tab left open on the unit's
  web UI can make setup or polling fail to connect until it is closed.
- **How the panel is identified.** It reports no serial and no MAC of its own
  (`Registration/AddDeviceMAC` enrolls a *client's* MAC, not the unit's), so the
  integration resolves the MAC from your network with an ARP lookup and keys the config
  entry on it. That only answers when Home Assistant shares a layer 2 segment with the
  panel; a routed or VLAN-separated install falls back to identifying it by address,
  where changing the panel's IP does read as a different panel. Existing entries adopt
  the MAC on their next start, and an address-identified entry that is reconfigured from
  a segment where the lookup answers adopts it then.
- Armed Instant is mapped to `armed_night`, the closest Home Assistant state to a Stay
  variant with no entry delay.
- The panel is not discovered automatically; its address is entered by hand.
- Verified against firmware `TUXW_V5.3.21.0_VA`. Older firmware may behave differently
  (see the docs) - not tested here.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Setup says "Failed to connect" | Home Assistant cannot reach the address and port, or the panel's single web session is held by a browser tab. Close any tab open on the unit and retry; check the port matches the HTTPS setting (443 on, 80 off). |
| Setup says "Invalid username or password" and they are right | Web access for that user was disabled, which a panel reset does. Re-enable it on the touchscreen under Setup -> Account, or use the Login settings page to set the credentials again. |
| Setup says the panel answered but the response could not be used | The login page came back without the challenge headers, or the key page was short: firmware this client does not know. The log line names which. Open an issue with the firmware version from the touchscreen's About page. |
| The entity is unavailable and the API call was redirected to HTTPS | "Secured Web Server Access" is on and the entry uses HTTP. A repair notification offers to switch the entry over; see [Repairs](#repairs). Reconfiguring by hand with Use HTTPS on and port 443 does the same thing. |
| Home Assistant asks to re-authenticate | The panel rejected the stored web login. Enter the current username and password; the address and code are kept. |
| The entity reads `unknown` after a restart | The panel is answering `Not available` and there is no earlier state to keep. It corrects itself on the first real status, or on your first arm or disarm. |
| The entity does not follow the keypad | The status feed is stuck on `Not available` (see Known limitations). Arm or disarm from Home Assistant to resync, or use an ECP-bus integration for status. |
| Two entries for one panel | A repair notification names both entries. Remove one; the other adopts the panel's identity on its next start. See [Repairs](#repairs). |
| Arm or disarm fails with "Tuxedo Touch command failed" | The panel refused: usually a wrong keypad code or a faulted zone when arming away. The message carries the panel's own reason. |

```yaml
logger:
  logs:
    custom_components.tuxedo_touch: debug
```

Download diagnostics from the entry's menu for a report with the host, MAC, credentials
and code redacted and the panel's raw status strings included.

## Development

```
python tests/test_api.py          # the client's pure logic, no Home Assistant needed
python -m pytest tests -q         # the whole suite; tests/ha needs the harness, Linux
python -m pytest tests -q --cov=custom_components/tuxedo_touch --cov-fail-under=95
python -m mypy custom_components/tuxedo_touch
python tools/validate_local.py    # the offline stand-in for hassfest and HACS
```

`tests/ha` needs `pytest-homeassistant-custom-component` and skips where it is absent, so
a run on Windows covers the client only. The GitHub Tests workflow runs the whole suite,
gates coverage of the integration at 95%, and runs mypy in strict mode and the validator
on every push. A local mypy run without Home Assistant installed reports its classes as
`Any`; that is the missing package, not the code.

Changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## Quality scale

Built to Home Assistant's Integration Quality Scale, tracked rule by rule in
[`quality_scale.yaml`](custom_components/tuxedo_touch/quality_scale.yaml) with a reason
on every exemption and on every rule still marked `todo`. `tools/validate_local.py`
checks the file against the pinned rule list, so a rule that is simply missing fails
rather than reading as complete. The scale is a core-integration concept; a custom
integration builds to the rules and is not scored.
