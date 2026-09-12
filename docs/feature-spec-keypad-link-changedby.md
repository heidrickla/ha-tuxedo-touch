# Spec: keypad display, ECP link health, and `changed_by`

Three additions, agreed with Lewis 2026-09-12. They are ordered by value against
cost and by dependency: (3) needs (1)'s parsing to exist.

None of them adds a request to the panel. Every byte they need is already
arriving on the push stream this integration is already connected to, or is
already held in the coordinator. That is the point — the panel's session budget
is ten slots and the integration deliberately holds one, so a feature that costs
a poll starts at a disadvantage.

Build to the platinum ruleset from the first commit, as every integration here
does: `quality_scale.yaml` updated in the same commit, entity and exception
translations, `PARALLEL_UPDATES`, typed `runtime_data`, `mypy --strict`, and
`tools/validate_local.py` green. No `quality_scale` key in the manifest — this
is a custom integration and the badge is core-only.

---

## 0. Stock is the default, and a stale verdict must not outlive it

Lewis, 2026-09-12: *"You should auto detect custom firmware so your default is
standard firmware unless custom is detected."*

**At setup this is already right, and deliberately so.** `async_probe_capabilities`
treats a capability list as the only thing that flips the verdict — a 404, a 302,
an HTML 200, a body with no list, even a 401 ("which neither firmware does")
all read as stock, and a connection failure is raised rather than cached, so the
next setup asks again instead of remembering an answer it never got. Do not
weaken any of that.

**What is missing is the stale case.** `self._tuxweb` is assigned once and never
reset; the only guard is `if self._tuxweb is not None: return self._tuxweb`. So
the verdict is cached for the client's lifetime and the probe runs at setup only.
Roll the panel back to stock while HA is running — and four rollback binaries are
staged on it right now — and the integration keeps speaking tuxweb: bearer token,
`command_result` semantics, the lot. It fails *visibly* rather than dangerously
(a 401 is correctly raised as an auth failure and never answered with a login),
but the symptom a user sees is "tuxweb refused the bearer token", which points at
the token rather than at the firmware having changed under it.

**Required:** when the panel is on tuxweb and a request fails in a way that is
consistent with the contract no longer being there — a 401 on the token, or the
tuxweb paths 404ing — re-run the probe once before surfacing an auth failure, and
fall back to stock if the capability list is gone. The default is stock; a
positive detection is the only thing that leaves it, and a detection that stops
being true must not persist.

Guard the obvious failure mode: a panel that is genuinely refusing a bad token
must not put the integration in a re-probe loop. One re-probe per failure, and
the re-probe must not log in — the same constraint the setup probe already
honours, because on stock a third bad login disables every web account.

---

## 1. Keypad display text — `sensor`

### ⚠ Premise corrected 2026-09-12 — this is NOT free any more

This item was written on the claim that "the data is already arriving and being
discarded", so it cost no panel traffic. **That was the VENDOR era.** The firmware
session reports that tuxweb routes `msgType 20` to its diagnostic channel and
emits nothing, so on the panel as it runs today **no console text reaches HA at
all**. Item 1 therefore needs a tuxweb change before the integration change is
worth anything, and it is no longer the cheapest item on this list.

Two further facts from that session that change the shape:

- `/tuxedo` only sends `msgType 20` while console mode is ON (command 19), which
  on the vendor happened only while someone had `/console.html` open. For HA to
  receive keypad text continuously, tuxweb must send 19 after registering and
  hold console mode on. That is passive — display only, no key sending — but it
  is a standing behaviour change on the panel.
- Console mode **and** the whole push broadcast (`F7_Mesgs_enabled`) are cleared
  by `home_back_press`. The vendor survived that because every new push
  connection re-registered; tuxweb registers once, so a Home/Back press on the
  touchscreen silences the stream until tuxweb relaunches. That is a latent
  defect in what is deployed now, independent of this feature.

### What exists in the vendor firmware

The vendor broadcast its keypad LCD and this integration discarded it.

