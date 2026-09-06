"""Decoding the panel's push frames, byte for byte.

The two payloads asserted here are the ones captured from the panel across a
live arm/disarm cycle (iot-protocol-tools/TUXEDO-HA-ENRICHMENT.md, "The push
frame format, decoded byte-exact"), including the raw 0xFE/0xFF state byte
that makes the latin-1 decoding load-bearing.
"""

import pytest

from tests.no_ha import load

push = load("push")

# Exactly what the stream carries, as bytes, so nothing here can quietly
# depend on a str that was already decoded the right way.
READY_BYTES = b"0:21:1:fe:\xfe1Ready To Arm:2"
ARMING_BYTES = b"0:21:1:ff:\xff259  Secs Remaining:2"


def frame(payload: bytes) -> str:
    """One statusMessageText part, decoded the way the stream reader does."""
    return (
        b"['ud','SimpleDbgServer2ClientIntf','statusMessageText',[\"" + payload + b'"]]'
    ).decode("latin-1")


# ------------------------------------------------------------------ payloads


def test_the_ready_payload_decodes_to_a_disarmed_green_status():
    status = push.decode_status_frame(READY_BYTES.decode("latin-1"))
    assert status is not None
    assert status.cmd == 21
    assert status.panel_status_code == 1
    assert status.armed is False
    assert status.colour == "green"
    assert status.text == "Ready To Arm"
    assert status.seconds_remaining is None


def test_the_arming_payload_carries_the_countdown_and_reads_red():
    """The digit after the flag byte is the colour, not part of the number:
    read it as part of the number and a 59-second exit delay becomes 259."""
    status = push.decode_status_frame(ARMING_BYTES.decode("latin-1"))
    assert status is not None
    assert status.armed is True
    assert status.colour == "red"
    assert status.text == "59  Secs Remaining"
    assert status.seconds_remaining == 59


def test_utf8_destroys_the_state_flag():
    """The positive control for the latin-1 rule: same bytes, wrong codec.

    utf-8 cannot represent 0xFE at all, so the flag becomes U+FFFD, the
    field carrying the display text can no longer be found, and the frame
    decodes to nothing instead of decoding wrongly.
    """
    mangled = READY_BYTES.decode("utf-8", "replace")
    assert push.decode_status_frame(mangled) is None
    assert push.decode_status_frame(READY_BYTES.decode("latin-1")) is not None


def test_an_unsolicited_update_carries_no_status_code_or_colour():
    """cmd -1, and the text follows the flag byte with nothing between.

    Field 2 is the display text here rather than a status code, so there is
    no code to read - and this frame's own `-1` sits in field 1, the command
    id, where it must not be mistaken for the dead-link marker.
    """
    status = push.decode_status_frame(b"0:-1:\xfeReady To Arm".decode("latin-1"))
    assert status is not None
    assert status.cmd == -1
    assert status.panel_status_code is None
    assert status.link_down is False
    assert status.colour is None
    assert status.text == "Ready To Arm"


def test_a_dead_ecp_link_is_carried_as_status_code_minus_one():
    """The defect 0.4.2 fixes, at the layer that decodes it.

    CReceiverThread::sltSendChangedPartitionStatus calls PanelIsTalking() and,
    when it answers 0, stores -1 into this field and sends the frame anyway
    (`mvneq r3, #0` then `streq r3, [sp, #8]` at 0x144a84, with the
    osal_MqSend at 0x144aa0 reached on both paths). A -1 here is the Tuxedo
    saying it has lost the ECP link to the VISTA, so the text beside it is
    stale by construction.
    """
    status = push.decode_status_frame(
        b"0:21:-1:fe:\xfe1Ready To Arm:2".decode("latin-1")
    )
    assert status is not None
    assert status.panel_status_code == -1
    assert status.link_down is True
    # Still decoded in full: what to do about the link is the coordinator's
    # decision, and the frame is not silently dropped on the way there.
    assert status.text == "Ready To Arm"
    assert status.armed is False


