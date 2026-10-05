"""Projection des payloads Cursor vers les vues MCP. Les champs inconnus sont ignorés."""

import json
from typing import Literal

from cursor_cloud_mcp.catalog import default_selection, find_model, reasoning_parameter, restricted
from cursor_cloud_mcp.config import RESULT_MAX_LIMIT
from cursor_cloud_mcp.errors import ErrorCode, failure
from cursor_cloud_mcp.models import (
    AccountView,
    AgentPageView,
    AgentView,
    CancelView,
    CostView,
    CreateAgentView,
    CreateRunView,
    GitBranchView,
    GitView,
    ModelListView,
    ModelView,
    RemoteAccount,
    RemoteAgent,
    RemoteAgentPage,
    RemoteCost,
    RemoteCreateAgent,
    RemoteCreateRun,
    RemoteModel,
    RemoteModelList,
    RemoteRepositoryList,
    RemoteRun,
    RemoteRunPage,
    RemoteUsage,
    RepositoryListView,
    RepoView,
    RunPageView,
    RunSummaryView,
    RunUsageView,
    RunView,
    UsageView,
    agent_status_known,
    cursor_of,
    run_terminal,
    summary_from,
    tokens_from,
)
from cursor_cloud_mcp.slicing import slice_text

_NEXT_POLL = "Suivre avec cursor_get_run (wait_seconds) ou cursor_read_run_events. Ne pas créer un autre agent."


def account_view(remote: RemoteAccount) -> AccountView:
    return AccountView(
        api_key_name=remote.apiKeyName,
        created_at=remote.createdAt,
        user_id=remote.userId,
        user_email=remote.userEmail,
        user_first_name=remote.userFirstName,
        user_last_name=remote.userLastName,
    )


def model_list_view(remote: RemoteModelList, *, model_id: str | None = None) -> ModelListView:
    """Catalogue compact. Avec model_id, un seul modèle et ses variantes valides."""
    if model_id is not None:
        model = find_model(remote, model_id)
        variants = [
            {item.id: item.value for item in variant.params if item.id in _published(model)}
            for variant in model.variants or []
        ]
        unique = [item for index, item in enumerate(variants) if item and item not in variants[:index]]
        return ModelListView(items=[_model_view(model).model_copy(update={"variants": unique or None})])
    return ModelListView(items=[_model_view(model) for model in remote.items])


def _model_view(model: RemoteModel) -> ModelView:
    params = {parameter.id: [item.value for item in parameter.values] for parameter in model.parameters or []}
    reasoning = reasoning_parameter(model)
    return ModelView(
        id=model.id,
        display_name=model.displayName,
        description=model.description,
        aliases=model.aliases or None,
        params=params or None,
        defaults=default_selection(model),
        reasoning_param=None if reasoning is None else reasoning.id,
        restricted_combinations=restricted(model) or None,
    )


def _published(model: RemoteModel) -> set[str]:
    return {parameter.id for parameter in model.parameters or []}


def repository_list_view(
    remote: RemoteRepositoryList,
    *,
    cache_hit: bool,
    ttl_seconds: int,
    query: str | None = None,
) -> RepositoryListView:
    urls = [item.url for item in remote.items]
    if query is not None:
        needle = query.casefold()
        urls = [url for url in urls if needle in url.casefold()]
    return RepositoryListView(
        items=urls,
        total_count=len(remote.items),
        cache_ttl_seconds=ttl_seconds,
        cache_hit=cache_hit,
    )


def agent_page_view(remote: RemoteAgentPage) -> AgentPageView:
    cursor, has_more = _cursor(remote.nextCursor)
    return AgentPageView(
        items=[summary_from(item) for item in remote.items],
        next_cursor=cursor,
        has_more=has_more,
    )


def next_page(remote: RemoteAgentPage) -> tuple[str | None, bool]:
    return _cursor(remote.nextCursor)


def agent_view(remote: RemoteAgent) -> AgentView:
    summary = summary_from(remote)
    repos = None
    if remote.repos is not None:
        repos = [
            RepoView(url=repo.url, starting_ref=repo.startingRef, pr_url=repo.prUrl) for repo in remote.repos
        ]
    return AgentView(
        agent_id=summary.agent_id,
        name=summary.name,
        status=summary.status,
        status_known=summary.status_known,
        url=summary.url,
        created_at=summary.created_at,
        updated_at=summary.updated_at,
        latest_run_id=summary.latest_run_id,
        env=summary.env,
        repos=repos,
        repo_count=None if repos is None else len(repos),
        work_on_current_branch=remote.workOnCurrentBranch,
        auto_create_pr=remote.autoCreatePR,
    )


def run_page_view(remote: RemoteRunPage) -> RunPageView:
    cursor, has_more = _cursor(remote.nextCursor)
    items = [
        RunSummaryView(
            run_id=item.id,
            agent_id=item.agentId,
            status=item.status,
            status_known=run_terminal(item.status) is not None,
            terminal=run_terminal(item.status),
            created_at=item.createdAt,
            updated_at=item.updatedAt,
            duration_ms=item.durationMs,
        )
        for item in remote.items
    ]
    return RunPageView(items=items, next_cursor=cursor, has_more=has_more)


