"""Configuration lue uniquement depuis l'environnement du processus."""

import os
import re
from dataclasses import dataclass

API_BASE = "https://api.cursor.com"
PROMPT_MAX_CHARS = 100_000
NAME_MAX_CHARS = 100
RESULT_DEFAULT_LIMIT = 12_000
RESULT_MAX_LIMIT = 20_000
DEFAULT_DEADLINE_SECONDS = 40.0
REPOSITORIES_DEADLINE_SECONDS = 90.0
REPOSITORY_CACHE_TTL_SECONDS = 300.0
MAX_RESPONSE_BYTES = 8_000_000
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
_UNEXPANDED = re.compile(r"^\$\{[^}]*\}$|^\{env:[^}]*\}$")


@dataclass(frozen=True)
class Settings:
    """Réglages du processus. ``api_key`` vaut ``None`` si elle est absente."""

    api_key: str | None
    allow_writes: bool
    log_level: str
    fixture: bool
    config_error: str | None


def load_settings() -> Settings:
    """Lit l'environnement. Ne charge aucun fichier ``.env``."""
    raw_key = os.environ.get("CURSOR_API_KEY")
    api_key, config_error = _interpret_key(raw_key)
    raw_level = os.environ.get("CURSOR_MCP_LOG_LEVEL", "INFO").upper()
    log_level = raw_level if raw_level in _LOG_LEVELS else "INFO"
    return Settings(
        api_key=api_key,
        allow_writes=os.environ.get("CURSOR_MCP_ALLOW_WRITES", "0") == "1",
        log_level=log_level,
        fixture=os.environ.get("CURSOR_MCP_FIXTURE") == "1",
        config_error=config_error,
    )


def _interpret_key(raw: str | None) -> tuple[str | None, str | None]:
    if raw is None:
        return None, None
    value = raw.strip()
    if value == "":
        return None, "CURSOR_API_KEY est vide."
    if _UNEXPANDED.match(value) or "${" in value or "{env:" in value:
        return None, "CURSOR_API_KEY n'est pas interpolée. Le serveur ne charge pas de fichier .env."
    return value, None