def test_an_ordinary_status_code_is_not_a_dead_link():
    """The positive control for the test above, on the same axis.

    An authenticated GET /eventhandler.html taken at the same moment as a
    frame answered `curStatus = "21:a1Ready To Arm:1"`, whose trailing value
    that page names panelStatusCode. A talking panel puts a non-negative
    number in this field; only PanelIsTalking() answering 0 produces -1.
    """
    status = push.decode_status_frame(
        b"0:21:1:fe:\xfe1Ready To Arm:2".decode("latin-1")
    )
    assert status is not None
    assert status.panel_status_code == 1
    assert status.link_down is False


def test_a_status_code_field_that_is_not_a_number_decodes_to_no_code():
    """str.isdigit() and int() are not the same question.

    The latin-1 superscripts 0xB9/0xB2/0xB3 satisfy isdigit() and raise on
    int(), and latin-1 is how this stream MUST be decoded - the state flag is
    a raw byte, so those characters are exactly what this decoder can see.
    Guarding the conversion with a different predicate put an unhandled
    ValueError inside the read loop, where nothing in async_run's except list
    catches it: one such frame ended the stream task for the life of the
    entry while the log and the diagnostics both said it was reconnecting.

    An unreadable code is not a dead link: `link_down` stays False, because
    reporting the ECP link down on a field that could not be parsed would
    take the alarm entity unavailable on a decoding fault.
    """
    status = push.decode_status_frame(
        b"0:21:1\xb2:fe:\xfe1Ready To Arm:2".decode("latin-1")
    )
    assert status is not None
    assert status.panel_status_code is None
    assert status.link_down is False
    assert status.text == "Ready To Arm"


@pytest.mark.parametrize(
    "payload",
    [
        "0:504:1:P1  H:1:0:3:3",  # registration/initial data: no flag byte
        "0:18:1 P1  H:2",  # home partition
        "0:21",  # too short to be anything
        "",
        "0:notanumber:1:fe:x",  # command id that is not a number
        b"0:21:1:fd:\xfd1Ready To Arm:2".decode("latin-1"),  # unknown flag
    ],
)
def test_a_payload_without_a_partition_status_decodes_to_nothing(payload):
    assert push.decode_status_frame(payload) is None


def test_the_armed_text_is_carried_through_as_the_panel_spells_it():
    """A text arriving with the armed flag is passed on exactly as spelled.

    The spelling used here is GetSecurityStatus's. Only `Ready To Arm` and the
    countdown have been captured on a real stream, and the stream matching the
    poll's armed spellings is an assumption, not an observation
    (docs/tuxedo_touch_api_notes.md, "Which display texts have actually been
    seen on the stream"). What this pins is the decoder's pass-through, which
    is what lets one status map serve both sources if the assumption holds -
    and if it does not, the flag still says armed and the poll names the mode.
    """
    status = push.decode_status_frame(b"0:21:1:ff:\xff2Armed Stay:2".decode("latin-1"))
    assert status is not None
    assert status.armed is True
    assert status.text == "Armed Stay"


# ------------------------------------------------------------------ decoder


def test_a_frame_split_across_reads_is_emitted_once_it_is_whole():
    decoder = push._FrameDecoder()
    whole = frame(READY_BYTES)
    assert decoder.feed(whole[:20]) == []
    assert decoder.feed(whole[20:]) == [whole]


def test_two_frames_in_one_read_come_out_in_order():
    decoder = push._FrameDecoder()
    first, second = frame(READY_BYTES), frame(ARMING_BYTES)
    parts = decoder.feed(f"--EH912ZZ\r\n{first}--EH912ZZ\r\n{second}")
    assert parts == [first, second]
    assert decoder.parts == 2


def test_the_setcid_frame_is_a_frame_too():
    decoder = push._FrameDecoder()
    assert decoder.feed("--EH912ZZ\r\n['setCid', 7]") == ["['setCid', 7]"]


def test_a_stream_of_nothing_that_parses_does_not_grow_the_buffer_forever():
    decoder = push._FrameDecoder()
    decoder.feed("x" * (push.MAX_BUFFER + 1))
    assert decoder._buffer == ""
    # And it still works afterwards.
    assert decoder.feed(frame(READY_BYTES)) != []


