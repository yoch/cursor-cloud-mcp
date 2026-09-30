"""Découpe locale du texte de résultat. Ce n'est pas un curseur REST Cursor."""

from cursor_cloud_mcp.config import RESULT_MAX_LIMIT
from cursor_cloud_mcp.errors import ErrorCode, failure


def slice_text(text: str, offset: int, limit: int) -> tuple[str, bool, int | None]:
    """Retourne le morceau, l'indicateur de troncature et le prochain offset."""
    if offset < 0 or limit < 1 or limit > RESULT_MAX_LIMIT:
        raise failure(
            ErrorCode.VALIDATION,
            f"result_offset doit être >= 0 et result_limit entre 1 et {RESULT_MAX_LIMIT}.",
        )
    if offset > len(text):
        raise failure(
            ErrorCode.VALIDATION,
            "result_offset dépasse la longueur du résultat.",
        )
    end = min(len(text), offset + limit)
    truncated = end < len(text)
    next_offset = end if truncated else None
    return text[offset:end], truncated, next_offset
