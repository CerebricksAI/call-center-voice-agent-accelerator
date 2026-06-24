"""LLM-driven extraction of key conversation details (no fixed form schema)."""

import asyncio
import json
import logging
import os
import re
import time

from azure.ai.voicelive.aio import connect as voicelive_connect
from azure.ai.voicelive.models import (
    InputTextContentPart,
    Modality,
    RequestSession,
    ServerEventType,
    UserMessageItem,
)
from azure.core.credentials import AzureKeyCredential
from azure.identity.aio import ManagedIdentityCredential
from app.usage_cost import normalize_usage
from app.transcript_sanitize import normalize_insight_value

logger = logging.getLogger(__name__)

_EXTRACT_SYSTEM = """You analyze live voice-call transcripts for a financial services agent.
Extract structured field values — not narrative sentences. Use labels and keys that match what was actually discussed.

Return JSON only:
{"insights":[{"key":"snake_case_slug","label":"Short field label","value":"Brief value","confidence":0.0-1.0}]}

Rules:
- Extract ONLY facts the CALLER stated or clearly confirmed — ignore options the agent lists.
- REQUIRED label: human-readable name for THIS fact in context (e.g. "Cash-out amount", "Current balance", "Current rate", "Lender", "Loan purpose", "Contact preference", "TCPA consent"). Never use purchase-only labels like "Down payment" or "Purchase timeline" unless the caller is buying a home.
- key: snake_case slug (e.g. loan_purpose, cash_out_amount, current_balance, current_rate, lender, contact_preference, tcpa_consent, recording_consent, credit_score, state, zip, down_payment, purchase_timeline, property_type, annual_income, employment_status). Pick keys that match the call type (purchase, refinance, cash-out, HELOC).
- value: brief phrase only (e.g. "Cash-out refinance", "$30,000", "Company Zeta", "~$100,000"). Never full sentences. Never start with "The caller".
- NEVER emit placeholder values: do not return "not provided", "unknown", "N/A", "not discussed", or similar — omit the field entirely if the caller did not state it.
- If the caller corrects an earlier fact, return the updated value with the SAME key.
- Correct speech-to-text errors when obvious.
- Do not repeat facts listed under "Already extracted" unless correcting them.
- Return {"insights":[]} if nothing new yet.
- confidence (0.0–1.0) reflects how clearly the CALLER stated the fact:
  - 0.9–1.0: stated clearly and directly, no hedging
  - 0.65–0.8: approximate ("around", "roughly", "about", "approximately", ranges)
  - 0.4–0.6: vague or uncertain
  - omit field if not stated — do not use low confidence as a substitute for missing data"""

SUMMARY_SYSTEM = """You summarize mortgage pre-qualification phone calls for loan-officer handoff.

Read the ENTIRE transcript. Your summary must reflect everything material the caller stated — do not omit names, companies, numbers, rates, balances, amounts, or preferences.

Include when present: loan purpose/type, current rate and lender, remaining balance, cash-out or loan amount, property location, credit/income/employment, purchase or refi timeline, TCPA/recording consent, contact preference, and next steps.

Write 2–3 concise prose paragraphs (no bullets, no markdown). Be complete and factual. Separate paragraphs with one blank line."""

EXTRACT_SYSTEM = _EXTRACT_SYSTEM

_EXTRACT_TIMEOUT_S = float(os.getenv("EXTRACT_TIMEOUT_S", "30"))
_SUMMARY_TIMEOUT_S = float(os.getenv("SUMMARY_TIMEOUT_S", "20"))
_SUMMARY_MAX_CHARS = int(os.getenv("SUMMARY_MAX_CHARS", "8000"))
_SUMMARY_MAX_TURNS = int(os.getenv("SUMMARY_MAX_TURNS", "48"))
_SUMMARY_MAX_OUTPUT_TOKENS = int(os.getenv("SUMMARY_MAX_OUTPUT_TOKENS", "350"))
_EXTRACT_SEMAPHORE: asyncio.Semaphore | None = None
_SUMMARY_SEMAPHORE: asyncio.Semaphore | None = None


def _extract_semaphore() -> asyncio.Semaphore:
    global _EXTRACT_SEMAPHORE
    if _EXTRACT_SEMAPHORE is None:
        limit = max(1, int(os.getenv("EXTRACT_MAX_PARALLEL", "1")))
        _EXTRACT_SEMAPHORE = asyncio.Semaphore(limit)
    return _EXTRACT_SEMAPHORE


