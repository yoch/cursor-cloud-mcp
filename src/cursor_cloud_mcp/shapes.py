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
