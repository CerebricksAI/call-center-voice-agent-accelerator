"""LLM-driven extraction of key conversation details (no fixed form schema).

Domain-agnostic by design: keys, labels, and values are derived purely from what
is said on each call — the agent's question paired with the caller's answer — not
from any hard-coded field taxonomy. The live paths are the Voice Live text
sessions in llm_extract_insights() (per-turn key details) and
llm_generate_call_summary() (end-of-call summary).
"""

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

_EXTRACT_SYSTEM = """You read a live transcript of a phone call between an AGENT and a human CALLER, and extract the key details the caller stated as structured fields shown live on a dashboard. Each line is marked Agent or Caller.

The call can be about ANYTHING (support, scheduling, intake, screening, sales, services, healthcare, HR, anything). You have NO domain knowledge and NO expected or predefined fields. Every key, label, and value must emerge purely from what was actually said in THIS conversation — never from assumptions about what this type of call "usually" involves.

Work by SEMANTIC QUESTION/ANSWER PAIRING: read every caller answer in the context of the agent's preceding question or prompt. The agent's question tells you the TOPIC (use it to derive the key and label); the caller's reply gives you the VALUE. An answer like "a couple months" is only meaningful next to the question it answers.

ANTI-HALLUCINATION (highest priority):
- Extract ONLY facts the CALLER explicitly stated or clearly confirmed (e.g. answered "yes" or "correct" to a direct question). The evidence must be in the caller's own words.
- NEVER infer, guess, assume, complete, or fabricate a fact the caller did not state.
- The agent's questions, examples, and listed options are NOT facts, even if the caller stayed silent — unless the caller explicitly adopts them.
- Do not normalize a value into something the caller did not say; keep their meaning. You may fix obvious speech-to-text errors only.
- If anything is uncertain, ambiguous, or only implied, OMIT it. Missing data is correct; fabrication is not.

Return JSON only, exactly this shape:
{"insights":[{"key":"snake_case_slug","label":"Short human label","value":"brief value","confidence":0.0-1.0}]}

- key: a generic snake_case slug naming the topic in plain language, derived from the question's meaning (e.g. timeline, reason_for_call, preferred_date, party_size). No industry jargon, no domain assumptions.
- label: a short human noun phrase for that topic (e.g. "Timeline", "Reason For Call", "Preferred Date").
- value: a brief phrase from what the caller said (e.g. "~2 months", "broken heater", "Friday afternoon"). Never a full sentence; never begin with "The caller".
- Never emit placeholder values like "not provided", "unknown", "N/A", or "none" — omit the field entirely instead.
- confidence:
  - 0.9-1.0: stated clearly and directly, no hedging
  - 0.65-0.8: approximate or hedged ("around", "about", "roughly", "I think", ranges)
  - 0.4-0.6: vague but still stated
  - not stated -> omit the field; do not use low confidence as a stand-in for missing data

Examples (cross-domain, illustrative only — not a menu of expected fields):
- Agent: "What's your timeline?" Caller: "a couple months" => {"key":"timeline","label":"Timeline","value":"~2 months","confidence":0.75}
- Agent: "What seems to be the issue?" Caller: "my heater stopped working" => {"key":"reported_issue","label":"Reported Issue","value":"heater not working","confidence":0.95}
- Agent: "How many people in your party?" Caller: "maybe four" => {"key":"party_size","label":"Party Size","value":"~4","confidence":0.7}

If the caller corrects an earlier fact, re-emit it with the SAME key and the new value. Do not repeat anything listed under "Already extracted" unless you are correcting it. Return {"insights":[]} when there is nothing new to add."""

SUMMARY_SYSTEM = """You write an end-of-call summary of a phone call between an AGENT and a human CALLER, for handoff to whoever handles the call next. Read the ENTIRE transcript. The call can be about anything, and you have no assumptions about what it should contain — summarize only what actually happened in this conversation.

Your summary must reflect everything material the caller stated: do not omit names, organizations, numbers, dates, amounts, identifiers, preferences, decisions, confirmations or consents, and any agreed next steps. Capture the reason for the call, what was discussed and decided, any unresolved or pending items, and what should happen next, so the next person can pick up without re-listening to the call. Do not invent or infer details the caller did not state, and do not normalize values into something that was not said.

Write 2-3 concise prose paragraphs in plain language. No bullet points, no markdown, no headings, no domain-specific jargon. Be complete and factual, attribute facts to the caller where it matters, and separate paragraphs with one blank line."""

_SUMMARY_SYSTEM_COMPACT = """\
Write a factual end-of-call handoff summary from the transcript (and key facts if provided).
Include: reason for call, what the caller stated (names, places, numbers, amounts, dates, preferences, consents), decisions, open items, next steps.
2 short prose paragraphs, plain language, no bullets/markdown. Do not invent facts."""

EXTRACT_SYSTEM = _EXTRACT_SYSTEM

_EXTRACT_TIMEOUT_S = float(os.getenv("EXTRACT_TIMEOUT_S", "30"))
_SUMMARY_TIMEOUT_S = float(os.getenv("SUMMARY_TIMEOUT_S", "20"))
_SUMMARY_MAX_CHARS = int(os.getenv("SUMMARY_MAX_CHARS", "5000"))
_SUMMARY_MAX_TURNS = int(os.getenv("SUMMARY_MAX_TURNS", "32"))
_SUMMARY_MAX_OUTPUT_TOKENS = int(os.getenv("SUMMARY_MAX_OUTPUT_TOKENS", "220"))
_SUMMARY_HEAD_TURNS = int(os.getenv("SUMMARY_HEAD_TURNS", "4"))
_SUMMARY_TAIL_TURNS = int(os.getenv("SUMMARY_TAIL_TURNS", "16"))
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
    """Human label for a fact. Prefer the model's label; else title-case the key.

    Fully generic — no domain-specific key→label mapping, so any topic the call
    surfaces gets a sensible label.
    """
    label = (explicit or "").strip()
    if label:
        return label
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


def _summary_system_instructions() -> str:
    mode = os.getenv("SUMMARY_PROMPT_MODE", "compact").strip().lower()
    if mode == "full":
        return SUMMARY_SYSTEM
    return _SUMMARY_SYSTEM_COMPACT


def _trim_turns_for_summary(turns: list[dict]) -> list[dict]:
    """Keep prompt bounded: head+tail when long, else drop middle until under char cap."""
    if not turns:
        return []
    kept = list(turns)
    if len(kept) > _SUMMARY_HEAD_TURNS + _SUMMARY_TAIL_TURNS:
        head = kept[: max(1, _SUMMARY_HEAD_TURNS)]
        tail = kept[-max(1, _SUMMARY_TAIL_TURNS) :]
        kept = head + tail
    while len(kept) > 4 and len(_format_transcript(kept)) > _SUMMARY_MAX_CHARS:
        kept.pop(len(kept) // 2)
    while len(kept) > _SUMMARY_MAX_TURNS:
        kept.pop(len(kept) // 2)
    return kept


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
        "Summarize this call in 2 short prose paragraphs. "
        "Include every specific fact the caller stated — names, places, numbers, "
        "amounts, dates, preferences, consents, and next steps. Do not invent facts."
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
                        instructions=_summary_system_instructions(),
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
