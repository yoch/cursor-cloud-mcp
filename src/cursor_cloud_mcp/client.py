"""Client REST Cursor. Les POST ne sont jamais rejoués."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from cursor_cloud_mcp import budget, redaction
from cursor_cloud_mcp.config import (
    API_BASE,
    CREATE_DEADLINE_SECONDS,
    DEFAULT_DEADLINE_SECONDS,
    MAX_RESPONSE_BYTES,
    MODEL_CACHE_TTL_SECONDS,
    MUTATION_MIN_SECONDS,
    REPOSITORIES_DEADLINE_SECONDS,
    REPOSITORY_CACHE_TTL_SECONDS,
)
from cursor_cloud_mcp.errors import CursorFailure, ErrorCode, explain, failure
from cursor_cloud_mcp.models import (
    RemoteAccount,
    RemoteAgent,
    RemoteAgentPage,
    RemoteArtifactDownload,
    RemoteArtifactList,
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

_REDIRECTS = {301, 302, 303, 307, 308}
_REQUEST_ID_HEADERS = ("x-request-id", "request-id", "x-cursor-request-id")
_RECOVERY = "Relire l'état distant avant toute nouvelle mutation."


@dataclass(frozen=True)
class MutationContext:
    agent_id: str | None = None
    run_id: str | None = None
    previous_latest_run_id: str | None = None
    lookup_name: str | None = None


class ResponseTooLarge(Exception):
    """Le corps dépasse la limite lue en flux."""


class _Retry(Exception):
    """Un GET peut être relancé une fois dans le même budget."""


@dataclass
class _TtlCache[T]:
    """Cache TTL sous verrou. Annuler un appelant n'annule que sa propre lecture."""

    ttl: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    entry: tuple[float, T] | None = None

    async def get(self, fetch: Callable[[], Awaitable[T]], *, wait_cap: float) -> tuple[T, bool]:
        try:
            async with asyncio.timeout_at(budget.deadline_at(wait_cap)):
                await self.lock.acquire()
        except TimeoutError:
            raise failure(ErrorCode.TIMEOUT, "Délai dépassé en attendant une lecture déjà en cours.") from None
        try:
            if self.entry is not None and time.monotonic() - self.entry[0] < self.ttl:
                return self.entry[1], True
            value = await fetch()
            self.entry = (time.monotonic(), value)
            return value, False
        finally:
            self.lock.release()


