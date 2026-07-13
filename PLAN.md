# PLAN — Skills + Orchestrator restructure (inbound, human-like first)

Turn Maya from one 419-line monolithic prompt into a **two-plane** agent adapted
from the `voice-poc-starter-kit`:

- **Behavior plane** = small, numbered `server/skills/*.md` files composed per
  stage (guardrails + current stage + verified-facts block), pushed via
  `session.update`. Small prompts → far less hallucination.
- **Control plane** = deterministic Python in `server/app/orchestrator/` that
  reads every finalized caller turn *before the model responds*, enforces
  compliance in code, and swaps skills on transitions. Later this becomes a
  LangGraph `StateGraph`.

**Scope:** inbound speech-to-speech only. Telephony/outbound and the kit's
machine-facing skills (voicemail drop, screener, IVR, beep detection, outbound
dialing, connect classifier) are **out of scope**.

**The rule that matters:** compliance lives in `server/app/orchestrator/`, never
in a prompt. See `CLAUDE.md`.

Drive one step per session with `/step N`. Do not start the next step until the
current step's **Verify** passes.

---

## Model choice

Default stays `VOICE_LIVE_MODEL=gpt-4o-mini` (cascaded — cheap, works everywhere).
Every change here works on both tiers. For *true* semantic barge-in and the most
human turn-taking, switch to a native speech-to-speech model with a one-line env
change: `VOICE_LIVE_MODEL=gpt-realtime`. Revisit before the demo.

---

## The skill set (inbound, numbered)

Tens digit = call phase; `00` is always composed in. Mapped from the current
`server/app/agent_persona.py` monolith.

| # | Skill file | From the monolith | Tools allowed |
|---|---|---|---|
| 00 | `00_global_guardrails.md` *(always on)* | Identity + AI disclosure, 8 hard rules, honesty/verified-facts boundary, **readback rule**, after-interruption rule, style, tool discipline | — |
| 10 | `10_greeting_intro.md` | Greeting + TCPA disclosure (verbatim) + consent | `end_call` |
| 20 | `20_qualify_core.md` | Q1–Q9 flow, one question at a time (split → `21_qualify_financial.md` only if it grows too long) | `capture_borrower_field`, `schedule_callback`, `transfer_to_lo`, `end_call` |
| 30 | `30_decline_close.md` | Graceful close after a respected "no" (one rebuttal spent) | `log_disposition`, `end_call` |
| 31 | `31_optout_dnc_close.md` | Opt-out / do-not-call close (compliance) | `end_call` |
| 32 | `32_callback_close.md` | Q9 completion + `schedule_callback` — **only skill allowed to promise contact** | `schedule_callback`, `log_disposition`, `end_call` |
| 40 | `40_transfer_escalation.md` | Escalation → `transfer_to_lo` (rate inquiry, hardship, requested human, out-of-scope, abuse, repeated confusion) | `capture_borrower_field`, `transfer_to_lo`, `end_call` |
| 50 | `50_language_route.md` | EN/ES routing (near-term multilingual goal) | `route_language`, `transfer_to_lo`, `end_call` |

---

## Phases

### Phase 0 — Scaffold + standing orders ✅ DONE (2026-07-10)
Create `server/skills/`, `server/config/session.yaml`, `server/app/orchestrator/`,
`server/evals/`, root `CLAUDE.md`, root `PLAN.md`. No behavior change.
**Verify:** repo imports; browser call still works. ✅

### Phase 1 — HUMAN-LIKE BEHAVIOR ✅ DONE (2026-07-10)
Done against the existing monolith so the wins land immediately.
1. ✅ **Model sampling temperature** knob `VOICE_LIVE_MODEL_TEMPERATURE` (default
   0.6) set on `RequestSession` in `voice_live_session.py` — distinct from the
   0.9 voice/TTS temp. Biggest single anti-hallucination lever. (Was previously
   unset → model ran at the 0.7 default.)
2. ✅ **Anti-hallucination boundary + readback rule** added to both prompt
   variants in `agent_persona.py`; removed the canned "congratulations /
   time-sensitive" line that was being emitted on garbled input.
3. ✅ **Barge-in flush** in `voicelive_media_handler.py`: `response.cancel()` now
   attempted for cascaded models too (was realtime-only), plus server-side
   `output_audio_buffer.clear()` and a `barge_in flush dispatched in N ms` log in
   `on_speech_started()` (the web handler delegates here via `super()`).
