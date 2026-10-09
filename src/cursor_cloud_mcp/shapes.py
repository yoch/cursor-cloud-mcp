"""Shapes of stream event payloads, shared by the event view and the activity summary."""

import json
from typing import Any

from cursor_cloud_mcp.config import EVENT_TEXT_MAX_CHARS


def parse_payload(data: str) -> object:
    """JSON payload of an event; the raw text if it is not JSON, None if empty."""
    if not data:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return data


def tool_payload(payload: object) -> dict[str, Any]:
    """The fields of a tool_call event. The Cursor SDK also reads them nested under ``data``."""
    if not isinstance(payload, dict):
        return {}
    if "name" not in payload and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def clip(value: object, limit: int = EVENT_TEXT_MAX_CHARS) -> tuple[str | None, bool]:
    """Bounded text and truncation flag. Non-text values are rendered as JSON."""
    if value is None:
        return None, False
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) > limit:
        return text[:limit] + "…", True
    return text, False


# Explicit cut marker of the head + tail form. Counted in the limit.
CLIP_MARKER = "…[clipped]…"


def clip_edges(value: object, limit: int = EVENT_TEXT_MAX_CHARS) -> tuple[str | None, bool]:
    """Bounded text keeping both edges: half head, half tail, an explicit marker between.

    Tool args and results only: the end of an output (a traceback) matters as much as its
    start. The total length, marker included, never exceeds ``limit``; a value that fits is
    returned intact. A limit too small for the marker falls back to the head-only form.
    """
    if value is None:
        return None, False
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= limit:
        return text, False
    budget = limit - len(CLIP_MARKER)
    if budget < 2:
        return text[:limit], True
    head = budget // 2
    return text[:head] + CLIP_MARKER + text[-(budget - head):], True
