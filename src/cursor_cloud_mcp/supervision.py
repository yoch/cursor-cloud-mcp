"""Supervision of runs: agent scans, stream tail, activity summary, overview."""

import asyncio
import datetime as dt
import hashlib
import weakref
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable, Sequence

from cursor_cloud_mcp import budget
from cursor_cloud_mcp.activity import event_time, iso
from cursor_cloud_mcp.client import CursorCloudClient
from cursor_cloud_mcp.config import (
    ACTIVITY_LAST_EVENTS,
    ACTIVITY_MAX_SECONDS,
    ACTIVITY_TOOL_TEXT_MAX_CHARS,
    AGENT_SCAN_MAX_PAGES,
    AGENT_SCAN_MIN_SECONDS,
    CLIENT_SAFE_BUDGET_SECONDS,
    SUPERVISE_ACTIVITY_BUDGET_SECONDS,
    SUPERVISE_DEFAULT_CONCURRENCY,
    SUPERVISE_STATUS_READ_SECONDS,
    TOOL_BUDGET_SECONDS,
    TOOL_TEXT_TOTAL_MAX_CHARS,
)
from cursor_cloud_mcp.errors import CursorFailure, ErrorCode, failure
from cursor_cloud_mcp.journal import ModelJournal
from cursor_cloud_mcp.models import (
    ActivityView,
    AgentPageView,
    AgentSummaryView,
    RemoteAgentSummary,
    RemoteUsage,
    RunEventsView,
    RunEventView,
    RunView,
    SupervisedAgentView,
    SuperviseSummaryView,
    SuperviseView,
    summary_from,
)
from cursor_cloud_mcp.present import page_cursor, run_view
from cursor_cloud_mcp.stream import Replay, tool_text_cost, walk_replay

# Below this, a stream walk cannot reasonably complete inside the tool budget.
_MIN_WALK_SECONDS = 2.0
# A status read that failed transiently is retried once, only if this much budget is left.
_REREAD_MIN_SECONDS = 5.0
# Worth a second try; 404, auth or quota errors would fail again (the client already retried 429/5xx once).
_TRANSIENT = {ErrorCode.UPSTREAM, ErrorCode.TIMEOUT}
# Agent pages are always read at the API maximum; a scan resumes inside a page with a skip.
_PAGE_SIZE = 100
# Composite cursor: "mcp~<skip>~<api cursor>" = read this API page, skip its first <skip> agents.
_CURSOR_PREFIX = "mcp~"
# Scoped cursor: "mcps~<scope>~<skip>~<api cursor>", only resumed under the same upstream filters.
_SCOPED_PREFIX = "mcps~"
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
    return cursor is not None and cursor.startswith((_CURSOR_PREFIX, _SCOPED_PREFIX))


def scan_scope(*, include_archived: bool | None, pr_url: str | None) -> str:
    """The upstream filters a scan's cursor belongs to: the same API cursor or offset means another list under
    other filters, so a cursor is only resumed with the filters that produced it."""
    archived = "a" if include_archived is not False else "x"
    if pr_url is None:
        return archived
    return archived + hashlib.sha256(pr_url.encode()).hexdigest()[:8]


def _split_cursor(cursor: str | None, scope: str | None) -> tuple[str | None, int]:
    if cursor is not None and cursor.startswith(_SCOPED_PREFIX):
        parts = cursor.split("~", 3)
        if len(parts) != 4:
            raise failure(ErrorCode.VALIDATION, "cursor is not one this server returned: pass next_cursor unchanged.")
        if scope is None or parts[1] != scope:
            raise failure(
                ErrorCode.VALIDATION,
                "cursor belongs to a scan with other filters (include_archived, pr_url, or another tool): "
                "pass it with the filters that returned it, or start again without cursor.",
            )
        return parts[3] or None, _skip_of(parts[2], minimum=0)
    if cursor is None:
        return None, 0
    if scope is not None:
        # Scoped scans only ever return scoped cursors: anything else comes from another tool or an older version,
        # whose list may differ from this one (archived agents were then included by default).
        raise failure(
            ErrorCode.VALIDATION,
            "cursor was not returned by this scan (another tool, or a version before 0.8.0): start again without cursor.",
        )
    if not is_scan_cursor(cursor):
        return cursor, 0
    parts = str(cursor).split("~", 2)
    return (parts[2] or None) if len(parts) == 3 else None, _skip_of(parts[1] if len(parts) == 3 else "", minimum=1)


def _skip_of(raw: str, *, minimum: int) -> int:
    # ASCII digits only, and short: str.isdigit() also accepts "²", which int() refuses.
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 3) or not minimum <= int(raw) < _PAGE_SIZE:
        raise failure(ErrorCode.VALIDATION, "cursor is not one this server returned: pass next_cursor unchanged.")
    return int(raw)


