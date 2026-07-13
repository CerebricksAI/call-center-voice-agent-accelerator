# Evals — the regression suite

Text-mode evals drive the FSM directly with scripted caller turns as *finalized
transcripts* (no audio), and assert the outcome. They run in seconds and gate
every orchestrator/skill change (Phase 5). This is how we prove human-like and
compliant behavior stays fixed as the skills grow.

## Running

```
cd server && uv run python evals/run_text.py --all
cd server && uv run python evals/run_text.py --all --engine langgraph
```

Prints a per-category scorecard; exits non-zero on any hard failure (so it gates
CI / the PostToolUse hook). Also run inside pytest via `tests/test_evals.py`.

## Scenario format (one YAML per scenario in `scenarios/`)

```yaml
id: optout_take_me_off
category: dnc          # dnc | decline | callback | escalation | language | accuracy
caller_turns:
  - "Hi, I filled out a form online."
  - "Actually, take me off your list."
expect:
  actions: [null, DNC_CLOSE]                          # per-turn gate action (null = none)
  tool_calls: [add_to_do_not_call, log_disposition]   # must fire (any order)
  disposition: do_not_call
  state: DNC_CLOSE                                     # final FSM state
  instructions_contain: ["Read back"]                 # composed instructions must include
forbid_phrases: ["we'll be in touch", "talk soon", "follow up", "reach out"]
```

- **Hard assertions** (`actions`, `tool_calls`, `disposition`, `state`,
  `instructions_contain`) pass/fail the run.
- **`forbid_phrases`** are checked against the final **stage skill** text (not the
  always-on guardrail, which intentionally quotes the forbidden phrases as "never
  say" examples).

## Status

**Phase 5 DONE (2026-07-11).** `run_text.py` + 8 seed scenarios (dnc×2, decline
one-rebuttal, callback, escalation×2, language, accuracy/readback). Runs green on
both the FSM and LangGraph engines.

**Not yet (needs a live Voice Live session + model judge):** audio-mode
(`run_audio.py`) for stop-latency / phantom-turn measurement, a `judge_rubric`
model grader (reported, never blocking), and silence scenarios (the silence policy
is deferred with Phase 3b).
