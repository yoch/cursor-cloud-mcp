"""Projects: a coordinator agent that starts and follows other agents. Read only."""

from collections.abc import Sequence

from cursor_cloud_mcp.client import CursorCloudClient
from cursor_cloud_mcp.errors import CursorFailure, ErrorCode, failure, failure_note
from cursor_cloud_mcp.models import AgentSummaryView, ProjectListView
from cursor_cloud_mcp.present import agent_page_view, page_cursor, project_view


async def list_projects(
    client: CursorCloudClient,
    *,
    limit: int | None,
    cursor: str | None,
    project_id: str | None,
    include_archived: bool | None,
) -> ProjectListView:
    """One page of Projects, or one Project with a page of the agents its coordinator started."""
    if project_id is None:
        page = await client.list_projects(limit=limit, cursor=cursor, include_archived=include_archived)
        # An empty page can still carry a cursor: has_more follows the cursor, never the item count.
        next_cursor, has_more = page_cursor(page.nextCursor)
        return ProjectListView(
            items=[project_view(item) for item in page.items], next_cursor=next_cursor, has_more=has_more
        )
    if include_archived is not None:
        raise failure(ErrorCode.VALIDATION, "project_id reads one project: it takes no include_archived.")
    project = project_view(await client.get_project(project_id))
    # The Project's own id, which the agents endpoint documents: the caller's coordinator id can differ from it.
    agents, error, next_cursor, has_more = await _agents(client, project.project_id, limit=limit, cursor=cursor)
    return ProjectListView(
        items=[project], agents=agents, agents_error=error, next_cursor=next_cursor, has_more=has_more
    )


async def _agents(
    client: CursorCloudClient, project_id: str, *, limit: int | None, cursor: str | None
) -> tuple[list[AgentSummaryView] | None, str | None, str | None, bool]:
    try:
        page = agent_page_view(await client.list_project_agents(project_id, limit=limit, cursor=cursor))
    except CursorFailure as exc:
        if cursor is not None:
            # The caller asked for this very page, with a cursor that may belong to another list: say so, not "ok".
            raise
        # Best effort on the first page, like an environment's active build: the Project itself was read.
        return None, failure_note(exc), None, False
    return page.items, None, page.next_cursor, page.has_more


async def mark_coordinators[R: AgentSummaryView](client: CursorCloudClient, rows: Sequence[R]) -> list[R]:
    """Rows whose agent is the caller's coordinator in a Project carry ``coordinator_of_project``.

    A marker only: nothing is refused. The coordinator is an ordinary agent to the API, so a fleet cleanup that
    archives every idle agent would archive the Project with it. Best effort: without the Projects list
    (turned off, service-account key, an error) the rows come back unmarked.
    """
    if not rows:
        return list(rows)
    try:
        coordinators = await client.project_coordinators()
    except CursorFailure:
        return list(rows)
    if not coordinators:
        return list(rows)
    return [
        row.model_copy(update={"coordinator_of_project": coordinators[row.agent_id.casefold()]})
        if row.agent_id.casefold() in coordinators
        else row
        for row in rows
    ]
