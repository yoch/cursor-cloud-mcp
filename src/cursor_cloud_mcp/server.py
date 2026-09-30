"""Onze outils MCP. Le catalogue reste stable ; les mutations sont refusées sans autorisation."""

import json
import logging
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal, TypeVar

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase
from mcp.types import ToolAnnotations
from pydantic import ConfigDict, Field

from cursor_cloud_mcp.config import (
    NAME_MAX_CHARS,
    PROMPT_MAX_CHARS,
    REPOSITORY_CACHE_TTL_SECONDS,
    RESULT_DEFAULT_LIMIT,
    RESULT_MAX_LIMIT,
    Settings,
    load_settings,
)
from cursor_cloud_mcp.client import CursorCloudClient
from cursor_cloud_mcp.errors import CursorFailure, ErrorCode, failure
from cursor_cloud_mcp.fixture import FixtureTransport
from cursor_cloud_mcp.models import (
    AccountView,
    AgentPageView,
    AgentView,
    CancelView,
    CreateAgentView,
    CreateRunView,
    ModelListView,
    ModelParam,
    RepositoryListView,
    RunPageView,
    RunView,
    UsageView,
)
from cursor_cloud_mcp.present import (
    account_view,
    agent_page_view,
    agent_view,
    cancel_view,
    create_agent_view,
    create_run_view,
    ensure_continuation_allowed,
    model_list_view,
    repository_list_view,
    run_page_view,
    run_view,
    usage_view,
)
from cursor_cloud_mcp.validation import normalize_repository, require_agent_id, require_prompt, require_sha

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Ce serveur pilote des Cursor Cloud Agents par l'API REST v1, un appel à la fois. "
    "cursor_create_agent et cursor_create_run peuvent coûter de l'argent : ne les appelle qu'avec "
    "un dépôt GitHub, un SHA complet déjà vérifié et une décision explicite. "
    "Conserve agent_id et run_id. Une continuation cible le même agent. "
    "Un agent occupé ou un résultat de mutation inconnu se règle en relisant l'état, pas en créant un autre agent. "
    "FINISHED ne prouve ni les tests, ni la revue, ni un SHA final. "
    "La validation GitHub reste dans le client qui appelle ce serveur. "
    "Les mutations échouent tant que CURSOR_MCP_ALLOW_WRITES n'est pas 1, et aucun outil ne change ce réglage."
)

_READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
_CREATE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
_CANCEL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)
_View = TypeVar("_View")


@dataclass
class AppContext:
    settings: Settings
    client: CursorCloudClient | None