def _summary_semaphore() -> asyncio.Semaphore:
    global _SUMMARY_SEMAPHORE
    if _SUMMARY_SEMAPHORE is None:
        _SUMMARY_SEMAPHORE = asyncio.Semaphore(1)
    return _SUMMARY_SEMAPHORE


def _build_extract_credential() -> AzureKeyCredential | ManagedIdentityCredential | None:
    """Match VoiceLiveMediaHandler: managed identity in Azure, API key for local dev."""
    client_id = os.getenv("AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID", "").strip()
    if client_id:
        return ManagedIdentityCredential(client_id=client_id)
    key = os.getenv("AZURE_VOICE_LIVE_API_KEY", "").strip()
    if key:
        return AzureKeyCredential(key)
    return None

_SLUG_RE = re.compile(r"[^a-z0-9]+")

_INSIGHT_KEY_LABELS = {
    "loan_purpose": "Loan purpose",
    "loan_type": "Loan type",
    "location": "State",
    "state": "State",
    "zip": "ZIP code",
    "zip_status": "ZIP status",
    "timeline": "Timeline",
    "purchase_timeline": "Purchase timeline",
    "amount": "Amount",
    "cash_out_amount": "Cash-out amount",
    "current_balance": "Current balance",
    "down_payment": "Down payment",
    "credit_score": "Credit score",
    "employment": "Employment status",
    "employment_status": "Employment status",
    "income": "Annual income",
    "annual_income": "Annual income",
    "contact": "Contact preference",
    "contact_preference": "Contact preference",
    "consent": "TCPA consent",
    "tcpa_consent": "TCPA consent",
    "recording_consent": "Call recording consent",
    "property_status": "Property type",
    "property_type": "Property type",
    "next_step": "Next step",
    "rate": "Current rate",
    "current_rate": "Current rate",
    "lender": "Lender",
    "other": "Other",
}

_PLACEHOLDER_VALUE_RE = re.compile(
    r"^(?:not[\s_]?provided|not[\s_]?discussed|not[\s_]?disclosed|unknown|n/a|na|none|"
    r"declined|not_provided|no(?:ne)?(?:\s+provided)?|unspecified)$",
    re.I,
)


def _is_placeholder_value(value: str) -> bool:
    cleaned = (value or "").strip().strip(" .")
    if not cleaned:
        return True
    return bool(_PLACEHOLDER_VALUE_RE.match(cleaned))


def _insight_label(key: str, explicit: str | None = None) -> str:
    label = (explicit or "").strip()
    if label:
        return label
    if key in _INSIGHT_KEY_LABELS:
        return _INSIGHT_KEY_LABELS[key]
    return (key or "Detail").replace("_", " ").strip().title()


def _normalize_insight_row(item: dict) -> dict:
    row = dict(item)
    raw_value = str(row.get("value") or "").strip()
    if _is_placeholder_value(raw_value):
        return {}
    value = normalize_insight_value(raw_value)
    if not value or _is_placeholder_value(value):
        return {}
    key = row.get("key") or _slug(value[:32])
    if "|" in key or len(key) > 40:
        return {}
    row["key"] = key
    row["label"] = _insight_label(key, row.get("label"))
    row["value"] = value
    row["id"] = key
    conf = row.get("confidence")
    try:
        conf = float(conf) if conf is not None else None
        if conf is not None:
            conf = max(0.0, min(1.0, conf))
    except (TypeError, ValueError):
        conf = None
    row["confidence"] = _adjust_confidence(
        str(item.get("value") or value), conf
    )
    return row

_CONFIDENCE_HEDGE_RE = re.compile(
    r"\b(?:approx(?:imately)?|around|roughly|about|maybe|probably|somewhat|"
    r"i think|i guess|or so|give or take|between)\b",
    re.I,
)
_CONFIDENCE_RANGE_RE = re.compile(r"\b\d+\s*(?:to|-)\s*\d+\b", re.I)


def _adjust_confidence(raw_value: str, conf: float | None) -> float | None:
    """Cap overconfident scores when the caller hedged or gave a range."""
    text = (raw_value or "").strip()
    hedged = bool(
        _CONFIDENCE_HEDGE_RE.search(text) or _CONFIDENCE_RANGE_RE.search(text)
    )
    if conf is None:
        return 0.72 if hedged else None
    if hedged:
        return min(conf, 0.75)
    return conf

