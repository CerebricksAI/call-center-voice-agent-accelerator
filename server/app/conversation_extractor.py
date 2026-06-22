"""LLM-driven extraction of key conversation details (no fixed form schema)."""

import asyncio
import json
import logging
import re

from azure.core.credentials import AzureKeyCredential
from azure.ai.voicelive.aio import connect as voicelive_connect
from azure.ai.voicelive.models import (
    InputTextContentPart,
    Modality,
    RequestSession,
    ServerEventType,
    UserMessageItem,
)

logger = logging.getLogger(__name__)

_EXTRACT_SYSTEM = """You analyze voice-call transcripts for a financial services agent.
Synthesize important facts — do NOT quote the caller verbatim.

Return JSON only:
{"insights":[{"key":"loan_purpose|location|zip|zip_status|timeline|amount|contact|consent|property_status|next_step","value":"One analytical sentence in your own words.","confidence":0.0-1.0}]}

Rules:
- ANALYZE and summarize; never copy transcript phrasing or use "Caller noted:" / "Caller said:".
- Extract ONLY facts the CALLER stated — ignore options or examples the agent lists.
- Correct obvious speech-to-text errors (e.g. "state of Los Angeles" → Los Angeles, California).
- Separate distinct facts (purpose, location, ZIP availability, timeline, amount, contact preference).
- If caller lacks a ZIP, say they have not provided one — do not repeat their exact wording.
- Omit greetings and filler. Return {"insights":[]} if nothing substantive yet.
- confidence: 0.9+ clear fact, 0.75–0.85 inferred, below 0.75 if uncertain."""

_extract_lock = asyncio.Lock()
_EXTRACT_TIMEOUT_S = 25.0
_SLUG_RE = re.compile(r"[^a-z0-9]+")

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
    r"\b(yes|yeah|sure|that works|okay|ok|no|nope|don't|do not)\b.*"
    r"(?:consent|works for you|recorded|contact)",
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
        value = (item.get("value") or "").strip()
        if not value:
            continue
        key = item.get("key") or _slug(value[:32])
        norm = value.lower()
        if key in seen_keys or norm in seen_values:
            continue
        seen_keys.add(key)
        seen_values.add(norm)
        item = dict(item)
        item["id"] = key
        item["key"] = key
        item["value"] = value
        out.append(item)
    return out


def _user_text(turns: list[dict]) -> str:
    return " ".join(
        (t.get("text") or "").strip()
        for t in turns
        if t.get("role") == "user" and (t.get("text") or "").strip()
    )


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

    if re.search(r"\bhome equity line of credit\b|\bheloc\b", user_lower):
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

    duration = _DURATION_RE.search(user)
    timeline = _TIMELINE_RE.search(user)
    when = None
    if duration:
        when = duration.group(1).strip()
    elif timeline:
        when = timeline.group(1).strip()
    if when:
        if purpose and re.search(r"heloc|home equity|refinanc", user_lower):
            found.append(
                _fact(
                    "timeline",
                    f"The caller plans to move forward within the next {when}.",
                    0.88,
                )
            )
        else:
            found.append(
                _fact(
                    "timeline",
                    f"The caller's expected timeline is about {when}.",
                    0.82,
                )
            )

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

    if re.search(r"\bstill looking\b", user_lower):
        found.append(
            _fact(
                "property_status",
                "The caller is still searching for a property.",
                0.9,
            )
        )

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
    turns: list[dict], emitted: dict[str, str]
) -> list[dict]:
    """Return new or corrected analytical facts for the client."""
    analyzed = analyze_conversation(turns)
    new: list[dict] = []
    for item in analyzed:
        key = item.get("key") or _slug(item.get("value", "")[:32])
        value = item["value"]
        if emitted.get(key) == value:
            continue
        item = dict(item)
        item["replace"] = key in emitted
        emitted[key] = value
        new.append(item)
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


