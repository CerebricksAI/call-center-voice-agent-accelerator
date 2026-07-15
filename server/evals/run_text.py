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

# UI-facing labels for scenario ``category`` keys.
CATEGORY_LABELS: dict[str, str] = {
    "dnc": "do not call",
    "decline": "decline",
    "callback": "callback",
    "escalation": "escalation",
    "language": "language",
    "accuracy": "truthfulness",
    "interruptions": "interruptions",
    "machines": "machines",
}


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


def run_scorecard(engine_name: str = "fsm") -> dict:
    """Run all text scenarios and return a JSON-ready scorecard.

    Shape::
        {
          "engine": "fsm",
          "categories": [{"id": "dnc", "label": "do not call", "pass": 2, "total": 2, "miss": false}, ...],
          "overall": {"pass": N, "total": M, "miss": false},
          "scenarioCount": M,
        }
    """
    engine = _engine(engine_name)
    files = sorted(SCEN_DIR.glob("*.yaml"))
    by_category: dict[str, list[tuple[str, list[str]]]] = {}
    for path in files:
        scenario = yaml.safe_load(path.read_text(encoding="utf-8"))
        errs = check(scenario, engine)
        cat = str(scenario.get("category") or "?")
        by_category.setdefault(cat, []).append((scenario.get("id", path.stem), errs))

    categories: list[dict] = []
    total = passed = 0
    for cat in sorted(by_category):
        rows = by_category[cat]
        ok = sum(1 for _, e in rows if not e)
        n = len(rows)
        total += n
        passed += ok
        categories.append(
            {
                "id": cat,
                "label": CATEGORY_LABELS.get(cat, cat.replace("_", " ")),
                "pass": ok,
                "total": n,
                "miss": ok < n,
            }
        )
    return {
        "engine": engine_name,
        "categories": categories,
        "overall": {
            "id": "overall",
            "label": "overall",
            "pass": passed,
            "total": total,
            "miss": passed < total,
        },
        "scenarioCount": total,
        "source": "evals/scenarios",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Text-mode orchestrator evals")
    ap.add_argument("--all", action="store_true", help="run every scenario")
    ap.add_argument("--engine", default="fsm", choices=["fsm", "langgraph"])
    args = ap.parse_args()

    card = run_scorecard(args.engine)
    if card["scenarioCount"] == 0:
        print("No scenarios found in", SCEN_DIR)
        return 1

    print(f"\n  Orchestrator text evals  (engine={args.engine})")
    print("  " + "-" * 46)
    for row in card["categories"]:
        mark = "OK " if not row["miss"] else "RED"
        print(f"  [{mark}] {row['id']:12} {row['pass']}/{row['total']}")
    print("  " + "-" * 46)
    overall = card["overall"]
    print(f"  {overall['pass']}/{overall['total']} scenarios passed\n")
    return 0 if not overall["miss"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
