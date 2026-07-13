"""FSM: legal transitions and the no-end-without-disposition guarantee."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.orchestrator.fsm import CallContext, CallStateMachine  # noqa: E402


def test_starts_in_greeting_and_logs_transitions():
    fsm = CallStateMachine(call_id="c1")
    assert fsm.state == "GREETING"
    fsm.transition("QUALIFY", reason="consent")
    assert fsm.state == "QUALIFY"
    assert fsm.transitions[-1] == {"call_id": "c1", "from": "GREETING", "to": "QUALIFY", "reason": "consent"}


def test_unknown_state_rejected():
    fsm = CallStateMachine()
    with pytest.raises(ValueError):
        fsm.transition("BOGUS", reason="x")


def test_cannot_end_without_disposition():
    fsm = CallStateMachine(state="DECLINE_CLOSE")
    ctx = CallContext()
    assert fsm.can_end(ctx) is False
    assert fsm.end(ctx) is False
    assert fsm.state == "DECLINE_CLOSE"

    ctx.disposition = "declined"
    assert fsm.can_end(ctx) is True
    assert fsm.end(ctx) is True
    assert fsm.state == "ENDED"


def test_facts_view_shape():
    ctx = CallContext(borrower_name="Alex Rivera")
    facts = ctx.facts()
    assert facts["borrower_name"] == "Alex Rivera"
    assert set(facts) == {"borrower_name", "lead_source", "prior_notes"}


if __name__ == "__main__":
    test_starts_in_greeting_and_logs_transitions()
    test_cannot_end_without_disposition()
    test_facts_view_shape()
    print("fsm: OK")
