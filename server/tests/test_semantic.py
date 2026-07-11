"""Semantic fallback gate: label mapping + classify wiring (mocked LLM, no network)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestrator import semantic  # noqa: E402


def test_map_label_maps_end_and_optout():
    assert semantic.map_label("END") == "DECLINE_CLOSE"
    assert semantic.map_label(" end ") == "DECLINE_CLOSE"
    assert semantic.map_label("OPT_OUT") == "DNC_CLOSE"
    assert semantic.map_label("optout") == "DNC_CLOSE"
    assert semantic.map_label("CONTINUE") is None
    assert semantic.map_label("") is None
    assert semantic.map_label("banana") is None


def test_optout_outranks_end_in_mapping():
    # If both tokens appear, opt-out (the stronger, DNC signal) wins.
    assert semantic.map_label("OPT_OUT / end call") == "DNC_CLOSE"


def test_classify_uses_llm_and_maps(monkeypatch):
    import app.conversation_extractor as ce

    async def fake_completion(endpoint, credential, model, prompt, *, instructions,
                              temperature, max_output_tokens):
        assert "Caller just said" in prompt
        return "END", None

    monkeypatch.setenv("AZURE_VOICE_LIVE_ENDPOINT", "https://example")
    monkeypatch.setattr(ce, "_build_extract_credential", lambda: object())
    monkeypatch.setattr(ce, "_voicelive_text_completion", fake_completion)

    result = asyncio.run(semantic.classify_disengagement("I think I'm all done here"))
    assert result == "DECLINE_CLOSE"


def test_classify_returns_none_without_endpoint(monkeypatch):
    monkeypatch.delenv("AZURE_VOICE_LIVE_ENDPOINT", raising=False)
    assert asyncio.run(semantic.classify_disengagement("")) is None
    assert asyncio.run(semantic.classify_disengagement("anything")) is None


def test_semantic_enabled_flag(monkeypatch):
    monkeypatch.delenv("SEMANTIC_INTENT_ENABLED", raising=False)
    assert semantic.semantic_enabled() is True
    monkeypatch.setenv("SEMANTIC_INTENT_ENABLED", "false")
    assert semantic.semantic_enabled() is False


if __name__ == "__main__":
    test_map_label_maps_end_and_optout()
    test_optout_outranks_end_in_mapping()
    test_semantic_enabled_flag(type("M", (), {"delenv": lambda *a, **k: None, "setenv": lambda *a, **k: None})())
    print("semantic: OK")
