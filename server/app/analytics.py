"""Aggregate analytics computed from saved Cosmos call records.

Everything here is derived from fields the app already persists per call
(see call_store / usage_cost): startedAt, durationSec, channel, the per-turn
``metrics`` array, ``keyDetails``, and ``costBreakdown``.

Metrics that have no ground truth stored (ASR/WER accuracy, extraction accuracy
vs. truth, call-resolution outcomes, objection tags) are intentionally NOT
fabricated. They are reframed into things we can actually measure — field
capture rate, average extraction confidence, latency within SLA, etc.
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from app import call_store
from app.metrics_latency import derive_e2e_response_ms, derive_first_audio_ms
from app.usage_cost import enrich_call_record

logger = logging.getLogger(__name__)

_SLA_E2E_S = float(os.getenv("ANALYTICS_E2E_SLA_S", os.getenv("ANALYTICS_LATENCY_SLA_S", "1.2")))
_SLA_TTFA_S = float(os.getenv("ANALYTICS_TTFA_SLA_S", "0.6"))
_POC_TTFA_P50_MS = float(os.getenv("ANALYTICS_TTFA_P50_MS", "600"))
_POC_TTFA_P95_MS = float(os.getenv("ANALYTICS_TTFA_P95_MS", "900"))
_POC_E2E_P50_MS = float(os.getenv("ANALYTICS_E2E_P50_MS", "1200"))
_POC_E2E_P95_MS = float(os.getenv("ANALYTICS_E2E_P95_MS", "2000"))
_RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90}

# Field name → 2-letter code for the property-state map (US states + DC).
_STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
_ABBR_NAME = {v: k.title() for k, v in _STATE_ABBR.items()}
_VALID_ABBR = set(_STATE_ABBR.values())


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _num(v):
    try:
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _pctile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _avg(values: list[float]) -> float:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    m, sec = divmod(s, 60)
    return f"{m}m {sec:02d}s" if m else f"{sec}s"


def _delta(cur: float, prev: float, *, higher_is_better: bool, as_seconds: bool = False) -> dict | None:
    """Build a KPI delta block vs. the previous equal-length window."""
    if prev is None or prev == 0:
        return None
    diff = cur - prev
    if abs(diff) < 1e-9:
        return {"delta": "flat vs prev", "dir": "flat", "tone": "flat"}
    direction = "up" if diff > 0 else "down"
    if as_seconds:
        text = f"{abs(diff):.2f}s vs prev"
    else:
        text = f"{abs(diff / prev) * 100:.0f}% vs prev"
    improved = (diff > 0) == higher_is_better
    return {"delta": text, "dir": direction, "tone": "good" if improved else "bad"}


def _kpi(label, value, *, unit=None, note=None, delta=None):
    row = {"label": label, "value": value}
    if unit:
        row["unit"] = unit
    if note:
        row["note"] = note
    if delta:
        row.update(delta)
    return row


def _fmt_ms_display(ms: float) -> dict:
    """Format milliseconds for KPI display (ms if under 1s, else seconds)."""
    if ms >= 1000:
        return {"value": f"{ms / 1000:.2f}", "unit": "s"}
    return {"value": str(round(ms)), "unit": "ms"}


def _latency_percentiles(ms_values: list[float]) -> dict:
    if not ms_values:
        return {
            "p50": {"value": "—", "unit": None},
            "p95": {"value": "—", "unit": None},
            "p50Ms": None,
            "p95Ms": None,
        }
    p50_ms = _pctile(ms_values, 50)
    p95_ms = _pctile(ms_values, 95)
    return {
        "p50": _fmt_ms_display(p50_ms),
        "p95": _fmt_ms_display(p95_ms),
        "p50Ms": p50_ms,
        "p95Ms": p95_ms,
    }


def _poc_targets(p50_ms: float | None, p95_ms: float | None, *, p50_target_ms: float, p95_target_ms: float) -> dict:
    return {
        "p50Ms": p50_target_ms,
        "p95Ms": p95_target_ms,
        "p50Label": _fmt_ms_display(p50_target_ms),
        "p95Label": _fmt_ms_display(p95_target_ms),
        "p50Met": p50_ms is not None and p50_ms < p50_target_ms,
        "p95Met": p95_ms is not None and p95_ms < p95_target_ms,
    }

def _details(call: dict) -> list[dict]:
    kd = call.get("keyDetails")
    return kd if isinstance(kd, list) else []


def _find_detail(call: dict, *needles: str) -> str | None:
    """Return the value of the first key detail whose key/label matches a needle."""
    for d in _details(call):
        text = f"{d.get('key', '')} {d.get('label', '')}".lower()
        if any(n in text for n in needles):
            val = (d.get("value") or "").strip()
            if val:
                return val
    return None


def _bucket_purpose(v: str) -> str:
    s = v.lower()
    if "cash" in s:
        return "Cash-out"
    if "heloc" in s or "equity" in s or "line of credit" in s:
        return "HELOC"
    if "refinan" in s:
        return "Refinance"
    if "purchase" in s or "buy" in s or "home" in s:
        return "Purchase"
    return "Other"


def _bucket_timeline(v: str) -> str:
    s = v.lower()
    if any(k in s for k in ("immediate", "asap", "right away", "30 day", "this month", "now")):
        return "Immediate"
    if any(k in s for k in ("1-3", "1 to 3", "one to three", "few month", "couple month", "2 month", "3 month")):
        return "1-3 months"
    if any(k in s for k in ("3-6", "three to six", "6 month", "six month", "later this year")):
        return "3-6 months"
    if any(k in s for k in ("research", "explor", "browsing", "early", "just looking", "not sure")):
        return "Exploring"
    return "Other"


def _bucket_credit(v: str) -> str | None:
    m = re.search(r"\b(\d{3})\b", v)
    if not m:
        s = v.lower()
        if "excellent" in s or "760" in s:
            return "760+"
        if "good" in s:
            return "700-759"
        if "fair" in s:
            return "640-699"
        if "poor" in s or "low" in s:
            return "< 640"
        return None
    n = int(m.group(1))
    if not (300 <= n <= 850):
        return None
    if n >= 760:
        return "760+"
    if n >= 700:
        return "700-759"
    if n >= 640:
        return "640-699"
    return "< 640"


def _state_code(v: str) -> str | None:
    s = v.strip().lower()
    if s in _STATE_ABBR:
        return _STATE_ABBR[s]
    up = v.strip().upper()
    if up in _VALID_ABBR:
        return up
    # value like "Austin, Texas" → match any state name contained
    for name, ab in _STATE_ABBR.items():
        if re.search(rf"\b{re.escape(name)}\b", s):
            return ab
    return None


def _pct_list(counter: Counter, order: list[str] | None = None) -> list[dict]:
    total = sum(counter.values())
    if not total:
        return []
    keys = order if order else [k for k, _ in counter.most_common()]
    out = []
    for k in keys:
        if counter.get(k):
            out.append({"label": k, "value": round(counter[k] / total * 100)})
    return out


# --------------------------------------------------------------------------
# Window aggregation
# --------------------------------------------------------------------------

def _aggregate(calls: list[dict]) -> dict:
    """Compute every numeric we need from one window of call docs."""
    n = len(calls)
    durations, detail_counts, total_cost = [], [], 0.0
    turn_latencies_ms = []  # caller e2e (borrower stop → response done)
    ttfa_ms = []  # caller first audio (borrower stop → first sound)
    stt, think, tts, tts_start = [], [], [], []
    confidences = []
    purpose, timeline, credit = Counter(), Counter(), Counter()
    states = Counter()
    channels = Counter()
    field_calls = Counter()  # label → # calls that captured it
    conf_bucket = Counter()  # High / Approximate / Vague
    within_sla = 0
    ttfa_within_sla = 0
    sla_e2e_ms = _SLA_E2E_S * 1000
    sla_ttfa_ms = _SLA_TTFA_S * 1000
    calls_with_summary = 0
    daily = defaultdict(int)
    daily_lat = defaultdict(list)
    daily_first_audio = defaultdict(list)

    for call in calls:
        d = _num(call.get("durationSec"))
        if d is not None:
            durations.append(d)
        kd = _details(call)
        detail_counts.append(len(kd))
        cb = call.get("costBreakdown") or {}
        total_cost += _num(cb.get("totalUsd")) or _num(call.get("callCostUsd")) or 0.0
        channels[(call.get("channel") or "web")] += 1
        if (call.get("callSummary") or "").strip():
            calls_with_summary += 1

        started = _parse_dt(call.get("startedAt"))
        day_key = started.date().isoformat() if started else None

        for d_row in kd:
            label = (d_row.get("label") or d_row.get("key") or "").strip()
            if label:
                field_calls[label] += 1
            c = _num(d_row.get("confidence"))
            if c is not None:
                confidences.append(c)
                conf_bucket["High" if c >= 0.85 else "Approximate" if c >= 0.6 else "Vague"] += 1

        v = _find_detail(call, "purpose", "loan_type", "loan")
        if v:
            purpose[_bucket_purpose(v)] += 1
        v = _find_detail(call, "timeline", "urgency")
        if v:
            timeline[_bucket_timeline(v)] += 1
        v = _find_detail(call, "credit", "score")
        if v and (b := _bucket_credit(v)):
            credit[b] += 1
        v = _find_detail(call, "state", "location")
        if v and (code := _state_code(v)):
            states[code] += 1

        metrics = call.get("metrics") if isinstance(call.get("metrics"), list) else []
        for row in metrics:
            m = row.get("metrics") if isinstance(row, dict) else None
            if not isinstance(m, dict):
                continue
            rms = derive_e2e_response_ms(m)
            if rms is not None:
                turn_latencies_ms.append(rms)
                within_sla += 1 if rms <= sla_e2e_ms else 0
                if day_key:
                    daily_lat[day_key].append(rms)
            ttfa = derive_first_audio_ms(m)
            if ttfa is not None:
                ttfa_ms.append(ttfa)
                ttfa_within_sla += 1 if ttfa <= sla_ttfa_ms else 0
                if day_key:
                    daily_first_audio[day_key].append(ttfa)
            for src, bucket in (("sttMs", stt), ("thinkMs", think), ("ttsMs", tts), ("ttsStartMs", tts_start)):
                mv = _num(m.get(src))
                if mv is not None:
                    bucket.append(mv)
        if day_key:
            daily[day_key] += 1

    return {
        "n": n,
        "durations": durations,
        "avg_duration": _avg(durations),
        "avg_details": _avg([float(c) for c in detail_counts]),
        "total_cost": total_cost,
        "lat_ms": turn_latencies_ms,
        "ttfa_ms": ttfa_ms,
        "ttfa_p50_ms": _pctile(ttfa_ms, 50),
        "ttfa_p95_ms": _pctile(ttfa_ms, 95),
        "p50_s": _pctile(turn_latencies_ms, 50) / 1000,
        "p95_s": _pctile(turn_latencies_ms, 95) / 1000,
        "stage": {"asr": _avg(stt), "llm": _avg(think), "tts": _avg(tts), "net": _avg(tts_start)},
        "avg_conf": _avg(confidences),
        "within_sla_pct": (within_sla / len(turn_latencies_ms) * 100) if turn_latencies_ms else 0.0,
        "ttfa_within_sla_pct": (ttfa_within_sla / len(ttfa_ms) * 100) if ttfa_ms else 0.0,
        "e2e_avg_s": _avg(turn_latencies_ms) / 1000,
        "ttfa_avg_s": _avg(ttfa_ms) / 1000,
        "summary_pct": (calls_with_summary / n * 100) if n else 0.0,
        "purpose": purpose, "timeline": timeline, "credit": credit,
        "states": states, "channels": channels, "field_calls": field_calls,
        "conf_bucket": conf_bucket,
        "daily": daily, "daily_lat": daily_lat, "daily_first_audio": daily_first_audio,
    }


def _daily_series(daily: dict, days: int):
    today = _now().date()
    labels, values = [], []
    for i in range(days):
        day = today - timedelta(days=days - 1 - i)
        labels.append(day.strftime("%a") if days <= 7 else day.strftime("%m/%d"))
        values.append(daily.get(day.isoformat(), 0))
    return labels, values


def _daily_latency_series(daily_lat: dict, days: int):
    today = _now().date()
    labels, values = [], []
    for i in range(days):
        day = today - timedelta(days=days - 1 - i)
        labels.append(day.strftime("%a") if days <= 7 else day.strftime("%m/%d"))
        vals = daily_lat.get(day.isoformat(), [])
        values.append(round(_avg(vals) / 1000, 2) if vals else 0.0)
    return labels, values


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

async def compute_analytics(range_key: str = "7d") -> dict:
    """Return the analytics payload for the dashboard. Reads Cosmos directly."""
    if not call_store.is_enabled():
        return {"enabled": False, "hasData": False, "range": range_key}

    days = _RANGE_DAYS.get(range_key, 7)
    all_time = range_key == "all"

    if all_time:
        rows = await call_store.query_items(
            "SELECT c.startedAt, c.durationSec, c.channel, c.metrics, c.keyDetails, "
            "c.costBreakdown, c.callCostUsd, c.callSummary FROM c"
        )
        cur_calls, prev_calls = rows, []
    else:
        cutoff = (_now() - timedelta(days=days * 2)).isoformat()
        boundary = (_now() - timedelta(days=days)).isoformat()
        rows = await call_store.query_items(
            "SELECT c.startedAt, c.durationSec, c.channel, c.metrics, c.keyDetails, "
            "c.costBreakdown, c.callCostUsd, c.callSummary FROM c WHERE c.startedAt >= @cutoff",
            [{"name": "@cutoff", "value": cutoff}],
        )
        cur_calls = [c for c in rows if (c.get("startedAt") or "") >= boundary]
        prev_calls = [c for c in rows if (c.get("startedAt") or "") < boundary]

    # Backfill any missing cost rows so totals match the invoice path.
    for c in cur_calls:
        enrich_call_record(c, include_timeline=False)

    if not cur_calls:
        return {"enabled": True, "hasData": False, "range": range_key}

    cur = _aggregate(cur_calls)
    prev = _aggregate(prev_calls) if prev_calls else None

    def pdelta(key, **kw):
        return _delta(cur[key], prev[key], **kw) if prev else None

    freq_labels, freq_values = _daily_series(cur["daily"], days if not all_time else 14)
    lat_labels, lat_values = _daily_latency_series(cur["daily_lat"], days if not all_time else 14)
    ttfa_labels, ttfa_values = _daily_latency_series(cur["daily_first_audio"], days if not all_time else 14)

    top_fields = [
        {"label": label, "value": round(cnt / cur["n"] * 100)}
        for label, cnt in cur["field_calls"].most_common(6)
    ]

    business = {
        "kpis": [
            _kpi("Total calls", str(cur["n"]),
                 delta=_delta(cur["n"], prev["n"], higher_is_better=True) if prev else None),
            _kpi("Avg call duration", _fmt_duration(cur["avg_duration"]),
                 delta=pdelta("avg_duration", higher_is_better=True)),
            _kpi("Avg details / call", f"{cur['avg_details']:.1f}",
                 delta=pdelta("avg_details", higher_is_better=True)),
            _kpi("Total est. cost", f"${cur['total_cost']:.2f}",
                 delta=pdelta("total_cost", higher_is_better=False)),
        ],
        "daily": {"labels": freq_labels, "values": freq_values},
        "avgPerDay": round(cur["n"] / max(1, days if not all_time else 14)),
        "loanPurpose": _pct_list(cur["purpose"],
                                 ["Purchase", "Refinance", "Cash-out", "HELOC", "Other"]),
        "topFields": top_fields,
        "timeline": _pct_list(cur["timeline"],
                              ["Immediate", "1-3 months", "3-6 months", "Exploring", "Other"]),
        "credit": _pct_list(cur["credit"], ["760+", "700-759", "640-699", "< 640"]),
        "states": dict(cur["states"]),
        "channels": _pct_list(cur["channels"]),
    }
    if cur["states"]:
        ab, cnt = cur["states"].most_common(1)[0]
        business["statePeak"] = {"name": _ABBR_NAME.get(ab, ab), "count": cnt}

    st = cur["stage"]
    ttfa_pct = _latency_percentiles(cur["ttfa_ms"])
    e2e_pct = _latency_percentiles(cur["lat_ms"])
    ai = {
        "latencySummary": [
            {
                "label": "Time to first audio",
                "note": "Caller stops speaking → agent starts speaking · matches Call History",
                "p50": ttfa_pct["p50"],
                "p95": ttfa_pct["p95"],
                "targets": _poc_targets(
                    ttfa_pct["p50Ms"], ttfa_pct["p95Ms"],
                    p50_target_ms=_POC_TTFA_P50_MS, p95_target_ms=_POC_TTFA_P95_MS,
                ),
            },
            {
                "label": "End-to-end response time",
                "note": "Caller stops speaking → agent finishes response · matches Call History",
                "p50": e2e_pct["p50"],
                "p95": e2e_pct["p95"],
                "targets": _poc_targets(
                    e2e_pct["p50Ms"], e2e_pct["p95Ms"],
                    p50_target_ms=_POC_E2E_P50_MS, p95_target_ms=_POC_E2E_P95_MS,
                ),
            },
        ],
        "kpis": [
            _kpi("Avg field confidence", f"{cur['avg_conf']:.2f}",
                 delta=pdelta("avg_conf", higher_is_better=True)),
            _kpi("TTFA within SLA", f"{cur['ttfa_within_sla_pct']:.0f}", unit="%",
                 note=f"≤{_SLA_TTFA_S:g}s",
                 delta=pdelta("ttfa_within_sla_pct", higher_is_better=True)),
            _kpi("E2E within SLA", f"{cur['within_sla_pct']:.0f}", unit="%",
                 note=f"≤{_SLA_E2E_S:g}s",
                 delta=pdelta("within_sla_pct", higher_is_better=True)),
            _kpi("Calls with summary", f"{cur['summary_pct']:.0f}", unit="%",
                 delta=pdelta("summary_pct", higher_is_better=True)),
        ],
        "latencyFirstAudio": {"labels": ttfa_labels, "values": ttfa_values, "sla": _SLA_TTFA_S},
        "latencyE2e": {"labels": lat_labels, "values": lat_values, "sla": _SLA_E2E_S},
        "stage": [
            {"label": "ASR (speech→text)", "ms": round(st["asr"])},
            {"label": "LLM (response)", "ms": round(st["llm"])},
            {"label": "TTS (text→speech)", "ms": round(st["tts"])},
            {"label": "Model / network overhead", "ms": round(st["net"])},
        ],
        "e2eAvgS": round(cur["e2e_avg_s"], 2),
        "ttfaAvgS": round(cur["ttfa_avg_s"], 2),
        "captureByField": top_fields,
        "confidenceDist": _pct_list(cur["conf_bucket"], ["High", "Approximate", "Vague"]),
        "reliability": [
            {"label": "Avg field confidence", "value": round(cur["avg_conf"] * 100),
             "display": f"{cur['avg_conf']:.2f}"},
            {"label": "TTFA within SLA", "value": round(cur["ttfa_within_sla_pct"]),
             "display": f"{cur['ttfa_within_sla_pct']:.0f}%"},
            {"label": "E2E within SLA", "value": round(cur["within_sla_pct"]),
             "display": f"{cur['within_sla_pct']:.0f}%"},
            {"label": "Calls with summary", "value": round(cur["summary_pct"]),
             "display": f"{cur['summary_pct']:.0f}%"},
        ],
    }

    return {
        "enabled": True,
        "hasData": True,
        "range": range_key,
        "callCount": cur["n"],
        "business": business,
        "ai": ai,
    }
