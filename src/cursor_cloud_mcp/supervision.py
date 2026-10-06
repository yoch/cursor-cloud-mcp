"""Supervision of runs: agent scans, stream tail, activity summary, overview."""

import asyncio
import datetime as dt
import weakref
from collections import Counter, OrderedDict
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
from cursor_cloud_mcp.errors import CursorFailure, ErrorCode, failure
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
# Agent pages are always read at the API maximum; a scan resumes inside a page with a skip.
_PAGE_SIZE = 100
# Composite cursor: "mcp~<skip>~<api cursor>" = read this API page, skip its first <skip> agents.
_CURSOR_PREFIX = "mcp~"
# Complete summaries of terminal runs: a terminal run never changes, its replay need not be repeated.
# One cache per API client, so separate servers in one process never share it.
_TERMINAL_CACHE_SIZE = 256
_terminal_caches: weakref.WeakKeyDictionary[CursorCloudClient, OrderedDict[tuple[str, str], ActivityView]] = (
    weakref.WeakKeyDictionary()
)


def name_filter(name: str) -> Callable[[RemoteAgentSummary], bool]:
    needle = name.casefold()
    return lambda agent: needle in (agent.name or "").casefold()


def is_scan_cursor(cursor: str | None) -> bool:
    return cursor is not None and cursor.startswith(_CURSOR_PREFIX)


def _split_cursor(cursor: str | None) -> tuple[str | None, int]:
    if not is_scan_cursor(cursor):
        return cursor, 0
    parts = str(cursor).split("~", 2)
    if len(parts) != 3 or not parts[1].isdigit() or not 0 < int(parts[1]) < _PAGE_SIZE:
        raise failure(ErrorCode.VALIDATION, "cursor is not one this server returned: pass next_cursor unchanged.")
    return parts[2] or None, int(parts[1])


def _join_cursor(api_cursor: str | None, skip: int) -> str:
    return f"{_CURSOR_PREFIX}{skip}~{api_cursor or ''}"


async def scan_agents(
    client: CursorCloudClient,
    *,
    cursor: str | None,
    include_archived: bool | None,
    pr_url: str | None,
    keep: Callable[[RemoteAgentSummary], bool],
    wanted: int,
) -> AgentPageView:
    """Bounded scan of agent pages with a local filter: the API filters by neither name nor status.

    Returns at most ``wanted`` matches. API cursors only point at page boundaries, so when the
    last match sits in the middle of a page, ``next_cursor`` is a composite cursor that resumes
    right after it: no match is ever skipped. Like any offset paging, it is best-effort if the
    list changes between two calls.
    """
    api_cursor, skip = _split_cursor(cursor)
    matches: list[AgentSummaryView] = []
    scanned = 0
    for _ in range(AGENT_SCAN_MAX_PAGES):
        page = await client.list_agents(
            limit=_PAGE_SIZE, cursor=api_cursor, include_archived=include_archived, pr_url=pr_url
        )
        for index in range(skip, len(page.items)):
            scanned += 1
            if keep(page.items[index]):
                matches.append(summary_from(page.items[index]))
            if len(matches) >= wanted and index + 1 < len(page.items):
                return AgentPageView(
                    items=matches,
                    next_cursor=_join_cursor(api_cursor, index + 1),
                    has_more=True,
                    scanned=scanned,
                )
            if len(matches) >= wanted:
                break
        skip = 0
        following, has_more = next_page(page)
        if not has_more:
            return AgentPageView(items=matches, next_cursor=None, has_more=False, scanned=scanned)
        api_cursor = following
        if len(matches) >= wanted or budget.remaining(TOOL_BUDGET_SECONDS) < AGENT_SCAN_MIN_SECONDS:
            break
    return AgentPageView(items=matches, next_cursor=api_cursor, has_more=True, scanned=scanned)


def needs_activity(view: RunView, requested: bool | None) -> bool:
    """Explicit choice, or by default a terminal run without a result (ERROR carries no cause)."""
    if requested is not None:
        return requested
    return view.terminal is True and not view.result_present


