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


def test_optout_off_your_list_variants():
    for phrase in [
        "could you please strike me off your list",
        "get me off your list",
        "please take my name off your list",
    ]:
        assert gate(phrase, Ctx()) == "DNC_CLOSE", phrase


def test_optout_matches_even_amid_other_words():
    assert gate("no thanks, take me off your list", Ctx()) == "DNC_CLOSE"


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


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print("intents: OK")
