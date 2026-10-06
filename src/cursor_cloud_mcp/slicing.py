"""Local slicing of the result text. This is not a Cursor REST cursor."""

from cursor_cloud_mcp.config import RESULT_MAX_LIMIT
from cursor_cloud_mcp.errors import ErrorCode, failure


def slice_text(text: str, offset: int, limit: int) -> tuple[str, bool, int | None]:
    """Return the chunk, the truncation flag and the next offset."""
    if offset < 0 or limit < 1 or limit > RESULT_MAX_LIMIT:
        raise failure(
            ErrorCode.VALIDATION,
            f"result_offset must be >= 0 and result_limit between 1 and {RESULT_MAX_LIMIT}.",
        )
    if offset > len(text):
        raise failure(
            ErrorCode.VALIDATION,
            "result_offset exceeds the length of the result.",
        )
    end = min(len(text), offset + limit)
    truncated = end < len(text)
    next_offset = end if truncated else None
    return text[offset:end], truncated, next_offset