async def read_activity(
    client: CursorCloudClient,
    *,
    agent_id: str,
    run_id: str,
    run_terminal: bool | None,
    max_seconds: float = ACTIVITY_MAX_SECONDS,
) -> tuple[ActivityView | None, str | None]:
    """Activity summary from a full stream walk, or the reason it could not be read.

    Complete summaries of terminal runs are cached: only ``idle_seconds`` depends on the clock.
    """
    key = (agent_id.lower(), run_id.lower())
    _terminal_cache = _terminal_caches.setdefault(client, OrderedDict())
    if run_terminal is True and key in _terminal_cache:
        _terminal_cache.move_to_end(key)
        return _with_current_idle(_terminal_cache[key]), None
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
    if replay.stream_error:
        # A partial summary would pass for a conclusive one: report the error, cache nothing.
        return None, f"{ErrorCode.UPSTREAM.value}: the stream reported an error before the end of the replay."
    summary = replay.tracker.summary(run_terminal=run_terminal).model_copy(update={"complete": replay.complete})
    if run_terminal is True and replay.complete:
        _terminal_cache[key] = summary
        while len(_terminal_cache) > _TERMINAL_CACHE_SIZE:
            _terminal_cache.popitem(last=False)
    return summary, None


def _with_current_idle(summary: ActivityView) -> ActivityView:
    last_at = event_time(summary.last_event_id)
    if last_at is None:
        return summary
    idle = max(0, int((dt.datetime.now(dt.UTC) - last_at).total_seconds()))
    return summary.model_copy(update={"idle_seconds": idle})


async def with_activity(client: CursorCloudClient, view: RunView, *, requested: bool | None) -> RunView:
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
    """Latest run of each agent of ``page``; one failed read never fails the pass.

    Two phases, each with its own concurrency limit: every status first (quick reads), then
    the stream replays (about 30 s each when idle). Replays never delay a status read.
    """
    status_gate = asyncio.Semaphore(SUPERVISE_CONCURRENCY)
    walk_gate = asyncio.Semaphore(SUPERVISE_CONCURRENCY)

    async def read_status(agent: AgentSummaryView) -> SupervisedAgentView:
        row = SupervisedAgentView(
            agent_id=agent.agent_id,
            name=agent.name,
            url=agent.url,
            agent_status=agent.status,
            run_id=agent.latest_run_id,
        )
        if agent.latest_run_id is None:
            return row
        async with status_gate:
            try:
                remote = await client.get_run(agent.agent_id, agent.latest_run_id)
            except CursorFailure as exc:
                return row.model_copy(update={"read_error": f"{exc.body.code.value}: {exc.body.message}"})
        view = run_view(remote, offset=0, limit=1)
        return row.model_copy(
            update={
                "status": view.status,
                "terminal": view.terminal,
                "created_at": view.created_at,
                "duration_ms": view.duration_ms,
                "result_present": view.result_present,
                "error": view.error,
            }
        )

    async def add_activity(row: SupervisedAgentView) -> SupervisedAgentView:
        # Finished runs included: a FINISHED run with a job last seen running is the case to catch.
        if row.run_id is None or row.status is None:
            return row
        async with walk_gate:
            # Parallel replays slow each other down: each read may use what is left of the pass.
            found, error = await read_activity(
                client,
                agent_id=row.agent_id,
                run_id=row.run_id,
                run_terminal=row.terminal,
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

    rows = list(await asyncio.gather(*(read_status(agent) for agent in page.items)))
    if activity:
        rows = list(await asyncio.gather(*(add_activity(row) for row in rows)))
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
    # Only complete walks are conclusive: a partial one sees an old last event and stale task states.
    complete = [row for row in rows if row.activity is not None and row.activity.complete]
    stale = [
        row.agent_id
        for row in complete
        if row.terminal is not True and row.activity is not None and (row.activity.idle_seconds or 0) >= limit
    ]
    unfinished = [
        row.agent_id
        for row in complete
        if row.terminal is True and row.activity is not None and (row.activity.unfinished_background_tasks or 0) > 0
    ]
    # Expired streams (older than the API's retention, 24 h) are not unfinished reads: their rows
    # keep their own activity_error, the list stays about what more time could have told.
    incomplete = [
        row.agent_id
        for row in rows
        if (row.activity_error is not None and not row.activity_error.startswith(ErrorCode.STREAM_EXPIRED.value))
        or (row.activity is not None and not row.activity.complete)
    ]
    errors = sum(1 for row in rows if row.read_error is not None)
    return SuperviseSummaryView(
        agents=len(rows),
        by_status=dict(Counter(_status_key(row) for row in rows)),
        stale=stale or None,
        unfinished_after_end=unfinished or None,
        incomplete=incomplete or None,
        read_errors=errors or None,
    )
