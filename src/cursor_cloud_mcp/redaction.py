"""Registre des secrets du processus et masquage des textes sortants.

Les logs restent des métadonnées. Ce masquage est une seconde barrière : il couvre le
message, la traceback et la pile, ainsi que les valeurs transmises pendant une opération.
"""

import logging
import threading

REDACTED = "[redacted]"
_MAX_SECRETS = 2048

_lock = threading.Lock()
_secrets: set[str] = set()


def register(*values: str | None) -> None:
    """Ajoute des valeurs à masquer pour toute la durée du processus."""
    with _lock:
        for value in values:
            if value and len(_secrets) < _MAX_SECRETS:
                _secrets.add(value)


def redact(text: str) -> str:
    with _lock:
        # Le plus long d'abord : un secret contenu dans un autre ne laisse pas de reste.
        ordered = sorted(_secrets, key=len, reverse=True)
    for secret in ordered:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


class RedactingFormatter(logging.Formatter):
    """Masque le texte final, traceback et pile comprises."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))

    def formatException(self, ei: object) -> str:
        return redact(super().formatException(ei))  # type: ignore[arg-type]

    def formatStack(self, stack_info: str) -> str:
        return redact(super().formatStack(stack_info))
