# Changelog

Notable changes to this integration, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version
numbers are the ones in `custom_components/tuxedo_touch/manifest.json`.

## [0.4.2] - 2026-09-05

### Fixed

- **A lost link between the Tuxedo and the alarm panel no longer leaves the
  entity reporting a state nothing can see.** The event stream's third field
  was read as the partition number. It is not: it is a panel status code, and
  the firmware writes `-1` into it - and sends the frame anyway - whenever the
  Tuxedo has lost its ECP link to the VISTA panel behind it. Since `-1` never
  matched the configured partition, every frame from the moment that link died
  was discarded, and the alarm entity sat on the last state it had accepted -
  `armed_away`, say - indefinitely, while the stream reported itself connected
  and the poll went on succeeding out of a cache nothing was refilling. The
  entity now goes **unavailable** for as long as the panel says it cannot see
  the alarm, with one log line naming the bus to check, and it comes back on
  its own within about half a minute of the link returning. A stale armed or
  disarmed reading is worse than no reading.
- **Frames are no longer rejected for naming the wrong partition.** They name
  no partition, and they need not: the firmware emits a status frame only when
  the partition that changed is the one the panel is currently displaying, so
  every frame that arrives is about that partition already. The 0.4.1 guard
  could only throw away valid frames. Unsolicited status updates, which 0.4.1
  discarded for "naming no partition", are applied too. The configured
  partition still governs the status poll and every arm and disarm.

### Changed

- The diagnostics download reports `ecp_link_down`: the one condition in which
  the poll is succeeding, the stream is connected, frames are arriving, and the
  alarm entity is unavailable anyway.

### Known limitations

- The event stream follows whichever partition the panel is currently
  displaying, and carries no marker saying which. On a multi-partition panel,
  changing the displayed partition at the touchscreen or in the panel's web UI
  makes the stream report a different partition's status to this entry, with
  nothing in the data to reveal it. Single-partition installs are unaffected.
  See Known limitations in the README.

## [0.4.1] - 2026-09-05

Supersedes 0.4.0, which was tagged from a commit that predated this work.
**Anyone running 0.4.0 should move to 0.4.1.**

### Fixed

- **Home Assistant no longer retries a rejected credential.** In 0.4.0 the new
  event stream reconnected on a fixed timer and re-ran the full login each time,
  roughly twelve credential attempts an hour, indefinitely. Repeated failed web
  logins can disable the panel's web accounts - on unpatched firmware
  permanently, recoverable only at the touchscreen. The integration now spends
  at most one automatic login attempt per credential set for the life of the
  entry, including across restarts, and waits for the user.
- **A server fault is no longer mistaken for a rejected credential.** A panel
  answering 500 on an unrelated page, or a session that did not yield its key
  material, is a fault to retry rather than a verdict on the password. Only a
  genuine refusal counts against the one-attempt budget.
- **Re-authentication can be retried deliberately.** Submitting the stored
  credentials unchanged now asks for confirmation and states the cost, instead
  of either refusing outright or silently spending an attempt.
- **A command the panel refused is no longer reported as carried out.** The
  entity keeps the panel's own reading and raises an error, rather than writing
  the requested state over a poll that says otherwise.
- **The stream's reconnect backoff no longer resets on the response headers.** A
  connection now has to last and carry a frame before it counts as one that
  worked, so a failure occurring after the connection opened backs off properly
  instead of looping at the floor.
- **A stream-sourced reading can no longer latch.** Corroboration applies only
  to a poll's reading, so the panel moving on cannot leave the entity holding a
  mode the panel has stopped reporting.
- **A malformed frame costs a frame, not the connection**, and a frame that
  names no partition is no longer taken as evidence about any entry.

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
- The diagnostics download reports the stream: whether it is connected, whether the
  firmware answered 404 and has no stream at all, whether it stopped because the panel
  refused the credentials, whether it stopped after repeated unexpected failures and
  what the last one was, the connection id, the client count,
  how many frames have arrived and how far a failing stream has backed off, alongside
  which source produced the status shown. A report naming only the poll could not
  distinguish a stream that never came up from a panel that is not answering.

### Fixed

- **Home Assistant now makes at most ONE automatic login attempt per set of
  credentials, and then waits for you.** The panel counts failed web logins, and on
  unpatched firmware three of them disable every web account it has - no timeout, no
  self-clear, and the only way back is at the touchscreen (Setup, then the account
  screen, re-enable web access, Enable All, Apply). The event stream added in this
  release is a background task that reconnects on its own, and Home Assistant leaves an
  entry loaded after credentials are rejected, so a web password changed at the keypad
  would have had it re-running the login handshake about twelve times an hour, for
  ever: past the three-strike limit inside a quarter of an hour, and then still
  knocking at a panel whose web accounts it had already disabled - with the
  re-authentication card failing afterwards even for the correct password. Now the API
  client will not spend a second attempt on credentials the panel has refused, the
  event stream stops instead of backing off, the refusal is recorded on the config
  entry so a restart costs nothing either, and a repair notification explains what
  happened beside the re-authentication card. Patched firmware allows five attempts and
  clears itself after five minutes, but the panel publishes no version anywhere, so the
  behaviour has to be safe on the stricter one.