def build_server(
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> MCPServer:
    """Construit le serveur. ``transport`` sert aux tests ; il n'est pas un paramètre d'outil."""

    ArgModelBase.model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[AppContext]:
        client: CursorCloudClient | None = None
        if settings.config_error is None and (settings.fixture or settings.api_key is not None):
            chosen = transport
            if chosen is None and settings.fixture:
                chosen = FixtureTransport()
            client = CursorCloudClient(api_key=settings.api_key, transport=chosen)
            await client.open()
        try:
            yield AppContext(settings=settings, client=client)
        finally:
            if client is not None:
                await client.aclose()

    mcp = MCPServer(
        "cursor-cloud-mcp",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        log_level=settings.log_level,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    @mcp.tool(name="cursor_get_account", annotations=_READ)
    async def cursor_get_account(ctx: Context[AppContext]) -> AccountView:
        """Vérifie la clé Cursor via GET /v1/me. Lecture seule. Renvoie le nom de la clé, pas son secret. Étape suivante : lister les modèles ou les dépôts."""
        return await _run("cursor_get_account", ctx, _account)

    @mcp.tool(name="cursor_list_models", annotations=_READ)
    async def cursor_list_models(ctx: Context[AppContext]) -> ModelListView:
        """Liste les modèles et paramètres officiels. Lecture seule. Utilise un id renvoyé ici comme model_id, sans en inventer. Étape suivante : créer un agent seulement si une écriture est voulue."""
        return await _run("cursor_list_models", ctx, _models)

    @mcp.tool(name="cursor_list_repositories", annotations=_READ)
    async def cursor_list_repositories(ctx: Context[AppContext]) -> RepositoryListView:
        """Liste les dépôts GitHub visibles par Cursor. Lecture seule, une réponse, cache mémoire de cinq minutes pour ce processus. Le quota distant est partagé entre processus. Étape suivante : choisir une URL à passer telle quelle à la création."""
        return await _run("cursor_list_repositories", ctx, _repositories)

    @mcp.tool(
        name="cursor_list_agents",
        annotations=_READ,
    )
    async def cursor_list_agents(
        ctx: Context[AppContext],
        limit: int | None = Field(default=None, ge=1, le=100),
        cursor: str | None = None,
    ) -> AgentPageView:
        """Une page d'agents, la plus récente d'abord. Lecture seule. limit et cursor seulement. Conserve next_cursor ; has_more faux signifie fin de liste. Étape suivante : cursor_get_agent pour les dépôts et work_on_current_branch."""
        return await _run("cursor_list_agents", ctx, lambda app: _agents(app, limit, cursor))

    @mcp.tool(name="cursor_get_agent", annotations=_READ)
    async def cursor_get_agent(ctx: Context[AppContext], agent_id: str) -> AgentView:
        """Lit les métadonnées durables d'un agent, y compris hors du périmètre de continuation. Lecture seule. L'état d'exécution est sur le run. Étape suivante : cursor_get_run ou cursor_list_runs."""
        return await _run("cursor_get_agent", ctx, lambda app: _agent(app, agent_id))

    @mcp.tool(name="cursor_create_agent", annotations=_CREATE)
    async def cursor_create_agent(
        ctx: Context[AppContext],
        repository: str,
        starting_sha: str,
        prompt: Annotated[str, Field(min_length=1, max_length=PROMPT_MAX_CHARS)],
        name: Annotated[str | None, Field(max_length=NAME_MAX_CHARS)] = None,
        model_id: str | None = None,
        model_params: list[ModelParam] | None = None,
        mode: Literal["agent", "plan"] | None = None,
        auto_create_pr: bool = False,
        agent_id: str | None = None,
    ) -> CreateAgentView:
        """Crée un agent et son premier run. Effet de bord payant possible. Exige un dépôt GitHub HTTPS et un SHA complet. workOnCurrentBranch est imposé à false. Renvoie agent_id, run_id et url sans attendre la fin du run. Étape suivante : cursor_get_run."""
        return await _run(
            "cursor_create_agent",
            ctx,
            lambda app: _create_agent(
                app,
                repository=repository,
                starting_sha=starting_sha,
                prompt=prompt,
                name=name,
                model_id=model_id,
                model_params=model_params,
                mode=mode,
                auto_create_pr=auto_create_pr,
                agent_id=agent_id,
            ),
            mutation=True,
        )

    @mcp.tool(name="cursor_list_runs", annotations=_READ)
    async def cursor_list_runs(
        ctx: Context[AppContext],
        agent_id: str,
        limit: int | None = Field(default=None, ge=1, le=100),
        cursor: str | None = None,
    ) -> RunPageView:
        """Une page de runs d'un agent, le plus récent d'abord. Lecture seule. Conserve next_cursor. Étape suivante : cursor_get_run sur l'identifiant choisi."""
        return await _run(
            "cursor_list_runs",
            ctx,
            lambda app: _runs(app, agent_id, limit, cursor),
        )

    @mcp.tool(name="cursor_get_run", annotations=_READ)
    async def cursor_get_run(
        ctx: Context[AppContext],
        agent_id: str,
        run_id: str,
        result_offset: int = 0,
        result_limit: int = Field(default=RESULT_DEFAULT_LIMIT, ge=1, le=RESULT_MAX_LIMIT),
    ) -> RunView:
        """Lit l'état, le résultat et les références Git rapportées. Lecture seule. Le résultat long se découpe localement, sans trou. git décrit l'état courant de l'agent, pas un SHA immuable. Un état inconnu reste visible. Étape suivante : relire GitHub, ou cursor_create_run sur le même agent."""
        return await _run(
            "cursor_get_run",
            ctx,
            lambda app: _run_detail(app, agent_id, run_id, result_offset, result_limit),
        )

    @mcp.tool(name="cursor_create_run", annotations=_CREATE)
    async def cursor_create_run(
        ctx: Context[AppContext],
        agent_id: str,
        prompt: Annotated[str, Field(min_length=1, max_length=PROMPT_MAX_CHARS)],
        mode: Literal["agent", "plan"] | None = None,
    ) -> CreateRunView:
        """Envoie une continuation au même agent. Effet de bord payant possible. Refuse si l'agent n'est pas mono-dépôt avec workOnCurrentBranch false. Un conflit d'agent occupé est renvoyé, pas contourné. Étape suivante : cursor_get_run sur le nouveau run_id."""
        return await _run(
            "cursor_create_run",
            ctx,
            lambda app: _create_run(app, agent_id, prompt, mode),
            mutation=True,
        )

    @mcp.tool(name="cursor_cancel_run", annotations=_CANCEL)
    async def cursor_cancel_run(ctx: Context[AppContext], agent_id: str, run_id: str) -> CancelView:
        """Demande l'annulation d'un run. Effet de bord. Ne supprime ni commits ni PR. Distingue demande acceptée et état terminal relu. Si la relecture échoue, le résultat n'est pas confirmé. Étape suivante : cursor_get_run."""
        return await _run(
            "cursor_cancel_run",
            ctx,
            lambda app: _cancel(app, agent_id, run_id),
            mutation=True,
        )

    @mcp.tool(name="cursor_get_usage", annotations=_READ)
    async def cursor_get_usage(
        ctx: Context[AppContext],
        agent_id: str,
        run_id: str | None = None,
    ) -> UsageView:
        """Lit les compteurs de jetons renvoyés pour un agent, ou un run si run_id est fourni. Lecture seule. N'invente pas de coût. Une fonction indisponible reste une erreur de permission. Étape suivante : aucune écriture implicite."""
        return await _run("cursor_get_usage", ctx, lambda app: _usage(app, agent_id, run_id))

    return mcp


def run_stdio() -> None:
    """Démarre le transport stdio. N'écrit rien sur stdout."""
    settings = load_settings()
    _configure_logging(settings)
    build_server(settings).run(transport="stdio")


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    redactor = _RedactFilter(settings.api_key)
    logging.getLogger().addFilter(redactor)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class _RedactFilter(logging.Filter):
    def __init__(self, secret: str | None) -> None:
        super().__init__()
        self._secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        secret = self._secret
        if not secret:
            return True
        rendered = record.getMessage()
        if secret not in rendered:
            return True
        record.msg = rendered.replace(secret, "[redacted]")
        record.args = ()
        return True


async def _run(
    name: str,
    ctx: Context[AppContext],
    action: Callable[[AppContext], Awaitable[_View]],
    *,
    mutation: bool = False,
) -> _View:
    started = time.perf_counter()
    outcome = "ok"
    try:
        app = _app(ctx)
        if mutation:
            _require_writes(app)
        return await action(app)
    except CursorFailure as exc:
        outcome = exc.body.code.value
        raise ToolError(json.dumps(exc.as_dict(), ensure_ascii=False)) from None
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info("tool=%s outcome=%s duration_ms=%d", name, outcome, duration_ms)


def _app(ctx: Context[AppContext]) -> AppContext:
    lifespan_context = ctx.request_context.lifespan_context
    if not isinstance(lifespan_context, AppContext):
        raise failure(ErrorCode.CONFIGURATION_MISSING, "Contexte serveur indisponible.")
    return lifespan_context


def _require_writes(app: AppContext) -> None:
    if app.settings.allow_writes:
        return
    raise failure(
        ErrorCode.READ_ONLY,
        "CURSOR_MCP_ALLOW_WRITES n'est pas 1. Le catalogue reste disponible et la mutation est refusée. "
        "Changez la variable puis redémarrez le serveur. Aucun outil ne modifie ce réglage.",
    )


def _client(app: AppContext) -> CursorCloudClient:
    if app.settings.config_error:
        raise failure(ErrorCode.CONFIGURATION_MISSING, app.settings.config_error)
    if app.client is None:
        raise failure(
            ErrorCode.CONFIGURATION_MISSING,
            "CURSOR_API_KEY est absente. Le serveur ne lit pas de fichier .env.",
        )
    return app.client


async def _account(app: AppContext) -> AccountView:
    return account_view(await _client(app).get_account())


async def _models(app: AppContext) -> ModelListView:
    return model_list_view(await _client(app).list_models())


async def _repositories(app: AppContext) -> RepositoryListView:
    remote, cache_hit = await _client(app).list_repositories()
    return repository_list_view(
        remote,
        cache_hit=cache_hit,
        ttl_seconds=int(REPOSITORY_CACHE_TTL_SECONDS),
    )


async def _agents(app: AppContext, limit: int | None, cursor: str | None) -> AgentPageView:
    _optional_cursor(cursor)
    return agent_page_view(await _client(app).list_agents(limit=limit, cursor=cursor))


async def _agent(app: AppContext, agent_id: str) -> AgentView:
    return agent_view(await _client(app).get_agent(agent_id))


async def _create_agent(
    app: AppContext,
    *,
    repository: str,
    starting_sha: str,
    prompt: str,
    name: str | None,
    model_id: str | None,
    model_params: list[ModelParam] | None,
    mode: str | None,
    auto_create_pr: bool,
    agent_id: str | None,
) -> CreateAgentView:
    checked_prompt = require_prompt(prompt)
    checked_mode = _mode(mode)
    if model_params and not model_id:
        raise failure(ErrorCode.VALIDATION, "model_params exige model_id. Aucun identifiant de modèle n'est inventé.")
    if name is not None and name.strip() == "":
        raise failure(ErrorCode.VALIDATION, "name est vide.")
    chosen_id = require_agent_id(agent_id) if agent_id is not None else f"bc-{uuid.uuid4()}"
    body: dict[str, object] = {
        "prompt": {"text": checked_prompt},
        "repos": [{"url": normalize_repository(repository), "startingRef": require_sha(starting_sha)}],
        "workOnCurrentBranch": False,
        "autoCreatePR": auto_create_pr,
        "agentId": chosen_id,
    }
    if name is not None:
        body["name"] = name
    if model_id is not None:
        model: dict[str, object] = {"id": model_id}
        if model_params:
            model["params"] = [{"id": item.id, "value": item.value} for item in model_params]
        body["model"] = model
    if checked_mode is not None:
        body["mode"] = checked_mode
    remote = await _client(app).create_agent(body, agent_id=chosen_id)
    return create_agent_view(remote)


async def _runs(app: AppContext, agent_id: str, limit: int | None, cursor: str | None) -> RunPageView:
    _optional_cursor(cursor)
    return run_page_view(await _client(app).list_runs(agent_id, limit=limit, cursor=cursor))


async def _run_detail(
    app: AppContext,
    agent_id: str,
    run_id: str,
    result_offset: int,
    result_limit: int,
) -> RunView:
    remote = await _client(app).get_run(agent_id, run_id)
    return run_view(remote, offset=result_offset, limit=result_limit)


async def _create_run(app: AppContext, agent_id: str, prompt: str, mode: str | None) -> CreateRunView:
    checked_prompt = require_prompt(prompt)
    checked_mode = _mode(mode)
    client = _client(app)
    agent = await client.get_agent(agent_id)
    ensure_continuation_allowed(agent)
    body: dict[str, object] = {"prompt": {"text": checked_prompt}}
    if checked_mode is not None:
        body["mode"] = checked_mode
    remote = await client.create_run(
        agent.id,
        body,
        previous_latest_run_id=agent.latestRunId,
    )
    return create_run_view(remote, previous_latest_run_id=agent.latestRunId, url=agent.url)


async def _cancel(app: AppContext, agent_id: str, run_id: str) -> CancelView:
    client = _client(app)
    await client.cancel_run(agent_id, run_id)
    try:
        remote = await client.get_run(agent_id, run_id)
    except CursorFailure as exc:
        return cancel_view(
            agent_id=agent_id,
            run_id=run_id,
            outcome_confirmed=False,
            observed_status=None,
            reread_error=exc.body.message,
        )
    return cancel_view(
        agent_id=agent_id,
        run_id=run_id,
        outcome_confirmed=True,
        observed_status=remote.status,
        reread_error=None,
    )


async def _usage(app: AppContext, agent_id: str, run_id: str | None) -> UsageView:
    return usage_view(await _client(app).get_usage(agent_id, run_id=run_id))


def _optional_cursor(cursor: str | None) -> None:
    if cursor is None or cursor != "":
        return
    raise failure(ErrorCode.VALIDATION, "cursor est vide.")


def _mode(mode: str | None) -> str | None:
    if mode is None:
        return None
    if mode in {"agent", "plan"}:
        return mode
    raise failure(ErrorCode.VALIDATION, "mode doit être agent ou plan.")
