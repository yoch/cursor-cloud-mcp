"""Registre des secrets du processus et masquage des textes sortants.

Les logs restent des métadonnées. Ce masquage est une seconde barrière : il couvre le
message, la traceback et la pile, ainsi que les valeurs transmises pendant une opération.
"""

import logging
import re
import threading
from collections import OrderedDict

REDACTED = "[redacted]"
# Sous cette longueur, une valeur n'est masquée que comme mot entier : « en » ne doit pas
# mutiler « agent », ni « 1 » transformer 401 en autre chose.
_WHOLE_WORD_BELOW = 8
# Valeurs par appel retenues ; au-delà, la plus ancienne sort. La clé et les valeurs
# de CURSOR_MCP_FORWARD_ENV, enregistrées comme permanentes, ne sortent jamais.
_MAX_RECENT = 4096

_lock = threading.Lock()
_permanent: set[str] = set()
_recent: OrderedDict[str, None] = OrderedDict()
_pattern: re.Pattern[str] | None = None


def register(*values: str | None, permanent: bool = False) -> None:
    """Ajoute des valeurs à masquer. ``permanent`` les garde toute la vie du processus."""
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
    """Masque les chaînes d'une structure JSON sans toucher à sa forme ni à ses clés."""
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
        # Le plus long d'abord : un secret contenu dans un autre ne laisse pas de reste.
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
    """Masque le texte final, traceback et pile comprises."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))

    def formatException(self, ei: object) -> str:
        return redact(super().formatException(ei))  # type: ignore[arg-type]

    def formatStack(self, stack_info: str) -> str:
        return redact(super().formatStack(stack_info))