`decode_status_frame` in `push.py` walks `fields[2:]` and accepts a field only
when its first byte is `0xFE`/`0xFF` (`FLAG_READY`/`FLAG_ARMED`):

```python
if not field or ord(field[0]) not in (FLAG_READY, FLAG_ARMED):
    continue
```

Console records carry no such flag byte, so every one of them decodes to `None`
and is dropped before the command filter is reached. That guard is load-bearing
for partition status — it is what locates the display field — so **do not
loosen it**. Add a second decoder rather than widening this one.

Observed console payload shape, latin-1 decoded:

```
0:20:2<line1>|<line2>
| |  |
| |  +-- the two LCD lines, pipe-separated
| +----- see below - do NOT assume this is a constant 2
+------- command id 20, SERV_CONSOLE_MSG_BROADCAST
```

**Searched for a verbatim capture and there is none.** No cmd-20 or cmd-`-1`
console record exists anywhere in `ha-tuxedo-touch`, `tuxedo-touch-firmware` or
`iot-protocol-tools`, and HA's current log holds none (the only rotated file,
`home-assistant.log.fault`, is 0 bytes). The shape above is **derived from
disassembly**, not observed on the wire — `tuxedo-touch-firmware`
`docs/TUXEDO-AUDIT-BUGS.md` §Code 20. Treat it as a reading to be confirmed, not
as evidence.

**SETTLED 2026-09-12 from the type-20 handler — the `2` is a fixed literal, not
a colour digit.** `bprintf`'s format is `"%d%s%d%s%s"` (0x85304) with args
(session, `":"`, 20, `":2"`, text); the second separator is the 2-character
constant `":2"` at 0x852f4, loaded from the constant pool and reused unchanged
for the `-1` copies. It cannot vary. The audit doc's "CSS digit" wording came
from the vendor's *page* reading it as a colour class, and that sentence has been
corrected. **Keying on the literal `":2"` is safe.** The earlier "accept any
digit" tolerance is harmless and can stay as belt-and-braces.

**Three more facts from the same handler, and they decide the decoder's shape:**

- Each LCD change produces **four** records: one `0:20:2…` and **three**
  `0:-1:2…` copies.
- The **id-20 record's text is lossy**: its first `:` at index > 0 is replaced by
  `-`. The three `-1` copies carry the **raw** text.
- The arm only runs when the reply session is `0`, i.e. a broadcast.

**Recommended: decode the id-20 record and ignore the `-1` copies.** One record
per change is its own deduplication, and the cost is a single mangled colon in a
16-character line. Taking the `-1` copies instead buys raw text at the price of
publishing every line three times, which needs dedup state that can drift.

Whichever is chosen, **the `-1` copies must not reach the partition path**. `-1`
is `CMD_UNSOLICITED`, one of the two ids the partition decoder ingests; today the
`0xFE`/`0xFF` guard rejects them and that must stay true. Test both directions:
a console frame produces no `PushStatus`, and a partition frame produces no
keypad reading.

The vendor also rebroadcasts the same text as command id `-1`
(`CMD_UNSOLICITED`), which is one of the two ids the partition path ingests —
hence `_status_code_of`'s note that "field 2 is the display text on the
unsolicited record". Both ids carry it; decode from one and ignore the other, or
you will publish every line twice.

**The `0xFE`/`0xFF` guard is currently the only thing keeping console text out of
the partition entity.** It is incidental rather than designed for that, so when
you add the console decoder, add a test that pins the existing behaviour: a
console frame must still never produce a `PushStatus`.

### What to build

- A `sensor` platform with one entity per entry, translation key `keypad_display`.
- State: the two lines joined with a single space, whitespace collapsed.
  **Guard the 255-character state limit** — HA drops a state longer than that and
  logs it, which would look like the sensor silently dying. Truncate and put the
  full text in an attribute.
- Attributes: `line_1`, `line_2`, and `raw`.
- `_attr_entity_category = EntityCategory.DIAGNOSTIC`.
- Availability follows the coordinator exactly as the alarm entity does: when the
  stream is down this is stale, not correct, and must read unavailable rather
  than hold the last line.

