"""The decoder must match docs/wire-contract.json, case for case.

That file is the machine-readable statement of what the panel actually sends,
recorded from real hardware. It exists so anything else that has to speak this
protocol - a replacement panel server, another client - can be held to the same
behaviour rather than to someone's reading of it.

Keeping it honest is the point of this module. The contract is only worth
consuming if it is provably what the shipped decoder does, so every case is run
through the real decoder here. Change the file without changing the code, or
the code without the file, and this fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.no_ha import load

push = load("push")

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "docs" / "wire-contract.json").read_text(
        encoding="utf-8"
    )
)
CASES = CONTRACT["push_frames"]["cases"]


def _payload(case: dict) -> str:
    """The case's payload as the decoder receives it: latin-1 decoded."""
    return bytes.fromhex(case["payload_latin1_hex"]).decode("latin-1")


def _ids(cases: list[dict]) -> list[str]:
    return [c["label"][:60] for c in cases]


DECODING = [c for c in CASES if c.get("decodes")]
NOT_DECODING = [c for c in CASES if not c.get("decodes")]


def test_the_contract_has_cases_of_both_kinds():
    """A guard on the guard: an empty list would make every test below vacuous."""
    assert len(DECODING) >= 4, "contract lost its decodable cases"
    assert len(NOT_DECODING) >= 3, "contract lost its no-status cases"


@pytest.mark.parametrize("case", DECODING, ids=_ids(DECODING))
def test_a_contract_frame_decodes_as_the_contract_says(case):
    """Every field the contract states, checked against the real decoder."""
    status = push.decode_status_frame(_payload(case))
    assert status is not None, f"{case['label']}: decoded to nothing"
    assert status.cmd == case["cmd"]
    assert status.panel_status_code == case["panel_status_code"]
    assert status.armed is case["armed"]
    assert status.colour == case["colour"]
    assert status.text == case["text"]
    assert status.seconds_remaining == case["seconds_remaining"]
    if "link_down" in case:
        assert status.link_down is case["link_down"], (
            "the dead-ECP-link marker is the one field whose meaning cannot drift: "
            "it is how a panel that has stopped seeing the alarm becomes visible"
        )


@pytest.mark.parametrize("case", NOT_DECODING, ids=_ids(NOT_DECODING))
def test_a_contract_frame_without_a_status_decodes_to_nothing(case):
    """No flag byte means no partition status, whatever the command id says.

    The command -1 record is the trap: the same id arrives as an 8-field
    partition record and as a 3-field status record. Forcing the first into a
    status would put partition text where the alarm state belongs.
    """
    assert push.decode_status_frame(_payload(case)) is None, case["label"]


def test_the_contract_states_the_quirks_that_must_not_be_tidied():
    """A reimplementation that corrects these breaks every stock-firmware client.

    Listed rather than asserted loosely, because each one has already been
    somebody's reasonable-looking improvement.
    """
    quirks = " ".join(CONTRACT["quirks_to_preserve"])
    assert "Sucess" in quirks, "the misspelling is part of the contract"
    assert "Result.Response" in quirks or "Result.Response" in json.dumps(CONTRACT)
    assert "DOUBLE space" in quirks
    # And the code really does depend on that double space.
    countdown = push.decode_status_frame("0:21:1:ff:\xff259  Secs Remaining:2")
    assert countdown is not None and countdown.seconds_remaining == 59


def test_the_contract_records_that_tls_is_mandatory_on_the_rest_api():
    """The silent-no-op path, stated where a reimplementer will read it."""
    rest = CONTRACT["transport"]["rest_api"]
    assert rest["tls"] == "REQUIRED"
    assert "Login still succeeds over HTTP" in rest["tls_note"], (
        "the dangerous half is that authentication works while commands do not"
    )