def _join_cursor(api_cursor: str | None, skip: int, scope: str | None = None) -> str | None:
    if scope is not None:
        return f"{_SCOPED_PREFIX}{scope}~{skip}~{api_cursor or ''}"
    if skip:
        return f"{_CURSOR_PREFIX}{skip}~{api_cursor or ''}"
    return api_cursor


async def scan_agents(
    client: CursorCloudClient,
    *,
    cursor: str | None,
    include_archived: bool | None,
    pr_url: str | None,
    keep: Callable[[RemoteAgentSummary], bool],
    wanted: int,
    reserve_seconds: float = 0.0,
    scoped: bool = False,
) -> AgentPageView:
    """Bounded scan of agent pages with a local filter: the API filters by neither name nor status.

    Returns at most ``wanted`` matches. API cursors only point at page boundaries, so when the
    last match sits in the middle of a page, ``next_cursor`` is a composite cursor that resumes
    right after it: no match is ever skipped. Like any offset paging, it is best-effort if the
    list changes between two calls. ``scoped`` cursors also carry the upstream filters (scan_scope):
    they are refused under other filters, where the same offset would point into another list.

    ``reserve_seconds`` of the tool budget are left to the caller: page reads are bounded to the
    rest, and no page starts once it is short. Once a page was read, a failed one (TIMEOUT,
    UPSTREAM, QUOTA, a broken cursor...) ends the scan with the matches found so far and a cursor
    on the unread page: the work already done is returned, never thrown away. ``scan_stopped``
    says why a scan ended before both the list and ``wanted``.
    """
    scope = scan_scope(include_archived=include_archived, pr_url=pr_url) if scoped else None
    api_cursor, skip = _split_cursor(cursor, scope)
    matches: list[AgentSummaryView] = []
    scanned = 0

    def page_view(
        next_cursor: str | None, has_more: bool, stopped: str | None = None, error: str | None = None
    ) -> AgentPageView:
        return AgentPageView(
            items=matches, next_cursor=next_cursor, has_more=has_more, scanned=scanned,
            scan_stopped=stopped, scan_error=error,
        )

    # Each pass either returns or reads one more page: the index is the number of pages read so far.
    for pages_read in range(AGENT_SCAN_MAX_PAGES):
        left = budget.remaining(CLIENT_SAFE_BUDGET_SECONDS) - reserve_seconds
        if pages_read and left < AGENT_SCAN_MIN_SECONDS:
            return page_view(_join_cursor(api_cursor, skip, scope), True, "budget")
        try:
            # The first page is always tried with what there is: without it there is nothing to return.
            with budget.tool_budget(max(left, AGENT_SCAN_MIN_SECONDS) if not pages_read else left):
                page = await client.list_agents(
                    limit=_PAGE_SIZE, cursor=api_cursor, include_archived=include_archived, pr_url=pr_url
                )
            # Checked before the page is used: a page whose cursor is broken is left unread, not half-counted.
            following, has_more = page_cursor(page.nextCursor)
        except CursorFailure as exc:
            if not pages_read:
                raise
            error = f"{exc.body.code.value}: {exc.body.message}"
            return page_view(_join_cursor(api_cursor, skip, scope), True, "read_error", error)
        for index in range(skip, len(page.items)):
            scanned += 1
            if keep(page.items[index]):
                matches.append(summary_from(page.items[index]))
            if len(matches) >= wanted and index + 1 < len(page.items):
                return page_view(_join_cursor(api_cursor, index + 1, scope), True)
            if len(matches) >= wanted:
                break
        skip = 0
        if not has_more:
            return page_view(None, False)
        api_cursor = following
        if len(matches) >= wanted:
            return page_view(_join_cursor(api_cursor, 0, scope), True)
    return page_view(_join_cursor(api_cursor, skip, scope), True, "max_pages")


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
            keep=ACTIVITY_LAST_EVENTS,
            include_thinking=False,
            max_wait_seconds=seconds,
            tool_text_limit=ACTIVITY_TOOL_TEXT_MAX_CHARS,
        )
    except CursorFailure as exc:
        return None, f"{exc.body.code.value}: {exc.body.message}"
    summary = replay.tracker.summary(run_terminal=run_terminal).model_copy(
        update={
            "complete": replay.complete,
            "last_events": replay.events or None,
        }
    )
    if replay.stream_error:
        # The walk stopped on a stream error: the summary is partial, says so, and is never
        # cached. The error event itself stays in last_events.
        return summary.model_copy(update={"stream_error": True}), None
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
    update: dict[str, object] = {"activity": activity, "activity_error": error}
    # Only a complete walk is conclusive: a partial one can stop before a task's completion event.
    if (
        view.terminal is True
        and activity is not None
        and activity.complete
        and (activity.unfinished_background_tasks or 0) > 0
    ):
        update["warnings"] = [*(view.warnings or []), MACHINE_PAUSE_WARNING]
    return view.model_copy(update=update)