def _parse_insights(raw: str) -> list[dict]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    data = json.loads(text)
    items = data.get("insights") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []

    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        if value.lower().startswith("caller noted:") or value.lower().startswith(
            "caller said:"
        ):
            continue
        key = str(item.get("key") or "").strip() or _slug(value[:32])
        conf = item.get("confidence")
        try:
            conf = float(conf) if conf is not None else None
            if conf is not None:
                conf = max(0.0, min(1.0, conf))
        except (TypeError, ValueError):
            conf = None
        out.append({"id": key, "key": key, "value": value, "confidence": conf})
    return _dedupe_insights(out)


async def _collect_text_response(conn) -> str:
    chunks: list[str] = []
    async for event in conn:
        event_type = getattr(event, "type", None)
        if event_type == ServerEventType.RESPONSE_TEXT_DELTA:
            chunks.append(getattr(event, "delta", "") or "")
        elif event_type == ServerEventType.RESPONSE_TEXT_DONE:
            done_text = getattr(event, "text", None)
            if done_text:
                return done_text
            break
        elif event_type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            transcript = getattr(event, "transcript", None)
            if transcript:
                return transcript
        elif event_type == ServerEventType.RESPONSE_DONE:
            break
        elif event_type == ServerEventType.ERROR:
            message = getattr(event, "message", None) or getattr(event, "error", None)
            raise RuntimeError(f"Voice Live extraction error: {message}")
    return "".join(chunks)


async def extract_conversation_insights_llm(
    turns: list[dict],
    *,
    endpoint: str,
    api_key: str,
    model: str,
) -> list[dict]:
    """Run a text-only Voice Live session to pull key details from the transcript."""
    transcript = _format_transcript(turns)
    if not transcript.strip():
        return []

    async def _run() -> list[dict]:
        async with voicelive_connect(
            endpoint=endpoint,
            credential=AzureKeyCredential(api_key),
            model=model.strip(),
        ) as conn:
            await conn.session.update(
                session=RequestSession(
                    modalities=[Modality.TEXT],
                    instructions=_EXTRACT_SYSTEM,
                )
            )
            await conn.conversation.item.create(
                item=UserMessageItem(
                    role="user",
                    content=[
                        InputTextContentPart(
                            text=(
                                f"Transcript so far:\n\n{transcript}\n\n"
                                "Analyze the conversation and extract key facts. "
                                "Synthesize — do not quote verbatim."
                            )
                        )
                    ],
                )
            )
            await conn.response.create()
            raw = await _collect_text_response(conn)
            if not raw.strip():
                return []
            return _parse_insights(raw)

    for attempt in range(3):
        try:
            async with _extract_lock:
                insights = await asyncio.wait_for(_run(), timeout=_EXTRACT_TIMEOUT_S)
            logger.info(
                "[Extract] LLM extracted %d insight(s) from %d turn(s)",
                len(insights),
                len(turns),
            )
            return insights
        except asyncio.TimeoutError:
            logger.warning(
                "[Extract] Timed out after %.0fs (attempt %d/3)",
                _EXTRACT_TIMEOUT_S,
                attempt + 1,
            )
        except json.JSONDecodeError:
            logger.exception("[Extract] Failed to parse extraction JSON")
            return []
        except Exception:
            logger.exception(
                "[Extract] LLM extraction failed (attempt %d/3)",
                attempt + 1,
            )
        if attempt < 2:
            await asyncio.sleep(1.5 * (attempt + 1))

    return []


async def extract_conversation_insights(
    turns: list[dict],
    *,
    endpoint: str,
    api_key: str,
    model: str,
    llm: bool = True,
    emitted_keys: dict[str, str] | None = None,
) -> list[dict]:
    """Heuristic during call; LLM synthesis after call ends."""
    emitted = emitted_keys if emitted_keys is not None else {}
    if not llm:
        return extract_new_insights(turns, emitted)
    llm_insights = await extract_conversation_insights_llm(
        turns, endpoint=endpoint, api_key=api_key, model=model
    )
    new: list[dict] = []
    for item in llm_insights:
        key = item.get("key") or _slug(item.get("value", "")[:32])
        value = item["value"]
        if emitted.get(key) == value:
            continue
        item = dict(item)
        item["replace"] = key in emitted
        emitted[key] = value
        new.append(item)
    if new:
        return new
    return extract_new_insights(turns, emitted)

