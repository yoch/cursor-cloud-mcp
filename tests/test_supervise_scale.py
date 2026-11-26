"""cursor_supervise on a large account: archived agents filtered upstream, partial scans, agent_ids, concurrency."""

import asyncio
import time

import httpx
import pytest
from mcp import Client

from cursor_cloud_mcp.server import build_server
from tests.test_tools import Router, _agent, _data, _error_payload, _run, _session, _settings

pytestmark = pytest.mark.anyio


def _ids(prefix: str, count: int) -> list[str]:
    return [f"bc-{prefix}-0000-0000-0000-{index:012d}" for index in range(count)]


def _run_for(request: httpx.Request, status: str = "RUNNING") -> httpx.Response:
    run_id = request.url.path.rsplit("/", 1)[-1]
    agent_id = request.url.path.split("/")[3]
    return httpx.Response(200, json=_run(id=run_id, agentId=agent_id, status=status, result=None))


def _listing_router(params: list[dict[str, str]]) -> Router:
    agents = [_agent(agent_id, name=f"fleet-w{index}", status="ACTIVE", latestRunId=f"run-{index}")
              for index, agent_id in enumerate(_ids("eeeeeeee", 3))]

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            params.append(dict(request.url.params))
            return httpx.Response(200, json={"items": agents})
        return _run_for(request)

    return Router(responder)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({}, "false"),  # status=active can never keep an archived agent: filtered upstream
        ({"status": "all"}, "false"),  # a fleet is rarely archived: opt in
        ({"status": "all", "include_archived": True}, "true"),
        ({"include_archived": True}, "false"),  # status=active would drop them anyway
    ],
)
async def test_the_scan_leaves_archived_agents_upstream(args: dict[str, object], expected: str) -> None:
    params: list[dict[str, str]] = []
    client, _ = await _session(_listing_router(params))
    try:
        view = _data(await client.call_tool("cursor_supervise", args))
    finally:
        await client.__aexit__(None, None, None)
    assert len(view["items"]) == 3
    assert [page["includeArchived"] for page in params] == [expected]


async def test_a_slow_page_returns_the_rows_already_found(monkeypatch: pytest.MonkeyPatch) -> None:
    # The reported failure: the scan ate the budget, then every run read failed before sending.
    monkeypatch.setattr("cursor_cloud_mcp.server.TOOL_BUDGET_SECONDS", 4.0)
    monkeypatch.setattr("cursor_cloud_mcp.server.SUPERVISE_READ_RESERVE_SECONDS", 2.0)
    monkeypatch.setattr("cursor_cloud_mcp.supervision.AGENT_SCAN_MIN_SECONDS", 0.5)
    first = [_agent(agent_id, name=f"fleet-w{index}", status="ACTIVE", latestRunId=f"run-{index}")
             for index, agent_id in enumerate(_ids("ffffffff", 10))]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/projects":
            return httpx.Response(200, json={"items": []})
        if request.url.path == "/v1/agents":
            if "cursor" in request.url.params:
                await asyncio.sleep(30)  # the second page never answers in time
            return httpx.Response(200, json={"items": first, "nextCursor": "page-2"})
        return _run_for(request)

    server = build_server(_settings(), transport=httpx.MockTransport(handler))
    started = time.monotonic()
    async with Client(server) as client:
        view = _data(await client.call_tool("cursor_supervise", {"name": "fleet"}))
    assert time.monotonic() - started < 4.0
    assert len(view["items"]) == 10
    assert all(row["status"] == "RUNNING" for row in view["items"])  # the reserve kept time for the reads
    assert view["scan_stopped"] == "read_error" and view["scan_error"].startswith("TIMEOUT")
    assert view["has_more"] is True and view["next_cursor"] == "mcps~x~0~page-2"  # resumes on the unread page


async def test_a_failing_first_page_is_still_an_error() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"code": "internal_error", "message": "boom"}})

    client, _ = await _session(Router(responder))
    try:
        refused = await client.call_tool("cursor_supervise", {})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(refused)["code"] == "UPSTREAM"