# The API exposes no machine state. Reported once (November 2026, ten cloud agents, not measured): the VM was
# paused about 10 to 15 minutes after the run ended, and the jobs it had left running stopped without a signal.
MACHINE_PAUSE_WARNING = (
    "This run ended while background tasks it started were last seen running. The API gives no machine state: "
    "once a run is over its VM may be paused or released (seen about 10 to 15 minutes after the end), which "
    "stops those jobs silently. Check that their output still advances (pushes), and keep long jobs inside a "
    "run that waits for them, or send a follow-up that restarts them from their last pushed state."
)


def tail_view(agent_id: str, run_id: str, replay: Replay) -> RunEventsView:
    events, capped = _cap_tail_tool_text(replay.events)
    last_event_id = replay.tracker.last_event_id
    return RunEventsView(
        agent_id=agent_id,
        run_id=run_id,
        events=events,
        last_event_id=last_event_id,
        finished=replay.finished,
        stream_error=replay.stream_error,
        interrupted=replay.interrupted,
        run_status=replay.run_status,
        retention_seconds=replay.retention_seconds,
        # The walk did not reach its stop criterion, or the tool-text cap dropped the
        # oldest events of the returned window.
        truncated=not replay.complete or capped,
        last_event_at=iso(event_time(last_event_id)),
        scanned_events=replay.tracker.scanned,
    )


def _cap_tail_tool_text(events: list[RunEventView]) -> tuple[list[RunEventView], bool]:
    """Bound the returned window by the global tool-text cap, dropping the oldest events.

    The replay walk always runs to its usual stop criterion: the newest events are the
    point of ``tail``. The cap trims the window from the old side, keeping it contiguous
    and ending on the same event, so ``last_event_id`` stays coherent.
    """
    total = 0
    start = len(events)
    while start > 0:
        cost = tool_text_cost(events[start - 1])
        if start < len(events) and total + cost > TOOL_TEXT_TOTAL_MAX_CHARS:
            break
        total += cost
        start -= 1
    return events[start:], start > 0


