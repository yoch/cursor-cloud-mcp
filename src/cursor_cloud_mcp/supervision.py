"""Supervision of runs: agent scans, stream tail, activity summary, overview."""

import asyncio
from collections import Counter
from collections.abc import Callable

from cursor_cloud_mcp import budget
from cursor_cloud_mcp.activity import event_time, iso
from cursor_cloud_mcp.client import CursorCloudClient
from cursor_cloud_mcp.config import (
    ACTIVITY_MAX_SECONDS,
    AGENT_SCAN_MAX_PAGES,
    AGENT_SCAN_MIN_SECONDS,
    SUPERVISE_ACTIVITY_BUDGET_SECONDS,
    TOOL_BUDGET_SECONDS,
)
from cursor_cloud_mcp.errors import CursorFailure
from cursor_cloud_mcp.models import (
    ActivityView,
    AgentPageView,
    AgentSummaryView,
    RemoteAgentSummary,
    RunEventsView,
    RunView,
    SupervisedAgentView,
    SuperviseSummaryView,
    SuperviseView,
    summary_from,
)
from cursor_cloud_mcp.present import next_page, run_view
from cursor_cloud_mcp.stream import Replay, walk_replay

# Below this, a stream walk cannot reasonably complete inside the tool budget.
_MIN_WALK_SECONDS = 2.0
# Parallel reads in one supervision pass: enough for a fleet of runners, gentle on the API.
SUPERVISE_CONCURRENCY = 8


def name_filter(name: str) -> Callable[[RemoteAgentSummary], bool]:
    needle = name.casefold()
    return lambda agent: needle in (agent.name or "").casefold()


async def scan_agents(
    client: CursorCloudClient,
    *,
    cursor: str | None,
    include_archived: bool | None,
    pr_url: str | None,
    keep: Callable[[RemoteAgentSummary], bool],
    wanted: int,
) -> AgentPageView:
    """Bounded scan of agent pages with a local filter: the API filters by neither name nor status."""
    matches = []
    scanned = 0
    page_cursor, has_more = cursor, False
    for _ in range(AGENT_SCAN_MAX_PAGES):
        page = await client.list_agents(limit=100, cursor=page_cursor, include_archived=include_archived, pr_url=pr_url)
        scanned += len(page.items)
        matches.extend(summary_from(item) for item in page.items if keep(item))
        page_cursor, has_more = next_page(page)
        if not has_more or len(matches) >= wanted or budget.remaining(TOOL_BUDGET_SECONDS) < AGENT_SCAN_MIN_SECONDS:
            break
    return AgentPageView(
        items=matches,
        next_cursor=page_cursor if has_more else None,
        has_more=has_more,
        scanned=scanned,
    )


def needs_activity(view: RunView, requested: bool) -> bool:
    """Requested, or implied: a terminal run without a result says nothing on its own (ERROR has no cause)."""
    return requested or (view.terminal is True and not view.result_present)


async def read_activity(
    client: CursorCloudClient,
    *,
    agent_id: str,
    run_id: str,
    run_terminal: bool | None,
    max_seconds: float = ACTIVITY_MAX_SECONDS,
) -> tuple[ActivityView | None, str | None]:
    """Activity summary from a full stream walk, or the reason it could not be read."""
    seconds = min(max_seconds, budget.remaining(max_seconds) - 1.0)
    if seconds < _MIN_WALK_SECONDS:
        return None, "TIMEOUT: tool budget exhausted before reading the stream."
    try:
        replay = await walk_replay(
            client,
            agent_id=agent_id,
            run_id=run_id,
            keep=0,
            include_thinking=False,
            max_wait_seconds=seconds,
        )
    except CursorFailure as exc:
        return None, f"{exc.body.code.value}: {exc.body.message}"
    summary = replay.tracker.summary(run_terminal=run_terminal)
    return summary.model_copy(update={"complete": replay.complete}), None


async def with_activity(client: CursorCloudClient, view: RunView, *, requested: bool) -> RunView:
    if not needs_activity(view, requested):
        return view
    activity, error = await read_activity(
        client,
        agent_id=view.agent_id,
        run_id=view.run_id,
        run_terminal=view.terminal,
    )
    return view.model_copy(update={"activity": activity, "activity_error": error})


