"""Client REST Cursor. Les POST ne sont jamais rejoués."""

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from cursor_cloud_mcp.config import (
    API_BASE,
    DEFAULT_DEADLINE_SECONDS,
    MAX_RESPONSE_BYTES,
    REPOSITORIES_DEADLINE_SECONDS,
    REPOSITORY_CACHE_TTL_SECONDS,
)
from cursor_cloud_mcp.errors import ErrorCode, CursorFailure, explain, failure
from cursor_cloud_mcp.models import (
    RemoteAccount,
    RemoteAgent,
    RemoteAgentPage,
    RemoteCreateAgent,
    RemoteCreateRun,
    RemoteId,
    RemoteModelList,
    RemoteRepositoryList,
    RemoteRun,
    RemoteRunPage,
    RemoteUsage,
)
from cursor_cloud_mcp.validation import require_segment

logger = logging.getLogger(__name__)

_JSON = TypeVar("_JSON", bound=BaseModel)
_REDIRECTS = {301, 302, 303, 307, 308}
_REQUEST_ID_HEADERS = ("x-request-id", "request-id", "x-cursor-request-id")
_RECOVERY = "Relire l'état distant avant toute nouvelle mutation."


@dataclass(frozen=True)
class MutationContext:
    agent_id: str | None = None
    run_id: str | None = None
    previous_latest_run_id: str | None = None