async def supervise(
    client: CursorCloudClient,
    page: AgentPageView,
    *,
    activity: bool,
    stale_after_minutes: int,
    usage: bool = False,
    journal: ModelJournal | None = None,
    agent_ids: Sequence[str] | None = None,
    concurrency: int = SUPERVISE_DEFAULT_CONCURRENCY,
    coordinators: Awaitable[dict[str, str]] | None = None,
) -> SuperviseView:
    """Latest run of each agent of ``page`` (or of ``agent_ids``, read one by one); one failed read never fails the pass.

    Each agent goes through its own pipeline, with no barrier between agents: agent read (``agent_ids``
    only), run status (a transient failure retried once), then the usage read and the stream replay
    side by side. A slow agent never holds back the others, and a replay starts as soon as its status is
    known. Each kind of read has its own concurrency limit, so the replays (about 30 s each when idle)
    never delay a status read. ``coordinators`` (the Project coordinator map, read meanwhile) marks the
    rows at the end.
    """
    # One slot per agent for its agent read, run read and retry: a row whose agent was read never queues its
    # run read behind every other agent read. Usage reads and replays have their own limits.
    read_gate = asyncio.Semaphore(concurrency)
    usage_gate = asyncio.Semaphore(concurrency)
    walk_gate = asyncio.Semaphore(concurrency)

    async def read_agent(agent_id: str) -> AgentSummaryView | SupervisedAgentView:
        try:
            with budget.tool_budget(SUPERVISE_STATUS_READ_SECONDS):
                remote = await client.get_agent(agent_id)
        except CursorFailure as exc:
            return SupervisedAgentView(
                agent_id=agent_id,
                agent_status="UNREAD",
                read_error=f"{exc.body.code.value}: {exc.body.message}",
            )
        return summary_from(remote)

    async def read_status(agent: AgentSummaryView) -> tuple[SupervisedAgentView, bool]:
        """The row, and whether its read failed transiently."""
        row = SupervisedAgentView(
            agent_id=agent.agent_id,
            name=agent.name,
            url=agent.url,
            agent_status=agent.status,
            coordinator_of_project=agent.coordinator_of_project,
            run_id=agent.latest_run_id,
        )
        if agent.latest_run_id is None:
            return row, False
        try:
            remote = await client.get_run(agent.agent_id, agent.latest_run_id, deadline=SUPERVISE_STATUS_READ_SECONDS)
        except CursorFailure as exc:
            failed = row.model_copy(update={"read_error": f"{exc.body.code.value}: {exc.body.message}"})
            return failed, exc.body.code in _TRANSIENT
        view = run_view(remote, offset=0, limit=1)
        model = None if journal is None else journal.for_run(agent.agent_id, view.run_id, view.created_at)
        return row.model_copy(
            update={
                "model": model,
                "status": view.status,
                "terminal": view.terminal,
                "created_at": view.created_at,
                "duration_ms": view.duration_ms,
                "result_present": view.result_present,
                "error": view.error,
                "git": view.git,
            }
        ), False

    async def activity_of(row: SupervisedAgentView) -> dict[str, object]:
        # Finished runs included: a FINISHED run with a job last seen running is the case to catch.
        if row.run_id is None or row.status is None:
            return {}
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
                    "last_events": None,
                    "last_tool_call": None if tool is None else tool.model_copy(update={"args": None, "result": None}),
                }
            )
        return {"activity": found, "activity_error": error}

    async def usage_of(row: SupervisedAgentView) -> dict[str, object]:
        async with usage_gate:
            try:
                remote = await client.get_usage(row.agent_id, run_id=None)
            except CursorFailure as exc:
                return {"usage_error": f"{exc.body.code.value}: {exc.body.message}"}
        silent = _usage_missing(row, remote) or None
        if remote.cost is None:
            return {"usage_error": "The API returned no cost for this agent.", "no_usage_recorded": silent}
        charged = round(remote.cost.chargedCents, 4)
        # A zero next to missing usage would read as "free": only a cost actually charged is shown then.
        shown = None if silent and charged == 0 else charged
        return {"charged_cents": shown, "no_usage_recorded": silent}

    async def follow(source: AgentSummaryView | str) -> SupervisedAgentView:
        async with read_gate:
            agent = await read_agent(source) if isinstance(source, str) else source
            if isinstance(agent, SupervisedAgentView):
                return agent
            row, transient = await read_status(agent)
        if transient and budget.remaining(TOOL_BUDGET_SECONDS) >= _REREAD_MIN_SECONDS:
            # A transient failure must not leave a runner unread: one more try, queued behind the first
            # attempts of the other rows rather than ahead of them.
            async with read_gate:
                row, _ = await read_status(agent)
        extras: list[Awaitable[dict[str, object]]] = []
        if usage:
            extras.append(usage_of(row))
        if activity:
            extras.append(activity_of(row))
        update: dict[str, object] = {}
        for part in await asyncio.gather(*extras):
            update.update(part)
        return row.model_copy(update=update) if update else row

    sources: Sequence[AgentSummaryView | str] = list(agent_ids) if agent_ids is not None else page.items
    rows = list(await asyncio.gather(*(follow(source) for source in sources)))
    if coordinators is not None:
        found = await coordinators
        rows = [
            row.model_copy(update={"coordinator_of_project": found[row.agent_id.casefold()]})
            if row.agent_id.casefold() in found
            else row
            for row in rows
        ]
    return SuperviseView(
        items=rows,
        summary=_summary(rows, stale_after_minutes),
        scanned=page.scanned,
        has_more=page.has_more,
        next_cursor=page.next_cursor,
        scan_stopped=page.scan_stopped,
        scan_error=page.scan_error,
    )


def _usage_missing(row: SupervisedAgentView, remote: RemoteUsage) -> bool:
    """True when the API recorded no tokens for a run that is over: its cost is unknown, not zero.

    The latest run is left out while it is still going: it may simply not have reported yet.
    """
    latest_active = row.terminal is not True
    ended = [item for item in remote.runs if not (latest_active and item.id == row.run_id)]
    if any(item.usage.totalTokens == 0 for item in ended):
        return True
    return row.run_id is not None and not latest_active and remote.totalUsage.totalTokens == 0


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
        # A run waiting for its background work is silent by design: not stale.
        if row.terminal is not True
        and row.status != "WAITING_FOR_BACKGROUND_WORK"
        and row.activity is not None
        and (row.activity.idle_seconds or 0) >= limit
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
    costs = [row.charged_cents for row in rows if row.charged_cents is not None]
    usage_errors = sum(1 for row in rows if row.usage_error is not None)
    silent = [row.agent_id for row in rows if row.no_usage_recorded]
    return SuperviseSummaryView(
        agents=len(rows),
        by_status=dict(Counter(_status_key(row) for row in rows)),
        stale=stale or None,
        unfinished_after_end=unfinished or None,
        incomplete=incomplete or None,
        read_errors=errors or None,
        charged_cents_total=round(sum(costs), 4) if costs else None,
        no_usage_recorded=silent or None,
        usage_errors=usage_errors or None,
    )
