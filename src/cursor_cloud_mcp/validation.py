"""Contrôles locaux. Ils ne prouvent pas qu'une ressource existe chez GitHub ou Cursor."""

import re
from urllib.parse import urlsplit

from cursor_cloud_mcp.config import PROMPT_MAX_CHARS
from cursor_cloud_mcp.errors import ErrorCode, failure

_SHA = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_AGENT_ID = re.compile(
    r"^bc-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_REPO_PIECE = re.compile(r"[A-Za-z0-9_.-]+")


def require_prompt(prompt: str) -> str:
    if prompt.strip() == "":
        raise failure(ErrorCode.VALIDATION, "Le prompt est vide. Il n'est pas tronqué.")
    if len(prompt) > PROMPT_MAX_CHARS:
        raise failure(
            ErrorCode.VALIDATION,
            "Le prompt dépasse 100000 caractères. Il est refusé, pas tronqué.",
        )
    return prompt


def require_sha(value: str) -> str:
    if _SHA.fullmatch(value) is None:
        raise failure(
            ErrorCode.VALIDATION,
            "starting_sha doit être un SHA complet de 40 ou 64 caractères hexadécimaux. "
            "Ce contrôle ne prouve pas que le commit existe sur GitHub.",
        )
    return value


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
    if path.endswith(".git"):
        path = path[: -len(".git")]
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