### Why it is worth doing

This is the only place the panel says things HA cannot otherwise see: which zone
is faulted **by name**, `Check` messages, bypass notices, trouble and AC-loss
text. The Envisalink gives zone *states*; it does not give the panel's own words.

---

## 2. ECP link health — `binary_sensor`

### What exists

Almost all of it. `coordinator.py` already tracks `self._ecp_link_down`, already
exposes it as a property (`ecp_link_down`), already flips it from the stream's
status code `-1`, and already reports it in diagnostics alongside
`relay_unreachable`. `const.py` already defines `CAP_PANEL_LINK_STATE`, which
tuxweb declares in `GetCapabilities`.

Today all of that is visible only by downloading a diagnostics file.

### What to build

- A `binary_sensor`, translation key `ecp_link`, `device_class: PROBLEM`, `on`
  when the link is down.
- `_attr_entity_category = EntityCategory.DIAGNOSTIC`.
- On **stock** firmware `CAP_PANEL_LINK_STATE` is absent. Decide explicitly and
  write the reason in the code: either the entity is not created on stock, or it
  is created and reports `unknown`. Prefer **not created** — an always-`off`
  problem sensor is worse than no sensor, because it reads as "checked, fine".
- A second entity for `relay_unreachable` is reasonable if it is independently
  meaningful; if it is not, say so in a comment rather than shipping two entities
  that always agree.

### Why it is worth doing

A dark ECP feed is the failure that cost this project months of investigation.
It is currently undetectable from HA without a human downloading diagnostics,
which means in practice it is undetectable. As a `problem` binary_sensor it is
one automation away from a notification.

---

## 3. `changed_by` on the alarm entity

### What exists

`AlarmControlPanelEntity` supports `changed_by`; ours is permanently `None`, so
the logbook cannot say who armed or disarmed.

### What to build

Populate it from the console text decoded in (1) — the panel names the user in
its display on an arm or disarm.

**Do not ship a guess.** Capture real frames for a disarm by user first and pin
the format in a test before writing the parser. If the text does not reliably
name a user, say so and drop this item rather than shipping a regex that is right
on one sample. A `changed_by` that is subtly wrong is worse than `None`, because
it will be believed.

---

## Explicitly NOT in scope

- **No keypad key sending, and no Lovelace keypad.** `/console.html` proves the
  path works, but **A/B/C/D are the panic keys** and the server does not
  distinguish them from a digit — the vendor UI gates them behind a confirmation
  the API has no equivalent of. Anything exposing key entry would put a silent
  police/fire/medical dispatch one mis-tap away, and nothing below our code would
  stop it.
- **No cameras or doorbell.** UniFi Protect already covers the house properly.
- **No zones.** `ha-envisalink-field-programmer` already exposes them.
- **Scenes are deferred, not rejected.** `GetSceneList` answers with data, but on
  stock firmware it leaked ~780 B per request and that was never attributed.
  Confirm whether tuxweb retired that before building on it.

---

## Verification bar

Per Lewis's standing rule, a green test is not evidence on its own: revert the
change and watch the new test go red, or it guards nothing.

- Unit tests against `tests/fake_panel.py` for both decoders, including the
  negative: a console frame must not produce a `PushStatus`, and a partition
  frame must not produce a keypad reading.
- An HA-level test under `tests/ha/` for entity creation, availability and the
  stock-firmware case where `CAP_PANEL_LINK_STATE` is absent.
- Live verification on the panel before release, the same shape as stage 8d:
  drive a real zone fault and a real arm/disarm, and confirm the keypad sensor
  and link sensor move. The Envisalink remains the independent witness on a
  second ECP path.

## Release

`main` is `066ca3c` at version 0.5.0. This is additive, so **0.6.0**, with the
`CHANGELOG.md` entry written under `[Unreleased]` and renamed at release — that
is how 0.5.0 was cut.

Both `main` and `tuxweb-api` declared 0.4.2 before the last release, which would
have shipped different code under a published version. **Bump the manifest in the
same commit that renames the changelog heading.**
