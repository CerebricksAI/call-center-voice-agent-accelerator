"""Semantic intent router: label mapping + whole-conversation wiring (mocked LLM)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestrator import semantic  # noqa: E402


def test_route_label_maps_all_intents():
    assert semantic.route_label("OPT_OUT") == "DNC_CLOSE"
    assert semantic.route_label("DECLINE") == "DECLINE_CLOSE"
    assert semantic.route_label("CALLBACK") == "CALLBACK_CLOSE"
    assert semantic.route_label("ESCALATE") == "ESCALATE"
    assert semantic.route_label("LANGUAGE") == "LANGUAGE_ROUTE"
    assert semantic.route_label("CONTINUE") is None
    assert semantic.route_label("") is None


def test_format_transcript_labels_roles():
    turns = [
        {"role": "agent", "text": "What state?"},
        {"role": "user", "text": "Arizona"},
        {"role": "assistant", "text": ""},  # empty dropped
    ]
    out = semantic.format_transcript(turns)
    assert out == "Agent: What state?\nCaller: Arizona"


def test_route_conversation_uses_whole_transcript(monkeypatch):
    import app.conversation_extractor as ce

    captured = {}

    async def fake_completion(endpoint, credential, model, prompt, *, instructions,
                              temperature, max_output_tokens):
        captured["prompt"] = prompt
        return "DECLINE", None

    monkeypatch.setenv("AZURE_VOICE_LIVE_ENDPOINT", "https://example")
    monkeypatch.setattr(ce, "_build_extract_credential", lambda: object())
    monkeypatch.setattr(ce, "_voicelive_text_completion", fake_completion)

    turns = [
        {"role": "agent", "text": "Your timeline?"},
        {"role": "user", "text": "I've changed my mind, not proceeding."},
    ]
    result = asyncio.run(semantic.route_conversation(turns))
    assert result == "DECLINE_CLOSE"
    assert "changed my mind" in captured["prompt"]  # the whole transcript is passed


def test_semantic_enabled_flag(monkeypatch):
    monkeypatch.delenv("SEMANTIC_INTENT_ENABLED", raising=False)
    assert semantic.semantic_enabled() is True
    monkeypatch.setenv("SEMANTIC_INTENT_ENABLED", "false")
    assert semantic.semantic_enabled() is False


if __name__ == "__main__":
    test_route_label_maps_all_intents()
    test_format_transcript_labels_roles()
    print("semantic: OK")
