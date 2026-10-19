"""Local checks. They do not prove that a resource exists on GitHub or Cursor."""

import base64
import binascii
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from cursor_cloud_mcp.config import (
    ENV_NAME_MAX_BYTES,
    ENV_VALUE_MAX_BYTES,
    IMAGE_MAX_BYTES,
    IMAGE_MAX_COUNT,
    IMAGE_MIME_TYPES,
    IMAGE_URL_MAX_CHARS,
    PROMPT_MAX_CHARS,
)
from cursor_cloud_mcp.errors import ErrorCode, failure

if TYPE_CHECKING:
    from cursor_cloud_mcp.models import ImageInput

_SHA = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")
_BRANCH_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_AGENT_ID = re.compile(
    r"^bc-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Agent and run ids as the API returns them: a prefix and a UUID, hexadecimal, so case-insensitive.
_PREFIXED_UUID = re.compile(
    r"^(bc|run)-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_REPO_PIECE = re.compile(r"[A-Za-z0-9_.-]+")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODES = {"agent", "plan"}


def same_id(received: str, expected: str) -> bool:
    """Whether a response names the requested resource.

    The API accepts a prefixed UUID with uppercase hexadecimal digits and answers in lowercase
    (verified on October 6, 2026): for that format only, case distinguishes nothing. Any other
    id must match exactly.
    """
    if received == expected:
        return True
    both_uuids = _PREFIXED_UUID.match(received) is not None and _PREFIXED_UUID.match(expected) is not None
    return both_uuids and received.lower() == expected.lower()


def require_prompt(prompt: str) -> str:
    if prompt.strip() == "":
        raise failure(ErrorCode.VALIDATION, "The prompt is empty. It is not truncated.")
    if len(prompt) > PROMPT_MAX_CHARS:
        raise failure(
            ErrorCode.VALIDATION,
            "The prompt exceeds 100000 characters. It is rejected, not truncated.",
        )
    return prompt


def require_images(images: "list[ImageInput] | None") -> list[dict[str, object]] | None:
    """``prompt.images`` for the API. Errors name the image's index and size, never its content."""
    if not images:
        return None
    if len(images) > IMAGE_MAX_COUNT:
        raise failure(ErrorCode.VALIDATION, f"At most {IMAGE_MAX_COUNT} images per prompt.")
    built: list[dict[str, object]] = []
    for index, image in enumerate(images, start=1):
        if (image.url is None) == (image.data is None):
            raise failure(ErrorCode.VALIDATION, f"Image {index}: give exactly one of url or data.")
        entry: dict[str, object]
        if image.url is not None:
            if image.mime_type is not None:
                raise failure(ErrorCode.VALIDATION, f"Image {index}: mime_type goes with data only; Cursor reads it from the url.")
            try:
                parts = urlsplit(image.url)
                # Reading the port validates it: urlsplit itself accepts "host:99999".
                _port = parts.port
            except ValueError:
                raise failure(ErrorCode.VALIDATION, f"Image {index}: url is not a valid URL.") from None
            if parts.scheme not in {"http", "https"} or not parts.hostname or len(image.url) > IMAGE_URL_MAX_CHARS:
                raise failure(ErrorCode.VALIDATION, f"Image {index}: url must be an http(s) URL of at most {IMAGE_URL_MAX_CHARS} characters.")
            if parts.username or parts.password:
                raise failure(ErrorCode.VALIDATION, f"Image {index}: url must not contain credentials.")
            entry = {"url": image.url}
        else:
            data = image.data or ""
            if image.mime_type not in IMAGE_MIME_TYPES:
                raise failure(
                    ErrorCode.VALIDATION,
                    f"Image {index}: mime_type is required with data, one of {', '.join(sorted(IMAGE_MIME_TYPES))}.",
                )
            # Bound the text before decoding it: base64 is 4 characters per 3 bytes.
            if len(data) > (IMAGE_MAX_BYTES + 2) // 3 * 4:
                raise failure(ErrorCode.VALIDATION, f"Image {index}: larger than {IMAGE_MAX_BYTES // (1024 * 1024)} MB.")
            try:
                size = len(base64.b64decode(data, validate=True))
            except (binascii.Error, ValueError):
                raise failure(ErrorCode.VALIDATION, f"Image {index}: data is not valid base64 ({len(data)} characters).") from None
            if size == 0 or size > IMAGE_MAX_BYTES:
                raise failure(ErrorCode.VALIDATION, f"Image {index}: {size} bytes once decoded, allowed 1 to {IMAGE_MAX_BYTES}.")
            entry = {"data": data, "mimeType": image.mime_type}
        if (image.width is None) != (image.height is None):
            raise failure(ErrorCode.VALIDATION, f"Image {index}: width and height go together.")
        if image.width is not None and image.height is not None:
            entry["dimension"] = {"width": image.width, "height": image.height}
        built.append(entry)
    return built


def require_starting_ref(value: str) -> str:
    """Branch name or full commit SHA sent in ``startingRef``.

    A full SHA was refused (400) on October 1, 2026, and accepted (201) on October 19, 2026.
    """
    if _SHA.fullmatch(value) is not None:
        return value
    if not _valid_branch_name(value):
        raise failure(
            ErrorCode.VALIDATION,
            "starting_ref must be a Git branch name or a full commit SHA. "
            "This check does not prove that it exists on GitHub.",
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
