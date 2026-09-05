# Changelog

Notable changes to this integration, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version
numbers are the ones in `custom_components/tuxedo_touch/manifest.json`.

## [0.3.0] - 2026-09-05

`manifest.json` carries 0.3.0; the GitHub release for it is not cut yet, so
everything below is what you get by installing from the default branch.

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
