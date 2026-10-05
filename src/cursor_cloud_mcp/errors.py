"""Erreurs opérationnelles exposées aux clients MCP."""

from enum import StrEnum
from typing import Never

from pydantic import BaseModel, ConfigDict


class ErrorCode(StrEnum):
    """Codes stables de ce MCP. Les codes distants restent dans ``remote_code``."""

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
    """Schéma d'erreur opérationnelle. ``safe_to_retry_automatically`` est toujours faux."""

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
    """Échec prévu, converti en résultat MCP ``isError`` par les outils."""

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
    """Phrase stable par code. Le ``Never`` rend un nouveau code incompilable."""
    match code:
        case ErrorCode.CONFIGURATION_MISSING:
            return "Configuration Cursor incomplète."
        case ErrorCode.READ_ONLY:
            return "Mutation refusée : le serveur est en lecture seule."
        case ErrorCode.VALIDATION:
            return "Paramètres refusés."
        case ErrorCode.AUTHENTICATION:
            return "Authentification Cursor refusée."
        case ErrorCode.PERMISSION:
            return "Permissions Cursor insuffisantes."
        case ErrorCode.NOT_FOUND:
            return "Ressource Cursor introuvable."
        case ErrorCode.QUOTA:
            return "Quota ou limite de débit Cursor atteinte."
        case ErrorCode.AGENT_BUSY:
            return "L'agent a déjà un run actif."
        case ErrorCode.CANCEL_NOT_POSSIBLE:
            return "Ce run ne peut pas être annulé."
        case ErrorCode.INCOMPATIBLE_RESPONSE:
            return "Réponse Cursor incompatible avec le contrat."
        case ErrorCode.TIMEOUT:
            return "Délai dépassé avant la fin de l'appel HTTP."
        case ErrorCode.MUTATION_OUTCOME_UNKNOWN:
            return "Le résultat de la mutation est inconnu."
        case ErrorCode.AGENT_ID_CONFLICT:
            return "Cet identifiant d'agent existe déjà."
        case ErrorCode.CONFLICT:
            return "Conflit d'état côté Cursor."
        case ErrorCode.CONTINUATION_REFUSED:
            return "Continuation hors du périmètre de ce MCP."
        case ErrorCode.UPSTREAM:
            return "Cursor a renvoyé une erreur."
        case ErrorCode.STREAM_EXPIRED:
            return "Le flux de ce run n'est plus disponible."
        case ErrorCode.DELETE_DISABLED:
            return "Suppression refusée."
        case _:
            unexpected: Never = code
            raise AssertionError(unexpected)