def test_the_boundary_is_taken_from_the_content_type_when_the_panel_names_one():
    assert push._boundary_of('multipart/x-mixed-replace; boundary="EH912ZZ"') == (
        "EH912ZZ"
    )
    assert push._boundary_of("multipart/x-mixed-replace; boundary=OTHER") == "OTHER"
    assert push._boundary_of("multipart/x-mixed-replace") == push.PUSH_BOUNDARY
    assert push._boundary_of(None) == push.PUSH_BOUNDARY


def test_the_reconnect_wait_doubles_up_to_the_ceiling():
    assert push.next_backoff(5.0) == 10.0
    assert push.next_backoff(push.PUSH_BACKOFF_MAX) == push.PUSH_BACKOFF_MAX
    assert push.next_backoff(push.PUSH_BACKOFF_MAX / 2 + 1) == push.PUSH_BACKOFF_MAX


# Every distinct payload shape seen in a 49-frame, 150 s capture of an idle
# disarmed panel on port 80. Two of them are the id -1 record, which accounts
# for 21 of those 49 frames and comes in TWO shapes - and 0.4.2 is the first
# version to apply id -1 at all, so mis-reading one is a live fault rather
# than a theoretical one.
#
# The trap this pins: the literal ":fe:"/":ff:" TEXT field appears ONLY on
# id 21 frames, 6 of the 49. The raw 0xFE/0xFF byte before the display text
# appears on id 21 AND id -1. A decoder keyed on the text field would ignore
# most of the frames that already carry the state; one keyed on field
# position or field count would read the partition shape's text as status.
# This decoder scans for the raw byte instead, which is why both work.
WIRE_SHAPES = [
    # (label, payload bytes, expected cmd, code, armed, text, colour)
    (
        "id 21, text field AND raw byte",
        b"0:21:1:fe:\xfe1Ready To Arm:2",
        21,
        1,
        False,
        "Ready To Arm",
        "green",
    ),
    (
        "id 21 arming, raw 0xFF",
        b"0:21:1:ff:\xff259  Secs Remaining:2",
        21,
        1,
        True,
        "59  Secs Remaining",
        "red",
    ),
    (
        "id -1 status, RAW BYTE ONLY",
        b"0:-1:\xfe1Ready To Arm",
        -1,
        None,
        False,
        "Ready To Arm",
        "green",
    ),
]

# Shapes that carry no partition status at all and must decode to None rather
# than be forced into one. The 8-field id -1 record is the dangerous one: it
# shares an id with the status shape above and differs only in layout.
NO_STATUS_SHAPES = [
    ("id -1 partition shape, 8 fields, no flag byte", b"0:-1:1:P1  H:1:0:3:3"),
    ("id 18 home partition, no flag byte", b"0:18:1 P1  H:2"),
    ("id 504 registration data", b"0:504:1:P1  H:1:0:3:3"),
]


@pytest.mark.parametrize(
    ("label", "raw", "cmd", "code", "armed", "text", "colour"), WIRE_SHAPES
)
def test_every_captured_wire_shape_decodes(label, raw, cmd, code, armed, text, colour):
    """Each shape the panel was actually seen to send decodes correctly."""
    status = push.decode_status_frame(raw.decode("latin-1"))
    assert status is not None, f"{label}: decoded to nothing"
    assert status.cmd == cmd, label
    assert status.panel_status_code == code, label
    assert status.armed is armed, label
    assert status.text == text, label
    assert status.colour == colour, label


@pytest.mark.parametrize(("label", "raw"), NO_STATUS_SHAPES)
def test_a_frame_without_a_flag_byte_carries_no_status(label, raw):
    """No flag byte means no partition status, whatever the id says.

    The id -1 record repeats whichever payload type last changed, so the same
    id arrives as an 8-field partition record and as a 3-field status record.
    Reading the first as a status would put partition text where the alarm
    state belongs.
    """
    assert push.decode_status_frame(raw.decode("latin-1")) is None, label
