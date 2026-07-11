"""Gate: priority order and the one-rebuttal budget, enforced in code."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestrator.intents import gate  # noqa: E402


@dataclass
class Ctx:
    rebuttal_used: bool = False


def test_hard_optout_variants_all_map_to_dnc():
    for phrase in [
        "please take me off your list",
        "stop calling me",
        "do not call this number again",
        "I want to be removed",
        "just remove me",
        "that's the second time I've told you",
    ]:
        assert gate(phrase, Ctx()) == "DNC_CLOSE", phrase


def test_optout_outranks_decline():
    # A turn that is both a decline and an opt-out is an opt-out.
    assert gate("no thanks, take me off your list", Ctx()) == "DNC_CLOSE"


def test_optout_off_your_list_variants():
    # "... me off your list" is an opt-out regardless of the verb. Repro from a
    # live call: "Could you please strike me off your list?" slipped through.
    for phrase in [
        "could you please strike me off your list",
        "get me off your list",
        "please take my name off your list",
    ]:
        assert gate(phrase, Ctx()) == "DNC_CLOSE", phrase


def test_escalation_and_language_and_busy():
    assert gate("let me speak to a human", Ctx()) == "ESCALATE"
    assert gate("I'm behind on my payments", Ctx()) == "ESCALATE"
    assert gate("can we do this in Spanish", Ctx()) == "LANGUAGE_ROUTE"
    assert gate("I'm busy, call me back later", Ctx()) == "CALLBACK_CLOSE"


def test_one_rebuttal_then_close():
    ctx = Ctx()
    assert gate("not interested", ctx) == "REBUTTAL_ONCE"
    assert ctx.rebuttal_used is True
    assert gate("no thanks", ctx) == "DECLINE_CLOSE"  # budget spent


def test_explicit_end_request_closes_without_rebuttal():
    # An explicit "end this call" is a termination request, not a soft decline:
    # close gracefully and do NOT spend the one rebuttal nudging the caller.
    # Repro from a live call: "How can we end this call? I don't want to call."
    ctx = Ctx()
    assert gate("how can we end this call? I don't want to call", ctx) == "DECLINE_CLOSE"
    assert ctx.rebuttal_used is False
    assert gate("let's end the call please", Ctx()) == "DECLINE_CLOSE"
    # A hard opt-out still outranks a plain end request (never weaken opt-out):
    # "stop this call" matches the existing stop...call opt-out pattern.
    assert gate("can you stop this call now", Ctx()) == "DNC_CLOSE"
    assert gate("end this call and take me off your list", Ctx()) == "DNC_CLOSE"


def test_neutral_turn_passes_through():
    assert gate("I'm looking to refinance my house", Ctx()) is None
    assert gate("", Ctx()) is None


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print("intents: OK")
