#!/usr/bin/env python
"""Text-mode eval runner — replay caller turns through the orchestrator, no audio.

Drives the deterministic decision core (gate -> tools -> FSM -> compose) exactly as
the live handler does, and asserts the outcome per scenario. Runs in milliseconds
and gates every orchestrator/skill change.

    cd server && uv run python evals/run_text.py --all
    cd server && uv run python evals/run_text.py --all --engine langgraph

Exit code is non-zero if any hard assertion fails, so it works as a CI/pre-push gate.
Sabotage a gate and only that category's rows go red.

Scenario YAML (server/evals/scenarios/*.yaml):
    id, category, caller_turns
    expect: {tool_calls: [...subset...], disposition, state, actions: [...per turn...],
             instructions_contain: [...]}
    forbid_phrases: [...]   # checked against the final STAGE skill (not the guardrail,
                            # which intentionally quotes forbidden phrases as examples)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # server/ on path

import yaml  # noqa: E402

from app.orchestrator.fsm import CallContext, CallStateMachine  # noqa: E402
from skills import loader  # noqa: E402

SCEN_DIR = Path(__file__).parent / "scenarios"


def _engine(name: str):
    if name == "langgraph":
        from app.orchestrator.graph import GraphEngine

        return GraphEngine()
    from app.orchestrator import dialog

    return dialog


def _drive(turns, engine):
    """Mirror the live handler: gate each turn; advance GREETING->QUALIFY otherwise."""
    fsm = CallStateMachine(call_id="eval")
    ctx = CallContext(call_id="eval")
    actions: list[str | None] = []
    for turn in turns:
        decision = engine.handle_caller_turn(turn, fsm, ctx)
        if decision is None and fsm.state == "GREETING":
            fsm.transition("QUALIFY", reason="consent")
        actions.append(None if decision is None else decision.action)
    return fsm, ctx, actions


def _stage_text(state: str) -> str:
    if state not in loader.SKILL_FOR_STATE:
        return ""
    return (loader.SKILLS_DIR / loader.skill_for_state(state)).read_text(encoding="utf-8")


def check(scenario, engine) -> list[str]:
    turns = scenario["caller_turns"]
    expect = scenario.get("expect", {})
    fsm, ctx, actions = _drive(turns, engine)
    composed = loader.compose(fsm.state, ctx.facts())
    stage_text = _stage_text(fsm.state)
    fired = [r["tool"] for r in ctx.tool_log]

    errs: list[str] = []
    for tool in expect.get("tool_calls", []):
        if tool not in fired:
            errs.append(f"tool {tool!r} not fired (fired={fired})")
    if "disposition" in expect and ctx.disposition != expect["disposition"]:
        errs.append(f"disposition {ctx.disposition!r} != {expect['disposition']!r}")
    if "state" in expect and fsm.state != expect["state"]:
        errs.append(f"state {fsm.state!r} != {expect['state']!r}")
    if "actions" in expect and actions != expect["actions"]:
        errs.append(f"actions {actions} != {expect['actions']}")
    for needle in expect.get("instructions_contain", []):
        if needle not in composed:
            errs.append(f"instructions missing {needle!r}")
    for phrase in scenario.get("forbid_phrases", []):
        if phrase.lower() in stage_text.lower():
            errs.append(f"forbidden phrase in {fsm.state} skill: {phrase!r}")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser(description="Text-mode orchestrator evals")
    ap.add_argument("--all", action="store_true", help="run every scenario")
    ap.add_argument("--engine", default="fsm", choices=["fsm", "langgraph"])
    args = ap.parse_args()

    engine = _engine(args.engine)
    files = sorted(SCEN_DIR.glob("*.yaml"))
    if not files:
        print("No scenarios found in", SCEN_DIR)
        return 1

    by_category: dict[str, list[tuple[str, list[str]]]] = {}
    for path in files:
        scenario = yaml.safe_load(path.read_text(encoding="utf-8"))
        errs = check(scenario, engine)
        cat = scenario.get("category", "?")
        by_category.setdefault(cat, []).append((scenario.get("id", path.stem), errs))

    total = passed = 0
    print(f"\n  Orchestrator text evals  (engine={args.engine})")
    print("  " + "-" * 46)
    for cat in sorted(by_category):
        rows = by_category[cat]
        ok = sum(1 for _, e in rows if not e)
        total += len(rows)
        passed += ok
        mark = "OK " if ok == len(rows) else "RED"
        print(f"  [{mark}] {cat:12} {ok}/{len(rows)}")
        for sid, errs in rows:
            for err in errs:
                print(f"         - {sid}: {err}")
    print("  " + "-" * 46)
    print(f"  {passed}/{total} scenarios passed\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
