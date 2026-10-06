"""Configuration read only from the process environment."""

import os
import re
from dataclasses import dataclass, field

API_BASE = "https://api.cursor.com"
PROMPT_MAX_CHARS = 100_000
NAME_MAX_CHARS = 100
RESULT_DEFAULT_LIMIT = 12_000
RESULT_MAX_LIMIT = 20_000
DEFAULT_DEADLINE_SECONDS = 40.0
REPOSITORIES_DEADLINE_SECONDS = 90.0
CREATE_DEADLINE_SECONDS = 90.0
# Absolute budgets per tool call: reads, re-reads and pauses included.
TOOL_BUDGET_SECONDS = 45.0
CREATE_TOOL_BUDGET_SECONDS = 95.0
CANCEL_TOOL_BUDGET_SECONDS = 45.0
# A mutation is not sent if less than this time remains (or half of its timeout).
MUTATION_MIN_SECONDS = 5.0
REPOSITORY_CACHE_TTL_SECONDS = 300.0
MODEL_CACHE_TTL_SECONDS = 600.0
MAX_RESPONSE_BYTES = 8_000_000
STREAM_MAX_BYTES = 1_000_000
ARTIFACT_MAX_BYTES = 5_000_000
EVENT_TEXT_MAX_CHARS = 500
# Consecutive text fragments merged into one event, up to this size.
EVENT_MERGED_MAX_CHARS = 4000
ENV_MAX_COUNT = 50
ENV_NAME_MAX_BYTES = 255
ENV_VALUE_MAX_BYTES = 4096
REPO_MAX_COUNT = 20
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
_UNEXPANDED = re.compile(r"^\$\{[^}]*\}$|^\{env:[^}]*\}$")
_FIXTURE_WITH_KEY = (
    "CURSOR_MCP_FIXTURE=1 cannot be combined with CURSOR_API_KEY. "
    "Remove the key for simulated mode, or remove CURSOR_MCP_FIXTURE for the real API."
)


@dataclass(frozen=True)
class Settings:
    """Process settings. ``api_key`` is ``None`` when it is absent."""

    api_key: str | None
    allow_writes: bool
    log_level: str
    fixture: bool
    config_error: str | None
    allow_delete: bool = False
    forward_env: frozenset[str] = field(default_factory=frozenset)


def load_settings() -> Settings:
    """Read the environment. Loads no ``.env`` file."""
    raw_key = os.environ.get("CURSOR_API_KEY")
    api_key, config_error = _interpret_key(raw_key)
    fixture = os.environ.get("CURSOR_MCP_FIXTURE") == "1"
    if config_error is None and fixture and api_key is not None:
        config_error = _FIXTURE_WITH_KEY
    raw_level = os.environ.get("CURSOR_MCP_LOG_LEVEL", "INFO").upper()
    log_level = raw_level if raw_level in _LOG_LEVELS else "INFO"
    return Settings(
        api_key=api_key,
        allow_writes=os.environ.get("CURSOR_MCP_ALLOW_WRITES", "0") == "1",
        log_level=log_level,
        fixture=fixture,
        config_error=config_error,
        allow_delete=os.environ.get("CURSOR_MCP_ALLOW_DELETE", "0") == "1",
        forward_env=_forward_allowlist(os.environ.get("CURSOR_MCP_FORWARD_ENV")),
    )


def _forward_allowlist(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    names = [part.strip() for part in raw.split(",") if part.strip()]
    return frozenset(names)


def _interpret_key(raw: str | None) -> tuple[str | None, str | None]:
    if raw is None:
        return None, None
    value = raw.strip()
    if value == "":
        return None, "CURSOR_API_KEY is empty."
    if _UNEXPANDED.match(value) or "${" in value or "{env:" in value:
        return None, "CURSOR_API_KEY is not interpolated. The server does not load a .env file."
    return value, None
