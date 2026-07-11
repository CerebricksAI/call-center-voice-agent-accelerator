"""Control plane — deterministic compliance + call-flow, in code (never in a prompt).

Scaffolded in Phase 0. Modules land in Phase 3 (see root PLAN.md):

    intents.py   gate(text, ctx) -> Action | None, run on every finalized caller
                 turn BEFORE the model responds (opt-out / decline / escalation /
                 language, priority-ordered).
    fsm.py       CallStateMachine + CallContext — legal transitions per stage;
                 end_call refused without a disposition.
    handler.py   OrchestratorMixin — wires the gate into on_user_transcript_done(),
                 composes skills on transitions, executes real tool calls.

Rule: compliance logic lives here and nowhere else.
"""
