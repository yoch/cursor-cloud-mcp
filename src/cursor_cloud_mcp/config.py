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
# cursor_read_run_events: the caller may widen the tool args/result text, within this bound.
TOOL_OUTPUT_MAX_CHARS = 4000
# Total tool text rendered by one cursor_read_run_events call; reaching it sets truncated and
# leaves the cursor on the last event actually returned.
TOOL_TEXT_TOTAL_MAX_CHARS = 256_000
# Consecutive text fragments merged into one event, up to this size.
EVENT_MERGED_MAX_CHARS = 4000
# Full replay read to reach the end of a stream: the API has no way to start from the end.
# Bytes are walked, not kept (a 7 h run replayed 839 KB on 2026-10-06).
TAIL_MAX_BYTES = 16_000_000
ACTIVITY_TEXT_MAX_CHARS = 1000
# Tool args/result in an activity summary: wider than a plain stream excerpt, for diagnosis.
# Applied during the walk (the tracker stores the simplified view), not when the summary is built.
ACTIVITY_TOOL_TEXT_MAX_CHARS = 2000
# Last events kept in the activity summary: the diagnosis tail. The walk is already paid for
# by the summary, so keeping them costs nothing more.
ACTIVITY_LAST_EVENTS = 10
# Time allowed to walk a stream for an activity summary, on top of the tool's own budget. An idle
# run is only known to be fully replayed at its first heartbeat, 30 to 36 s after connecting.
ACTIVITY_MAX_SECONDS = 45.0
# Agent scans (name search, supervision): the API filters by neither name nor status.
AGENT_SCAN_MAX_PAGES = 5
AGENT_SCAN_DEFAULT_MATCHES = 20
AGENT_SCAN_MIN_SECONDS = 8.0
SUPERVISE_DEFAULT_AGENTS = 50
# Two waves of stream replays (8 in parallel, about 35 s each when idle) fit, under the
# 100 s client timeout of the examples.
SUPERVISE_ACTIVITY_BUDGET_SECONDS = 90.0
# No tool budget goes above this: the example clients give up after 100 s.
CLIENT_SAFE_BUDGET_SECONDS = 95.0
TAIL_DEFAULT_WAIT_SECONDS = 45
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

# Cancellation is asynchronous: re-read the run this many times, this far apart.
CANCEL_REREADS = 4
CANCEL_REREAD_PAUSE_SECONDS = 2.0
