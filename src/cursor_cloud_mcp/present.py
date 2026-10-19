"""Projection of Cursor payloads onto MCP views. Unknown fields are ignored."""

import json
import logging
from typing import Literal

from cursor_cloud_mcp import __version__
from cursor_cloud_mcp.catalog import default_selection, find_model, reasoning_parameter, restricted
from cursor_cloud_mcp.config import CONVERSATION_TEXT_TOTAL_MAX_CHARS, RESULT_MAX_LIMIT, Settings
from cursor_cloud_mcp.errors import ErrorCode, failure
from cursor_cloud_mcp.models import (
    AccountView,
    ActiveBuildView,
    AgentPageView,
    AgentView,
    CancelView,
    ConversationMessageView,
    ConversationView,
    CostView,
    CreateAgentView,
    CreateRunView,
    EnvironmentView,
    GitBranchView,
    GitView,
    ModelBriefView,
    ModelListView,
    ModelView,
    PoolView,
    RemoteAccount,
    RemoteActiveBuild,
    RemoteAgent,
    RemoteAgentPage,
    RemoteBuild,
    RemoteConversation,
    RemoteCost,
    RemoteCreateAgent,
    RemoteCreateRun,
    RemoteEnvironment,
    RemoteGit,
    RemoteModel,
    RemoteModelList,
    RemotePool,
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
    ServerStatusView,
    UsageView,
    agent_status_known,
    cursor_of,
    run_terminal,
    summary_from,
    tokens_from,
)
from cursor_cloud_mcp.shapes import clip_edges
from cursor_cloud_mcp.slicing import slice_text

logger = logging.getLogger(__name__)
# Unknown run statuses already logged: once per value, to learn new ones without flooding.
_unknown_statuses: set[str] = set()

_NEXT_POLL = "Follow with cursor_get_run (wait_seconds) or cursor_read_run_events. Do not create another agent."
_REUSED_NEXT_STEP = (
    "reused=true: this agent already existed, no new run was sent and the prompt was not applied. "
    "Inspect latest_run_id with cursor_get_run if it is present."
)


def account_view(remote: RemoteAccount, settings: Settings | None = None) -> AccountView:
    view = AccountView(
        api_key_name=remote.apiKeyName,
        created_at=remote.createdAt,
        user_id=remote.userId,
        user_email=remote.userEmail,
        user_first_name=remote.userFirstName,
        user_last_name=remote.userLastName,
        service_account_key=(remote.userId is None and remote.userEmail is None) or None,
    )
    return view if settings is None else view.model_copy(update={"server": server_status(settings)})


def server_status(settings: Settings) -> ServerStatusView:
    default_model = None
    if settings.default_model is not None:
        model_id, params = settings.default_model
        default_model = model_id + (":" + ",".join(f"{key}={value}" for key, value in params) if params else "")
    return ServerStatusView(
        version=__version__,
        writes_enabled=settings.allow_writes,
        delete_enabled=settings.allow_writes and settings.allow_delete,
        fixture=settings.fixture,
        default_model=default_model,
        default_model_error=settings.default_model_error,
        journal_dir=settings.state_dir,
        journal="on" if settings.state_dir is not None else "off",
        forward_env=sorted(settings.forward_env) or None,
    )


def model_list_view(
    remote: RemoteModelList,
    *,
    model_id: str | None = None,
    brief: bool = False,
    query: str | None = None,
) -> ModelListView:
    """Compact catalog. With model_id, a single model and its valid variants."""
    if model_id is not None:
        model = find_model(remote, model_id)
        variants = [
            {item.id: item.value for item in variant.params if item.id in _published(model)}
            for variant in model.variants or []
        ]
        unique = [item for index, item in enumerate(variants) if item and item not in variants[:index]]
        return ModelListView(items=[_model_view(model).model_copy(update={"variants": unique or None})])
    models = remote.items
    if query is not None:
        needle = query.casefold()
        models = [
            model
            for model in models
            if any(needle in name.casefold() for name in [model.id, model.displayName, *(model.aliases or [])])
        ]
    if brief:
        return ModelListView(
            items=[
                ModelBriefView(id=model.id, aliases=model.aliases or None, defaults=default_selection(model))
                for model in models
            ]
        )
    return ModelListView(items=[_model_view(model) for model in models])


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


