"""Saved environments and self-hosted pools: where an agent can run, and whether it is ready."""

import asyncio

from cursor_cloud_mcp.client import CursorCloudClient
from cursor_cloud_mcp.errors import CursorFailure, ErrorCode, failure
from cursor_cloud_mcp.models import ActiveBuildView, EnvironmentListView, PoolView
from cursor_cloud_mcp.present import active_build_view, environment_view, page_cursor, pool_view


def environment_filters(
    *,
    query: str | None,
    owner: str | None,
    named: bool | None,
    repositories: list[str] | None,
) -> dict[str, str | list[str]]:
    """Query parameters of GET /v1/environments, only those provided. The API applies and combines them."""
    filters: dict[str, str | list[str]] = {}
    if query is not None:
        if query.strip() == "":
            raise failure(ErrorCode.VALIDATION, "query is empty.")
        filters["query"] = query
    if owner is not None:
        filters["owner"] = owner
    if named is not None:
        filters["named"] = "true" if named else "false"
    if repositories is not None:
        if any(item.strip() == "" for item in repositories):
            raise failure(ErrorCode.VALIDATION, "repositories contains an empty entry.")
        filters["repo"] = list(repositories)
    return filters


async def list_environments(
    client: CursorCloudClient,
    *,
    limit: int | None,
    cursor: str | None,
    environment_id: str | None,
    filters: dict[str, str | list[str]] | None = None,
) -> EnvironmentListView:
    """One page of environments with the pools, or one environment with its active build."""
    if environment_id is not None:
        if filters:
            raise failure(ErrorCode.VALIDATION, "environment_id reads one environment: it takes no filter.")
        environment = environment_view(await client.get_environment(environment_id))
        build, error = await _active_build(client, environment.environment_id)
        detail = environment.model_copy(update={"active_build": build, "active_build_error": error})
        return EnvironmentListView(items=[detail], has_more=False)
    # Pools are not paginated: the first page carries them, read alongside it.
    pools_task = asyncio.create_task(_pools(client)) if cursor is None else None
    try:
        page = await client.list_environments(limit=limit, cursor=cursor, filters=filters)
    except BaseException:
        if pools_task is not None:
            pools_task.cancel()
        raise
    next_cursor, has_more = page_cursor(page.nextCursor)
    view = EnvironmentListView(
        items=[environment_view(item) for item in page.items], next_cursor=next_cursor, has_more=has_more
    )
    if pools_task is None:
        return view
    pools, error = await pools_task
    return view.model_copy(update={"pools": pools, "pools_error": error})


async def _pools(client: CursorCloudClient) -> tuple[list[PoolView] | None, str | None]:
    try:
        pools = await client.list_pools()
    except CursorFailure as exc:
        # Best effort: the documentation reserves this list to service-account keys.
        return None, f"{exc.body.code.value}: {exc.body.message}"
    return [pool_view(item) for item in pools.pools], None


async def _active_build(client: CursorCloudClient, environment_id: str) -> tuple[ActiveBuildView | None, str | None]:
    try:
        active = await client.get_active_build(environment_id)
    except CursorFailure as exc:
        return None, f"{exc.body.code.value}: {exc.body.message}"
    if active.buildId is None:
        return active_build_view(active, None), None
    try:
        build = await client.get_build(environment_id, active.buildId)
    except CursorFailure as exc:
        return active_build_view(active, None), f"{exc.body.code.value}: {exc.body.message}"
    return active_build_view(active, build), None
