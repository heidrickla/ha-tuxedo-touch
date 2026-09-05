"""Constants for the Honeywell Tuxedo Touch integration."""

import re
from datetime import timedelta

DOMAIN = "tuxedo_touch"

CONF_MAC = "mac"
CONF_PARTITION = "partition"
CONF_USE_HTTPS = "use_https"

# Set on the entry's OPTIONS once the panel has refused the stored credentials.
# On the options rather than in memory because surviving a restart is the whole
# point: a client's failed-login budget dies with the process, so without this
# every restart would spend a fresh login on credentials already known to be
# refused - and three refused logins disable the panel's web accounts. Cleared
# by a reauthentication that works.
OPT_CREDENTIALS_REJECTED = "credentials_rejected"

# Repair issue translation keys. The issue id appends the entry id so two
# entries can be in the same state without sharing one issue.
ISSUE_HTTPS_REDIRECT = "https_redirect"
ISSUE_DUPLICATE_ENTRY = "duplicate_entry"
ISSUE_CREDENTIALS_REJECTED = "credentials_rejected"

DEFAULT_PARTITION = 1
DEFAULT_PORT_HTTP = 80
DEFAULT_PORT_HTTPS = 443
# Even when the login/tuxedoapi.html pages tolerate plain HTTP, the actual
# /system_http_api/API_REV01/... command endpoints get 302-redirected to
# HTTPS whenever "Secured Web Server Access (HTTPS)" is enabled on the unit
# (the common/default state) - so default to HTTPS here even though the
# sibling Hubitat driver in heidrickla/Hubitat defaults to HTTP.
DEFAULT_USE_HTTPS = True

API_REV = "API_REV01"
API_BASE_PATH = f"/system_http_api/{API_REV}"
LOGIN_PATH = "/authenticated/index.html"
KEYS_PATH = "/tuxedoapi.html"

# The push stream, which is where the state actually comes from. The slash
# before "G." is required: without it the server answers 404. Nothing else is
# needed - no token, no query string, just the session cookie.
PUSH_PATH = "/SimpleDebugger.interface/G."
PUSH_BOUNDARY = "EH912ZZ"
PUSH_CONNECT_TIMEOUT = 15
# No total timeout applies to the stream at all - the point of the request is
# to stay open. This is how long silence may last before the connection is
# treated as dead and reopened. An idle panel is NOT silent: it repeats the
# partition status on its own timer roughly every 33 seconds (measured over a
# five-minute hold, 81 frames, last at t+296 s), so a gap this long is three
# missed refreshes and means the socket, not the house, has gone quiet.
PUSH_READ_TIMEOUT = 90
PUSH_BACKOFF_INITIAL = 5.0
# Reconnecting costs the panel nothing measurable - six connect/disconnect
# cycles on one session left noOfClient at 1 every time and the session
# usable - so the ceiling is about not hammering a panel that is down, not
# about protecting a scarce slot.
PUSH_BACKOFF_MAX = 300.0

# How long a command waits for the panel to report the change on the stream
# before falling back to a poll. Arming pushes its first exit-delay frame
# within seconds; the entity's PARALLEL_UPDATES = 1 holds other calls to it
# for at most this long, so it is a bound rather than a target.
COMMAND_CONFIRM_TIMEOUT = 8.0

# Where a status came from, on TuxedoStatus.source.
SOURCE_STREAM = "stream"
SOURCE_POLL = "poll"
# Neither source reported the change, so the entity shows what was asked for
# rather than a state the panel never confirmed.
SOURCE_ASSUMED = "assumed"

# The fallback poll, and the initial sync before the stream is open.
SCAN_INTERVAL = timedelta(seconds=30)


def issue_id(key: str, entry_id: str) -> str:
    """A repair issue id: the condition, then the entry it is about."""
    return f"{key}_{entry_id}"


# Tuxedo panel status strings we know about, and the alarm state each one
# means. The values are Home Assistant's own AlarmControlPanelState values as
# plain text: alarm_control_panel.py turns them into the enum, and keeping the
# map here lets the coordinator - which must not import a platform module -
# ask whether a display text names a state at all.
#
# One map serves BOTH sources. Only "Ready To Arm" and the exit-delay
# countdown have been captured on the push stream; the armed spellings below
# are GetSecurityStatus's, and the stream is ASSUMED to match them - an
# assumption, not an observation. Nothing rests on it: a streamed text this
# map does not know settles nothing, so the coordinator lets the poll through
# to name the mode rather than guessing. See
# ../../../docs/tuxedo_touch_api_notes.md, "The push stream", for where the
# list came from and which texts each source has actually been seen to spell.
STATUS_STATES: dict[str, str] = {
    "Ready To Arm": "disarmed",
    "Ready Fault": "disarmed",
    "Not Ready": "disarmed",
    "Not Ready Fault": "disarmed",
    "Armed Stay": "armed_home",
    "Armed Stay Fault": "armed_home",
    "Armed Away": "armed_away",
    "Armed Away Fault": "armed_away",
    "Armed Night": "armed_night",
    "Armed Night Fault": "armed_night",
    "Armed Instant": "armed_night",
    "Armed Instant Fault": "armed_night",
    "Armed Instant Alarm": "triggered",
    "Entry Delay Active": "pending",
    "Not Ready Alarm": "triggered",
    "Armed Stay Alarm": "triggered",
    "Armed Night Alarm": "triggered",
    "Armed Away Alarm": "triggered",
}

# The exit-delay countdown as the panel spells it, double space included.
# Matching it loosely would also swallow statuses that are not a countdown.
COUNTDOWN_RE = re.compile(r"^(\d+)\s+Secs Remaining$")

# What the panel's status cache answers with while it is empty. It is a
# failed read rather than a state, and it cannot appear on the push stream.
STATUS_NOT_AVAILABLE = "Not available"


def status_names_a_state(text: str) -> bool:
    """Whether a display text says what the partition is doing, on its own.

    False for a text no firmware anyone has watched produces. It matters
    because the two sources are not equal: the stream's armed flag says
    whether the partition is armed but never in which mode, so a stream text
    that fails this leaves the mode unsettled and the poll's own reading is
    what disambiguates it.
    """
    stripped = text.strip()
    return stripped in STATUS_STATES or COUNTDOWN_RE.match(stripped) is not None
