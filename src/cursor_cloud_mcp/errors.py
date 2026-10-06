"""Operational errors exposed to MCP clients."""

from enum import StrEnum
from typing import Never

from pydantic import BaseModel, ConfigDict


class ErrorCode(StrEnum):
    """Stable codes of this MCP. Remote codes stay in ``remote_code``."""

    CONFIGURATION_MISSING = "CONFIGURATION_MISSING"
    READ_ONLY = "READ_ONLY"
    VALIDATION = "VALIDATION"
    AUTHENTICATION = "AUTHENTICATION"
    PERMISSION = "PERMISSION"
    NOT_FOUND = "NOT_FOUND"
    QUOTA = "QUOTA"
    AGENT_BUSY = "AGENT_BUSY"
    CANCEL_NOT_POSSIBLE = "CANCEL_NOT_POSSIBLE"
    INCOMPATIBLE_RESPONSE = "INCOMPATIBLE_RESPONSE"
    TIMEOUT = "TIMEOUT"
    MUTATION_OUTCOME_UNKNOWN = "MUTATION_OUTCOME_UNKNOWN"
    AGENT_ID_CONFLICT = "AGENT_ID_CONFLICT"
    CONFLICT = "CONFLICT"
    CONTINUATION_REFUSED = "CONTINUATION_REFUSED"
    UPSTREAM = "UPSTREAM"
    STREAM_EXPIRED = "STREAM_EXPIRED"
    DELETE_DISABLED = "DELETE_DISABLED"


class ErrorBody(BaseModel):
    """Operational error schema. ``safe_to_retry_automatically`` is always false."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    safe_to_retry_automatically: bool = False
    agent_id: str | None = None
    run_id: str | None = None
    previous_latest_run_id: str | None = None
    recovery: str | None = None
    http_status: int | None = None
    remote_code: str | None = None
    request_id: str | None = None
    retry_after_seconds: float | None = None
    help_url: str | None = None
    provider: str | None = None


class CursorFailure(Exception):
    """Expected failure, converted into an MCP ``isError`` result by the tools."""

    def __init__(self, body: ErrorBody) -> None:
        if body.safe_to_retry_automatically:
            body = body.model_copy(update={"safe_to_retry_automatically": False})
        self.body = body
        super().__init__(body.message)

    def as_dict(self) -> dict[str, object]:
        return self.body.model_dump(mode="json", exclude_none=True)


def failure(
    code: ErrorCode,
    message: str,
    *,
    agent_id: str | None = None,
    run_id: str | None = None,
    previous_latest_run_id: str | None = None,
    recovery: str | None = None,
    http_status: int | None = None,
    remote_code: str | None = None,
    request_id: str | None = None,
    retry_after_seconds: float | None = None,
    help_url: str | None = None,
    provider: str | None = None,
) -> CursorFailure:
    return CursorFailure(
        ErrorBody(
            code=code,
            message=message,
            safe_to_retry_automatically=False,
            agent_id=agent_id,
            run_id=run_id,
            previous_latest_run_id=previous_latest_run_id,
            recovery=recovery,
            http_status=http_status,
            remote_code=remote_code,
            request_id=request_id,
            retry_after_seconds=retry_after_seconds,
            help_url=help_url,
            provider=provider,
        )
    )


def explain(code: ErrorCode) -> str:
    """Stable sentence per code. The ``Never`` makes a new code fail type checking."""
    match code:
        case ErrorCode.CONFIGURATION_MISSING:
            return "Cursor configuration incomplete."
        case ErrorCode.READ_ONLY:
            return "Mutation refused: the server is read-only."
        case ErrorCode.VALIDATION:
            return "Parameters rejected."
        case ErrorCode.AUTHENTICATION:
            return "Cursor authentication refused."
        case ErrorCode.PERMISSION:
            return "Insufficient Cursor permissions."
        case ErrorCode.NOT_FOUND:
            return "Cursor resource not found."
        case ErrorCode.QUOTA:
            return "Cursor quota or rate limit reached."
        case ErrorCode.AGENT_BUSY:
            return "The agent already has an active run."
        case ErrorCode.CANCEL_NOT_POSSIBLE:
            return "This run cannot be cancelled."
        case ErrorCode.INCOMPATIBLE_RESPONSE:
            return "Cursor response incompatible with the contract."
        case ErrorCode.TIMEOUT:
            return "Timeout exceeded before the HTTP call finished."
        case ErrorCode.MUTATION_OUTCOME_UNKNOWN:
            return "The outcome of the mutation is unknown."
        case ErrorCode.AGENT_ID_CONFLICT:
            return "This agent identifier already exists."
        case ErrorCode.CONFLICT:
            return "State conflict on the Cursor side."
        case ErrorCode.CONTINUATION_REFUSED:
            return "Follow-up run outside the scope of this MCP."
        case ErrorCode.UPSTREAM:
            return "Cursor returned an error."
        case ErrorCode.STREAM_EXPIRED:
            return "The stream of this run is no longer available."
        case ErrorCode.DELETE_DISABLED:
            return "Deletion refused."
        case _:
            unexpected: Never = code
            raise AssertionError(unexpected)
