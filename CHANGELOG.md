# Changelog

Notable changes to this integration, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version
numbers are the ones in `custom_components/tuxedo_touch/manifest.json`.

## [0.4.0] - 2026-09-05

### Added

- **The panel's state now arrives by push.** The integration holds one long-lived
  request open to the panel's event stream for the life of an entry, and the panel
  reports partition status on it as it happens - an arm, a disarm, and the exit-delay
  countdown a second at a time. A change made at the keypad reaches Home Assistant in
  seconds instead of at the next poll. The 30-second status read stays as the first
  read at setup and as the fallback whenever the stream is not connected.
- `arming_seconds_remaining`, an entity attribute carrying the seconds left of the exit
  delay while the state is `arming`. A 30-second poll used to miss the countdown
  entirely.
- `tuxedo_source`, an entity attribute saying where the shown state came from:
  `stream`, `poll`, or `assumed` for a command the panel accepted but neither source
  reported.

### Fixed

- **`Not available` can no longer reach the entity at all on firmware that has the
  event stream.** The panel's `GetSecurityStatus` endpoint reads a cache its firmware
  fills only from a message on the alarm bus, and answers that placeholder while the
  cache is empty - on a quiet house, for hours. The event stream does not read that
  cache, so a client on it never sees the condition this integration has been working
  around. The workaround stays for firmware that answers 404 to the stream: such a poll
  is still a failed read rather than a state, on the first poll after a load as much as
  on the hundredth.
- The entity is now available while **either** source is working, and unavailable only
  when both are down. A poll answering `Not available` while the stream is delivering
  real statuses is not an outage of anything, and no longer shows as one.
- A command the panel accepted but has not carried out is no longer reported as done.
  Arm and disarm answer HTTP 200 with an empty body - the panel says what it did on the
  event stream, not in the reply - so a command now waits for that report, falls back to
  a poll, and only if neither could say anything shows the state that was asked for,
  marked `assumed`. Up to 0.3.2 the requested state was written through as soon as the
  request returned.
- An empty response body is read correctly. `aiohttp` returns nothing rather than
  raising for such a body, so the handling meant to accept a command's empty answer
  never ran.

### Changed

- `iot_class` is now `local_push`. The push stream is the primary source of state and
  the poll is the fallback under it.
- Losing the event stream is logged the way losing the poll already was: one line when
  it drops and one when it returns, not one per reconnect attempt.

## [0.3.2] - 2026-09-05

### Fixed

- The alarm entity no longer latches on `unknown` when the panel is having one
  of its `Not available` spells. The panel intermittently answers that instead
  of a security status - its own firmware quirk, not something this integration
  can talk it out of - and the entity used to store the placeholder as its state
  whenever there was no earlier status to keep, which is exactly the case on the
  first poll after a restart or a reload. Every later `Not available` then
  preserved that stored placeholder, so the entity sat on `unknown` until
  somebody armed or disarmed. `Not available` is now treated as a failed read on
  every poll, the first one included: the entity is `unavailable` for as long as
  the panel keeps saying it, the last real status is kept underneath, and the
  first genuine status brings the entity back on its own. The log gets one line
  when an outage starts and one when it ends, rather than one per poll. Home
  Assistant skips unavailable entities in service calls, so arming and disarming
  from Home Assistant are out for the length of an outage as well; the panel's
  touchscreen is not, and this replaces an entity that read `unknown` while
  claiming to be fine.
- The entry still loads while the panel is in one of those spells. A panel that
  answers `Not available` has answered - the address, port, scheme and
  credentials are all proven by that reply - so refusing to set the entry up
  would take the device, the entity and its history away for an outage that can
  last hours. Only a panel that cannot be read at all still holds setup back.
- A reconfigure no longer runs its check against the panel while a poll of this
  integration's own is still in flight. Standing the entry down stops the next
  poll but says nothing about one already running, and that poll is holding the
  connection to a unit that serves one client at a time - so the check could
  still be the second client the panel answers with silence. Unloading now waits
  for the poll in flight before it returns, which is what the reconfigure form
  awaits before it dials.

### Changed

- Polls are serialized against each other, so two of them can never be on the
  panel at once however they were triggered.

## [0.3.1] - 2026-09-05

### Fixed

- Reconfigure no longer competes with the entry's own polling for the panel's
  one connection. The panel serves a single client at a time and contention
  shows up as a hang, so the check the form ran could time out and report
  "Failed to connect" about a panel that was answering the poller perfectly.
  Changing only the keypad code or the partition now contacts the panel not at
  all - nothing the login depends on changed - and changing the address, port,
  scheme, username or password stands the entry down for the moment the check
  takes, then sets it up again: on the new settings if they worked, on the old
  ones if they did not. An entry that is retrying its setup is stood down the
  same way as one that is polling - that is the state you most often
  reconfigure from, and the retry on the clock logs into the panel just as a
  poll does.
- Every request this integration makes now shares one connection-pool key per
  panel, so a form's check takes over the connection the poller was using
  rather than opening a second one beside it. Home Assistant's pool holds an
  idle connection for fifteen seconds after a poll, and it keys that pool on
  the TLS context object as well as the address - so the HTTPS clients, which
  each built a context of their own, were landing on separate keys and could
  hold two connections to a unit that accepts a second one and then answers it
  with silence. The permissive context this panel's 2009-era certificate needs
  is now built once and shared.

## [0.3.0] - 2026-09-05

### Added