def page_cursor(value: str | None) -> tuple[str | None, bool]:
    """A page's next cursor and whether there is more; an empty cursor breaks the contract."""
    return _cursor(value)


_INJECTED = "<system_notification>"


def injected_turn(message_type: str, text: str | None) -> bool:
    """A turn Cursor added (a background task or subagent ended): a user turn opening with its tag."""
    return message_type == "user_message" and text is not None and text.lstrip().startswith(_INJECTED)


def conversation_view(
    remote: RemoteConversation, *, agent_id: str, offset: int | None, limit: int, text_limit: int
) -> ConversationView:
    """A window of messages; without offset, the last ``limit``. Texts keep both edges when clipped.

    The window stops early when its texts reach CONVERSATION_TEXT_TOTAL_MAX_CHARS (``truncated``);
    next_offset then resumes right after the last message returned.
    """
    total = len(remote.messages)
    start = max(0, total - limit) if offset is None else offset
    if start > total:
        raise failure(ErrorCode.VALIDATION, f"offset exceeds the {total} messages of this conversation.")
    flags = [injected_turn(item.type, item.text) for item in remote.messages]
    messages: list[ConversationMessageView] = []
    used = 0
    end = start
    for index in range(start, min(total, start + limit)):
        item = remote.messages[index]
        text, clipped = clip_edges(item.text, text_limit)
        cost = len(text or "")
        if messages and used + cost > CONVERSATION_TEXT_TOTAL_MAX_CHARS:
            break
        used += cost
        end = index + 1
        messages.append(
            ConversationMessageView(
                index=index,
                type=item.type,
                text=text,
                clipped=clipped or None,
                injected=flags[index] or None,
            )
        )
    return ConversationView(
        agent_id=agent_id,
        total_messages=total,
        offset=start,
        messages=messages,
        next_offset=end if end < total else None,
        truncated=end < min(total, start + limit) or None,
        injected_turns=sum(flags) or None,
    )


def environment_view(remote: RemoteEnvironment) -> EnvironmentView:
    return EnvironmentView(
        environment_id=remote.id,
        name=remote.name,
        owner=remote.owner,
        repos=[repo.url for repo in remote.repos] or None,
        updated_at=remote.updatedAt,
    )


def active_build_view(active: RemoteActiveBuild, build: RemoteBuild | None) -> ActiveBuildView:
    view = ActiveBuildView(type=active.type, build_id=active.buildId)
    if build is None:
        return view
    failure_text = None
    if build.failure is not None:
        failure_text = build.failure.type if build.failure.code is None else f"{build.failure.type}: {build.failure.code}"
    return view.model_copy(
        update={
            "status": build.status,
            "trigger": build.trigger,
            "draft": build.draft,
            "failure": failure_text,
            "completed_at": build.completedAt,
        }
    )


def pool_view(remote: RemotePool) -> PoolView:
    return PoolView(
        name=remote.poolName,
        scope=remote.scope,
        repo_url=remote.repoUrl,
        connected_workers=remote.connectedWorkerCount,
        in_use_workers=remote.inUseWorkerCount,
        stale=remote.isStale,
    )


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
    for item in remote.items:
        _note_status(item.status)
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
            git=_git_view(item.git),
        )
        for item in remote.items
    ]
    return RunPageView(items=items, next_cursor=cursor, has_more=has_more)


def run_view(remote: RemoteRun, *, offset: int, limit: int) -> RunView:
    _bounds(offset, limit)
    terminal = run_terminal(remote.status)
    _note_status(remote.status)
    common = {
        "run_id": remote.id,
        "agent_id": remote.agentId,
        "status": remote.status,
        "status_known": terminal is not None,
        "terminal": terminal,
        "created_at": remote.createdAt,
        "updated_at": remote.updatedAt,
        "duration_ms": remote.durationMs,
        "git": _git_view(remote.git),
        "error": _run_error(remote.error),
    }
    if remote.result is None:
        if offset != 0:
            raise failure(ErrorCode.VALIDATION, "This run has no result field to slice.")
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