async def test_agent_ids_skip_the_scan_and_isolate_unknown_agents() -> None:
    known, unknown = _ids("abababab", 2)

    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert path != "/v1/agents", "agent_ids must not scan the account"
        if path == f"/v1/agents/{known}":
            return httpx.Response(200, json=_agent(known, name="fleet-w01", status="IDLE", latestRunId="run-k"))
        if path == f"/v1/agents/{unknown}":
            return httpx.Response(404, json={"error": {"code": "agent_not_found", "message": "no such agent"}})
        return _run_for(request, status="FINISHED")

    client, router = await _session(Router(responder))
    try:
        view = _data(
            await client.call_tool("cursor_supervise", {"agent_ids": [known, unknown, known.upper().replace("BC-", "bc-")]})
        )
    finally:
        await client.__aexit__(None, None, None)
    rows = {row["agent_id"]: row for row in view["items"]}
    assert list(rows) == [known, unknown]  # duplicates read once, order kept
    assert rows[known]["status"] == "FINISHED" and rows[known]["name"] == "fleet-w01"
    assert rows[unknown]["agent_status"] == "UNREAD" and rows[unknown]["read_error"].startswith("NOT_FOUND")
    assert "url" not in rows[unknown]
    assert view["summary"]["by_status"] == {"FINISHED": 1, "UNREAD": 1}
    assert view["has_more"] is False and "scanned" not in view
    assert [path for _method, path, _body in router.calls].count(f"/v1/agents/{known}") == 1


@pytest.mark.parametrize("extra", [{"name": "x"}, {"status": "all"}, {"cursor": "c"}, {"limit": 3}])
async def test_agent_ids_refuse_scan_arguments(extra: dict[str, object]) -> None:
    client, router = await _session(Router(lambda _request: httpx.Response(500)))
    try:
        refused = await client.call_tool("cursor_supervise", {"agent_ids": _ids("cdcdcdcd", 1), **extra})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(refused)["code"] == "VALIDATION"
    assert router.calls == []


async def test_concurrency_bounds_the_parallel_reads() -> None:
    agents = [_agent(agent_id, status="ACTIVE", latestRunId=f"run-{index}")
              for index, agent_id in enumerate(_ids("efefefef", 12))]
    state = {"now": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"items": agents})
        if request.url.path == "/v1/projects":
            return httpx.Response(200, json={"items": []})
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.02)
        state["now"] -= 1
        return _run_for(request)

    server = build_server(_settings(), transport=httpx.MockTransport(handler))
    async with Client(server) as client:
        view = _data(await client.call_tool("cursor_supervise", {"concurrency": 2}))
    assert len(view["items"]) == 12
    assert state["peak"] == 2


async def test_a_slow_projects_read_does_not_delay_the_run_reads() -> None:
    agents = [_agent(agent_id, status="ACTIVE", latestRunId=f"run-{index}")
              for index, agent_id in enumerate(_ids("dededede", 3))]
    order: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/projects":
            await asyncio.sleep(0.3)
            order.append("projects")
            return httpx.Response(200, json={"items": [{"id": agents[0]["id"], "coordinatorAgentId": agents[0]["id"],
                                                         "name": "p", "status": "ACTIVE", "url": "https://cursor.com/p",
                                                         "createdAt": "2026-11-16T00:00:00Z",
                                                         "updatedAt": "2026-11-16T00:00:00Z"}]})
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"items": agents})
        order.append("run")
        return _run_for(request)

    server = build_server(_settings(), transport=httpx.MockTransport(handler))
    async with Client(server) as client:
        view = _data(await client.call_tool("cursor_supervise", {}))
    assert order == ["run", "run", "run", "projects"]  # the reads did not wait for the Projects list
    assert view["items"][0]["coordinator_of_project"] == agents[0]["id"]  # the marker still lands


async def test_slow_pages_stop_the_scan_in_time_for_the_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    # The reported failure's other shape: every page answers, but slowly; the scan must leave time for the reads.
    monkeypatch.setattr("cursor_cloud_mcp.server.TOOL_BUDGET_SECONDS", 3.0)
    monkeypatch.setattr("cursor_cloud_mcp.server.SUPERVISE_READ_RESERVE_SECONDS", 1.5)
    monkeypatch.setattr("cursor_cloud_mcp.supervision.AGENT_SCAN_MIN_SECONDS", 0.5)
    ids = _ids("acacacac", 30)
    pages = {str(start): [_agent(agent_id, name=f"fleet-w{start + index}", status="ACTIVE", latestRunId=f"run-{start + index}")
                          for index, agent_id in enumerate(ids[start : start + 10])] for start in (0, 10, 20)}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/projects":
            return httpx.Response(200, json={"items": []})
        if request.url.path == "/v1/agents":
            start = request.url.params.get("cursor", "0")
            await asyncio.sleep(0.6)
            payload: dict[str, object] = {"items": pages[start]}
            if start != "20":
                payload["nextCursor"] = str(int(start) + 10)
            return httpx.Response(200, json=payload)
        return _run_for(request)

    server = build_server(_settings(), transport=httpx.MockTransport(handler))
    async with Client(server) as client:
        view = _data(await client.call_tool("cursor_supervise", {"name": "fleet"}))
    assert view["scan_stopped"] == "budget" and "scan_error" not in view
    assert 0 < len(view["items"]) < 30
    assert all(row["status"] == "RUNNING" for row in view["items"])
    assert view["has_more"] is True and view["next_cursor"] in {"mcps~x~0~10", "mcps~x~0~20"}


