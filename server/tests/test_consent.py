"""Greeting consent routing — refuse must not enter QUALIFY."""

from app.orchestrator.consent import is_consent_affirm, is_consent_refusal


def test_refuse_disclosure():
    assert is_consent_refusal("It doesn't work for me as of now, what you're telling.")
    assert is_consent_refusal("I don't agree with your compliance.")
    assert is_consent_refusal("No thanks")
    assert is_consent_refusal("I'm not comfortable with that")


def test_affirm_disclosure():
    assert is_consent_affirm("Yes")
    assert is_consent_affirm("Yeah that works")
    assert is_consent_affirm("Sure, go ahead")
    assert is_consent_affirm("Okay")
    assert is_consent_affirm("Let's go.")
    assert is_consent_affirm("Lets go")
    assert is_consent_affirm("Sounds good")
    assert is_consent_affirm("I'm ready")


def test_ambiguous_not_forced():
    assert not is_consent_affirm("Looking to buy a home")
    assert not is_consent_refusal("Looking to buy a home")
    assert not is_consent_affirm("What did you say?")
