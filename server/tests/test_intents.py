"""Gate = the deterministic COMPLIANCE floor.

It fires ONLY on a hard opt-out (TCPA "do not call") — the one intent that legally
must never be missed. All other intent (decline / callback / escalate / language)
is understood semantically by the router (app.orchestrator.semantic), so the gate
stays silent on those and lets the model decide.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestrator.intents import gate, matched_opt_out  # noqa: E402


@dataclass
class Ctx:
    rebuttal_used: bool = False


def test_matched_opt_out_returns_phrase():
    assert matched_opt_out("Hey, don't call me over again") is not None
    assert "call" in matched_opt_out("don't call me").lower()
    assert matched_opt_out("still exploring") is None


def test_hard_optout_variants_all_map_to_dnc():
    for phrase in [
        "please take me off your list",
        "stop calling me",
        "do not call this number again",
        "I want to be removed",
        "just remove me",
        "that's the second time I've told you",
        # T9 creative opt-outs (scorecard)
        "No. Cancel it.",
        "Quit calling my phone.",
        "please quit calling me",
        "cancel this call",
    ]:
        assert gate(phrase, Ctx()) == "DNC_CLOSE", phrase


def test_optout_off_your_list_variants():
    for phrase in [
        "could you please strike me off your list",
        "get me off your list",
        "please take my name off your list",
    ]:
        assert gate(phrase, Ctx()) == "DNC_CLOSE", phrase


def test_optout_contact_again_phrases():
    for phrase in [
        "I don't want to be contacted again",
        "do not contact me again",
        "please never contact me",
        "no more contact please",
    ]:
        assert gate(phrase, Ctx()) == "DNC_CLOSE", phrase



def test_call_again_phrases_are_dnc_not_decline():
    for phrase in [
        "I would not like to proceed with this call again.",
        "I don't want to proceed with this call again",
        "please don't call again",
        "I would not like to do this call again",
    ]:
        assert gate(phrase, Ctx()) == "DNC_CLOSE", phrase


def test_gate_fires_only_on_opt_out_everything_else_defers_to_router():
    # The gate is the compliance floor — ONLY hard opt-out. Decline / callback /
    # escalate / language are now semantic (the router decides), so the gate is silent.
    for phrase in [
        "not interested",
        "no thanks",
        "I don't want to proceed",
        "let me speak to a human",
        "I'm behind on my payments",
        "can we do this in Spanish",
        "catch me another time",
        "I'd like to refinance my house",
        "",
    ]:
        assert gate(phrase, Ctx()) is None, phrase


def test_wants_resume_qualify_phrases():
    from app.orchestrator.intents import wants_resume_qualify

    for phrase in [
        "Proceed with your questions.",
        "Let's continue",
        "I want to keep going",
        "please continue with the call",
        "go ahead with the questions",
    ]:
        assert wants_resume_qualify(phrase) is True, phrase
    for phrase in [
        "Yes",
        "okay",
        "I continue to get spam calls",
        "don't call me again",
        "I don't want to proceed",
        "Too many calls and the rates felt high",
    ]:
        assert wants_resume_qualify(phrase) is False, phrase


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print("intents: OK")
