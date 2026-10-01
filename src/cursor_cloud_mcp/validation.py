"""Contrôles locaux. Ils ne prouvent pas qu'une ressource existe chez GitHub ou Cursor."""

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
        raise failure(ErrorCode.VALIDATION, "Le prompt est vide. Il n'est pas tronqué.")
    if len(prompt) > PROMPT_MAX_CHARS:
        raise failure(
            ErrorCode.VALIDATION,
            "Le prompt dépasse 100000 caractères. Il est refusé, pas tronqué.",
        )
    return prompt


def require_starting_ref(value: str) -> str:
    """Nom de branche envoyé dans ``startingRef``. Un SHA complet est refusé par l'API."""
    if _SHA.fullmatch(value) is not None:
        raise failure(
            ErrorCode.VALIDATION,
            "L'API Cursor refuse un SHA complet dans startingRef. "
            "Pousse ce commit sur une branche, vérifie que sa tête est ce SHA, "
            "puis passe le nom de la branche dans starting_sha.",
        )
    if not _valid_branch_name(value):
        raise failure(
            ErrorCode.VALIDATION,
            "starting_sha doit être un nom de branche Git. "
            "Ce contrôle ne prouve pas que la branche existe sur GitHub.",
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
            f"{label} doit être un identifiant d'un seul segment, sans séparateur d'URL.",
        )
    return value


def require_agent_id(value: str) -> str:
    if _AGENT_ID.fullmatch(value) is None:
        raise failure(
            ErrorCode.VALIDATION,
            "agent_id doit avoir la forme bc-<uuid> documentée par l'API Cursor.",
        )
    return value


def normalize_repository(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or (parts.hostname or "").lower() != "github.com":
        raise failure(
            ErrorCode.VALIDATION,
            "repository doit être une URL HTTPS github.com, sans hôte différent.",
        )
    if parts.username or parts.password or parts.query or parts.fragment:
        raise failure(
            ErrorCode.VALIDATION,
            "repository ne doit contenir ni identifiants, ni requête, ni fragment.",
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
            "repository doit viser exactement un dépôt GitHub, sous la forme https://github.com/owner/name.",
        )
    return f"https://github.com/{pieces[0]}/{pieces[1]}"


def require_mode(mode: str | None) -> str | None:
    if mode is None:
        return None
    if mode in _MODES:
        return mode
    raise failure(ErrorCode.VALIDATION, "mode doit être agent ou plan.")


def require_event_id(value: str) -> str:
    if value.strip() == "" or len(value) > 200 or any(char in value for char in "\r\n"):
        raise failure(ErrorCode.VALIDATION, "after_event_id est invalide.")
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
            "Un nom de variable est vide, trop long, commence par CURSOR_ ou contient un caractère refusé.",
        )
    return name


def require_env_value(value: str) -> str:
    if value == "" or len(value.encode("utf-8")) > ENV_VALUE_MAX_BYTES:
        raise failure(
            ErrorCode.VALIDATION,
            "Une valeur de variable est vide ou dépasse 4096 octets. La valeur n'est pas renvoyée.",
        )
    return value


def require_artifact_path(path: str) -> str:
    if path != path.strip() or len(path) > 512 or "\\" in path or path.startswith("/"):
        raise failure(
            ErrorCode.VALIDATION,
            "Le chemin d'artefact doit être relatif, commencer par artifacts/ et ne pas contenir de .. .",
        )
    pieces = path.split("/")
    if any(piece in {"", ".", ".."} for piece in pieces) or not path.startswith("artifacts/"):
        raise failure(
            ErrorCode.VALIDATION,
            "Le chemin d'artefact doit être relatif, commencer par artifacts/ et ne pas contenir de .. .",
        )
    return path