_MONEY_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d{2})?"
    r"|\b\d[\d,]{2,}(?:\.\d{2})?\s*(?:dollars|usd)\b"
    r"|\b\d+(?:\.\d+)?\s*(?:k|thousand|million)\b",
    re.I,
)
_TIMELINE_RE = re.compile(
    r"\b(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|"
    r"tomorrow|today|this morning|this afternoon|next week)\b",
    re.I,
)
_DURATION_RE = re.compile(r"\b(\d+\s*(?:days?|weeks?|months?))\b", re.I)
_ZIP_RE = re.compile(r"\b(\d{5})\b")
_CONTACT_RE = re.compile(
    r"\b(prefer(?:s|red)?\s+(?:a\s+)?(?:phone\s+)?call|"
    r"text(?: message)?|email|phone call)\b",
    re.I,
)
_CONSENT_RE = re.compile(
    r"\b(yes|yeah|sure|that works|works for me|okay|ok|no|nope|don't|do not)\b.*"
    r"(?:consent|works for you|works for me|recorded|contact|fine with)",
    re.I,
)

_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming",
}

_CITY_LABELS = {
    "los angeles": "Los Angeles, California",
    "san francisco": "San Francisco, California",
    "san diego": "San Diego, California",
    "new york city": "New York City, New York",
    "new york": "New York",
    "chicago": "Chicago, Illinois",
    "houston": "Houston, Texas",
    "phoenix": "Phoenix, Arizona",
    "dallas": "Dallas, Texas",
    "austin": "Austin, Texas",
    "seattle": "Seattle, Washington",
    "denver": "Denver, Colorado",
    "miami": "Miami, Florida",
    "atlanta": "Atlanta, Georgia",
    "boston": "Boston, Massachusetts",
}


def _slug(label: str) -> str:
    slug = _SLUG_RE.sub("_", (label or "").lower()).strip("_")
    return (slug[:56] or "insight")


def _format_transcript(turns: list[dict]) -> str:
    lines = []
    for i, turn in enumerate(turns, start=1):
        role = turn.get("role", "unknown")
        text = (turn.get("text") or "").strip()
        if not text:
            continue
        speaker = "Caller" if role == "user" else "Agent"
        lines.append(f"[{i}] {speaker}: {text}")
    return "\n".join(lines)