def tail_view(agent_id: str, run_id: str, replay: Replay) -> RunEventsView:
    last_event_id = replay.tracker.last_event_id
    return RunEventsView(
        agent_id=agent_id,
        run_id=run_id,
        events=replay.events,
        last_event_id=last_event_id,
        finished=replay.finished,
        stream_error=replay.stream_error,
        interrupted=replay.interrupted,
        run_status=replay.run_status,
        retention_seconds=replay.retention_seconds,
        truncated=not replay.complete,
        last_event_at=iso(event_time(last_event_id)),
        scanned_events=replay.tracker.scanned,
    )


async def supervise(
    client: CursorCloudClient,
    page: AgentPageView,
    *,
    activity: bool,
    stale_after_minutes: int,
) -> SuperviseView:
    """Latest run of each agent of ``page``, read in parallel; one failed read never fails the pass."""
    gate = asyncio.Semaphore(SUPERVISE_CONCURRENCY)

    async def one(agent: AgentSummaryView) -> SupervisedAgentView:
        row = SupervisedAgentView(
            agent_id=agent.agent_id,
            name=agent.name,
            url=agent.url,
            agent_status=agent.status,
            run_id=agent.latest_run_id,
        )
        if agent.latest_run_id is None:
            return row
        async with gate:
            try:
                remote = await client.get_run(agent.agent_id, agent.latest_run_id)
            except CursorFailure as exc:
                return row.model_copy(update={"read_error": f"{exc.body.code.value}: {exc.body.message}"})
            view = run_view(remote, offset=0, limit=1)
            row = row.model_copy(
                update={
                    "status": view.status,
                    "terminal": view.terminal,
                    "created_at": view.created_at,
                    "duration_ms": view.duration_ms,
                    "result_present": view.result_present,
                    "error": view.error,
                }
            )
            if not activity or (view.terminal is True and view.result_present):
                return row
            # Parallel replays slow each other down: each read may use what is left of the pass.
            found, error = await read_activity(
                client,
                agent_id=agent.agent_id,
                run_id=agent.latest_run_id,
                run_terminal=view.terminal,
                max_seconds=SUPERVISE_ACTIVITY_BUDGET_SECONDS,
            )
        if found is not None:
            # An overview keeps the signals, not the texts: cursor_get_run(activity=true) has the detail.
            tool = found.last_tool_call
            found = found.model_copy(
                update={
                    "last_assistant_text": None,
                    "background_tasks": None,
                    "last_tool_call": None if tool is None else tool.model_copy(update={"args": None, "result": None}),
                }
            )
        return row.model_copy(update={"activity": found, "activity_error": error})

    rows = list(await asyncio.gather(*(one(agent) for agent in page.items)))
    return SuperviseView(
        items=rows,
        summary=_summary(rows, stale_after_minutes),
        scanned=page.scanned,
        has_more=page.has_more,
        next_cursor=page.next_cursor,
    )


def _status_key(row: SupervisedAgentView) -> str:
    if row.status is not None:
        return row.status
    return "UNREAD" if row.read_error is not None else "NO_RUN"


def _summary(rows: list[SupervisedAgentView], stale_after_minutes: int) -> SuperviseSummaryView:
    limit = stale_after_minutes * 60
    stale = [
        row.agent_id
        for row in rows
        if row.terminal is not True
        and row.activity is not None
        and row.activity.idle_seconds is not None
        and row.activity.idle_seconds >= limit
    ]
    unfinished = [
        row.agent_id
        for row in rows
        if row.terminal is True and row.activity is not None and (row.activity.unfinished_background_tasks or 0) > 0
    ]
    errors = sum(1 for row in rows if row.read_error is not None)
    return SuperviseSummaryView(
        agents=len(rows),
        by_status=dict(Counter(_status_key(row) for row in rows)),
        stale=stale or None,
        unfinished_after_end=unfinished or None,
        read_errors=errors or None,
    )