4. ⏳ **`session.yaml` tuning** — recommended human-like values captured in
   `server/config/session.yaml`; values active today via the existing env knobs
   (`VOICE_LIVE_*`). Full typed wiring happens in Phase 3.
5. ⏳ **Silence policy** (reprompt 8s/16s varied, close by 25s, disposition
   `no_response`) — deferred to Phase 3 (needs the FSM turn counter).

**Verify:** live browser call — talking over Maya stops her within a syllable
(watch the `barge_in flush` log); "umm" doesn't cut her off; she reads back slots
and stops inventing details. *(Manual call verification pending.)*

### Phase 2 — Skills decomposition ✅ DONE (2026-07-10)
1. ✅ Split the monolith into the 8 numbered files under `server/skills/`
   (`00_global_guardrails`, `10_greeting_intro`, `20_qualify_core`,
   `30_decline_close`, `31_optout_dnc_close`, `32_callback_close`,
   `40_transfer_escalation`, `50_language_route`). Faithful extraction; TCPA and
   opt-out wording kept verbatim.
2. ✅ `server/skills/loader.py` — `compose(state, ctx)` = `00_global_guardrails` +
   stage file + rendered VERIFIED FACTS block, reads files fresh each call,
   substitutes `{brokerage_name}`/`{first_name}`/`{followup_window}`/`{close_name}`.
   `SKILL_FOR_STATE` maps FSM states → files.
3. ✅ `server/tests/test_skill_lint.py` (only `32_callback_close.md` may promise
   future contact) + `test_skills_compose.py`. **9 tests green.**

**Not yet wired live** — the running call still uses the monolith. Phase 3's FSM
calls `compose()` on each transition. Verified instead by unit test: `compose()`
reads skills fresh from disk (the "no redeploy" property) and the lint holds.
Per-stage prompts are now focused (e.g. opt-out close ≈ guardrails + ~500 chars,
vs the 6710-char everything-prompt), which is the anti-hallucination win.

### Phase 3a — Orchestrator core (plain Python, unit-tested) ✅ DONE (2026-07-10)
Zero runtime risk — pure modules under `server/app/orchestrator/`, nothing live yet.
1. ✅ `intents.py` — `gate(text, ctx)` returns a forced `Action` or None, priority
   order: hard opt-out → escalate (hardship/human/abuse) → language → busy →
   soft-decline (one rebuttal, then close). Compliance in code, not the prompt.
2. ✅ `fsm.py` — `CallContext` (facts + flags + tool_log) and `CallStateMachine`
   (logged transitions; `can_end`/`end` refuse to end without a disposition).
3. ✅ `tools.py` — real `FunctionTool` specs + `tools_for(state)` allow-lists +
   `execute_tool()` (mutates ctx, appends the paper trail, optional `jsonl_sink`
   stub CRM). `end_call` refused until a disposition exists.
4. ✅ `dialog.py` — `handle_caller_turn()`: the shared brain (gate → fire
   compliance tools BEFORE speech → transition FSM → recompose skill+tools). The
   live handler and the tests call the same function.
5. ✅ Tests `test_intents.py` / `test_fsm.py` / `test_dialog.py` — **22 green total.**

**Verify (met):** the walk GREETING→QUALIFY→DECLINE_CLOSE→ENDED holds; "take me off
your list" deterministically → `DNC_CLOSE` with `add_to_do_not_call` +
`log_disposition` fired *before* the close composes; composed close contains only
the opt-out skill; `end_call` refused without a disposition.

### Phase 3b — Live wiring ✅ IMPLEMENTED (2026-07-10, pending live-call verify)
Behind env flag `ORCHESTRATOR_ENABLED` (default OFF). Flag off → `/web/ws` uses the
classic monolith handler, unchanged (verified: server imports clean, 25 tests green).
1. ✅ Base loop dispatches `response.function_call_arguments.done` → new no-op
   `on_function_call()` hook (never fires without registered tools → safe).
2. ✅ `server/app/orchestrator/handler.py` — `OrchestratorMixin` +
   `OrchestratedWebHandler`:
   - `_session_config()` calls `super()` (keeps transcription + 0.6 temp) then swaps
     `instructions = compose(state, facts)` and `tools = function_tools(tools_for(state))`.
   - `on_user_transcript_done()` runs `handle_caller_turn`; on a gate Decision:
     `session.update` + `response.cancel` + `response.create`. Advances GREETING→QUALIFY
     on the first non-gated turn.
   - `on_function_call()` → `execute_tool` → `FunctionCallOutputItem` reply; state-changing
     tools (transfer/schedule/route) advance the FSM; `end_call` triggers finalize.