def _trim_turns_for_summary(turns: list[dict]) -> list[dict]:
    """Keep prompt bounded while preserving the full conversation when possible."""
    if not turns:
        return []
    kept = list(turns)
    while len(kept) > 4 and len(_format_transcript(kept)) > _SUMMARY_MAX_CHARS:
        kept.pop(len(kept) // 2)
    while len(kept) > _SUMMARY_MAX_TURNS:
        kept.pop(len(kept) // 2)
    return kept


def _fact(key: str, value: str, confidence: float = 0.8) -> dict:
    return {
        "id": key,
        "key": key,
        "value": value,
        "confidence": confidence,
    }


def _dedupe_insights(items: list[dict]) -> list[dict]:
    out = []
    seen_keys = set()
    seen_values = set()
    for item in items:
        normalized = _normalize_insight_row(item)
        if not normalized:
            continue
        value = normalized["value"]
        key = normalized["key"]
        norm = value.lower()
        if key in seen_keys or norm in seen_values:
            continue
        seen_keys.add(key)
        seen_values.add(norm)
        out.append(normalized)
    return out


def _normalize_asr(text: str) -> str:
    """Fix common speech-to-text concatenations before heuristic matching."""
    return re.sub(r"home\s*equity", "home equity", text, flags=re.I)


def _user_turn_texts(turns: list[dict]) -> list[str]:
    return [
        _normalize_asr((t.get("text") or "").strip())
        for t in turns
        if t.get("role") == "user" and (t.get("text") or "").strip()
    ]


def _user_text(turns: list[dict]) -> str:
    return " ".join(_user_turn_texts(turns))


def _parse_location(text: str) -> str | None:
    lower = text.lower()
    for city, label in sorted(_CITY_LABELS.items(), key=lambda x: -len(x[0])):
        if city in lower:
            return label
    for state in sorted(_US_STATES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(state)}\b", lower):
            return state.title()
    match = re.search(
        r"\b(?:in|from|live in|located in|property in|state of)\s+"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b",
        text,
    )
    if match:
        place = match.group(1).strip()
        if place.lower() not in {"time", "the", "a", "an", "this", "that", "well", "state"}:
            return place
    return None


def _loan_purpose_fact(user_lower: str) -> dict | None:
    """Detect loan purpose from caller speech only — ignore agent suggestions."""
    if not user_lower.strip():
        return None

    if re.search(
        r"\bhome equity line of credit\b|\bheloc\b|\bhome equity\b.*\bline of credit\b",
        user_lower,
    ):
        return _fact(
            "loan_purpose",
            "The caller is seeking a home equity line of credit (HELOC).",
            0.92,
        )
    if re.search(r"\bcash[- ]?out\b", user_lower):
        return _fact(
            "loan_purpose",
            "The caller wants a cash-out refinance.",
            0.9,
        )
    if re.search(r"\brefinanc\w*\b", user_lower):
        if "existing mortgage" in user_lower:
            return _fact(
                "loan_purpose",
                "The caller wants to refinance their existing mortgage.",
                0.93,
            )
        return _fact(
            "loan_purpose",
            "The caller is interested in refinancing.",
            0.9,
        )
    if re.search(r"\bhome equity\b", user_lower):
        return _fact(
            "loan_purpose",
            "The caller wants to access home equity.",
            0.88,
        )
    if re.search(
        r"\b(?:purchase|buy(?:ing)?)\s+(?:a\s+)?(?:home|house|property)\b", user_lower
    ) or re.search(r"\blooking to purchase\b", user_lower):
        return _fact(
            "loan_purpose",
            "The caller is looking to purchase a home.",
            0.9,
        )
    return None


def _timeline_fact(turns: list[dict]) -> dict | None:
    """Use the caller's most recent timeline statement (supports corrections)."""
    for text in reversed(_user_turn_texts(turns)):
        lower = text.lower()
        if not re.search(
            r"\b\d+\s*(?:days?|weeks?|months?|years?)\b|"
            r"\bfew months\b|"
            r"\btimeline\b|"
            r"\bnext week\b|"
            r"\btomorrow\b|"
            r"\btoday\b",
            lower,
        ):
            continue

        m = re.search(r"\b(\d+)\s*(?:to|-)\s*(\d+)\s*months?\b", lower)
        if m:
            return _fact(
                "timeline",
                f"The caller's expected timeline is about {m.group(1)} to {m.group(2)} months.",
                0.9,
            )

        if re.search(r"\bfew months\b", lower) and re.search(
            r"shift|changed|later|instead|now|personal|family", lower
        ):
            return _fact(
                "timeline",
                "The caller's timeline has shifted to a few months out.",
                0.87,
            )

        m = _DURATION_RE.search(lower)
        if m:
            return _fact(
                "timeline",
                f"The caller's expected timeline is about {m.group(1).strip()}.",
                0.85,
            )

        m = _TIMELINE_RE.search(lower)
        if m:
            return _fact(
                "timeline",
                f"The caller's expected timeline is {m.group(1).strip()}.",
                0.82,
            )
    return None


def _credit_score_fact(turns: list[dict]) -> dict | None:
    """Extract the caller's latest credit-score range from their own words."""
    for text in reversed(_user_turn_texts(turns)):
        lower = text.lower()
        if not re.search(r"credit|\bscore\b|\b\d{3}\b", lower):
            continue

        m = re.search(
            r"(?:now|currently|today).{0,60}"
            r"(?:around|about|roughly|near|is|at)?\s*(\d{3})\b",
            lower,
        )
        if m and 300 <= int(m.group(1)) <= 850:
            return _fact(
                "credit_score",
                f"The caller's current credit score is around {m.group(1)}.",
                0.88,
            )

        m = re.search(
            r"(?:around|about|roughly|near|somewhat)\s*(\d{3})\b(?:\s*now)?",
            lower,
        )
        if m and 300 <= int(m.group(1)) <= 850:
            return _fact(
                "credit_score",
                f"The caller's credit score is around {m.group(1)}.",
                0.86,
            )

        m = re.search(r"(?:above|over|in the)\s*(\d{3})\b", lower)
        if m and 300 <= int(m.group(1)) <= 850:
            qualifier = "above" if re.search(r"above|over", lower) else "around"
            return _fact(
                "credit_score",
                f"The caller's credit score is {qualifier} {m.group(1)}.",
                0.84,
            )

        m = re.search(r"\b(\d{3})s\b", lower)
        if m and 300 <= int(m.group(1)) <= 850:
            return _fact(
                "credit_score",
                f"The caller's credit score is in the {m.group(1)}s.",
                0.84,
            )
    return None


def _employment_fact(turns: list[dict]) -> dict | None:
    """Extract employment status from the caller's most recent statement."""
    for text in reversed(_user_turn_texts(turns)):
        lower = text.lower()
        if not re.search(
            r"employ|unemploy|retired|self[- ]?employ|income|lpa|lakh",
            lower,
        ):
            continue

        prev_income = re.search(
            r"(?:earlier|previous|prior|was).{0,40}"
            r"income.{0,25}(?:around|about|roughly)?\s*(\d+)\s*(?:lpa|lakh)",
            lower,
        )
        no_income = re.search(
            r"(?:now|currently).{0,30}(?:0|zero)|income.{0,20}(?:0|zero|none)",
            lower,
        )

        if "unemployed" in lower:
            if prev_income and no_income:
                return _fact(
                    "employment",
                    f"The caller is currently unemployed with no current income; "
                    f"prior income was about {prev_income.group(1)} LPA.",
                    0.91,
                )
            if prev_income:
                return _fact(
                    "employment",
                    f"The caller is currently unemployed; prior income was about "
                    f"{prev_income.group(1)} LPA.",
                    0.9,
                )
            if no_income:
                return _fact(
                    "employment",
                    "The caller is currently unemployed with no current income.",
                    0.9,
                )
            return _fact(
                "employment",
                "The caller is currently unemployed.",
                0.88,
            )

        if re.search(r"\bself[- ]?employed\b", lower):
            return _fact("employment", "The caller is self-employed.", 0.88)

        if re.search(r"\bretired\b", lower):
            return _fact("employment", "The caller is retired.", 0.88)

        if re.search(r"\bemployed\b", lower):
            income = re.search(
                r"income.{0,25}(?:around|about|roughly)?\s*(\d+)\s*(?:lpa|lakh)",
                lower,
            )
            if income:
                return _fact(
                    "employment",
                    f"The caller is employed with income around {income.group(1)} LPA.",
                    0.87,
                )
            return _fact("employment", "The caller is employed.", 0.85)
    return None


def _property_status_fact(turns: list[dict]) -> dict | None:
    """Property search status from the caller's latest relevant statement."""
    for text in reversed(_user_turn_texts(turns)):
        lower = text.lower()
        if re.search(r"\bfound a property\b|\bunder contract\b|\balready own\b", lower):
            return _fact(
                "property_status",
                "The caller already has or has found a property.",
                0.88,
            )
        if re.search(r"\bstill looking\b|\bstill searching\b|\blooking for a property\b", lower):
            return _fact(
                "property_status",
                "The caller is still searching for a property.",
                0.9,
            )
    return None


def analyze_conversation(turns: list[dict]) -> list[dict]:
    """Synthesize analytical key facts from the full conversation so far."""
    user = _user_text(turns)
    if not user.strip():
        return []

    user_lower = user.lower()
    found: list[dict] = []

    purpose = _loan_purpose_fact(user_lower)
    if purpose:
        found.append(purpose)

    location = _parse_location(user)
    if location:
        found.append(
            _fact(
                "location",
                f"The caller's property is in {location}.",
                0.85,
            )
        )

    zip_match = _ZIP_RE.search(user)
    if zip_match:
        found.append(
            _fact(
                "zip",
                f"The caller's ZIP code is {zip_match.group(1)}.",
                0.92,
            )
        )
    elif re.search(
        r"(?:don't|do not|dont|no|not)\s+(?:have|got)?\s*(?:any\s+)?zip",
        user_lower,
    ) or re.search(r"\bwithout a zip\b", user_lower):
        found.append(
            _fact(
                "zip_status",
                "The caller has not provided a ZIP code yet.",
                0.88,
            )
        )

    money = _MONEY_RE.search(user)
    if money:
        found.append(
            _fact(
                "amount",
                f"The caller mentioned a loan amount of {money.group(0).strip()}.",
                0.85,
            )
        )

    timeline = _timeline_fact(turns)
    if timeline:
        found.append(timeline)

    credit = _credit_score_fact(turns)
    if credit:
        found.append(credit)

    employment = _employment_fact(turns)
    if employment:
        found.append(employment)

    contact = _CONTACT_RE.search(user)
    if contact:
        pref = contact.group(1).strip().lower()
        if "text" in pref:
            value = "The caller prefers follow-up by text message."
        elif "email" in pref:
            value = "The caller prefers follow-up by email."
        else:
            value = "The caller prefers follow-up by phone call."
        found.append(_fact("contact", value, 0.85))

    if _CONSENT_RE.search(user):
        found.append(
            _fact(
                "consent",
                "The caller responded to the consent and contact-preference question.",
                0.78,
            )
        )

    property_status = _property_status_fact(turns)
    if property_status:
        found.append(property_status)

    for turn in reversed(turns):
        if turn.get("role") != "assistant":
            continue
        text = (turn.get("text") or "").strip()
        lower = text.lower()
        if "loan officer" in lower and ("reach out" in lower or "follow up" in lower):
            found.append(
                _fact(
                    "next_step",
                    "A loan officer will follow up with the caller.",
                    0.84,
                )
            )
            break

    return _dedupe_insights(found)


def extract_new_insights(
    turns: list[dict], emitted: dict
) -> list[dict]:
    """Return new or corrected analytical facts for the client."""
    analyzed = analyze_conversation(turns)
    new: list[dict] = []
    for item in analyzed:
        row = _normalize_insight_row(item)
        if not row:
            continue
        key = row["key"]
        value = row["value"]
        if _emitted_value(emitted.get(key)) == value:
            continue
        row["replace"] = key in emitted
        emitted[key] = {
            "key": key,
            "label": row.get("label"),
            "value": value,
            "confidence": row.get("confidence"),
        }
        new.append(row)
    return new


def extract_insights_incremental(
    turns: list[dict], processed: int = 0
) -> list[dict]:
    """Compatibility wrapper — prefer extract_new_insights with emitted dict."""
    _ = processed
    return extract_new_insights(turns, {})


def extract_insights_heuristic(turns: list[dict]) -> list[dict]:
    """Full heuristic analysis pass."""
    return extract_new_insights(turns, {})


def _extract_json_payload(raw: str) -> str:
    """Return the first JSON object from an LLM response."""
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    # Voice Live may duplicate the payload in text-done + response.output.
    brace = 0
    for i, ch in enumerate(text):
        if ch == "{":
            brace += 1
        elif ch == "}":
            brace -= 1
            if brace == 0:
                return text[: i + 1]
    return text


def _parse_insights(raw: str) -> list[dict]:
    text = _extract_json_payload(raw)
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("[Extract] Could not parse LLM JSON: %r", (raw or "")[:240])
        return []
    items = data.get("insights") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []

    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = _normalize_insight_row(item)
        if row:
            out.append(row)
    return _dedupe_insights(out)


def _emitted_value(entry: dict | str | None) -> str:
    if isinstance(entry, dict):
        return str(entry.get("value") or "").strip()
    return str(entry or "").strip()


def _emitted_label(key: str, entry: dict | str | None) -> str:
    if isinstance(entry, dict):
        label = (entry.get("label") or "").strip()
        if label:
            return label
    return _insight_label(key, None)


def build_extract_user_prompt(turns: list[dict], emitted: dict) -> str:
    """Build the user prompt for live LLM extraction."""
    transcript = _format_transcript(turns)
    if emitted:
        already_lines = [
            f"- [{_emitted_label(k, v)}] {_emitted_value(v)}"
            for k, v in emitted.items()
            if _emitted_value(v)
        ]
        already = "\n".join(already_lines) if already_lines else "(none yet)"
    else:
        already = "(none yet)"
    return (
        f"Transcript so far:\n\n{transcript}\n\n"
        f"Already extracted (do not repeat unless correcting):\n{already}\n\n"
        "Analyze the full transcript and return ONLY new or corrected key facts as JSON."
    )


async def _voicelive_text_completion(
    endpoint: str,
    credential: AzureKeyCredential | ManagedIdentityCredential,
    model: str,
    user_prompt: str,
    *,
    instructions: str = EXTRACT_SYSTEM,
    temperature: float = 0.1,
    max_output_tokens: int | None = None,
) -> tuple[str, dict | None]:
    """Run a short text-only Voice Live session for transcript analysis."""
    usage: dict | None = None
    session_kwargs: dict = {
        "modalities": [Modality.TEXT],
        "instructions": instructions,
        "temperature": temperature,
    }
    if max_output_tokens is not None:
        session_kwargs["max_response_output_tokens"] = max_output_tokens
    async with voicelive_connect(
        endpoint=endpoint,
        credential=credential,
        model=model,
    ) as conn:
        await conn.session.update(session=RequestSession(**session_kwargs))
        await conn.conversation.item.create(
            item=UserMessageItem(
                role="user",
                content=[InputTextContentPart(text=user_prompt)],
            )
        )
        await conn.response.create()
        chunks: list[str] = []
        async for event in conn:
            event_type = getattr(event, "type", None)
            if event_type == ServerEventType.RESPONSE_TEXT_DELTA:
                delta = getattr(event, "delta", None)
                if delta:
                    chunks.append(delta)
            elif event_type == ServerEventType.RESPONSE_TEXT_DONE:
                text = getattr(event, "text", None)
                if text:
                    chunks = [text]
            elif event_type == ServerEventType.RESPONSE_DONE:
                response = getattr(event, "response", None)
                usage = normalize_usage(response)
                if not chunks:
                    for item in getattr(response, "output", None) or []:
                        for part in getattr(item, "content", None) or []:
                            text = getattr(part, "text", None)
                            if text:
                                chunks.append(text)
                return "".join(chunks).strip(), usage
            elif event_type == ServerEventType.ERROR:
                err = getattr(event, "error", event)
                raise RuntimeError(str(err))
    return "", usage


async def llm_extract_insights(
    turns: list[dict],
    emitted: dict[str, str],
) -> tuple[list[dict], dict | None]:
    """Analyze the transcript with a dedicated Voice Live text session (parallel-safe)."""
    endpoint = os.getenv("AZURE_VOICE_LIVE_ENDPOINT", "").rstrip("/")
    model = (
        os.getenv("EXTRACT_MODEL")
        or os.getenv("VOICE_LIVE_MODEL", "gpt-4o-mini")
    ).strip()
    if not endpoint:
        logger.warning("[Extract] AZURE_VOICE_LIVE_ENDPOINT is not set")
        return [], None
    if not turns:
        return [], None

    credential = _build_extract_credential()
    if credential is None:
        logger.warning(
            "[Extract] AZURE_VOICE_LIVE credentials missing "
            "(set AZURE_VOICE_LIVE_API_KEY or AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID)"
        )
        return [], None

    prompt = build_extract_user_prompt(turns, emitted)
    raw = ""
    usage: dict | None = None
    try:
        async with _extract_semaphore():
            try:
                raw, usage = await asyncio.wait_for(
                    _voicelive_text_completion(endpoint, credential, model, prompt),
                    timeout=_EXTRACT_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                logger.warning("[Extract] LLM timed out after %.0fs", _EXTRACT_TIMEOUT_S)
                return [], None
            except Exception:
                logger.exception("[Extract] Voice Live LLM extraction failed")
                return [], None
    finally:
        if isinstance(credential, ManagedIdentityCredential):
            await credential.close()

    insights = merge_llm_insights(raw, emitted)
    if not insights and (raw or "").strip():
        logger.warning(
            "[Extract] LLM returned text but 0 parsed insights (raw_len=%d)",
            len(raw),
        )
    logger.info("[Extract] LLM returned %d new insight(s)", len(insights))
    return insights, usage


def build_call_summary_prompt(
    turns: list[dict],
    key_facts: dict | None = None,
) -> str:
    """Build the user prompt for end-of-call summary from the full transcript."""
    transcript = _format_transcript(turns)
    parts = [f"Full call transcript:\n\n{transcript}\n"]
    if key_facts:
        facts = "\n".join(
            f"- [{_emitted_label(k, v)}] {_emitted_value(v)}"
            for k, v in key_facts.items()
            if _emitted_value(v)
        )
        if facts:
            parts.append(
                "Extracted key facts (cross-check against transcript; include all in summary):\n"
                f"{facts}\n"
            )
    parts.append(
        "Summarize this entire call from the transcript above. Include every specific fact "
        "the caller stated — loan type, amounts, rates, lender/company names, balance, contact "
        "preference, consent, and next steps. Do not skip proper nouns or numbers."
    )
    return "\n".join(parts)


def format_summary_as_prose(text: str) -> str:
    """Normalize LLM output to paragraph prose (no bullet list formatting)."""
    text = (text or "").strip()
    if not text:
        return ""
    if re.search(r"^[\s]*[-•*]\s", text, re.MULTILINE):
        lines = []
        for line in text.replace("\r\n", "\n").split("\n"):
            line = re.sub(r"^[\s•\-*]+\s*", "", line.strip())
            if line:
                lines.append(line)
        if not lines:
            return text
        mid = max(1, len(lines) // 2)
        return "\n\n".join(
            p
            for p in (" ".join(lines[:mid]), " ".join(lines[mid:]))
            if p.strip()
        )
    if "\n\n" in text:
        return text
    return text.replace("\n", " ").strip()


async def llm_generate_call_summary(
    turns: list[dict],
    key_facts: dict[str, str] | None = None,
) -> tuple[str, dict | None]:
    """Generate a plain-text summary of the full call transcript."""
    t0 = time.perf_counter()
    endpoint = os.getenv("AZURE_VOICE_LIVE_ENDPOINT", "").rstrip("/")
    model = (
        os.getenv("SUMMARY_MODEL")
        or os.getenv("EXTRACT_MODEL")
        or os.getenv("VOICE_LIVE_MODEL", "gpt-4o-mini")
    ).strip()
    if not endpoint:
        logger.warning("[Summary] AZURE_VOICE_LIVE_ENDPOINT is not set")
        return "", None
    if not turns:
        return "", None

    credential = _build_extract_credential()
    if credential is None:
        logger.warning(
            "[Summary] AZURE_VOICE_LIVE credentials missing "
            "(set AZURE_VOICE_LIVE_API_KEY or AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID)"
        )
        return "", None

    trimmed = _trim_turns_for_summary(turns)
    prompt = build_call_summary_prompt(trimmed, key_facts)
    logger.info(
        "[Summary] Starting LLM pass (turns=%d→%d, prompt_chars=%d, model=%s)",
        len(turns),
        len(trimmed),
        len(prompt),
        model,
    )
    raw = ""
    usage: dict | None = None
    try:
        async with _summary_semaphore():
            try:
                raw, usage = await asyncio.wait_for(
                    _voicelive_text_completion(
                        endpoint,
                        credential,
                        model,
                        prompt,
                        instructions=SUMMARY_SYSTEM,
                        temperature=0.2,
                        max_output_tokens=_SUMMARY_MAX_OUTPUT_TOKENS,
                    ),
                    timeout=_SUMMARY_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[Summary] LLM timed out after %.0fs", _SUMMARY_TIMEOUT_S
                )
                return "", None
            except Exception:
                logger.exception("[Summary] Voice Live summary generation failed")
                return "", None
    finally:
        if isinstance(credential, ManagedIdentityCredential):
            await credential.close()

    summary = format_summary_as_prose(raw)
    logger.info(
        "[Summary] Generated call summary (%d chars) in %.2fs",
        len(summary),
        time.perf_counter() - t0,
    )
    return summary, usage


def insight_detail_rows(emitted: dict) -> list[dict]:
    """Build persisted key-detail rows with labels for call history."""
    rows: list[dict] = []
    for key, entry in (emitted or {}).items():
        payload = entry if isinstance(entry, dict) else {"key": key, "value": entry}
        normalized = _normalize_insight_row({**payload, "key": key})
        if normalized:
            row = {
                "key": normalized["key"],
                "label": normalized["label"],
                "value": normalized["value"],
            }
            if normalized.get("confidence") is not None:
                row["confidence"] = normalized["confidence"]
            rows.append(row)
    return rows


def merge_llm_insights(raw: str, emitted: dict) -> list[dict]:
    """Parse LLM JSON and return new/updated insight rows."""
    if not (raw or "").strip():
        return []
    return _merge_new_insights(_parse_insights(raw), emitted)


def _merge_new_insights(
    parsed: list[dict], emitted: dict
) -> list[dict]:
    new: list[dict] = []
    for item in parsed:
        row = _normalize_insight_row(item)
        if not row:
            continue
        key = row["key"]
        value = row["value"]
        if _emitted_value(emitted.get(key)) == value:
            continue
        row["replace"] = key in emitted
        emitted[key] = {
            "key": key,
            "label": row.get("label"),
            "value": value,
            "confidence": row.get("confidence"),
        }
        new.append(row)
    return new

