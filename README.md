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

Since 0.4.0 the panel **pushes** its state: the integration holds the unit's own event
stream open, so an arm or a disarm at the keypad shows up in seconds, the exit-delay
countdown is visible while it runs, and the firmware's long-standing `Not available`
answer - which used to leave the entity with nothing to show - cannot reach the entity
at all. See [How it updates](#how-it-updates).

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
| `Not available` | `unavailable` - the panel's status cache was empty, and only the 30 s poll can ever see this; see [How it updates](#how-it-updates) |
| Anything else | `unknown` |

The panel spells the same strings on both of the sources described in
[How it updates](#how-it-updates), so the table applies to whichever reported the state.

Commands are the alarm panel domain's own actions: `alarm_control_panel.alarm_arm_home`
(Stay), `alarm_arm_away`, `alarm_arm_night` and `alarm_disarm`. Each takes an optional
`code`; without one the keypad code stored at setup is used. There are no actions,
triggers or conditions of this integration's own.

The entity carries four attributes:

| Attribute | What it is |
|---|---|
| `tuxedo_status` | the panel's own status string, as reported |
| `tuxedo_color` | the colour the panel showed it in: `green`, `red` or `yellow` |
| `tuxedo_source` | where it came from: `stream` (the panel pushed it), `poll` (the 30 s status read) or `assumed` (a command the panel accepted but neither source reported) |
| `arming_seconds_remaining` | seconds left of the exit delay while the state is `arming`, `null` otherwise |

## Use cases

- Arm away when the last person leaves and disarm when the first arrives, with the
  keypad code stored so automations need not carry it.
- Arm home at bedtime from a dashboard tile or a voice assistant, and be told if the
  panel refused because a zone was faulted.
- Notify a phone when the panel goes into alarm, from a Tuxedo that has no keypad bus
  interface (Envisalink or similar) attached.
- Keep the Tuxedo's lighting, thermostat and lock features out of Home Assistant while
  still owning the alarm from it - only security is implemented here.
- Count the exit delay down on a dashboard, or hold a "leaving the house" scene until
  `arming_seconds_remaining` reaches zero - the panel pushes it once a second.

## Requirements

- Home Assistant 2026.3 or newer. The integration's icon and logo ship in the repository
  and are served by Home Assistant from that release on. That is the only floor - the
  DHCP discovery described below needs nothing newer.
- A Tuxedo Touch WIFI unit reachable on your LAN. Its **DHCP lease** is how Home
  Assistant discovers the panel and learns which unit it is talking to, so leaving the
  panel on DHCP - with a reservation if you want its address fixed - suits this
  integration better than a static address set on the unit itself. Where Home Assistant
  sees the lease it knows the panel's MAC and follows a changed address by itself, though
  polling stops for the gap between the lease changing and Home Assistant seeing the new
  one. An install routed or VLAN-separated from the panel sees no lease, is identified by
  address, and has to be corrected under Settings -> Devices & Services -> Configure. See
  "How the panel is identified" below.
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

After the restart, watch Settings -> Devices & Services for a **discovered** "Honeywell
Tuxedo Touch" card: the panel is found from its DHCP lease and only asks for the login
(see [Discovery](#discovery-and-moving-addresses)). If it does not appear - Home
Assistant is routed away from the panel, or the panel holds a static address and issues
no lease - add it by hand with Add Integration -> "Honeywell Tuxedo Touch".

### Installation parameters

| Field | Meaning |
|---|---|
| IP address | IP address or hostname of the Tuxedo Touch unit on your LAN. A discovered panel does not ask for this - the lease supplies it. |
| Port | The web server port on the unit. 443 with HTTPS enabled (the default state), 80 without. |
| Use HTTPS | On by default. Required whenever "Secured Web Server Access" is enabled on the unit, which it is unless you turned it off. |
| Web login username | The unit's web login username (touchscreen: Settings -> Login settings), not the keypad code. |
| Web login password | The unit's web login password. Masked; never shown again once stored. |
| Keypad user code | Optional. The 4-digit user code used as the default for arm and disarm so automations and dashboards need not supply one. Masked; never shown again once stored. Leave it empty to be asked for a code on every arm and disarm. |
| Partition number | The panel partition this entry controls. Default 1. Add the integration once per partition for a multi-partition panel. |

Setup performs a real login against the panel before the entry is created, so a wrong
password, an unreachable address or a panel that answered oddly is caught on the form
with a message that says which. This is true of the discovered panel's form too: the
address comes from the lease, everything else is asked for and checked the same way.

**On HTTPS**: leave it enabled unless you have specifically disabled "Secured Web Server
Access" in the unit's settings. The unit's command endpoints redirect to HTTPS regardless
of the scheme you request whenever that setting is on, so mixing an HTTP login with
HTTPS-only commands would silently break arming and disarming. The integration detects
the redirect and raises a repair notification that switches the entry over for you. See
[docs/tuxedo_touch_api_notes.md](docs/tuxedo_touch_api_notes.md) for the full writeup.

### Reconfiguring

Settings -> Devices & Services -> Honeywell Tuxedo Touch -> Reconfigure changes the
address, port, scheme, credentials, keypad code or partition. The password and keypad
code fields come up empty; leaving them empty keeps the stored values. The entry keeps
the panel identity it already has - a reconfigure moves an entry, it never turns it into
a different panel - and its title follows the new address unless you renamed the entry.
There are no options beyond these.

The panel answers one client at a time, so the form takes care not to compete with the
polling it is reconfiguring. Changing only the keypad code or the partition contacts the
panel not at all: nothing the login depends on has changed, and the entry is already
proof that what it does depend on works. Changing the address, port, scheme, username or
password does need a login, so the entry is stood down for the moment that check takes
and set up again straight afterwards - on the new settings if they worked, on the old
ones if they did not. That applies to an entry that is retrying as much as to one that
is polling: the state you most often reconfigure from is a panel that has moved, and a
retry on the clock logs into the panel just as a poll does. Without this, the check
competes with the entry for the panel's only connection, and contention on this unit is
a hang rather than a refusal: the form waits out its timeout and reports "Failed to
connect" about a panel that is perfectly well.

Standing the entry down stops it starting new work, and the check then takes over the
connection it was using rather than opening a second one: every request this integration
makes goes out on the same key in Home Assistant's connection pool, so a socket left
idle by the poller is the socket the check picks up. That matters because the unit
counts connections, not sessions - a second one is accepted and then answered with
silence.

When the panel starts refusing the stored credentials, Home Assistant stops polling and
asks for them again rather than re-running the login handshake against doomed
credentials every thirty seconds.

### Discovery and moving addresses

The panel announces nothing on mDNS or SSDP, but it is a DHCP client and its lease is
distinctive: the unit's network interface uses Resideo's `00:D0:2D` OUI and it puts `Tux`
followed by the twelve hex digits of its own MAC in the lease hostname - for example
`Tux00D02D4DD7B6`. The manifest matches on both together, so another vendor's device is
never offered as a Tuxedo panel.

**A panel you have not added** shows up under Settings -> Devices & Services as a
discovered device. Opening it asks only for what a lease cannot say: the web login
username and password, the keypad code and the partition, plus the port and the HTTPS
toggle in case you turned "Secured Web Server Access" off. The integration logs in to the
panel before creating the entry, exactly as the manual form does, and keys the entry on
the MAC the lease carried.

**A second partition is added by hand.** Discovery offers a panel once. As soon as one
entry carries the panel's MAC, later leases from it are treated as a move rather than a
new device, so the discovered card does not come back for partition 2. Add it with
Settings -> Devices & Services -> Add integration -> Honeywell Tuxedo Touch, giving the
same address and the other partition number; the next lease Home Assistant sees for that
address hands the new entry the panel's MAC, and from then on both entries move together.

**A panel you ignore stays ignored.** Dismissing the discovered card with Ignore keeps
Home Assistant from raising it again, however often the panel renews its lease. Undo it
with Settings -> Devices & Services -> the three-dot menu -> Show ignored integrations.

**A panel that moves** is followed automatically. Home Assistant hands over the new lease,
the stored address is corrected on every entry for that panel - one per partition - and
the integration reloads. The entry's title follows the new address unless you renamed the
entry. An entry you added by hand was keyed on its address; the first lease Home Assistant
sees for that address gives it the panel's MAC instead, and from then on it moves with the
panel too.

All of this needs Home Assistant to see the lease, which means it is on the panel's own
network segment. A routed or VLAN-separated install is identified by address and has to be
corrected by hand - see "How the panel is identified" under Known limitations.

### Removing it

Settings -> Devices & Services -> Honeywell Tuxedo Touch -> the entry's menu -> Delete.
That removes the entry, its device, its entity and any repair notification it raised.
Nothing is written to the panel at any point, so there is nothing to undo on the unit;
the web login account you used stays as it was.

## How it updates

**The panel pushes its state, and that is what the entity shows.** As soon as an entry
is set up, the integration opens one long-lived request to the panel's event stream and
holds it open for the life of the entry. The panel reports partition status on it as it
happens - an arm, a disarm, and the exit-delay countdown a second at a time - so a
change made at the keypad reaches Home Assistant in seconds rather than at the next
poll. The `tuxedo_source` attribute says `stream` when the state came from there.

The 30-second status read is still there, doing two smaller jobs: it is the first read
at setup, which is what proves the address, the scheme and the credentials, and it is
the fallback whenever the stream is not connected. While the stream is delivering, what
the poll reads is ignored rather than written over the pushed status - with one
exception, described below.

**This closes the `Not available` story.** The panel's `GetSecurityStatus` endpoint
reads a cache its firmware can fill only from a message on the alarm bus, and answers
the literal `Not available` while that cache is empty; on a quiet house that could last
hours, and it is what used to leave the entity with nothing to show. The event stream
does not read that cache. A client on it cannot see `Not available` at all, so on
firmware that has the stream the condition no longer reaches the entity. It is still
handled for firmware that does not: a poll answering `Not available` is a failed read,
not a state, on the first poll after a load as much as on the hundredth.

The entity is available while **either** source is working, and unavailable only when
both are down - so a poll answering `Not available` while the stream is up is not an
outage of anything. The stream counts once it has something to show rather than the
moment its socket opens: an entry that loads during a `Not available` spell stays
`unavailable` for the second or two before the first pushed status, instead of going
`unknown` with nothing behind it. When the stream drops it reconnects on its own, with a wait that
doubles up to five minutes and resets the moment a connection comes up; the log gets one
line when it goes and one when it returns. A panel whose firmware has no such endpoint
answers 404, the stream stops asking, and the integration runs on the poll alone exactly
as it did before 0.4.0.

### Arming and disarming

Arm and disarm answer HTTP 200 with an empty body: the panel says what it did on the
event stream, seconds later, and not in the reply. So a command waits for the panel's
own report, for up to eight seconds. If none arrives, the integration polls; if the poll
cannot show the change either, the entity shows the state that was asked for and marks
it `assumed` in `tuxedo_source`, because nothing confirmed it. The next real status from
either source replaces it. A poll that was already in flight when the command went out
is discarded rather than allowed to flip the entity back.

### When the two sources disagree

The stream says outright whether the partition is armed, but never in which mode - the
mode comes from the display text. A display text this integration does not recognise
therefore settles nothing, and that is the one case where the poll's own reading is let
through to settle it. Everything in the table under
[Supported functions](#supported-functions) is recognised on both sources; a firmware
spelling a mode some other way would take this path rather than showing `unknown` for
ever.

The entry loads through an outage of either kind. A panel that answers `Not available`
has answered - address, port, scheme and credentials are all proven by that reply - so
refusing to set the entry up would take the device, the entity and its history away for
as long as it lasted.

Requests go out on Home Assistant's own HTTP connection pool rather than a pool of this
integration's own, and every client here - each entry's poller, and the checks the setup
and reconfigure forms run - shares one pool key, so a check takes over the connection the
poller left idle rather than opening a second one. Sharing the key is what makes the
takeover possible; it does not by itself cap us at one connection, because two requests
of ours that overlap in time are two connections. What keeps them from overlapping is the
reconfigure form's stand-down: it unloads the entry, which stops the next poll, and waits
for any poll already in flight to finish before the check dials the panel. That is the
point rather than a detail: the unit serves one connection at a time and answers a second
with silence, so anything of ours that opened its own would starve the poller instead of
queueing behind it.

The pool keeps an idle connection for fifteen seconds. A thirty-second poll therefore
opens a fresh one each time and pays for the panel's slow legacy TLS handshake once per
poll, and for the second half of every interval nothing of ours is polling the panel.

The event stream is a second connection and it is held open permanently, which sounds
like exactly the contention described above and is not: the panel's stream endpoint is
measurably not subject to that limit. Two clients have each held a stream while commands
went out on a separate request, all three served at once, and six connect-disconnect
cycles on one session reclaimed their slot every time. So the stream can be held while
the poll, a setup check and the panel's own web UI all work. Unloading an entry cancels
it and waits for the cancellation, so a returned unload still means nothing of ours is
on the panel - which is what the reconfigure form relies on before it dials.

## Repairs

Two conditions cannot be cleared by retrying, so they arrive as repair notifications
under Settings -> System -> Repairs rather than as log lines.

| Notification | What it does |
|---|---|
| **Tuxedo Touch needs HTTPS** | The panel redirected the API to HTTPS because "Secured Web Server Access" is on. Opening the notification and submitting turns on Use HTTPS for that entry, moves port 80 to 443 (any other port is left as you set it) and reloads the integration. |
| **Two Tuxedo Touch entries reach one panel** | Two entries reach the same panel and partition - by different addresses, or by the same address on ports 80 and 443 - so only one of them can hold the panel's identity. The notification names both entries; remove whichever you do not want and the other adopts the panel's MAC on the panel's next DHCP lease. |

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

Announce the exit delay as the panel counts it down, and again when it has armed:

```yaml
automation:
  - alias: Call the exit delay out loud
    triggers:
      - trigger: state
        entity_id: alarm_control_panel.honeywell_tuxedo_touch_partition_1
        attribute: arming_seconds_remaining
    conditions:
      - condition: template
        value_template: >
          {{ trigger.to_state.attributes.arming_seconds_remaining in [30, 10] }}
    actions:
      - action: tts.speak
        target:
          entity_id: tts.piper
        data:
          media_player_entity_id: media_player.hallway
          message: >
            {{ trigger.to_state.attributes.arming_seconds_remaining }} seconds
            to leave.
```

## Known limitations

- Only security arm/disarm/status is implemented. The panel's API also exposes lighting,
  thermostat, door lock, scene, and garage door control - untested and unimplemented here,
  though they should follow the same request-signing pattern.
- **The event stream carries the alarm state and nothing else.** Zone-level detail and
  the event log are not obtainable from this panel over HTTP by any route: there is no
  zone endpoint, the configuration files are not served, and the one command that would
  report zones is accepted and answered by nothing. For zones, use an ECP-bus
  integration (Envisalink, esphome-vistaECP) on the same panel.
- **The status feed can still go quiet on firmware without the stream.** While the poll
  is the only source and the panel answers `Not available`, it is reporting no status at
  all, so the entity is `unavailable`: it cannot see changes made at the physical keypad,
  and because Home Assistant skips unavailable entities in service calls, it cannot be
  armed or disarmed from Home Assistant either until a real status arrives. The panel's
  own touchscreen is unaffected throughout - the command path and the status-reporting
  path fail independently on this firmware. On firmware that has the event stream this
  cannot happen: that path does not read the cache the placeholder comes from.
- Status is polled without a partition parameter (the firmware's `GetSecurityStatus`
  doesn't take one), so on multi-partition panels the reported status is whatever the
  Tuxedo module itself reports; arm/disarm do target the configured partition.
- **The panel serves one web session at a time.** A browser tab left open on the unit's
  web UI can make setup or polling fail to connect until it is closed.
- **How the panel is identified.** It reports no identifier of its own: no serial, no
  hostname and no MAC over its API (`Registration/AddDeviceMAC` enrolls a *client's* MAC,
  not the unit's, and no documented endpoint returns the unit's network configuration).
  The MAC therefore comes from the panel's DHCP lease, which Home Assistant watches
  anyway, and the config entry is keyed on it. A panel discovered from its lease has it
  immediately; a panel added by hand is keyed on its address until Home Assistant sees a
  lease for that address, and adopts the MAC then. An install that never sees a lease -
  routed, or on another VLAN, or a panel given a static address on the unit itself -
  stays identified by address, where changing the panel's IP does read as a different
  panel and has to be reconfigured by hand. Nothing here resolves a MAC by ARP any more:
  the integration ships no synchronous dependency and does no lookup of its own.
- Armed Instant is mapped to `armed_night`, the closest Home Assistant state to a Stay
  variant with no entry delay.
- **Discovery is DHCP only.** The unit answers nothing on mDNS or SSDP, so a panel that
  issues no DHCP lease - one given a static address on its own touchscreen, or one Home
  Assistant is routed away from - is never discovered and is added by hand. See
  "Discovery and moving addresses".
- Verified against firmware `TUXW_V5.3.21.0_VA`. Older firmware may behave differently
  (see the docs) - not tested here.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Setup says "Failed to connect" | Home Assistant cannot reach the address and port, or the panel's single web session is held by a browser tab. Close any tab open on the unit and retry; check the port matches the HTTPS setting (443 on, 80 off). |
| Reconfigure says "Failed to connect" | The new address, port, scheme or credentials did not answer. The entry has already been put back on its old settings and is running as it was before, so correct the form and submit it once more. It is not the entry itself getting in the way: whether it was polling or retrying, it is stood down for the moment the check takes. |
| Setup says "Invalid username or password" and they are right | Web access for that user was disabled, which a panel reset does. Re-enable it on the touchscreen under Setup -> Account, or use the Login settings page to set the credentials again. |
| Setup says the panel answered but the response could not be used | The login page came back without the challenge headers, or the key page was short: firmware this client does not know. The log line names which. Open an issue with the firmware version from the touchscreen's About page. |
| The entity is unavailable and the API call was redirected to HTTPS | "Secured Web Server Access" is on and the entry uses HTTP. A repair notification offers to switch the entry over; see [Repairs](#repairs). Reconfiguring by hand with Use HTTPS on and port 443 does the same thing. |
| Home Assistant asks to re-authenticate | The panel rejected the stored web login. Enter the current username and password; the address and code are kept. |
| The entity is unavailable and the panel is up | Both sources are down: the event stream is not connected and the poll is failing or answering `Not available`. The log says so once per outage for each, and the entity comes back on the first real status without anything from you. |
| The entity does not follow the keypad | Check `tuxedo_source` on the entity. `stream` means the panel is pushing changes and they should arrive in seconds. `poll` means the stream is not connected - the log says why, and a firmware without the endpoint says so once at setup - so changes wait for the 30-second read and can be masked by `Not available`. |
| The log says the panel has no push stream | That firmware does not serve the endpoint (it answers 404). Nothing is broken; the integration runs on the 30-second poll alone, with the `Not available` behaviour described in Known limitations. |
| Two entries for one panel | A repair notification names both entries. Remove one; the other adopts the panel's identity on the panel's next DHCP lease. See [Repairs](#repairs). |
| The panel moved to a new IP and stayed unavailable | The stored address is only corrected automatically where Home Assistant sees the panel's DHCP lease. On a routed install, or where the panel holds a static address and issues no lease, reconfigure the entry with the new address. |
| The panel is not discovered | Home Assistant only discovers it from a DHCP lease. Check that the panel is on DHCP rather than a static address set on its touchscreen, and that Home Assistant is on the same network segment. Add it by hand otherwise; it works exactly the same, it is just keyed on the address. |
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
python -m pytest tests -q         # the whole suite; tests/ha needs the harness
python -m pytest tests -q --cov=custom_components/tuxedo_touch --cov-fail-under=95
python -m mypy custom_components/tuxedo_touch
python tools/validate_local.py    # the offline stand-in for hassfest and HACS
```

`tests/ha` needs `pytest-homeassistant-custom-component`, which brings Home Assistant
with it, and skips where it is absent - so a run without it covers the client only, and
the coverage figure from such a run measures the skip rather than the code. What the
harness needs is the Python version, not the platform: 2026.x is written for 3.14 and
will not install under 3.12. Given 3.14 the whole suite runs on Windows as it does on
Linux, measured here on 2026-09-05 with `pytest-homeassistant-custom-component`
0.13.357, Home Assistant 2026.8.3 and CPython 3.14.7 - 183 tests, 99% coverage, mypy
strict clean. Several of them stand a fake panel up on 127.0.0.1 and talk to it over a
real socket, which the test harness blocks by default; those ask for the `socket_enabled`
fixture, and the harness's own guard still allows nothing but 127.0.0.1. The GitHub Tests
workflow is still the gate: it runs the whole suite,
holds coverage of the integration at 95%, and runs mypy in strict mode and the validator
on every push. A mypy run without Home Assistant installed reports its classes as `Any`;
that is the missing package, not the code.

Changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## Quality scale

Built to Home Assistant's Integration Quality Scale, tracked rule by rule in
[`quality_scale.yaml`](custom_components/tuxedo_touch/quality_scale.yaml) with a reason
on every exemption and on every rule still marked `todo`. `tools/validate_local.py`
checks the file against the pinned rule list, so a rule that is simply missing fails
rather than reading as complete. The scale is a core-integration concept; a custom
integration builds to the rules and is not scored.