class CursorCloudClient:
    """Un client HTTP par processus, avec cache mémoire des dépôts."""

    def __init__(
        self,
        *,
        api_key: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        repositories_deadline_seconds: float = REPOSITORIES_DEADLINE_SECONDS,
        repository_cache_ttl_seconds: float = REPOSITORY_CACHE_TTL_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self._api_key = api_key
        self._transport = transport
        self._deadline = deadline_seconds
        self._repositories_deadline = repositories_deadline_seconds
        self._repository_ttl = repository_cache_ttl_seconds
        self._max_body = max_response_bytes
        self._http: httpx.AsyncClient | None = None
        self._repo_lock = asyncio.Lock()
        self._repo_cache: tuple[float, RemoteRepositoryList] | None = None
        self._repo_task: asyncio.Task[RemoteRepositoryList] | None = None

    async def open(self) -> None:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            headers=headers,
            follow_redirects=False,
            transport=self._transport,
            timeout=httpx.Timeout(self._deadline),
        )

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def get_account(self) -> RemoteAccount:
        return await self._get_model("/v1/me", RemoteAccount, deadline=self._deadline)

    async def list_models(self) -> RemoteModelList:
        return await self._get_model("/v1/models", RemoteModelList, deadline=self._deadline)

    async def list_repositories(self) -> tuple[RemoteRepositoryList, bool]:
        async with self._repo_lock:
            cached = self._repo_cache
            now = time.monotonic()
            if cached is not None and now - cached[0] < self._repository_ttl:
                return cached[1], True
            if self._repo_task is None or self._repo_task.done():
                self._repo_task = asyncio.create_task(self._fetch_repositories())
            task = self._repo_task
        try:
            payload = await task
        except Exception:
            async with self._repo_lock:
                if self._repo_task is task:
                    self._repo_task = None
            raise
        async with self._repo_lock:
            if self._repo_task is task:
                self._repo_cache = (time.monotonic(), payload)
                self._repo_task = None
        return payload, False

    async def list_agents(self, *, limit: int | None, cursor: str | None) -> RemoteAgentPage:
        return await self._get_model(
            "/v1/agents",
            RemoteAgentPage,
            query=_page_query(limit, cursor),
            deadline=self._deadline,
        )

    async def get_agent(self, agent_id: str) -> RemoteAgent:
        segment = require_segment(agent_id, label="agent_id")
        return await self._get_model(
            f"/v1/agents/{quote(segment, safe='')}",
            RemoteAgent,
            deadline=self._deadline,
        )

    async def create_agent(self, body: Mapping[str, object], *, agent_id: str) -> RemoteCreateAgent:
        context = MutationContext(agent_id=agent_id)
        payload = await self._send(
            "POST",
            "/v1/agents",
            json_body=dict(body),
            deadline=self._deadline,
            mutation=context,
        )
        return _parse(RemoteCreateAgent, payload, mutation=context)

    async def list_runs(
        self,
        agent_id: str,
        *,
        limit: int | None,
        cursor: str | None,
    ) -> RemoteRunPage:
        segment = require_segment(agent_id, label="agent_id")
        return await self._get_model(
            f"/v1/agents/{quote(segment, safe='')}/runs",
            RemoteRunPage,
            query=_page_query(limit, cursor),
            deadline=self._deadline,
        )

    async def get_run(self, agent_id: str, run_id: str) -> RemoteRun:
        agent = require_segment(agent_id, label="agent_id")
        run = require_segment(run_id, label="run_id")
        return await self._get_model(
            f"/v1/agents/{quote(agent, safe='')}/runs/{quote(run, safe='')}",
            RemoteRun,
            deadline=self._deadline,
        )

    async def create_run(
        self,
        agent_id: str,
        body: Mapping[str, object],
        *,
        previous_latest_run_id: str | None,
    ) -> RemoteCreateRun:
        segment = require_segment(agent_id, label="agent_id")
        context = MutationContext(agent_id=segment, previous_latest_run_id=previous_latest_run_id)
        payload = await self._send(
            "POST",
            f"/v1/agents/{quote(segment, safe='')}/runs",
            json_body=dict(body),
            deadline=self._deadline,
            mutation=context,
        )
        return _parse(RemoteCreateRun, payload, mutation=context)

    async def cancel_run(self, agent_id: str, run_id: str) -> RemoteId:
        agent = require_segment(agent_id, label="agent_id")
        run = require_segment(run_id, label="run_id")
        context = MutationContext(agent_id=agent, run_id=run)
        payload = await self._send(
            "POST",
            f"/v1/agents/{quote(agent, safe='')}/runs/{quote(run, safe='')}/cancel",
            deadline=self._deadline,
            mutation=context,
        )
        if payload is None:
            return RemoteId(id=None)
        return _parse(RemoteId, payload, mutation=context)

    async def get_usage(self, agent_id: str, *, run_id: str | None) -> RemoteUsage:
        segment = require_segment(agent_id, label="agent_id")
        query: dict[str, str] = {}
        if run_id is not None:
            query["runId"] = require_segment(run_id, label="run_id")
        return await self._get_model(
            f"/v1/agents/{quote(segment, safe='')}/usage",
            RemoteUsage,
            query=query,
            deadline=self._deadline,
        )

    async def _fetch_repositories(self) -> RemoteRepositoryList:
        return await self._get_model(
            "/v1/repositories",
            RemoteRepositoryList,
            deadline=self._repositories_deadline,
        )

    async def _get_model(
        self,
        path: str,
        model: type[_JSON],
        *,
        query: Mapping[str, str] | None = None,
        deadline: float,
    ) -> _JSON:
        payload = await self._send("GET", path, query=query, deadline=deadline, mutation=None)
        return _parse(model, payload, mutation=None)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        deadline: float,
        mutation: MutationContext | None,
    ) -> object | None:
        if self._http is None:
            raise failure(ErrorCode.CONFIGURATION_MISSING, "Le client HTTP n'est pas ouvert.")
        loop = asyncio.get_running_loop()
        deadline_at = loop.time() + deadline
        attempt = 0
        try:
            async with asyncio.timeout_at(deadline_at):
                while True:
                    remaining = deadline_at - loop.time()
                    if remaining <= 0:
                        raise TimeoutError
                    started = time.perf_counter()
                    try:
                        response = await self._http.request(
                            method,
                            path,
                            params=dict(query or {}),
                            json=dict(json_body) if json_body is not None else None,
                            timeout=httpx.Timeout(remaining),
                        )
                    except httpx.TimeoutException:
                        raise TimeoutError from None
                    except httpx.TransportError:
                        _log(method, path, "transport", started, None)
                        if method == "GET" and attempt == 0 and deadline_at - loop.time() > 0:
                            attempt = 1
                            continue
                        raise _transport_failure(mutation) from None
                    request_id = _request_id(response.headers)
                    _log(method, path, str(response.status_code), started, request_id)
                    if response.status_code in _REDIRECTS:
                        raise _redirect_failure(response, request_id, mutation, self._api_key)
                    if method == "GET" and attempt == 0 and _retryable(response.status_code):
                        wait = _retry_after_seconds(response)
                        remaining = deadline_at - loop.time()
                        if wait is not None and wait > remaining:
                            raise _status_failure(
                                response,
                                mutation=None,
                                request_id=request_id,
                                retry_after=wait,
                                blocked_by_deadline=True,
                                secret=self._api_key,
                            )
                        if wait:
                            await asyncio.sleep(wait)
                        attempt = 1
                        continue
                    return _interpret(
                        response,
                        mutation=mutation,
                        request_id=request_id,
                        max_body=self._max_body,
                        secret=self._api_key,
                    )
        except TimeoutError:
            raise _timeout_failure(mutation) from None