def reused_agent_view(remote: RemoteAgent) -> CreateAgentView:
    """A creation whose identifier already existed: the agent is returned as observed, no run sent."""
    return CreateAgentView(
        agent_id=remote.id,
        agent_status=remote.status,
        url=remote.url,
        name=remote.name,
        latest_run_id=remote.latestRunId,
        reused=True,
        next_step=_REUSED_NEXT_STEP,
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
    """Distinguishes an accepted request, a terminal run and a run actually cancelled."""
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
        charged_cents_per_million_tokens=_rate(remote.cost, remote.totalUsage.totalTokens),
        runs=[
            RunUsageView(
                run_id=item.id,
                usage_uuid=item.usageUuid,
                usage=tokens_from(item.usage),
                cost=_cost(item.cost),
                charged_cents_per_million_tokens=_rate(item.cost, item.usage.totalTokens),
            )
            for item in remote.runs
        ],
    )


def _rate(cost: RemoteCost | None, tokens: int) -> float | None:
    """Observed ratio of two returned numbers; absent when either is missing or zero."""
    if cost is None or tokens <= 0:
        return None
    return round(cost.chargedCents / tokens * 1_000_000, 2)


def _note_status(status: str) -> None:
    if run_terminal(status) is None and status not in _unknown_statuses:
        _unknown_statuses.add(status)
        logger.warning("unknown_run_status=%s", status[:64])


def _cost(remote: RemoteCost | None) -> CostView | None:
    if remote is None:
        return None
    return CostView(raw_cents=round(remote.rawCostCents, 4), charged_cents=round(remote.chargedCents, 4))


def _git_view(remote: RemoteGit | None) -> GitView | None:
    """The agent's current pushed state (scope says so), never a run's frozen checkout."""
    if remote is None:
        return None
    return GitView(
        branches=[
            GitBranchView(repo_url=branch.repoUrl, branch=branch.branch, pr_url=branch.prUrl)
            for branch in remote.branches
        ]
    )


def _run_error(value: object) -> str | None:
    """``error`` is free-form in the contract: readable message if present, otherwise bounded compact JSON."""
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
    """A write requires knowing it is allowed: missing information means refusal."""
    if agent.status == "ARCHIVED":
        raise failure(
            ErrorCode.CONTINUATION_REFUSED,
            "The agent is archived. Unarchive it with cursor_archive_agent (unarchive=true) before a follow-up run.",
        )
    if not agent_status_known(agent.status):
        raise failure(
            ErrorCode.CONTINUATION_REFUSED,
            f"Unknown agent status ({agent.status}). This MCP only follows up on IDLE or ACTIVE agents.",
            agent_id=agent.id,
        )
    if agent.workOnCurrentBranch is True:
        raise failure(
            ErrorCode.CONTINUATION_REFUSED,
            "workOnCurrentBranch is true. This MCP does not follow up on an agent that pushes to the starting branch.",
            agent_id=agent.id,
        )
    if agent.workOnCurrentBranch is None:
        raise failure(
            ErrorCode.CONTINUATION_REFUSED,
            "workOnCurrentBranch is absent from the Cursor response: cannot establish that the agent "
            "does not push to the starting branch. Continue from the Cursor interface if that is intended.",
            agent_id=agent.id,
        )


def _cursor(value: str | None) -> tuple[str | None, bool]:
    try:
        return cursor_of(value)
    except ValueError:
        raise failure(
            ErrorCode.INCOMPATIBLE_RESPONSE,
            "The pagination cursor received is empty.",
        ) from None


def _bounds(offset: int, limit: int) -> None:
    if offset < 0 or limit < 1 or limit > RESULT_MAX_LIMIT:
        raise failure(
            ErrorCode.VALIDATION,
            f"result_offset must be >= 0 and result_limit between 1 and {RESULT_MAX_LIMIT}.",
        )