- **Only the panel refusing a credential counts as the panel refusing a credential.**
  A key page that answers 500, a key page missing its key material, and a login POST
  answering 5xx are all faults of a busy embedded web server rather than verdicts on
  an account - the first two happen behind a login the panel accepted and counted as
  successful. Treating them as refusals spent the one-login budget, recorded the
  refusal on the config entry for good, stopped the event stream permanently and had
  setup refuse before touching the network, so one transient HTTP error cost the alarm
  entity until the entry was deleted and re-added. They are now ordinary faults: the
  next poll retries and a healthy panel recovers by itself.
- **The re-authentication card can retry the credentials that are stored.** Submitting
  them unchanged used to be answered "invalid username or password" without the panel
  being contacted, and a reconfigure that changed nothing the login depends on cleared
  nothing - so an owner whose panel had been reset, and who had re-enabled web access
  at the touchscreen exactly as the card told them to, had no way to say so. The only
  submission that reached the panel was a different password, which is a real refused
  login on a unit that disables every web account at three. Submitting the stored
  credentials now asks for confirmation, naming what it costs, and spends one login if
  you confirm. Nothing automatic gains a retry.
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
  real statuses is not an outage of anything, and no longer shows as one. The stream
  counts once it has a status to show rather than the moment it connects, so an entry
  loading during an outage stays `unavailable` until one arrives instead of reading
  `unknown` with nothing behind it.
- A command the panel accepted but has not carried out is no longer reported as done.
  Arm and disarm answer HTTP 200 with an empty body - the panel says what it did on the
  event stream, not in the reply - so a command now waits for that report, falls back to
  a poll, and only if neither could say anything shows the state that was asked for,
  marked `assumed`. Up to 0.3.2 the requested state was written through as soon as the
  request returned.
- **A command the panel REFUSED now says so instead of reporting success.** The
  commonest refusals are the everyday ones: an arm on a faulted zone, a disarm with a
  code the panel will not take. The fallback poll that follows such a command reports
  the partition in the opposite state, and that answer used to be discarded exactly as
  if the poll had been unable to say anything - so the entity showed `armed_away` on a
  disarmed house, or `disarmed` on an armed one, fired a state-change event that any
  "when the alarm disarms" automation acts on, and the service call returned success.
  A poll that names the opposite state is now the panel's own account of what it did:
  the entity keeps showing it and the service call fails with the two statuses in the
  message.
- **A stream that is accepted and then dropped backs off.** The reconnect wait was
  reset the moment the response status said 200, before a byte of the body had been
  read, so every failure after the headers - a body that ends at once, an error page,
  a reset mid-body, a read timeout on a half-open socket - looked like a healthy
  connection and returned the wait to its five-second floor. That is a connection every
  five seconds indefinitely, on a unit that serves one at a time and whose contention
  behaviour needs a reset to clear. The wait is now reset when a connection ends, and
  only if it lasted a minute and carried a frame.
- **A status frame that does not name a partition is no longer taken as this entry's.**
  The panel's unsolicited status record carries no partition field, and every entry
  accepted it whatever partition it was configured for - so on a two-partition install
  one partition's state landed on the other's entity, latched there because a pushed
  status suppresses the poll that would correct it, and could confirm the wrong
  partition's arm or disarm.
- **A streamed status text this integration does not recognise no longer erases the
  mode the poll named.** The stream's flag says whether the partition is armed but
  never in which mode, so such a text settles nothing - and writing it through meant
  the poll's correct answer survived only until the panel's next status repeat, about
  33 seconds, with the entity reading `unknown` in between. It now corroborates the
  poll's reading when the two agree about arming, and replaces it when they do not.
- **One unreadable frame costs a frame, not the event stream.** A partition field the
  decoder could not convert raised inside the read loop, where nothing caught it: the
  background task ended for the life of the entry while the log said "reconnecting" and
  the diagnostics download said the stream was backing off. Bad frames are now dropped
  and logged, the connection is kept, and a stream that keeps failing for a reason the
  integration cannot name stops with a flag that says so.
- An empty response body is read correctly. `aiohttp` returns nothing rather than
  raising for such a body, so the handling meant to accept a command's empty answer
  never ran.

### Changed

- `iot_class` is now `local_push`. The push stream is the primary source of state and
  the poll is the fallback under it.
- Losing the event stream is logged the way losing the poll already was: one line when
  it drops and one when it returns, not one per reconnect attempt.
- The `tuxedo_color` attribute is lower case whichever source reported the status. The
  panel's REST answer carries its own capitalised word (`Green`) and the stream carries a
  digit this integration names in lower case; the REST word is now lower-cased as it is
  read, so a template comparing the attribute to `green` keeps matching when the source
  changes - which it does at every setup, at every stream drop, and permanently on
  firmware with no stream.

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
