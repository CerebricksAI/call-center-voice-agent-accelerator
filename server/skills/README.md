# Skills — the behavior plane

One short Markdown file per conversation stage. The agent is **never** given one
giant prompt. At each stage the orchestrator composes:

```
00_global_guardrails.md   (always)
+ <current stage file>    (e.g. 20_qualify_core.md)
+ <verified facts block>  (rendered from CallContext: name, lead source, ...)
```

joined with `\n\n---\n\n` and pushed via `session.update`. Small, focused prompts
are the main defense against hallucination. Editing a skill changes behavior with
**no redeploy**.

## Numbering (tens digit = call phase)

| Range | Meaning | Files |
|---|---|---|
| 00 | Global, always composed in | `00_global_guardrails.md` |
| 10 | Opening | `10_greeting_intro.md` |
| 20 | Core task | `20_qualify_core.md` (`21_qualify_financial.md` if split) |
| 30 | Human close outcomes | `30_decline_close.md`, `31_optout_dnc_close.md`, `32_callback_close.md` |
| 40 | Escalation / handoff | `40_transfer_escalation.md` |
| 50 | Routing / special | `50_language_route.md` |

## Rules for writing a skill (enforced by `test_skill_lint.py`, Phase 2)

- Keep it under a page. Say what to do **before** what to avoid.
- Give exact wording for legally sensitive lines (TCPA, opt-out close).
- Name the tools the model may call in this stage (`Tools allowed:` line).
- **Never promise future contact** except in `32_callback_close.md`, and only
  after the callback tool has succeeded.

## Status

Directory scaffolded in Phase 0. The actual decomposition of the current
monolith (`server/app/agent_persona.py`) into these files happens in **Phase 2**
(see `PLAN.md`).
