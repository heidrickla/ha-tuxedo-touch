"""Downloadable diagnostics."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_MAC, CONF_TUXWEB_TOKEN
from .coordinator import TuxedoTouchConfigEntry

# The host is on the user's LAN and the credentials open their alarm panel.
# The tuxweb token is a credential in exactly that sense.
REDACT = {
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_HOST,
    CONF_MAC,
    CONF_TUXWEB_TOKEN,
    "code",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TuxedoTouchConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    push = coordinator.push
    status = coordinator.data
    return {
        "config": async_redact_data(dict(entry.data), REDACT),
        # Which contract the panel answered to when it was asked at setup:
        # stock, or tuxweb with the capabilities it declared. Read before
        # anything else in the report, because the two fail differently - a
        # tuxweb entry has no login to be refused and no status cache to
        # answer "Not available" from, and its commands are confirmed or
        # failed rather than merely sent.
        "firmware": "tuxweb" if coordinator.client.tuxweb else "stock",
        "capabilities": sorted(coordinator.client.capabilities),
        # The partition this entry addresses on the REST poll and on every
        # arm/disarm command. NOT a filter on the push stream, which carries
        # no partition field and is scoped by the firmware to whichever
        # partition the panel is currently showing.
        "partition": coordinator.partition,
        # Where the stream connects. It belongs in the report rather than
        # being inferable from it: a relay-fed install fails in ways a
        # panel-fed one cannot - `connected` then says the relay is up and
        # says nothing about the panel - so several fields below read
        # differently once this is not "panel". The URL only; the push token
        # is a credential and is never reported.
        "push_source": push.source,
        # True once a configured relay has failed repeatedly. Distinct from
        # `connected` being false, which is also what a momentary reconnect
        # looks like: this says the relay has not been there for a run of
        # attempts and has been reported in the log.
        "relay_unreachable": push.relay_unreachable,
        # The one condition in which both sources answer and neither can be
        # believed: the Tuxedo has lost its ECP link to the VISTA, so its
        # frames carry the panel-status code -1 beside the text it last drew,
        # and the poll reads a cache nothing is refilling. True here is the
        # whole explanation for an alarm entity that is unavailable while
        # `last_update_success` and `push.connected` below are both true.
        "ecp_link_down": coordinator.ecp_link_down,
        # The other half of the panel's health: whether the VISTA's last
        # status came as a command-22 record - the panel reporting ITSELF as
        # not online - and the online byte that record carried (2..4, or -1
        # when the link was down at the same time; null while online). A
        # different fact from the link above: the Tuxedo may hear the panel
        # perfectly well while the panel says it is busy or downloading.
        "panel_offline": coordinator.panel_offline,
        "panel_offline_code": coordinator.panel_offline_code,
        # The fallback poll: its interval, and whether it last worked.
        "update_interval": str(coordinator.update_interval),
        "last_update_success": coordinator.last_update_success,
        # Which source produced the status below, and the strings the panel
        # spelled it with. `source` first, because the two sources fail
        # differently and the rest of the report cannot be read without
        # knowing which was speaking: `stream` is the panel reporting itself,
        # `poll` the fallback status read, `assumed` a command the panel
        # accepted that neither source has confirmed yet, `command` one that
        # tuxweb confirmed in its reply before either source spoke.
        # The strings are the last good ones - a `Not available` answer fails
        # the poll rather than being stored. That answer is a firmware quirk
        # rather than a fault, and it is why a poll-only install can sit
        # unavailable while the panel is up; the stream cannot produce it, so
        # the block below is what says whether that is the situation.
        "status_source": status.source if status else None,
        "panel_status": status.status if status else None,
        "panel_color": status.color if status else None,
        # The keypad LCD as the stream last carried it, the raw record and
        # all: what the keypad display sensor is showing, or None while no
        # console record has arrived on this connection. Panel words, not
        # user data - a faulted zone's descriptor is the most personal thing
        # a line can hold, and it is the thing a report about the sensor
        # reading wrongly needs to show.
        "keypad_display": (
            coordinator.keypad_display.raw if coordinator.keypad_display else None
        ),
        # The primary source's own account of itself. `unsupported` is the
        # firmware having answered 404 - permanent, and the whole reason an
        # install would be back on the poll alone. `auth_failed` is the other
        # terminal state: the panel refused the credentials, so the stream
        # stopped rather than spend more failed logins on them, and a report
        # carrying it explains a stream that is down and staying down.
        # `stopped` is the third, and it says a bug rather than a panel: the
        # loop failed repeatedly for a reason the module does not name and
        # gave up, with `last_error` naming it. Without those two a report of
        # a task that has died reads exactly like one of a stream that is
        # briefly down and backing off, which is what it used to do.
        # `frames` separates a stream that is up and silent from one that
        # never carried anything, and `reconnect_wait` says how far a failing
        # one has backed off. None of it is user data, so none is redacted.
        "push": {
            "connected": push.connected,
            "unsupported": push.unsupported,
            "auth_failed": push.auth_failed,
            "stopped": push.stopped,
            "last_error": push.last_error,
            "connection_id": push.connection_id,
            "client_count": push.client_count,
            "frames": push.frames,
            "reconnect_wait": push.reconnect_wait,
        },
    }
