"""CLI event schema — single source of truth for all event types.

Events are plain dicts with a "type" key. Constructor functions enforce
consistent shapes. This module is imported by cli.py (producer),
worker.py (consumer/broadcaster), governor.py (consumer), and
queue_routes.py (SSE serialization).

Event types:
    _raw              Internal — raw NDJSON line for DB persistence. Never broadcast.
    text              Mid-turn text content (before a tool_use, or trailing after tools).
    thinking          Extended thinking block content.
    tool_use          Agent invoked a tool.
    assistant_complete Text-only assistant turn (no tool invocations). Stored as response.
    result_meta       CLI session ID + execution metadata (stop_reason, turns, cost).
    session_id        Early session ID from CLI init/system events.
    error             Error from CLI, cancellation, or streaming failure.
    done              End-of-stream signal for task SSE subscribers.
    queue_changed     Global signal that queue state changed (task enqueued/transitioned).
"""

import json


# ── Event constructors ────────────────────────────────────

def raw(raw_json: str, event_type: str) -> dict:
    """Raw NDJSON line from CLI — persisted to chat_events, never broadcast."""
    return {"type": "_raw", "raw_json": raw_json, "event_type": event_type}


def text(content: str) -> dict:
    """Text content from an assistant turn (mid-turn preamble or trailing text)."""
    return {"type": "text", "content": content}


def thinking(content: str) -> dict:
    """Extended thinking block content."""
    return {"type": "thinking", "content": content}


def tool_use(tool: str, input: dict) -> dict:
    """Agent invoked a tool."""
    return {"type": "tool_use", "tool": tool, "input": input}


def assistant_complete(content: str) -> dict:
    """Complete assistant response from a text-only turn (no tool invocations)."""
    return {"type": "assistant_complete", "content": content}


def result_meta(
    cli_session_id: str | None = None,
    stop_reason: str | None = None,
    num_turns: int | None = None,
    cost_usd: float | None = None,
) -> dict:
    """Execution metadata from the CLI result event."""
    event = {"type": "result_meta"}
    if cli_session_id:
        event["cli_session_id"] = cli_session_id
    event["stop_reason"] = stop_reason
    event["num_turns"] = num_turns
    event["cost_usd"] = cost_usd
    return event


def session_id(cli_session_id: str) -> dict:
    """Early session ID from CLI system/init events."""
    return {"type": "session_id", "cli_session_id": cli_session_id}


def error(message: str) -> dict:
    """Error from CLI, cancellation, or streaming failure."""
    return {"type": "error", "message": message}


def done() -> dict:
    """End-of-stream signal for task SSE subscribers."""
    return {"type": "done"}


def queue_changed() -> dict:
    """Global signal that queue state changed."""
    return {"type": "queue_changed"}


# ── SSE serialization ─────────────────────────────────────

def to_sse(event: dict) -> dict:
    """Convert an internal event dict to SSE wire format.

    Returns {"event": <name>, "data": <json string>} suitable for
    EventSourceResponse yield.
    """
    etype = event.get("type", "")

    if etype == "done":
        return {"event": "done", "data": json.dumps({"status": "completed"})}

    if etype == "error":
        return {"event": "error", "data": json.dumps(event)}

    if etype == "session_id":
        return {"event": "session_id", "data": json.dumps({"cli_session_id": event.get("cli_session_id")})}

    # Default: use type as event name, send remaining fields as data
    data = {k: v for k, v in event.items() if k != "type"}
    return {"event": etype, "data": json.dumps(data)}
