"""Usage of an agent or of one run, with the model this MCP recorded for each run."""

import logging

from cursor_cloud_mcp import budget
from cursor_cloud_mcp.client import CursorCloudClient
from cursor_cloud_mcp.config import AGENT_SCAN_MAX_PAGES, AGENT_SCAN_MIN_SECONDS, TOOL_BUDGET_SECONDS
from cursor_cloud_mcp.errors import CursorFailure
from cursor_cloud_mcp.journal import ModelJournal
from cursor_cloud_mcp.models import UsageView
from cursor_cloud_mcp.present import page_cursor, usage_view

logger = logging.getLogger(__name__)


async def read_usage(
    client: CursorCloudClient,
    journal: ModelJournal,
    agent_id: str,
    run_id: str | None,
) -> UsageView:
    """Tokens and cost as returned; ``model`` per run from the local journal when it knows it."""
    view = usage_view(await client.get_usage(agent_id, run_id=run_id))
    if not journal.has_agent(agent_id):
        return view
    # A run recorded by id needs nothing more; one sent without a model inherits the previous
    # model, which takes its start time (absent from the usage rows).
    undated = {row.run_id for row in view.runs if journal.for_run(agent_id, row.run_id, None) is None}
    started = await _start_times(client, agent_id, run_id, undated) if undated else {}
    rows = [
        row.model_copy(update={"model": journal.for_run(agent_id, row.run_id, started.get(row.run_id))})
        for row in view.runs
    ]
    return view.model_copy(update={"runs": rows})


async def _start_times(
    client: CursorCloudClient,
    agent_id: str,
    run_id: str | None,
    wanted: set[str],
) -> dict[str, str]:
    """createdAt of the wanted runs. Best effort: a run left undated only loses its inherited model."""
    found: dict[str, str] = {}
    try:
        if run_id is not None:
            remote = await client.get_run(agent_id, run_id)
            return {remote.id: remote.createdAt}
        cursor: str | None = None
        for _ in range(AGENT_SCAN_MAX_PAGES):
            page = await client.list_runs(agent_id, limit=100, cursor=cursor)
            found.update({item.id: item.createdAt for item in page.items if item.id in wanted})
            cursor, has_more = page_cursor(page.nextCursor)
            if not has_more or wanted <= found.keys() or budget.remaining(TOOL_BUDGET_SECONDS) < AGENT_SCAN_MIN_SECONDS:
                break
    except CursorFailure as exc:
        logger.debug("usage_run_dates_unavailable=%s", exc.body.code.value)
    return found
