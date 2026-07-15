"""Tools — the agent's hands and the paper trail.

Every promise the agent speaks must correspond to a tool record. Tools are
registered on the Voice Live session as typed ``FunctionTool``s; when the model
calls one, ``execute_tool`` runs it, mutates the CallContext, appends to the
tool log (and optionally a stub CRM file), and returns a result the handler sends
back as a ``FunctionCallOutputItem``.

``end_call`` is the single guarded exit: it is REFUSED until a disposition exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from azure.ai.voicelive.models import FunctionTool

# --- Tool specifications (JSON-Schema parameters) --------------------------

_STR = {"type": "string"}

TOOL_SPECS: dict[str, dict[str, Any]] = {
    "capture_borrower_field": {
        "description": "Record one qualification field the caller provided.",
        "parameters": {
            "type": "object",
            "properties": {
                "field": _STR,
                "value": _STR,
                "confidence": {"type": "number"},
            },
            "required": ["field", "value"],
        },
    },
    "schedule_callback": {
        "description": "Schedule a callback from a licensed loan officer.",
        "parameters": {
            "type": "object",
            "properties": {"preferred_time": _STR, "channel": _STR},
            "required": ["preferred_time"],
        },
    },
    "transfer_to_lo": {
        "description": "Warm-transfer the call to a licensed loan officer now.",
        "parameters": {
            "type": "object",
            "properties": {"reason": _STR, "context_summary": _STR},
            "required": ["reason"],
        },
    },
    "add_to_do_not_call": {
        "description": "Record a do-not-call request. Fired by code before the close speaks.",
        "parameters": {
            "type": "object",
            "properties": {"reason": _STR},
        },
    },
    "log_disposition": {
        "description": "Record the final outcome of the call.",
        "parameters": {
            "type": "object",
            "properties": {"disposition": _STR},
            "required": ["disposition"],
        },
    },
    "route_language": {
        "description": "Switch or route the call to another language.",
        "parameters": {
            "type": "object",
            "properties": {"language": _STR, "action": _STR},
            "required": ["language"],
        },
    },
    "end_call": {
        "description": "End the call. Refused unless a disposition has been recorded.",
        "parameters": {
            "type": "object",
            "properties": {"reason": _STR},
        },
    },
}

# Which tools the model may call in each state (mirrors each skill's "Tools allowed").
TOOLS_FOR_STATE: dict[str, list[str]] = {
    "GREETING": ["end_call"],
    "QUALIFY": ["capture_borrower_field", "schedule_callback", "transfer_to_lo", "end_call"],
    "DECLINE_CLOSE": ["log_disposition", "end_call"],
    "DNC_CLOSE": ["end_call"],
    "CALLBACK_CLOSE": ["schedule_callback", "log_disposition", "capture_borrower_field", "end_call"],
    "NO_RESPONSE_CLOSE": ["end_call"],
    "TRANSFER": ["capture_borrower_field", "transfer_to_lo", "end_call"],
    "LANGUAGE_ROUTE": ["route_language", "transfer_to_lo", "schedule_callback", "end_call"],
}


def tools_for(state: str) -> list[str]:
    return list(TOOLS_FOR_STATE.get(state, ["end_call"]))


def function_tools(names: list[str]) -> list[FunctionTool]:
    """Build typed FunctionTool objects for the given tool names."""
    return [
        FunctionTool(
            name=name,
            description=TOOL_SPECS[name]["description"],
            parameters=TOOL_SPECS[name]["parameters"],
        )
        for name in names
        if name in TOOL_SPECS
    ]


# --- Execution + paper trail ------------------------------------------------

def jsonl_sink(path: str | Path) -> Callable[[str, dict[str, Any]], None]:
    """A sink that appends each tool record as a JSON line to a stub CRM file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    def _write(call_id: str, record: dict[str, Any]) -> None:
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"call_id": call_id, **record}) + "\n")

    return _write


def execute_tool(
    name: str,
    args: dict[str, Any] | None,
    ctx: Any,
    *,
    sink: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run a tool against the CallContext and return a result for the model."""
    args = args or {}

    if name == "end_call":
        if getattr(ctx, "disposition", None) is None:
            result: dict[str, Any] = {"ok": False, "error": "refused: no disposition recorded"}
        else:
            ctx.ended = True
            result = {"ok": True}
    elif name == "add_to_do_not_call":
        ctx.dnc_recorded = True
        # Stable display id for the trust-console promise ledger (not a CRM primary key).
        rid = f"DNC {(getattr(ctx, 'call_id', None) or 'local').replace('-', '')[-4:].upper() or '0000'}"
        result = {"ok": True, "record_id": rid}
    elif name == "log_disposition":
        ctx.disposition = str(args.get("disposition") or getattr(ctx, "disposition", None) or "unknown")
        result = {"ok": True, "disposition": ctx.disposition}
    elif name == "schedule_callback":
        ctx.callback_scheduled = True
        # Record the outcome so a follow-up end_call isn't refused (a hand-off tool
        # called directly by the model would otherwise leave no disposition).
        if getattr(ctx, "disposition", None) is None:
            ctx.disposition = "callback_requested"
        rid = f"CB {(getattr(ctx, 'call_id', None) or 'local').replace('-', '')[-4:].upper() or '0000'}"
        result = {
            "ok": True,
            "preferred_time": args.get("preferred_time"),
            "record_id": rid,
        }
    elif name == "capture_borrower_field":
        field_name = args.get("field")
        if field_name:
            ctx.fields[field_name] = {
                "value": args.get("value"),
                "confidence": args.get("confidence"),
            }
        result = {"ok": bool(field_name)}
    elif name in ("transfer_to_lo", "route_language"):
        # Hand-off tools record their outcome too, so end_call can proceed and the
        # call can actually close after a model-initiated transfer/route.
        if getattr(ctx, "disposition", None) is None:
            ctx.disposition = "transferred" if name == "transfer_to_lo" else "language_routed"
        result = {"ok": True}
    else:
        result = {"ok": False, "error": f"unknown tool {name!r}"}

    record = {"tool": name, "args": args, "result": result}
    ctx.tool_log.append(record)
    if sink is not None:
        sink(getattr(ctx, "call_id", "local"), record)
    return result
