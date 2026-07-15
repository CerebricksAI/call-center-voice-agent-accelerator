"""Live scorecard helper used by /api/scorecard and the CLI runner."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import evals.run_text as rt  # noqa: E402


def test_run_scorecard_fsm_has_overall():
    card = rt.run_scorecard("fsm")
    assert card["engine"] == "fsm"
    assert card["scenarioCount"] >= 1
    assert card["overall"]["total"] == card["scenarioCount"]
    if card["overall"]["pass"] != card["scenarioCount"]:
        # Surface which YAML scenarios failed so CI logs are actionable.
        from pathlib import Path

        import yaml

        engine = rt._engine("fsm")
        fails = []
        for path in sorted(Path(rt.SCEN_DIR).glob("*.yaml")):
            scenario = yaml.safe_load(path.read_text(encoding="utf-8"))
            errs = rt.check(scenario, engine)
            if errs:
                fails.append(f"{scenario.get('id', path.stem)}: {errs}")
        raise AssertionError(
            f"scorecard {card['overall']['pass']}/{card['overall']['total']} — "
            + "; ".join(fails)
        )
    assert any(c["id"] == "dnc" for c in card["categories"])