3. ✅ `server.py` `/web/ws` picks the handler by the flag.
4. ✅ Regression test `test_orchestrated_handler.py` (offline): composes skills (not the
   monolith), tools per stage, transcription/temp preserved. **25 tests green total.**

**Known rough edges for live iteration (not yet verified on a call):**
- GREETING→QUALIFY advances on the first non-gated turn — consent-decline ("no") is
  not robustly detected yet; demo script should answer the disclosure with "yes".
- `session.yaml` is not yet parsed field-by-field; tuning stays env-driven (Phase 1).
- `super().on_user_transcript_done` keeps the web auto-end heuristic, which may overlap
  with tool-driven `end_call` — harmless (finalize is guarded) but worth watching.
- Silence policy (8s/16s/25s) still TODO.

**Verify (live, TODO):** `ORCHESTRATOR_ENABLED=true`, `cd server && uv run server.py`,
browser call → say "stop calling me": agent stops, closes compliantly, and
`server/data/crm_stub.jsonl` shows `add_to_do_not_call` before the goodbye.

### Phase 4 — LangGraph orchestration ✅ DONE (2026-07-11)
1. ✅ Added `langgraph` (1.2.9) as an optional extra in `pyproject.toml`
   (`[langgraph]`); installed into the venv.
2. ✅ `server/app/orchestrator/graph.py` — the FSM as a `StateGraph`: `listen`
   node runs the **unchanged** `intents.gate()`; a **conditional edge** routes to
   one outcome node per Action; each outcome reuses **`dialog.apply_action()`**
   (same tools, same `compose()` skill files). Compiled with a **`MemorySaver`
   checkpointer**. State is plain primitives (serializable); byte-identical
   behavior to the FSM engine.
3. ✅ `GraphEngine` is a drop-in for the dialog module
   (`handle_caller_turn(text, fsm, ctx, sink=...)`), so the handler swaps engines
   with one call. Selected by `ORCHESTRATOR_ENGINE=langgraph` (default `fsm`);
   `handler.py._select_engine()` picks it, imported lazily.
4. ✅ `tests/test_graph.py` — parity across 6 scenarios (opt-out, decline×2, busy,
   language, escalate, neutral), tool-before-speech ordering, skill isolation, and
   **checkpointer durability** (`get_state` by `thread_id`). **29 tests green total.**

**Verify (met):** every scenario routes through the compiled graph to the same
Decision/effects as the FSM engine; per-call state (disposition, rebuttal_used) is
retrievable via `app.get_state({thread_id})`.

> LangGraph does **not** make the agent sound more human — Phase 1 does. It makes
> multi-skill orchestration durable/resumable and node-for-node portable, so the
> prompt never collapses back into a monolith. Live use still rides on the Phase 3b
> flag (`ORCHESTRATOR_ENABLED`) and awaits the same live-call verification.

### Phase 5 — Evals + guardrail hook ✅ DONE (2026-07-11)
1. ✅ `server/evals/run_text.py` — replays scripted caller turns through the
   deterministic core (mirrors the live handler; no audio/model), asserts
   `actions` / `tool_calls` / `disposition` / `state` / `instructions_contain`, and
   checks `forbid_phrases` against the final stage skill. Prints a per-category
   scorecard; non-zero exit on failure. `--engine fsm|langgraph`.
2. ✅ 8 seed scenarios in `scenarios/`: opt-out ×2, decline/one-rebuttal, busy
   callback, escalation ×2 (human, hardship), language, accuracy/readback.
3. ✅ `tests/test_evals.py` runs all scenarios on **both** engines inside pytest.
4. ✅ Guardrail hook: `scripts/eval_gate.sh` (re-runs text evals on edits to
   `server/app/orchestrator/` or `server/skills/`, exit 2 on red) wired as a
   `PostToolUse` hook in `.claude/settings.json`.

**Verify (met):** `uv run python evals/run_text.py --all` → **8/8 green** on both
engines; sabotaging `OPT_OUT_HARD` reddened **only** the `dnc` category (0/2),
everything else stayed green. **32 tests green total.**

> Hook activation caveat: `.claude/` was created this session, so the settings
> watcher picks it up only after `/hooks` is opened once (or a restart).

> Not yet (need a live Voice Live session): audio-mode eval (`run_audio.py`) for
> stop-latency / phantom-turn measurement, a model `judge_rubric` grader, and
> silence scenarios (paired with the deferred silence policy).
