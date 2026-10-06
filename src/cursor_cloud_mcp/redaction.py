"""Registry of the process secrets and masking of outgoing texts.

Logs stay metadata. This masking is a second barrier: it covers the
message, the traceback and the stack, as well as values passed during an operation.
"""

import logging
import re
import threading
from collections import OrderedDict

REDACTED = "[redacted]"
# Below this length, a value is masked only as a whole word: "en" must not
# mangle "agent", nor "1" turn 401 into something else.
_WHOLE_WORD_BELOW = 8
# Per-call values kept; beyond that, the oldest drops out. The key and the values
# of CURSOR_MCP_FORWARD_ENV, registered as permanent, never drop out.
_MAX_RECENT = 4096

_lock = threading.Lock()
_permanent: set[str] = set()
_recent: OrderedDict[str, None] = OrderedDict()
_pattern: re.Pattern[str] | None = None


def register(*values: str | None, permanent: bool = False) -> None:
    """Add values to mask. ``permanent`` keeps them for the whole life of the process."""
    global _pattern
    with _lock:
        for value in values:
            if not value:
                continue
            if permanent:
                _permanent.add(value)
                _recent.pop(value, None)
            elif value not in _permanent:
                _recent[value] = None
                _recent.move_to_end(value)
                while len(_recent) > _MAX_RECENT:
                    _recent.popitem(last=False)
        _pattern = None


def redact(text: str) -> str:
    pattern = _compiled()
    if pattern is None:
        return text
    return pattern.sub(REDACTED, text)


def redact_value(value: object) -> object:
    """Mask the strings of a JSON structure without touching its shape or its keys."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


def _compiled() -> re.Pattern[str] | None:
    global _pattern
    with _lock:
        if _pattern is not None or not (_permanent or _recent):
            return _pattern
        # Longest first: a secret contained in another leaves no remainder.
        ordered = sorted(_permanent | set(_recent), key=len, reverse=True)
        parts = [
            re.escape(secret)
            if len(secret) >= _WHOLE_WORD_BELOW
            else rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])"
            for secret in ordered
        ]
        _pattern = re.compile("|".join(parts))
        return _pattern


class RedactingFormatter(logging.Formatter):
    """Mask the final text, traceback and stack included."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))

    def formatException(self, ei: object) -> str:
        return redact(super().formatException(ei))  # type: ignore[arg-type]

    def formatStack(self, stack_info: str) -> str:
        return redact(super().formatStack(stack_info))
