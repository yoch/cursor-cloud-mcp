"""Local checks. They do not prove that a resource exists on GitHub or Cursor."""

import re
from urllib.parse import urlsplit

from cursor_cloud_mcp.config import (
    ENV_NAME_MAX_BYTES,
    ENV_VALUE_MAX_BYTES,
    PROMPT_MAX_CHARS,
)
from cursor_cloud_mcp.errors import ErrorCode, failure

_SHA = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")
_BRANCH_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_AGENT_ID = re.compile(
    r"^bc-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_REPO_PIECE = re.compile(r"[A-Za-z0-9_.-]+")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODES = {"agent", "plan"}


def require_prompt(prompt: str) -> str:
    if prompt.strip() == "":
        raise failure(ErrorCode.VALIDATION, "The prompt is empty. It is not truncated.")
    if len(prompt) > PROMPT_MAX_CHARS:
        raise failure(
            ErrorCode.VALIDATION,
            "The prompt exceeds 100000 characters. It is rejected, not truncated.",
        )
    return prompt


def require_starting_ref(value: str) -> str:
    """Branch name sent in ``startingRef``.

    Rejecting a full SHA is a dated workaround: the API answered ``400`` on
    October 1, 2026, even though the REST documentation says a reference may be a
    SHA. Requalify with an authorized real test before removing it.
    """
    if _SHA.fullmatch(value) is not None:
        raise failure(
            ErrorCode.VALIDATION,
            "A full SHA in startingRef was rejected by the Cursor API (observed on October 1, 2026). "
            "Push this commit to a branch, check that its head is this SHA, "
            "then pass the branch name in starting_ref.",
        )
    if not _valid_branch_name(value):
        raise failure(
            ErrorCode.VALIDATION,
            "starting_ref must be a Git branch name. "
            "This check does not prove that the branch exists on GitHub.",
        )
    return value


def _valid_branch_name(value: str) -> bool:
    if value in {"", "@"} or value.startswith(("/", "-")) or value.endswith(("/", ".")):
        return False
    if len(value.encode("utf-8")) > 255 or "//" in value or ".." in value or "@{" in value:
        return False
    if _BRANCH_FORBIDDEN.search(value) is not None:
        return False
    pieces = value.split("/")
    return all(piece and not piece.startswith(".") and not piece.endswith(".lock") for piece in pieces)


def require_segment(value: str, *, label: str) -> str:
    if _SEGMENT.fullmatch(value) is None:
        raise failure(
            ErrorCode.VALIDATION,
            f"{label} must be a single-segment identifier, without URL separators.",
        )
    return value


def require_agent_id(value: str) -> str:
    if _AGENT_ID.fullmatch(value) is None:
        raise failure(
            ErrorCode.VALIDATION,
            "agent_id must have the bc-<uuid> form documented by the Cursor API.",
        )
    return value


def normalize_repository(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or (parts.hostname or "").lower() != "github.com":
        raise failure(
            ErrorCode.VALIDATION,
            "repository must be an HTTPS github.com URL, with no different host.",
        )
    if parts.username or parts.password or parts.query or parts.fragment:
        raise failure(
            ErrorCode.VALIDATION,
            "repository must not contain credentials, a query string or a fragment.",
        )
    path = parts.path.strip("/")
    path = path.removesuffix(".git")
    pieces = path.split("/")
    if (
        len(pieces) != 2
        or any(piece in {"", ".", ".."} for piece in pieces)
        or any(_REPO_PIECE.fullmatch(piece) is None for piece in pieces)
    ):
        raise failure(
            ErrorCode.VALIDATION,
            "repository must point to exactly one GitHub repository, in the form https://github.com/owner/name.",
        )
    return f"https://github.com/{pieces[0]}/{pieces[1]}"


def require_mode(mode: str | None) -> str | None:
    if mode is None:
        return None
    if mode in _MODES:
        return mode
    raise failure(ErrorCode.VALIDATION, "mode must be agent or plan.")


def require_event_id(value: str) -> str:
    if value.strip() == "" or len(value) > 200 or any(char in value for char in "\r\n"):
        raise failure(ErrorCode.VALIDATION, "after_event_id is invalid.")
    return value


def require_env_name(name: str) -> str:
    encoded = name.encode("utf-8")
    if (
        name == ""
        or len(encoded) > ENV_NAME_MAX_BYTES
        or name.startswith("CURSOR_")
        or _ENV_NAME.fullmatch(name) is None
    ):
        raise failure(
            ErrorCode.VALIDATION,
            "A variable name is empty, too long, starts with CURSOR_ or contains a forbidden character.",
        )
    return name


def require_env_value(value: str) -> str:
    if value == "" or len(value.encode("utf-8")) > ENV_VALUE_MAX_BYTES:
        raise failure(
            ErrorCode.VALIDATION,
            "A variable value is empty or exceeds 4096 bytes. The value is not returned.",
        )
    return value


def require_artifact_path(path: str) -> str:
    if path != path.strip() or len(path) > 512 or "\\" in path or path.startswith("/"):
        raise failure(
            ErrorCode.VALIDATION,
            "The artifact path must be relative, start with artifacts/ and not contain .. .",
        )
    pieces = path.split("/")
    if any(piece in {"", ".", ".."} for piece in pieces) or not path.startswith("artifacts/"):
        raise failure(
            ErrorCode.VALIDATION,
            "The artifact path must be relative, start with artifacts/ and not contain .. .",
        )
    return path