async def test_archived_rows_are_dropped_even_if_the_api_ignores_the_filter() -> None:
    agents = [_agent(agent_id, name=f"fleet-w{index}", status=status, latestRunId=f"run-{index}")
              for index, (agent_id, status) in enumerate(zip(_ids("bdbdbdbd", 2), ["IDLE", "ARCHIVED"], strict=True))]

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"items": agents})  # includeArchived=false not honoured
        return _run_for(request, status="FINISHED")

    client, _ = await _session(Router(responder))
    try:
        hidden = _data(await client.call_tool("cursor_supervise", {"status": "all"}))
        shown = _data(await client.call_tool("cursor_supervise", {"status": "all", "include_archived": True}))
    finally:
        await client.__aexit__(None, None, None)
    assert [row["agent_status"] for row in hidden["items"]] == ["IDLE"]
    assert [row["agent_status"] for row in shown["items"]] == ["IDLE", "ARCHIVED"]


def _two_pages(second: httpx.Response) -> Router:
    first = [_agent(agent_id, name=f"fleet-w{index}", status="ACTIVE", latestRunId=f"run-{index}")
             for index, agent_id in enumerate(_ids("a1a1a1a1", 3))]

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            if "cursor" in request.url.params:
                return second
            return httpx.Response(200, json={"items": first, "nextCursor": "p2"})
        return _run_for(request)

    return Router(responder)


@pytest.mark.parametrize(
    ("second", "code"),
    [
        (httpx.Response(429, json={"error": {"code": "rate_limit_exceeded", "message": "slow down"}}), "QUOTA"),
        (httpx.Response(200, json={"items": [], "nextCursor": ""}), "INCOMPATIBLE_RESPONSE"),
    ],
)
async def test_any_failure_after_the_first_page_keeps_the_rows(second: httpx.Response, code: str) -> None:
    client, _ = await _session(_two_pages(second))
    try:
        view = _data(await client.call_tool("cursor_supervise", {"name": "fleet"}))
    finally:
        await client.__aexit__(None, None, None)
    assert len(view["items"]) == 3 and all(row["status"] == "RUNNING" for row in view["items"])
    assert view["scan_stopped"] == "read_error" and view["scan_error"].startswith(code)
    assert view["next_cursor"] == "mcps~x~0~p2"  # the failed page is read again on resume


async def test_a_cursor_is_only_resumed_under_its_own_filters() -> None:
    client, router = await _session(_paged_router())
    try:
        first = _data(await client.call_tool("cursor_supervise", {"status": "all", "limit": 2}))
        cursor = first["next_cursor"]
        resumed = _data(await client.call_tool("cursor_supervise", {"status": "all", "limit": 2, "cursor": cursor}))
        calls = len(router.calls)
        other = await client.call_tool(
            "cursor_supervise", {"status": "all", "limit": 2, "cursor": cursor, "include_archived": True}
        )
        legacy = await client.call_tool("cursor_supervise", {"status": "all", "cursor": "mcp~2~"})
        plain = await client.call_tool("cursor_supervise", {"status": "all", "cursor": "api-cursor"})
        assert len(router.calls) == calls  # refused before any read
    finally:
        await client.__aexit__(None, None, None)
    assert [row["name"] for row in resumed["items"]] == ["fleet-w2", "fleet-w3"]
    for refused in (other, legacy, plain):
        assert _error_payload(refused)["code"] == "VALIDATION"
    assert "other filters" in _error_payload(other)["message"]


def _paged_router() -> Router:
    agents = [_agent(agent_id, name=f"fleet-w{index}", status="IDLE", latestRunId=f"run-{index}")
              for index, agent_id in enumerate(_ids("a2a2a2a2", 5))]

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"items": agents})
        return _run_for(request, status="FINISHED")

    return Router(responder)


async def test_an_agent_read_does_not_queue_its_run_read_behind_the_others() -> None:
    ids = _ids("a3a3a3a3", 4)
    order: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/projects":
            return httpx.Response(200, json={"items": []})
        agent_id = path.split("/")[3]
        if path == f"/v1/agents/{agent_id}":
            order.append(f"agent-{ids.index(agent_id)}")
            await asyncio.sleep(0.02)
            return httpx.Response(200, json=_agent(agent_id, status="ACTIVE", latestRunId="run-x"))
        order.append(f"run-{ids.index(agent_id)}")
        return _run_for(request)

    server = build_server(_settings(), transport=httpx.MockTransport(handler))
    async with Client(server) as client:
        view = _data(await client.call_tool("cursor_supervise", {"agent_ids": ids, "concurrency": 1}))
    assert len(view["items"]) == 4
    assert order == [step for index in range(4) for step in (f"agent-{index}", f"run-{index}")]