def _page_query(limit: int | None, cursor: str | None) -> dict[str, str]:
    query: dict[str, str] = {}
    if limit is not None:
        query["limit"] = str(limit)
    if cursor is not None:
        query["cursor"] = cursor
    return query


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _request_id(headers: httpx.Headers) -> str | None:
    for name in _REQUEST_ID_HEADERS:
        value = headers.get(name)
        if value:
            return value
    return None


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if not isinstance(parsed, datetime):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, parsed.timestamp() - time.time())


def _log(method: str, path: str, status: str, started: float, request_id: str | None) -> None:
    duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "http method=%s path=%s status=%s duration_ms=%d request_id=%s",
        method,
        path,
        status,
        duration_ms,
        request_id or "-",
    )


def _interpret(
    response: httpx.Response,
    *,
    mutation: MutationContext | None,
    request_id: str | None,
    max_body: int,
    secret: str | None,
) -> object | None:
    if response.status_code not in {200, 201}:
        raise _status_failure(
            response,
            mutation=mutation,
            request_id=request_id,
            retry_after=None,
            blocked_by_deadline=False,
            secret=secret,
        )
    raw = response.content
    if len(raw) > max_body:
        raise _body_failure(mutation, request_id, response.status_code, "La réponse dépasse la limite locale.")
    if not raw:
        return None
    content_type = response.headers.get("content-type", "")
    if "html" in content_type.lower() or raw.lstrip().startswith(b"<"):
        raise _body_failure(mutation, request_id, response.status_code, "La réponse est du HTML.")
    try:
        return response.json()
    except ValueError:
        raise _body_failure(mutation, request_id, response.status_code, "La réponse n'est pas du JSON.") from None


def _parse(model: type[_JSON], payload: object | None, *, mutation: MutationContext | None) -> _JSON:
    try:
        return model.model_validate(payload)
    except ValidationError:
        if mutation is not None:
            raise failure(
                ErrorCode.MUTATION_OUTCOME_UNKNOWN,
                f"{explain(ErrorCode.MUTATION_OUTCOME_UNKNOWN)} Le corps de succès ne respecte pas le schéma.",
                agent_id=mutation.agent_id,
                run_id=mutation.run_id,
                previous_latest_run_id=mutation.previous_latest_run_id,
                recovery=_RECOVERY,
            ) from None
        raise failure(
            ErrorCode.INCOMPATIBLE_RESPONSE,
            f"{explain(ErrorCode.INCOMPATIBLE_RESPONSE)} Un champ essentiel est absent ou d'un type inattendu.",
        ) from None


def _timeout_failure(mutation: MutationContext | None) -> CursorFailure:
    if mutation is not None:
        return failure(
            ErrorCode.MUTATION_OUTCOME_UNKNOWN,
            f"{explain(ErrorCode.MUTATION_OUTCOME_UNKNOWN)} Le délai a expiré après l'envoi possible de la requête.",
            agent_id=mutation.agent_id,
            run_id=mutation.run_id,
            previous_latest_run_id=mutation.previous_latest_run_id,
            recovery=_RECOVERY,
        )
    return failure(ErrorCode.TIMEOUT, explain(ErrorCode.TIMEOUT))


def _transport_failure(mutation: MutationContext | None) -> CursorFailure:
    if mutation is not None:
        return failure(
            ErrorCode.MUTATION_OUTCOME_UNKNOWN,
            f"{explain(ErrorCode.MUTATION_OUTCOME_UNKNOWN)} La connexion a été coupée.",
            agent_id=mutation.agent_id,
            run_id=mutation.run_id,
            previous_latest_run_id=mutation.previous_latest_run_id,
            recovery=_RECOVERY,
        )
    return failure(ErrorCode.TIMEOUT, "Connexion interrompue pendant la lecture.")


def _body_failure(
    mutation: MutationContext | None,
    request_id: str | None,
    status: int,
    detail: str,
) -> CursorFailure:
    if mutation is not None:
        return failure(
            ErrorCode.MUTATION_OUTCOME_UNKNOWN,
            f"{explain(ErrorCode.MUTATION_OUTCOME_UNKNOWN)} {detail}",
            agent_id=mutation.agent_id,
            run_id=mutation.run_id,
            previous_latest_run_id=mutation.previous_latest_run_id,
            recovery=_RECOVERY,
            http_status=status,
            request_id=request_id,
        )
    return failure(
        ErrorCode.INCOMPATIBLE_RESPONSE,
        f"{explain(ErrorCode.INCOMPATIBLE_RESPONSE)} {detail}",
        http_status=status,
        request_id=request_id,
    )


