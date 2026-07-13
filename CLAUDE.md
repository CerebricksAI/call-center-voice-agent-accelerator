# Standing orders — call-center-voice-agent-accelerator

Inbound speech-to-speech mortgage pre-qualification agent ("Maya") on Azure Voice
Live. We are restructuring it into a two-plane design (behavior = skills, control =
orchestrator). The step-by-step plan is in `PLAN.md`; drive it with `/step N`.

Run the server: `cd server && uv run server.py` (port 8080; auth required).

## Hard rules

1. **Compliance logic lives ONLY in `server/app/orchestrator/`** — never in a
   prompt or skill file. Never weaken a gate or an eval to make something pass.
2. Any change to an orchestrator gate requires a **failing test first**, then the
   fix, then green evals (`cd server && uv run python evals/run_text.py --all`)
   once that runner exists.
3. **Extend, don't rewrite.** This is a fork of Microsoft's accelerator — keep
   diffs small and localized so upstream fixes keep merging. One plan step per
   session. No new dependencies without asking (managed with `uv`).
4. **Never invent Azure API shapes.** Read the `azure-ai-voicelive` SDK (in
   `server/.venv`) or the existing handlers before using a field or method.
5. **Secrets from env only.** Never print, commit, or hardcode keys. Do not read
   `.env`.
6. After a change, byte-compile / run the relevant tests and evals, and summarize
   what changed in ≤5 lines.
7. **Only `32_callback_close.md` may promise future contact.** Once skills exist,
   `server/tests/test_skill_lint.py` enforces this.

## Two planes

- **Behavior** (`server/skills/*.md`): what the agent says, per stage. Composed as
  `00_global_guardrails` + the current stage file + a verified-facts block, pushed
  via `session.update`. Editing text changes behavior with no redeploy.
- **Control** (`server/app/orchestrator/*.py`): a gate + state machine that reads
  every finalized caller turn *before* the model responds, enforces opt-out /
  one-rebuttal / escalation deterministically, fires tools before speech, and
  refuses to end a call without a disposition.

## Human-likeness is the Voice Live layer, not LangGraph

The agent sounds human because of: model temperature 0.6 (anti-hallucination),
fast barge-in flush, semantic VAD + filler-word removal, short turns, and readback
discipline. LangGraph (Phase 4) is orchestration plumbing — it does not change
voice quality.

## Style

Python 3.12, type hints, functions under ~40 lines. The FSM should stay ~250
readable lines. Amounts spoken in words. Keep skills under a page.
