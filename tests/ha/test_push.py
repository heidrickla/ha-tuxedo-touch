"""The push stream as Home Assistant sees it, against a fake panel.

A real HTTP server on 127.0.0.1 speaking the panel's own wire format - the
login handshake, the encrypted API and the multipart stream - so what is
exercised is the entry setting up, the coordinator choosing between its two
sources, the entity following, and the entry unloading. Nothing here talks to
a panel.
"""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.exceptions import HomeAssistantError

from custom_components.tuxedo_touch.api import TuxedoTouchError
from tests.fake_panel import COUNTDOWN_FRAME, status_frame, wait_until

PANEL = "alarm_control_panel.honeywell_tuxedo_touch_partition_1"
STATUS = "custom_components.tuxedo_touch.api.TuxedoTouchClient.get_status"
CONFIRM_TIMEOUT = "custom_components.tuxedo_touch.coordinator.COMMAND_CONFIRM_TIMEOUT"


async def _setup(hass, entry, *, wait_for_stream=True):
    """Load the entry against the fake panel and return its coordinator."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    if wait_for_stream:
        await wait_until(lambda: coordinator.push.connected)
    return coordinator


def _state(hass):
    return hass.states.get(PANEL)


def _stream_tasks():
    """Every live task the coordinator started for its stream, by name."""
    return [
        task
        for task in asyncio.all_tasks()
        if "push stream" in task.get_name() and not task.done()
    ]


async def test_the_entry_loads_and_the_stream_becomes_the_state_source(
    hass, fake_panel, panel_entry
):
    """The poll is the initial sync; from then on the panel's own report is
    what the entity shows, and it says so in `tuxedo_source`."""
    coordinator = await _setup(hass, panel_entry)
    assert panel_entry.state is ConfigEntryState.LOADED
    # The first refresh was a poll, which is what proves the credentials.
    assert fake_panel.polls == 1
    assert _state(hass).attributes["tuxedo_source"] == "poll"

    await fake_panel.push_status_text("Armed Stay", armed=True)
    await wait_until(lambda: _state(hass).state == "armed_home")
    reported = _state(hass)
    assert reported.attributes["tuxedo_source"] == "stream"
    assert reported.attributes["tuxedo_status"] == "Armed Stay"
    assert reported.attributes["tuxedo_color"] == "red"
    assert coordinator.push.connection_id == 7


async def test_both_sources_report_the_colour_in_one_vocabulary(
    hass, fake_panel, panel_entry
):
    """The panel spells its own colour `Green`; the stream sends a digit this
    integration names in lower case. The attribute has to read the same either
    way, or a template comparing it to `green` stops matching every time the
    source changes - at setup, at a stream drop, and for good on firmware that
    has no stream."""
    await _setup(hass, panel_entry)
    # What the panel itself sent, before the client read it.
    assert fake_panel.colour == "Green"
    assert _state(hass).attributes["tuxedo_source"] == "poll"
    assert _state(hass).attributes["tuxedo_color"] == "green"

    await fake_panel.push_status_text("Ready To Arm", armed=False)
    await wait_until(lambda: _state(hass).attributes["tuxedo_source"] == "stream")
    assert _state(hass).attributes["tuxedo_color"] == "green"


async def test_the_exit_delay_countdown_is_exposed_while_it_runs(
    hass, fake_panel, panel_entry
):
    """A 30 s poll would miss the countdown entirely; the stream pushes it
    once a second and the seconds left are an attribute of the entity."""
    await _setup(hass, panel_entry)

    await fake_panel.push(COUNTDOWN_FRAME)
    await wait_until(lambda: _state(hass).state == "arming")
    assert _state(hass).attributes["arming_seconds_remaining"] == 59

    await fake_panel.push_status_text("Armed Stay", armed=True)
    await wait_until(lambda: _state(hass).state == "armed_home")
    assert _state(hass).attributes["arming_seconds_remaining"] is None


async def test_a_poll_does_not_write_over_what_the_stream_reported(
    hass, fake_panel, panel_entry
):
    """The poll reads the status cache, which can be stale or empty at a
    moment the stream knows exactly what the partition is doing - so while
    the stream is delivering, the poll only proves the panel is answering."""
    coordinator = await _setup(hass, panel_entry)
    await fake_panel.push_status_text("Armed Away", armed=True)
    await wait_until(lambda: _state(hass).state == "armed_away")

    fake_panel.status = "Not available"
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert _state(hass).state == "armed_away"
    assert _state(hass).attributes["tuxedo_source"] == "stream"


async def test_a_streamed_text_the_map_does_not_know_leaves_the_poll_to_settle_it(
    hass, fake_panel, panel_entry
):
    """The stream's flag says whether the partition is armed but never in
    which mode. A display text outside the map therefore settles nothing, and
    the poll - which is the only other thing that names a mode - is let
    through instead of being suppressed as usual."""
    coordinator = await _setup(hass, panel_entry)

    await fake_panel.push(status_frame("Armed With Some New Word", armed=True))
    await wait_until(lambda: coordinator.data.status == "Armed With Some New Word")
    # Armed, but not a mode: unknown rather than a guess.
    assert _state(hass).state == "unknown"

    fake_panel.status = "Armed Away"
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass).state == "armed_away"
    assert _state(hass).attributes["tuxedo_source"] == "poll"

    # And it STAYS settled. The panel repeats its partition status on its own
    # timer roughly every 33 s, so the correction above used to last about
    # three seconds in every thirty-three: the repeat wrote the unrecognised
    # text back over the poll's answer and the entity read `unknown` again -
    # and, because writing a pushed status restarts the 30 s poll clock, the
    # poll that would have settled it a second time was pushed out past the
    # next repeat. The stream's flag corroborates the mode the poll named
    # rather than erasing it.
    frames_before = coordinator.push.frames
    await fake_panel.push(status_frame("Armed With Some New Word", armed=True))
    await wait_until(lambda: coordinator.push.frames > frames_before)
    await hass.async_block_till_done()

    assert _state(hass).state == "armed_away"
    assert _state(hass).attributes["tuxedo_status"] == "Armed Away"

    # And the poll keeps naming the mode, which is the other half of the
    # documented fallback. The mode standing here is the POLL's, not
    # anything the stream said, so suppressing the poll as "the stream is
    # carrying the state" would latch it: the panel could change mode and the
    # entity would go on reporting the one the poll last saw.
    fake_panel.status = "Armed Stay"
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass).state == "armed_home"
    assert _state(hass).attributes["tuxedo_source"] == "poll"


async def test_the_entity_is_available_while_either_source_works(
    hass, fake_panel, panel_entry
):
    """Unavailable only when both are down. A failing poll while the stream
    is up is not an outage of anything - and this is the whole point of the
    release: the placeholder the poll can answer with cannot reach the
    stream."""
    coordinator = await _setup(hass, panel_entry)
    assert _state(hass).state != "unavailable"

    with patch(STATUS, side_effect=TuxedoTouchError("panel busy")):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert not coordinator.last_update_success
        assert coordinator.push.connected
        assert _state(hass).state != "unavailable"

        # Now the stream goes too, and cannot come back.
        fake_panel.push_status = 500
        fake_panel.drop_stream()
        await wait_until(lambda: not coordinator.push.connected)
        await hass.async_block_till_done()
        assert _state(hass).state == "unavailable"


async def test_an_entry_loading_during_an_outage_never_shows_unknown(
    hass, fake_panel, panel_entry
):
    """The stream opening is not by itself something to show. An entity that
    is available with no status reads `unknown`, which is the state this
    release exists to remove; it stays unavailable until a status arrives."""
    fake_panel.status = "Not available"
    coordinator = await _setup(hass, panel_entry)
    assert coordinator.data is None
    assert coordinator.push.connected
    assert _state(hass).state == "unavailable"

    await fake_panel.push_status_text("Ready To Arm", armed=False)
    await wait_until(lambda: _state(hass).state == "disarmed")
    assert _state(hass).attributes["tuxedo_source"] == "stream"


async def test_the_stream_dropping_and_returning_is_logged_once_each_way(
    hass, fake_panel, panel_entry, caplog
):
    """log-when-unavailable: one line when the stream goes, one when it is
    back - not one per reconnect attempt."""
    coordinator = await _setup(hass, panel_entry)
    caplog.clear()

    fake_panel.drop_stream()
    await wait_until(lambda: not coordinator.push.connected)
    await wait_until(lambda: coordinator.push.connected)

    messages = [record.getMessage() for record in caplog.records]
    assert len([m for m in messages if "push stream dropped" in m]) == 1
    assert len([m for m in messages if "push stream is connected again" in m]) == 1


async def test_a_command_is_confirmed_by_the_stream_and_needs_no_poll(
    hass, fake_panel, panel_entry
):
    """Arm and disarm answer HTTP 200 with a zero-byte body; what they did
    arrives on the stream. Waiting for that is the confirmation, and no poll
    is needed to get it."""
    fake_panel.auto_push = True
    await _setup(hass, panel_entry)
    polls_before = fake_panel.polls

    await hass.services.async_call(
        ALARM_DOMAIN, "alarm_arm_home", {ATTR_ENTITY_ID: PANEL}, blocking=True
    )
    await hass.async_block_till_done()

    assert fake_panel.commands == ["ArmWithCode"]
    assert fake_panel.polls == polls_before
    assert _state(hass).state == "arming"
    assert _state(hass).attributes["tuxedo_source"] == "stream"
    assert _state(hass).attributes["arming_seconds_remaining"] == 59


async def test_a_disarm_is_confirmed_by_the_stream(hass, fake_panel, panel_entry):
    fake_panel.auto_push = True
    fake_panel.status = "Armed Away"
    await _setup(hass, panel_entry)
    await fake_panel.push_status_text("Armed Away", armed=True)
    await wait_until(lambda: _state(hass).state == "armed_away")

    await hass.services.async_call(
        ALARM_DOMAIN, "alarm_disarm", {ATTR_ENTITY_ID: PANEL}, blocking=True
    )
    await hass.async_block_till_done()

    assert fake_panel.commands == ["DisarmWithCode"]
    assert _state(hass).state == "disarmed"
    assert _state(hass).attributes["tuxedo_source"] == "stream"


async def test_a_command_the_stream_never_reports_falls_back_to_a_poll(
    hass, fake_panel, panel_entry
):
    """The panel accepted the command and said nothing about it. A poll is
    the second rung of the ladder, and its answer is written through even
    though a pushed status is normally left in charge - the stream has just
    shown it is not carrying this change."""
    await _setup(hass, panel_entry)
    await fake_panel.push_status_text("Ready To Arm", armed=False)
    await wait_until(lambda: _state(hass).attributes["tuxedo_source"] == "stream")

    fake_panel.status = "Armed Stay"
    with patch(CONFIRM_TIMEOUT, 0.05):
        await hass.services.async_call(
            ALARM_DOMAIN, "alarm_arm_home", {ATTR_ENTITY_ID: PANEL}, blocking=True
        )
        await hass.async_block_till_done()

    assert fake_panel.commands == ["ArmWithCode"]
    assert _state(hass).state == "armed_home"
    assert _state(hass).attributes["tuxedo_source"] == "poll"


async def test_a_poll_that_reports_a_countdown_confirms_an_arm(
    hass, fake_panel, panel_entry
):
    """The poll carries display text and no flag, so an exit-delay countdown
    is how it says "arming" - and that confirms an arm as much as an armed
    mode does."""
    await _setup(hass, panel_entry)

    fake_panel.status = "59  Secs Remaining"
    with patch(CONFIRM_TIMEOUT, 0.05):
        await hass.services.async_call(
            ALARM_DOMAIN, "alarm_arm_away", {ATTR_ENTITY_ID: PANEL}, blocking=True
        )
        await hass.async_block_till_done()

    assert _state(hass).state == "arming"
    # The poll confirmed it, so nothing was assumed.
    assert _state(hass).attributes["tuxedo_source"] == "poll"


async def test_the_stream_ends_a_not_available_outage(
    hass, fake_panel, panel_entry, caplog
):
    """The placeholder cannot appear on the stream, so a status arriving
    there ends the outage the poll was in - and closes off the log line that
    opened it, rather than leaving one that never gets its pair."""
    coordinator = await _setup(hass, panel_entry)
    fake_panel.status = "Not available"
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.last_update_success

    caplog.clear()
    await fake_panel.push_status_text("Armed Stay", armed=True)
    await wait_until(lambda: _state(hass).state == "armed_home")

    messages = [record.getMessage() for record in caplog.records]
    assert [m for m in messages if "on its push stream" in m]


async def test_a_command_neither_source_reports_is_marked_assumed(
    hass, fake_panel, panel_entry
):
    """The command itself succeeded and nothing could say what it did.

    This is the only case the assumed rung is for: the poll answered with a
    text that names no state, so it neither confirmed the command nor refuted
    it. The stream's flag would settle whether the partition is armed, but no
    frame arrived - so the entity shows what was asked for, labelled assumed.

    The status here has to be one the map does not know. A poll answering
    "Ready To Arm" after an arm is a refutation, not a silence, and this test
    used to use exactly that - asserting the entity showed armed_away while
    the panel was reporting itself disarmed.
    """
    await _setup(hass, panel_entry)

    fake_panel.status = "Armed With Some New Word"
    with patch(CONFIRM_TIMEOUT, 0.05):
        await hass.services.async_call(
            ALARM_DOMAIN, "alarm_arm_away", {ATTR_ENTITY_ID: PANEL}, blocking=True
        )
        await hass.async_block_till_done()

    assert _state(hass).state == "armed_away"
    assert _state(hass).attributes["tuxedo_source"] == "assumed"


async def test_an_arm_the_poll_refutes_is_not_reported_as_done(
    hass, fake_panel, panel_entry
):
    """A refused arm is the ordinary one: a faulted zone.

    Arm and disarm answer HTTP 200 with a zero-byte body whether or not the
    panel acted, so "the command succeeded" is not evidence of anything. The
    poll that follows is the only evidence there is, and a poll that reports
    the partition in the opposite state is a refutation - not the silence the
    assumed rung was written for. Writing the assumed status over it put
    armed_away on a disarmed house, fired a state-change event, and returned
    success to the caller.
    """
    fake_panel.status = "Not Ready Fault"
    await _setup(hass, panel_entry)
    polls_before = fake_panel.polls

    with patch(CONFIRM_TIMEOUT, 0.05), pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            ALARM_DOMAIN, "alarm_arm_away", {ATTR_ENTITY_ID: PANEL}, blocking=True
        )
    await hass.async_block_till_done()

    # The poll ran and succeeded; its answer is what stands.
    assert fake_panel.polls > polls_before
    assert fake_panel.commands == ["ArmWithCode"]
    assert _state(hass).state == "disarmed"
    assert _state(hass).attributes["tuxedo_status"] == "Not Ready Fault"
    assert _state(hass).attributes["tuxedo_source"] == "poll"
    assert "Not Ready Fault" in str(raised.value)


async def test_a_disarm_the_poll_refutes_is_not_reported_as_done(
    hass, fake_panel, panel_entry
):
    """The mirror image, and the dangerous direction.

    A disarm the panel will not carry out - a code it does not accept, a
    partition that will not disarm - used to leave the entity reading
    disarmed on an armed house, which is what any "when the alarm disarms,
    unlock the doors" automation acts on.
    """
    fake_panel.status = "Armed Away"
    coordinator = await _setup(hass, panel_entry)
    await fake_panel.push_status_text("Armed Away", armed=True)
    await wait_until(lambda: _state(hass).state == "armed_away")

    with patch(CONFIRM_TIMEOUT, 0.05), pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            ALARM_DOMAIN, "alarm_disarm", {ATTR_ENTITY_ID: PANEL}, blocking=True
        )
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert fake_panel.status == "Armed Away"
    assert _state(hass).state == "armed_away"
    assert _state(hass).attributes["tuxedo_source"] == "poll"


async def test_a_status_for_another_partition_is_not_this_entry(
    hass, fake_panel, panel_entry
):
    """The stream carries every partition; this entry is partition 1."""
    coordinator = await _setup(hass, panel_entry)
    await fake_panel.push_status_text("Ready To Arm", armed=False)
    await wait_until(lambda: coordinator.data.source == "stream")

    await fake_panel.push(status_frame("Armed Away", armed=True, partition=2))
    await wait_until(lambda: coordinator.push.frames >= 3)
    await hass.async_block_till_done()

    assert _state(hass).state == "disarmed"


async def test_a_status_naming_no_partition_is_not_taken_as_this_entry(
    hass, fake_panel, panel_entry
):
    """A frame that does not say which partition it is about is not evidence
    about any particular one.

    The unsolicited record (command id -1) carries no partition field at all,
    and the filter used to read "no partition" as "mine": every entry on the
    panel accepted it, whatever partition it was configured for. On a
    two-partition install that puts one partition's state on the other's
    entity - and then latches it, because a pushed status that names a state
    suppresses the poll that would have corrected it. It could satisfy a
    pending command's confirmation too, so an arm sent to partition 2 could
    be confirmed by a frame about partition 1.

    Command 21 carries every real change and the panel repeats it about every
    33 seconds, so nothing is lost by requiring the frame to name the entry
    it is applied to.
    """
    coordinator = await _setup(hass, panel_entry)
    await fake_panel.push_status_text("Armed Away", armed=True)
    await wait_until(lambda: _state(hass).state == "armed_away")
    frames_before = coordinator.push.frames

    await fake_panel.push(
        b"['ud','SimpleDbgServer2ClientIntf','statusMessageText',"
        b'["0:-1:\xfeReady To Arm"]]'
    )
    await wait_until(lambda: coordinator.push.frames > frames_before)
    await hass.async_block_till_done()

    # The frame was read and then ignored, not missed.
    assert _state(hass).state == "armed_away"
    assert _state(hass).attributes["tuxedo_status"] == "Armed Away"


async def test_unloading_closes_the_stream_and_leaves_nothing_running(
    hass, fake_panel, panel_entry
):
    """The stream request never ends by itself, so nothing short of
    cancelling its task closes it - and the unload awaits that, which is what
    lets the reconfigure flow treat a returned unload as a free panel."""
    coordinator = await _setup(hass, panel_entry)
    assert fake_panel.stream_open.is_set()
    assert _stream_tasks()

    assert await hass.config_entries.async_unload(panel_entry.entry_id)
    await hass.async_block_till_done()

    assert panel_entry.state is ConfigEntryState.NOT_LOADED
    assert coordinator.push.connected is False
    # Not "cancellation was requested": the task is gone by the time the
    # unload returns, which is what makes the panel actually free.
    assert _stream_tasks() == []
    assert coordinator.session.closed

    # And it does not come back: nothing is left to reconnect.
    requests = fake_panel.stream_requests
    await asyncio.sleep(0.1)
    assert fake_panel.stream_requests == requests


async def test_a_refused_login_ends_the_stream_task_rather_than_slowing_it(
    hass, fake_panel, panel_entry
):
    """The defect this release exists to keep out of 0.4.0.

    Nothing but an unload ever cancelled this task, and a rejected poll does
    not unload the entry: Home Assistant records the failure, starts a reauth
    flow and leaves everything else running. So a web password changed at the
    keypad left this loop re-running the whole login handshake - a login page
    GET and a credential POST - roughly twelve times an hour, indefinitely,
    against a panel that disables every web account after three failed
    logins.

    Now the loop ends. Not a longer wait: an end, one attempt spent and the
    task gone.
    """
    coordinator = await _setup(hass, panel_entry)
    assert fake_panel.login_attempts == 1

    # The password changes at the keypad, and the panel forgets the cookie
    # the stream is holding - what a session dying under it looks like.
    fake_panel.password = "changed at the keypad"
    fake_panel.expire_session()
    # A reconnect would otherwise wait out the five-second floor first.
    coordinator.push.reconnect_wait = 0.01
    fake_panel.drop_stream()

    await wait_until(lambda: not _stream_tasks())

    assert coordinator.push.auth_failed is True
    assert coordinator.push.connected is False
    # One attempt: it logged in once on the reconnect, was refused, stopped.
    assert fake_panel.login_attempts == 2
    requests = fake_panel.stream_requests
    await asyncio.sleep(0.2)
    assert fake_panel.login_attempts == 2
    assert fake_panel.stream_requests == requests


async def test_a_key_page_hiccup_does_not_stop_the_stream_for_good(
    hass, fake_panel, panel_entry
):
    """The other half of the same misclassification, on the primary source.

    A cookie dying under the stream is routine: the reconnect logs in again.
    Here the panel ACCEPTS that login and the key page behind it answers 500
    once. That used to raise the same exception class as a refused password,
    so the stream set its terminal auth_failed flag and stopped for the life
    of the entry - one transient HTTP 500 killing both state sources at once.

    Now it is an ordinary fault: back off, reconnect, carry on.
    """
    coordinator = await _setup(hass, panel_entry)
    logins_before = fake_panel.logins

    fake_panel.expire_session()
    fake_panel.keys_status = 500
    fake_panel.keys_failures_left = 1
    # Otherwise the reconnect waits out the five-second floor first.
    coordinator.push.reconnect_wait = 0.01
    fake_panel.drop_stream()

    await wait_until(lambda: not coordinator.push.connected)
    await wait_until(lambda: coordinator.push.connected)

    assert coordinator.push.auth_failed is False
    # It logged in twice: once for the key page that failed, once for the
    # reconnect that worked. The panel accepted both.
    assert fake_panel.logins >= logins_before + 2
    assert _stream_tasks()

    await fake_panel.push_status_text("Armed Away", armed=True)
    await wait_until(lambda: _state(hass).state == "armed_away")


async def test_firmware_without_a_push_stream_runs_on_the_poll_alone(
    hass, fake_panel, panel_entry
):
    """A 404 is permanent, so the stream stops asking and the entity is
    carried by the poll - the pre-0.4.0 behaviour, quirk and all."""
    fake_panel.push_status = 404
    coordinator = await _setup(hass, panel_entry, wait_for_stream=False)
    await wait_until(lambda: coordinator.push.unsupported)

    assert fake_panel.stream_requests == 1
    assert _state(hass).state == "disarmed"
    assert _state(hass).attributes["tuxedo_source"] == "poll"

    fake_panel.status = "Armed Night"
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _state(hass).state == "armed_night"


@pytest.mark.parametrize(
    ("text", "armed", "expected"),
    [
        ("Ready To Arm", False, "disarmed"),
        ("Armed Stay", True, "armed_home"),
        ("Armed Away", True, "armed_away"),
        ("Armed Night", True, "armed_night"),
        ("Armed Instant", True, "armed_night"),
        ("Armed Away Alarm", True, "triggered"),
        ("Entry Delay Active", False, "pending"),
    ],
)
async def test_each_mode_the_map_knows_is_taken_from_a_streamed_text(
    hass, fake_panel, panel_entry, text, armed, expected
):
    """One status map serves both sources, so a text the map knows must reach
    the entity as that state whichever source carried it.

    Only `Ready To Arm` and the countdown have been captured on a real
    stream; the armed spellings here are GetSecurityStatus's, and the fake
    spells them on the stream because the integration assumes the panel does
    (docs/tuxedo_touch_api_notes.md, "Which display texts have actually been
    seen on the stream"). This test pins the mapping, not the assumption -
    what happens when the assumption is wrong is the test above, where a text
    outside the map leaves the poll to settle the mode."""
    await _setup(hass, panel_entry)
    await fake_panel.push_status_text(text, armed=armed)
    await wait_until(lambda: _state(hass).state == expected)
