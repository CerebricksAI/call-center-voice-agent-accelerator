"""Unit tests for concrete vs vague callback windows."""

from app.orchestrator.callback_time import is_concrete_callback_time


def test_concrete_windows():
    assert is_concrete_callback_time("tomorrow at 10 a.m.")
    assert is_concrete_callback_time("9 a.m. tomorrow")
    assert is_concrete_callback_time("tomorrow morning")
    assert is_concrete_callback_time("Thursday afternoon")


def test_vague_half_answers():
    assert not is_concrete_callback_time("morning")
    assert not is_concrete_callback_time("anything")
    assert not is_concrete_callback_time("later")
    assert not is_concrete_callback_time("anytime")
    assert not is_concrete_callback_time("")
    assert not is_concrete_callback_time(None)