def run_view(remote: RemoteRun, *, offset: int, limit: int) -> RunView:
    _bounds(offset, limit)
    git = None
    if remote.git is not None:
        git = GitView(
            branches=[
                GitBranchView(repo_url=branch.repoUrl, branch=branch.branch, pr_url=branch.prUrl)
                for branch in remote.git.branches
            ]
        )
    terminal = run_terminal(remote.status)
    common = {
        "run_id": remote.id,
        "agent_id": remote.agentId,
        "status": remote.status,
        "status_known": terminal is not None,
        "terminal": terminal,
        "created_at": remote.createdAt,
        "updated_at": remote.updatedAt,
        "duration_ms": remote.durationMs,
        "git": git,
        "error": _run_error(remote.error),
    }
    if remote.result is None:
        if offset != 0:
            raise failure(ErrorCode.VALIDATION, "Ce run n'a pas de champ result à découper.")
        return RunView(result_present=False, **common)
    chunk, truncated, next_offset = slice_text(remote.result, offset, limit)
    return RunView(
        result_present=True,
        result=chunk,
        result_total_chars=len(remote.result),
        result_offset=offset,
        result_limit=limit,
        result_truncated=truncated,
        next_result_offset=next_offset,
        **common,
    )


def create_agent_view(remote: RemoteCreateAgent) -> CreateAgentView:
    return CreateAgentView(
        agent_id=remote.agent.id,
        run_id=remote.run.id,
        agent_status=remote.agent.status,
        run_status=remote.run.status,
        url=remote.agent.url,
        name=remote.agent.name,
        latest_run_id=remote.agent.latestRunId,
        next_step=_NEXT_POLL,
    )


def create_run_view(remote: RemoteCreateRun, *, previous_latest_run_id: str | None, url: str | None) -> CreateRunView:
    return CreateRunView(
        agent_id=remote.run.agentId,
        run_id=remote.run.id,
        status=remote.run.status,
        previous_latest_run_id=previous_latest_run_id,
        url=url,
        next_step=_NEXT_POLL,
    )


def cancel_view(
    *,
    agent_id: str,
    run_id: str,
    observed_status: str | None,
    reread_error: str | None,
) -> CancelView:
    """Distingue demande acceptée, run terminal et run réellement annulé."""
    observed_terminal = None if observed_status is None else run_terminal(observed_status)
    outcome: Literal["cancelled", "ended_without_cancel", "still_running", "unknown"]
    if observed_status == "CANCELLED":
        outcome = "cancelled"
    elif observed_terminal is True:
        outcome = "ended_without_cancel"
    elif observed_terminal is False:
        outcome = "still_running"
    else:
        outcome = "unknown"
    return CancelView(
        agent_id=agent_id,
        run_id=run_id,
        cancel_request_accepted=True,
        outcome=outcome,
        outcome_confirmed=outcome == "cancelled",
        observed_status=observed_status,
        observed_terminal=observed_terminal,
        reread_error=reread_error,
    )


def usage_view(remote: RemoteUsage) -> UsageView:
    return UsageView(
        total_usage=tokens_from(remote.totalUsage),
        total_cost=_cost(remote.cost),
        runs=[
            RunUsageView(
                run_id=item.id,
                usage_uuid=item.usageUuid,
                usage=tokens_from(item.usage),
                cost=_cost(item.cost),
            )
            for item in remote.runs
        ],
    )


def _cost(remote: RemoteCost | None) -> CostView | None:
    if remote is None:
        return None
    return CostView(raw_cents=round(remote.rawCostCents, 4), charged_cents=round(remote.chargedCents, 4))


def _run_error(value: object) -> str | None:
    """``error`` est libre dans le contrat : message lisible si présent, sinon JSON compact borné."""
    if value is None:
        return None
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, dict):
        message = value.get("message")
        code = value.get("code")
        if isinstance(message, str):
            return f"{code}: {message}"[:2000] if isinstance(code, str) else message[:2000]
    return json.dumps(value, ensure_ascii=False)[:2000]


def ensure_continuation_allowed(agent: RemoteAgent) -> None:
    """Une écriture exige de savoir qu'elle est permise : une information absente refuse."""
    if agent.status == "ARCHIVED":
        raise failure(
            ErrorCode.CONTINUATION_REFUSED,
            "L'agent est archivé. Le désarchiver avec cursor_archive_agent (unarchive=true) avant une continuation.",
        )
    if not agent_status_known(agent.status):
        raise failure(
            ErrorCode.CONTINUATION_REFUSED,
            f"Statut d'agent inconnu ({agent.status}). Ce MCP ne continue que des agents IDLE ou ACTIVE.",
            agent_id=agent.id,
        )
    if agent.workOnCurrentBranch is True:
        raise failure(
            ErrorCode.CONTINUATION_REFUSED,
            "workOnCurrentBranch est true. Ce MCP ne continue pas un agent qui pousse sur la branche de départ.",
            agent_id=agent.id,
        )
    if agent.workOnCurrentBranch is None:
        raise failure(
            ErrorCode.CONTINUATION_REFUSED,
            "workOnCurrentBranch est absent de la réponse Cursor : impossible d'établir que l'agent "
            "ne pousse pas sur la branche de départ. Continuer depuis l'interface Cursor si c'est voulu.",
            agent_id=agent.id,
        )


def _cursor(value: str | None) -> tuple[str | None, bool]:
    try:
        return cursor_of(value)
    except ValueError:
        raise failure(
            ErrorCode.INCOMPATIBLE_RESPONSE,
            "Le curseur de pagination reçu est vide.",
        ) from None


def _bounds(offset: int, limit: int) -> None:
    if offset < 0 or limit < 1 or limit > RESULT_MAX_LIMIT:
        raise failure(
            ErrorCode.VALIDATION,
            f"result_offset doit être >= 0 et result_limit entre 1 et {RESULT_MAX_LIMIT}.",
        )

