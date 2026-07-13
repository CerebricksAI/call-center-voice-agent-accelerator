"""Skill-lint: only 32_callback_close.md may promise future contact.

Mirrors CLAUDE.md rule 7. Forbidden future-contact phrasing must not appear in any
skill except the callback close. Paragraphs that begin with "Never" are skipped so
a guardrail may quote the forbidden phrases as examples of what NOT to say.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # server/ on path

from app.agent_persona import BROKERAGE_NAME  # noqa: E402
from skills import loader  # noqa: E402

FORBIDDEN = [
    r"we'?ll (be in touch|call)",
    r"talk soon",
    r"follow[ -]?up",
    r"reach (back )?out",
]
ALLOWED_FILE = "32_callback_close.md"


def _paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def _starts_with_never(paragraph: str) -> bool:
    first = paragraph.strip().splitlines()[0]
    return first.lstrip("#-*> ").strip().lower().startswith("never")


def test_no_future_contact_promises_outside_callback():
    offenders: list[str] = []
    for name in loader.available_skills():
        if name == ALLOWED_FILE:
            continue
        text = (loader.SKILLS_DIR / name).read_text(encoding="utf-8")
        for para in _paragraphs(text):
            if _starts_with_never(para):
                continue
            for pat in FORBIDDEN:
                if re.search(pat, para, re.IGNORECASE):
                    offenders.append(f"{name}: /{pat}/ in {para.strip()[:80]!r}")
    assert not offenders, "Future-contact phrasing outside the callback skill:\n" + "\n".join(offenders)


def test_every_mapped_state_has_an_existing_file():
    for state, filename in loader.SKILL_FOR_STATE.items():
        assert (loader.SKILLS_DIR / filename).is_file(), f"{state} -> missing {filename}"


def test_global_guardrail_exists():
    assert (loader.SKILLS_DIR / loader.GLOBAL_SKILL).is_file()


def test_all_skills_compose_without_leftover_placeholders():
    ctx = {"borrower_name": "Alex Rivera"}
    for state in loader.SKILL_FOR_STATE:
        composed = loader.compose(state, ctx)
        assert BROKERAGE_NAME in composed or "{brokerage_name}" not in composed
        for ph in ("{brokerage_name}", "{first_name}", "{followup_window}", "{close_name}"):
            assert ph not in composed, f"{state}: unresolved {ph}"


if __name__ == "__main__":
    test_no_future_contact_promises_outside_callback()
    test_every_mapped_state_has_an_existing_file()
    test_global_guardrail_exists()
    test_all_skills_compose_without_leftover_placeholders()
    print("skill-lint: OK")