- Repair notifications for the two conditions that need you rather than a
  retry. When the panel redirects the API to HTTPS, the notification switches
  the entry over for you: it turns on Use HTTPS, moves port 80 to 443 and
  reloads. When two entries reach the same panel and partition, the
  notification names both so you can remove one. Both clear themselves when
  the condition goes and when the entry is deleted.
- Reconfigure: the address, port, scheme, username, password, keypad code and
  partition can all be changed without removing the entry. The password and
  keypad code fields come up blank and blank keeps the stored value.
- Re-authentication: when the panel starts refusing the stored web login,
  Home Assistant asks for it again instead of retrying doomed credentials
  every thirty seconds.
- Downloadable diagnostics, with the host, MAC, username, password and keypad
  code redacted and the panel's raw status strings kept.
- **The panel is discovered from its DHCP lease.** A Tuxedo Touch you have not
  added shows up under Settings -> Devices & Services on its own and asks only
  for what a lease cannot supply: the web login, the keypad code, the
  partition, and the port and HTTPS toggle in case "Secured Web Server Access"
  is off. It logs in to the panel before the entry is created, exactly as the
  manual form does. The matcher is the unit's Resideo OUI `00:D0:2D` together
  with the lease hostname it sets - `Tux` followed by the twelve hex digits of
  its own MAC - both measured from a real unit, so no other vendor's device is
  offered as a Tuxedo panel. Dismissing the discovered panel with Ignore
  sticks: no later lease renewal raises the card again. Discovery offers a
  panel once - once an entry carries the MAC, a later lease is a move rather
  than a new device - so a second partition on the same panel is added with
  Add integration and adopts the MAC from the next lease.
- A panel that takes a new DHCP lease is followed automatically: the stored
  address is corrected on every entry for that panel - one per partition - and
  the integration reloads. An entry you added by hand was keyed on its address
  and adopts the panel's MAC from the first lease Home Assistant sees for that
  address, after which it moves with the panel too.
- The README gained installation parameters, the update cadence, example
  automations, use cases, troubleshooting and removal instructions.

### Changed

- The panel is identified by its MAC address rather than by the address it
  happens to hold, so a changed DHCP lease is a reconfigure rather than a new
  device. The MAC comes from the lease, which is the only place it exists: the
  panel returns no serial, hostname or MAC of its own over its API. An install
  that never sees a lease - routed, on another VLAN, or a panel given a static
  address on its own touchscreen - keeps address identity, which is ordinary.
- The entity is named after its partition: `Partition 1` under the device
  `Honeywell Tuxedo Touch`. Two partitions on one panel are two devices with
  the same name, and the partition in the entity name is what tells the two
  alarms apart. An install from before this keeps the entity id it had.
- The entry's title follows the panel to a new address, unless you renamed
  the entry, in which case your name stays.
- The password and keypad code are masked in every form and are never sent
  back to the browser as a suggested value.
- Setup and polling failures now carry translated messages instead of raw
  text, so the integration card says what went wrong in your own language.
- The HTTP session comes from Home Assistant's own helper and runs on the
  shared connector. The dedicated connector it replaced held connections open
  between polls; the shared one uses aiohttp's default keep-alive of fifteen
  seconds, so a thirty-second poll opens a fresh connection, TLS handshake
  included. The panel serves one connection at a time, so nothing queues
  behind a stale one.
- Minimum Home Assistant is 2026.3.0: the integration's icon and logo ship in
  the repository and that is the release which serves them.
- `cryptography` is declared in the manifest, unpinned on purpose, because
  Home Assistant core pins it and constrains every install to that pin.

### Removed

- The `getmac` requirement. It existed only to resolve the panel's MAC by ARP,
  which is a blocking operating-system call run in a worker thread and only
  ever answered on the panel's own network segment. DHCP discovery replaced it:
  Home Assistant's own lease watcher supplies the MAC, and `cryptography` is
  now the integration's only requirement. Nothing user-visible changes for an
  install that already has the MAC; an address-identified entry now gains one
  on the next lease rather than on the next restart.

### Fixed

- A partition change no longer orphans the entity's registry row and mints
  a `_2` entity id. The old `<entry_id>_partition_N` unique id is migrated to
  the entry id on the next start.
- The entity unique-id migration is awaited, so it actually runs.
- A panel that is down when Home Assistant starts no longer leaks one HTTP
  session per setup retry.
- Adding a panel that is already configured at the same address, port and
  partition is refused on the form instead of silently doing nothing. Adding
  one again at a *different* address makes a second entry, which nothing on
  the form can recognise as the same unit; the panel's next DHCP lease raises
  the "Two Tuxedo Touch entries reach one panel" notification naming both.
- A login page that answers without its challenge headers, or a key page that
  is short, now reports itself as a panel problem rather than as a wrong
  password or an unreachable unit.

## [0.2.1] - 2026-08-11

- Panel-side failures are surfaced instead of a bare "unexpected error".
- The single-connection limit of the panel's web server is documented.
- README badge row and an Open-in-HACS button.

## [0.2.0] - 2026-08-04

- Brand images ship inside the integration, per Home Assistant 2026.3's
  brands proxy.
- The exit-delay countdown maps to `arming`.
- Client and coordinator hardening: login locking, re-authentication,
  timeouts and fewer round-trips per poll.
- Packaging fixed for HACS, with lint CI and dependabot.

## [0.1.1] - 2026-07-03

- First release: one `alarm_control_panel` entity per partition, arm home,
  arm away, arm night and disarm over the panel's local API.
- Works around the firmware's persistent `Not available` answer from
  `GetSecurityStatus` by keeping the last known state.
