"""Run the text-eval scenarios as part of pytest, on both engines."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

import evals.run_text as rt  # noqa: E402
from app.orchestrator import dialog  # noqa: E402


def _run_all(engine) -> list[tuple[str, list[str]]]:
    failures = []
    for path in sorted(rt.SCEN_DIR.glob("*.yaml")):
        scenario = yaml.safe_load(path.read_text(encoding="utf-8"))
        errs = rt.check(scenario, engine)
        if errs:
            failures.append((scenario.get("id", path.stem), errs))
    return failures


def test_scenarios_exist():
    assert list(rt.SCEN_DIR.glob("*.yaml")), "no eval scenarios found"


def test_all_scenarios_pass_on_fsm_engine():
    assert _run_all(dialog) == []


def test_all_scenarios_pass_on_langgraph_engine():
    from app.orchestrator.graph import GraphEngine

    assert _run_all(GraphEngine()) == []