def _redirect_failure(
    response: httpx.Response,
    request_id: str | None,
    mutation: MutationContext | None,
    secret: str | None,
) -> CursorFailure:
    del secret
    location = response.headers.get("location", "")
    host = ""
    if location:
        try:
            host = httpx.URL(location).host
        except httpx.InvalidURL:
            host = ""
    if host and host != "api.cursor.com":
        message = "Redirection d'authentification vers un autre domaine refusée."
    else:
        message = "Redirection HTTP refusée."
    if mutation is not None:
        return failure(
            ErrorCode.MUTATION_OUTCOME_UNKNOWN,
            f"{explain(ErrorCode.MUTATION_OUTCOME_UNKNOWN)} {message}",
            agent_id=mutation.agent_id,
            run_id=mutation.run_id,
            previous_latest_run_id=mutation.previous_latest_run_id,
            recovery=_RECOVERY,
            http_status=response.status_code,
            request_id=request_id,
        )
    return failure(
        ErrorCode.INCOMPATIBLE_RESPONSE,
        message,
        http_status=response.status_code,
        request_id=request_id,
    )


def _status_failure(
    response: httpx.Response,
    *,
    mutation: MutationContext | None,
    request_id: str | None,
    retry_after: float | None,
    blocked_by_deadline: bool,
    secret: str | None,
) -> CursorFailure:
    remote_code, remote_message = _remote_error(response)
    remote_message = _clean(remote_message, secret)
    code = _classify(response.status_code, remote_code, mutation=mutation is not None)
    if code is ErrorCode.MUTATION_OUTCOME_UNKNOWN and mutation is not None:
        return failure(
            code,
            f"{explain(code)} {remote_message}".strip(),
            agent_id=mutation.agent_id,
            run_id=mutation.run_id,
            previous_latest_run_id=mutation.previous_latest_run_id,
            recovery=_RECOVERY,
            http_status=response.status_code,
            remote_code=remote_code,
            request_id=request_id,
        )
    message = explain(code)
    if blocked_by_deadline:
        message = f"{message} L'attente Retry-After dépasse la deadline."
    if remote_message:
        message = f"{message} {remote_message}"
    agent_id = mutation.agent_id if mutation is not None else None
    run_id = mutation.run_id if mutation is not None else None
    previous = mutation.previous_latest_run_id if mutation is not None else None
    recovery = None
    if code is ErrorCode.AGENT_ID_CONFLICT:
        recovery = "Relire cet agent avant toute nouvelle création. Ne pas changer l'identifiant."
    if code is ErrorCode.AGENT_BUSY:
        recovery = "Attendre la fin du run ou l'annuler. Ne pas créer un autre agent."
    return failure(
        code,
        message,
        agent_id=agent_id,
        run_id=run_id,
        previous_latest_run_id=previous,
        recovery=recovery,
        http_status=response.status_code,
        remote_code=remote_code,
        request_id=request_id,
        retry_after_seconds=retry_after,
    )


def _classify(status: int, remote_code: str | None, *, mutation: bool) -> ErrorCode:
    if remote_code == "agent_busy":
        return ErrorCode.AGENT_BUSY
    if remote_code == "run_not_cancellable":
        return ErrorCode.CANCEL_NOT_POSSIBLE
    if remote_code == "agent_id_conflict":
        return ErrorCode.AGENT_ID_CONFLICT
    if remote_code in {"rate_limit_exceeded", "usage_limit_exceeded"} or status == 429:
        return ErrorCode.QUOTA
    if status == 401 or remote_code in {"unauthorized", "api_key_not_found"}:
        return ErrorCode.AUTHENTICATION
    if status == 403:
        return ErrorCode.PERMISSION
    if status == 404:
        return ErrorCode.NOT_FOUND
    if status == 409:
        return ErrorCode.CONFLICT
    if status == 400:
        return ErrorCode.VALIDATION
    if status >= 500 and mutation:
        return ErrorCode.MUTATION_OUTCOME_UNKNOWN
    if status >= 500:
        return ErrorCode.UPSTREAM
    return ErrorCode.INCOMPATIBLE_RESPONSE


def _remote_error(response: httpx.Response) -> tuple[str | None, str]:
    content_type = response.headers.get("content-type", "")
    if "html" in content_type.lower() or response.content.lstrip().startswith(b"<"):
        return None, ""
    try:
        payload = response.json()
    except ValueError:
        return None, ""
    if not isinstance(payload, dict):
        return None, ""
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        return (
            code if isinstance(code, str) else None,
            message if isinstance(message, str) else "",
        )
    if isinstance(error, str):
        return None, error
    message = payload.get("message")
    if isinstance(message, str):
        return None, message
    return None, ""


def _clean(message: str, secret: str | None) -> str:
    compact = " ".join(message.split())
    if secret:
        compact = compact.replace(secret, "[redacted]")
    if len(compact) > 300:
        return compact[:300] + "…"
    return compact