class CursorCloudClient:
    """Un client HTTP par processus, avec cache mémoire des dépôts."""

    def __init__(
        self,
        *,
        api_key: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        repositories_deadline_seconds: float = REPOSITORIES_DEADLINE_SECONDS,
        create_deadline_seconds: float = CREATE_DEADLINE_SECONDS,
        repository_cache_ttl_seconds: float = REPOSITORY_CACHE_TTL_SECONDS,
        model_cache_ttl_seconds: float = MODEL_CACHE_TTL_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        download_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        redaction.register(api_key)
        self._transport = transport
        self.download_transport = download_transport
        self._deadline = deadline_seconds
        self._repositories_deadline = repositories_deadline_seconds
        self._create_deadline = create_deadline_seconds
        self._max_body = max_response_bytes
        self._http: httpx.AsyncClient | None = None
        self._repo_cache: _TtlCache[RemoteRepositoryList] = _TtlCache(ttl=repository_cache_ttl_seconds)
        self._model_cache: _TtlCache[RemoteModelList] = _TtlCache(ttl=model_cache_ttl_seconds)

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

    @property
    def deadline_seconds(self) -> float:
        return self._deadline

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def get_account(self) -> RemoteAccount:
        return await self._get_model("/v1/me", RemoteAccount, deadline=self._deadline)

    async def list_models(self) -> RemoteModelList:
        return await self._get_model("/v1/models", RemoteModelList, deadline=self._deadline)

    async def cached_models(self) -> tuple[RemoteModelList, bool]:
        return await self._model_cache.get(self.list_models, wait_cap=self._deadline)

    async def list_repositories(self) -> tuple[RemoteRepositoryList, bool]:
        return await self._repo_cache.get(self._fetch_repositories, wait_cap=self._repositories_deadline)

    async def list_agents(
        self,
        *,
        limit: int | None,
        cursor: str | None,
        include_archived: bool | None = None,
    ) -> RemoteAgentPage:
        query = _page_query(limit, cursor)
        if include_archived is not None:
            query["includeArchived"] = "true" if include_archived else "false"
        return await self._get_model(
            "/v1/agents",
            RemoteAgentPage,
            query=query,
            deadline=self._deadline,
        )

    async def get_agent(self, agent_id: str) -> RemoteAgent:
        segment = require_segment(agent_id, label="agent_id")
        return await self._get_model(
            f"/v1/agents/{quote(segment, safe='')}",
            RemoteAgent,
            deadline=self._deadline,
        )

    async def create_agent(
        self,
        body: Mapping[str, object],
        *,
        agent_id: str | None,
        lookup_name: str | None = None,
    ) -> RemoteCreateAgent:
        context = MutationContext(agent_id=agent_id, lookup_name=lookup_name)
        payload = await self._send(
            "POST",
            "/v1/agents",
            json_body=dict(body),
            deadline=self._create_deadline,
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

    async def get_run(self, agent_id: str, run_id: str, *, deadline: float | None = None) -> RemoteRun:
        agent = require_segment(agent_id, label="agent_id")
        run = require_segment(run_id, label="run_id")
        return await self._get_model(
            f"/v1/agents/{quote(agent, safe='')}/runs/{quote(run, safe='')}",
            RemoteRun,
            deadline=self._deadline if deadline is None else deadline,
        )

    def run_stream_path(self, agent_id: str, run_id: str) -> str:
        agent = require_segment(agent_id, label="agent_id")
        run = require_segment(run_id, label="run_id")
        return f"/v1/agents/{quote(agent, safe='')}/runs/{quote(run, safe='')}/stream"

    @asynccontextmanager
    async def stream_get(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None,
        deadline: float,
    ) -> AsyncIterator[httpx.Response]:
        """GET en flux, sans retry. L'appelant ferme la réponse via ce contexte."""
        if self._http is None:
            raise failure(ErrorCode.CONFIGURATION_MISSING, "Le client HTTP n'est pas ouvert.")
        request = self._http.build_request(
            "GET",
            path,
            headers=dict(headers or {}),
            timeout=httpx.Timeout(deadline),
        )
        started = time.perf_counter()
        try:
            response = await self._http.send(request, stream=True)
        except httpx.TimeoutException:
            raise failure(ErrorCode.TIMEOUT, explain(ErrorCode.TIMEOUT)) from None
        except httpx.RequestError:
            raise failure(ErrorCode.TIMEOUT, "Connexion interrompue pendant la lecture.") from None
        request_id = _request_id(response.headers)
        _log("GET", path, str(response.status_code), started, request_id)
        try:
            yield response
        finally:
            await close_quietly(response)

    def error_from_response(self, response: httpx.Response, raw: bytes) -> CursorFailure:
        return _status_from(
            response.status_code,
            response.headers,
            raw,
            mutation=None,
            request_id=_request_id(response.headers),
            retry_after=None,
            blocked_by_deadline=False,
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
            deadline=self._create_deadline,
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

    async def list_artifacts(self, agent_id: str) -> RemoteArtifactList:
        segment = require_segment(agent_id, label="agent_id")
        return await self._get_model(
            f"/v1/agents/{quote(segment, safe='')}/artifacts",
            RemoteArtifactList,
            deadline=self._deadline,
        )

    async def artifact_download(self, agent_id: str, path: str) -> RemoteArtifactDownload:
        segment = require_segment(agent_id, label="agent_id")
        return await self._get_model(
            f"/v1/agents/{quote(segment, safe='')}/artifacts/download",
            RemoteArtifactDownload,
            query={"path": path},
            deadline=self._deadline,
        )

    async def archive_agent(self, agent_id: str) -> RemoteId:
        return await self._id_mutation(agent_id, "archive")

    async def unarchive_agent(self, agent_id: str) -> RemoteId:
        return await self._id_mutation(agent_id, "unarchive")

    async def delete_agent(self, agent_id: str) -> RemoteId:
        segment = require_segment(agent_id, label="agent_id")
        context = MutationContext(agent_id=segment)
        payload = await self._send(
            "DELETE",
            f"/v1/agents/{quote(segment, safe='')}",
            deadline=self._deadline,
            mutation=context,
        )
        return _parse(RemoteId, payload, mutation=context)

    async def _id_mutation(self, agent_id: str, action: str) -> RemoteId:
        segment = require_segment(agent_id, label="agent_id")
        context = MutationContext(agent_id=segment)
        payload = await self._send(
            "POST",
            f"/v1/agents/{quote(segment, safe='')}/{action}",
            deadline=self._deadline,
            mutation=context,
        )
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

    async def _get_model[M: BaseModel](
        self,
        path: str,
        model: type[M],
        *,
        query: Mapping[str, str] | None = None,
        deadline: float,
    ) -> M:
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
        """Un appel HTTP borné par ``deadline`` et par le budget de l'outil.

        Tout ce qui suit l'envoi d'une mutation — en-têtes, corps, décodage, fermeture —
        produit au pire ``MUTATION_OUTCOME_UNKNOWN`` avec le contexte connu. Une mutation
        n'est jamais rejouée ; un GET l'est au plus une fois.
        """
        if self._http is None:
            raise failure(ErrorCode.CONFIGURATION_MISSING, "Le client HTTP n'est pas ouvert.")
        loop = asyncio.get_running_loop()
        deadline_at = budget.deadline_at(deadline)
        floor = min(MUTATION_MIN_SECONDS, deadline / 2) if mutation is not None else 0.0
        if deadline_at - loop.time() <= floor:
            # Rien n'est envoyé : mieux vaut un refus net qu'une mutation à l'issue inconnue.
            raise failure(
                ErrorCode.TIMEOUT,
                "Budget de l'outil épuisé avant l'envoi. Aucune requête n'a été envoyée.",
                agent_id=mutation.agent_id if mutation is not None else None,
                run_id=mutation.run_id if mutation is not None else None,
            )
        attempt = 0
        status: int | None = None
        request_id: str | None = None
        try:
            async with asyncio.timeout_at(deadline_at):
                while True:
                    status, request_id = None, None
                    remaining = deadline_at - loop.time()
                    if remaining <= 0:
                        raise TimeoutError
                    started = time.perf_counter()
                    request = self._http.build_request(
                        method,
                        path,
                        params=dict(query or {}),
                        json=dict(json_body) if json_body is not None else None,
                        timeout=httpx.Timeout(remaining),
                    )
                    try:
                        response = await self._http.send(request, stream=True)
                    except httpx.TimeoutException:
                        raise TimeoutError from None
                    except httpx.RequestError:
                        _log(method, path, "transport", started, None)
                        if method == "GET" and attempt == 0 and deadline_at - loop.time() > 0:
                            attempt = 1
                            continue
                        raise _interrupted(mutation, "La connexion a été coupée.") from None
                    status = response.status_code
                    request_id = _request_id(response.headers)
                    _log(method, path, str(status), started, request_id)
                    try:
                        return await self._handle(
                            method,
                            response,
                            request_id=request_id,
                            mutation=mutation,
                            retry_allowed=attempt == 0,
                            deadline_at=deadline_at,
                        )
                    except _Retry:
                        attempt = 1
                        continue
                    finally:
                        await close_quietly(response)
        except (TimeoutError, httpx.TimeoutException):
            detail = "Le délai a expiré après l'envoi possible de la requête."
            if status is not None:
                detail = "Le délai a expiré pendant la lecture de la réponse."
            raise _interrupted(mutation, detail, status=status, request_id=request_id, timeout=True) from None
        except httpx.RequestError:
            raise _interrupted(
                mutation,
                "La connexion a été coupée pendant la lecture de la réponse.",
                status=status,
                request_id=request_id,
            ) from None

    async def _handle(
        self,
        method: str,
        response: httpx.Response,
        *,
        request_id: str | None,
        mutation: MutationContext | None,
        retry_allowed: bool,
        deadline_at: float,
    ) -> object | None:
        if response.status_code in _REDIRECTS:
            raise _redirect_failure(response, request_id, mutation)
        if method == "GET" and retry_allowed and _retryable(response.status_code):
            header_wait = _retry_after_seconds(response)
            wait = 1.0 if header_wait is None and response.status_code == 429 else header_wait
            if wait is None:
                wait = 0.0
            if wait > deadline_at - asyncio.get_running_loop().time():
                raw = await self._read(response, mutation, request_id)
                raise _status_from(
                    response.status_code,
                    response.headers,
                    raw,
                    mutation=None,
                    request_id=request_id,
                    retry_after=header_wait,
                    blocked_by_deadline=True,
                )
            if wait:
                await asyncio.sleep(wait)
            raise _Retry
        raw = await self._read(response, mutation, request_id)
        return _interpret_bytes(
            response.status_code,
            response.headers,
            raw,
            mutation=mutation,
            request_id=request_id,
        )

    async def _read(
        self,
        response: httpx.Response,
        mutation: MutationContext | None,
        request_id: str | None,
    ) -> bytes:
        try:
            return await read_bounded(response, self._max_body)
        except ResponseTooLarge:
            raise _body_failure(
                mutation,
                request_id,
                response.status_code,
                "La réponse dépasse la limite locale.",
            ) from None


async def close_quietly(response: httpx.Response) -> None:
    """Une erreur de fermeture n'écrase ni un résultat établi ni l'erreur d'origine."""
    try:
        await response.aclose()
    except (httpx.HTTPError, OSError) as exc:
        logger.debug("http close_error=%s", type(exc).__name__)


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


async def read_bounded(response: httpx.Response, limit: int) -> bytes:
    """Lit le corps en flux et s'arrête au-delà de ``limit``."""
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                raise ResponseTooLarge
            chunks.append(chunk)
    except httpx.RequestError:
        # httpx ferme le flux en fin d'itération : une erreur de fermeture n'écrase pas
        # un corps reçu en entier, ce que seul Content-Length (sans encodage) établit.
        if _complete(response, total):
            logger.debug("http close_error_after_full_body")
            return b"".join(chunks)
        raise
    return b"".join(chunks)


def _complete(response: httpx.Response, received: int) -> bool:
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        return False
    declared = response.headers.get("content-length", "")
    return declared.isdigit() and int(declared) == received


def _interpret_bytes(
    status: int,
    headers: httpx.Headers,
    raw: bytes,
    *,
    mutation: MutationContext | None,
    request_id: str | None,
) -> object | None:
    if status not in {200, 201}:
        raise _status_from(
            status,
            headers,
            raw,
            mutation=mutation,
            request_id=request_id,
            retry_after=None,
            blocked_by_deadline=False,
        )
    if not raw:
        return None
    content_type = headers.get("content-type", "")
    if "html" in content_type.lower() or raw.lstrip().startswith(b"<"):
        raise _body_failure(mutation, request_id, status, "La réponse est du HTML.")
    try:
        return json.loads(raw.decode())
    except (ValueError, UnicodeError):
        raise _body_failure(mutation, request_id, status, "La réponse n'est pas du JSON.") from None


def _parse[M: BaseModel](model: type[M], payload: object | None, *, mutation: MutationContext | None) -> M:
    try:
        return model.model_validate(payload)
    except ValidationError:
        if mutation is not None:
            raise _uncertain(mutation, "Le corps de succès ne respecte pas le schéma.") from None
        raise failure(
            ErrorCode.INCOMPATIBLE_RESPONSE,
            f"{explain(ErrorCode.INCOMPATIBLE_RESPONSE)} Un champ essentiel est absent ou d'un type inattendu.",
        ) from None


def _uncertain(
    mutation: MutationContext,
    detail: str,
    *,
    status: int | None = None,
    request_id: str | None = None,
    remote_code: str | None = None,
) -> CursorFailure:
    """Issue de mutation inconnue, avec tout le contexte utile à une reprise sans doublon."""
    return failure(
        ErrorCode.MUTATION_OUTCOME_UNKNOWN,
        f"{explain(ErrorCode.MUTATION_OUTCOME_UNKNOWN)} {detail}".strip(),
        agent_id=mutation.agent_id,
        run_id=mutation.run_id,
        previous_latest_run_id=mutation.previous_latest_run_id,
        recovery=_recovery_for(mutation),
        http_status=status,
        remote_code=remote_code,
        request_id=request_id,
    )


def _interrupted(
    mutation: MutationContext | None,
    detail: str,
    *,
    status: int | None = None,
    request_id: str | None = None,
    timeout: bool = False,
) -> CursorFailure:
    if mutation is not None:
        return _uncertain(mutation, detail, status=status, request_id=request_id)
    message = explain(ErrorCode.TIMEOUT) if timeout else "Connexion interrompue pendant la lecture."
    return failure(ErrorCode.TIMEOUT, message, http_status=status, request_id=request_id)


def _body_failure(
    mutation: MutationContext | None,
    request_id: str | None,
    status: int,
    detail: str,
) -> CursorFailure:
    if mutation is not None:
        return _uncertain(mutation, detail, status=status, request_id=request_id)
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
) -> CursorFailure:
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
        return _uncertain(mutation, message, status=response.status_code, request_id=request_id)
    return failure(
        ErrorCode.INCOMPATIBLE_RESPONSE,
        message,
        http_status=response.status_code,
        request_id=request_id,
    )


def _recovery_for(mutation: MutationContext | None) -> str:
    if mutation is not None and mutation.agent_id is None and mutation.lookup_name:
        return (
            f"Chercher l'agent nommé {mutation.lookup_name} avec cursor_list_agents "
            "avant toute nouvelle tentative."
        )
    return _RECOVERY


def _status_from(
    status: int,
    headers: httpx.Headers,
    raw: bytes,
    *,
    mutation: MutationContext | None,
    request_id: str | None,
    retry_after: float | None,
    blocked_by_deadline: bool,
) -> CursorFailure:
    remote_code, remote_message = _remote_error_bytes(headers, raw)
    remote_message = _clean(remote_message)
    code = _classify(status, remote_code, mutation=mutation is not None)
    if code is ErrorCode.MUTATION_OUTCOME_UNKNOWN and mutation is not None:
        return _uncertain(mutation, remote_message, status=status, request_id=request_id, remote_code=remote_code)
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
    if code is ErrorCode.STREAM_EXPIRED:
        recovery = "Le flux n'est plus rejouable. Lire le résultat avec cursor_get_run."
    return failure(
        code,
        message,
        agent_id=agent_id,
        run_id=run_id,
        previous_latest_run_id=previous,
        recovery=recovery,
        http_status=status,
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
    if remote_code in {"stream_expired", "stream_unavailable"} or status == 410:
        return ErrorCode.STREAM_EXPIRED
    if status == 400:
        return ErrorCode.VALIDATION
    if status >= 500 and mutation:
        return ErrorCode.MUTATION_OUTCOME_UNKNOWN
    if status >= 500:
        return ErrorCode.UPSTREAM
    return ErrorCode.INCOMPATIBLE_RESPONSE


def _remote_error_bytes(headers: httpx.Headers, raw: bytes) -> tuple[str | None, str]:
    content_type = headers.get("content-type", "")
    if "html" in content_type.lower() or raw.lstrip().startswith(b"<"):
        return None, ""
    try:
        payload = json.loads(raw.decode()) if raw else None
    except (ValueError, UnicodeError):
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


def _clean(message: str) -> str:
    """Message distant compacté, borné et débarrassé de tout secret connu du processus."""
    compact = redaction.redact(" ".join(message.split()))
    if len(compact) > 300:
        return compact[:300] + "…"
    return compact
