"""November 16, 2026 re-read: the read-only Projects tool."""

from collections.abc import Callable

import httpx
import pytest
from mcp import Client

from cursor_cloud_mcp import budget
from cursor_cloud_mcp.errors import ErrorCode, failure, failure_note
from cursor_cloud_mcp.fixture import PROJECT_ID as FIXTURE_PROJECT_ID
from cursor_cloud_mcp.fixture import SEEDED_AGENT_ID
from cursor_cloud_mcp.server import build_server
from tests.test_tools import _AGENT, Router, _agent, _data, _error_payload, _run, _session, _settings

pytestmark = pytest.mark.anyio


def _Router(responder: Callable[[httpx.Request], httpx.Response]) -> Router:
    """These tests are about Projects: their responders answer ``/v1/projects`` themselves."""
    return Router(responder, stub_projects=False)


_PROJECT = "bc-33333333-3333-3333-3333-333333333333"
_COORDINATOR = "bc-44444444-4444-4444-4444-444444444444"


def _project(project_id: str = _PROJECT, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": project_id,
        "name": "Payments migration",
        "status": "ACTIVE",
        "coordinatorAgentId": project_id,
        "appearance": {"icon": "rocket", "color": "blue"},
        "createdAt": "2026-11-16T10:00:00.000Z",
        "updatedAt": "2026-11-16T11:00:00.000Z",
        "url": f"https://cursor.com/agents/{project_id}",
    }
    payload.update(overrides)
    return payload


async def test_a_list_sends_only_what_was_asked_and_never_writes() -> None:
    queries: list[list[tuple[str, str]]] = []

    def responder(request: httpx.Request) -> httpx.Response:
        queries.append(list(request.url.params.multi_items()))
        return httpx.Response(200, json={"items": [_project()]})

    client, router = await _session(_Router(responder))
    try:
        plain = _data(await client.call_tool("cursor_list_projects", {}))
        _data(await client.call_tool("cursor_list_projects", {"include_archived": False}))
        _data(await client.call_tool("cursor_list_projects", {"limit": 5, "cursor": "p2", "include_archived": True}))
    finally:
        await client.__aexit__(None, None, None)
    assert queries == [
        [],
        [("includeArchived", "false")],
        [("limit", "5"), ("cursor", "p2"), ("includeArchived", "true")],
    ]
    assert [call[:2] for call in router.calls] == [("GET", "/v1/projects")] * 3
    [view] = plain["items"]  # type: ignore[misc]
    assert view == {
        "project_id": _PROJECT,
        "name": "Payments migration",
        "status": "ACTIVE",
        "coordinator_agent_id": _PROJECT,
        "icon": "rocket",
        "color": "blue",
        "url": f"https://cursor.com/agents/{_PROJECT}",
        "created_at": "2026-11-16T10:00:00.000Z",
        "updated_at": "2026-11-16T11:00:00.000Z",
    }
    assert plain["has_more"] is False
    assert "next_cursor" not in plain and "agents" not in plain


async def test_an_empty_page_with_a_cursor_is_not_the_end() -> None:
    # The spec: a page can hold fewer than `limit` Projects, or none, while `nextCursor` is still set.
    router = _Router(lambda _request: httpx.Response(200, json={"items": [], "nextCursor": "more"}))
    client, _ = await _session(router)
    try:
        page = _data(await client.call_tool("cursor_list_projects", {}))
    finally:
        await client.__aexit__(None, None, None)
    assert page == {"items": [], "next_cursor": "more", "has_more": True}


async def test_a_project_without_name_or_appearance_is_still_listed() -> None:
    bare = {"id": _PROJECT, "status": "ARCHIVED", "createdAt": "2026-11-16T10:00:00.000Z"}
    router = _Router(lambda _request: httpx.Response(200, json={"items": [bare, {"id": _COORDINATOR}]}))
    client, _ = await _session(router)
    try:
        page = _data(await client.call_tool("cursor_list_projects", {}))
    finally:
        await client.__aexit__(None, None, None)
    assert page["items"] == [
        {"project_id": _PROJECT, "status": "ARCHIVED", "created_at": "2026-11-16T10:00:00.000Z"},
        {"project_id": _COORDINATOR},
    ]


async def test_one_project_comes_with_the_agents_its_coordinator_started() -> None:
    queries: dict[str, list[tuple[str, str]]] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        queries[request.url.path] = list(request.url.params.multi_items())
        if request.url.path == f"/v1/projects/{_PROJECT}":
            return httpx.Response(200, json=_project())
        if request.url.path == f"/v1/projects/{_PROJECT}/agents":
            return httpx.Response(200, json={"items": [_agent(_AGENT, status="ACTIVE")], "nextCursor": "a2"})
        raise AssertionError(request.url.path)

    client, router = await _session(_Router(responder))
    try:
        view = _data(await client.call_tool("cursor_list_projects", {"project_id": _PROJECT, "limit": 7, "cursor": "a1"}))
    finally:
        await client.__aexit__(None, None, None)
    assert [call[:2] for call in router.calls] == [
        ("GET", f"/v1/projects/{_PROJECT}"),
        ("GET", f"/v1/projects/{_PROJECT}/agents"),
    ]
    assert queries[f"/v1/projects/{_PROJECT}"] == []
    assert queries[f"/v1/projects/{_PROJECT}/agents"] == [("limit", "7"), ("cursor", "a1")]
    assert [item["project_id"] for item in view["items"]] == [_PROJECT]  # type: ignore[index, union-attr]
    [agent] = view["agents"]  # type: ignore[misc]
    assert agent["agent_id"] == _AGENT and agent["status"] == "ACTIVE"
    # Cursor and has_more belong to the agents, not to the single Project.
    assert view["next_cursor"] == "a2" and view["has_more"] is True
    assert "agents_error" not in view


async def test_include_archived_is_refused_with_a_project_id_before_any_request() -> None:
    router = _Router(lambda _request: (_ for _ in ()).throw(AssertionError("network")))
    client, _ = await _session(router)
    try:
        refused = await client.call_tool("cursor_list_projects", {"project_id": _PROJECT, "include_archived": True})
        slash = await client.call_tool("cursor_list_projects", {"project_id": "a/b"})
        loose = await client.call_tool("cursor_list_projects", {"project_id": "foo"})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(refused)["code"] == "VALIDATION"
    assert _error_payload(slash)["code"] == "VALIDATION"
    # The spec gives the path parameter the bc-<uuid> form: a loose single segment is refused locally too.
    assert _error_payload(loose)["code"] == "VALIDATION"
    assert "project_id" in str(_error_payload(loose)["message"])
    assert router.calls == []


async def test_a_project_read_by_the_id_of_the_callers_own_coordinator_is_accepted() -> None:
    # The spec: the id of your own coordinator in a Project you joined also works as `id`.
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/projects/{_COORDINATOR}":
            return httpx.Response(200, json=_project(_PROJECT, coordinatorAgentId=_COORDINATOR))
        if request.url.path == f"/v1/projects/{_PROJECT}/agents":
            return httpx.Response(200, json={"items": []})
        raise AssertionError(request.url.path)

    client, router = await _session(_Router(responder))
    try:
        view = _data(await client.call_tool("cursor_list_projects", {"project_id": _COORDINATOR}))
    finally:
        await client.__aexit__(None, None, None)
    [project] = view["items"]  # type: ignore[misc]
    assert project["project_id"] == _PROJECT and project["coordinator_agent_id"] == _COORDINATOR
    assert view["agents"] == []
    # The agents endpoint documents the Project's id, so that is what it gets.
    assert router.calls[-1][1] == f"/v1/projects/{_PROJECT}/agents"


async def test_a_response_for_another_project_is_refused() -> None:
    router = _Router(lambda _request: httpx.Response(200, json=_project(_COORDINATOR, coordinatorAgentId=_COORDINATOR)))
    client, _ = await _session(router)
    try:
        refused = await client.call_tool("cursor_list_projects", {"project_id": _PROJECT})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(refused)["code"] == "INCOMPATIBLE_RESPONSE"


async def test_an_unreadable_agent_list_does_not_hide_the_project() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents"):
            return httpx.Response(403, json={"error": {"code": "feature_unavailable", "message": "Not for this account."}})
        return httpx.Response(200, json=_project())

    client, _ = await _session(_Router(responder))
    try:
        view = _data(await client.call_tool("cursor_list_projects", {"project_id": _PROJECT}))
    finally:
        await client.__aexit__(None, None, None)
    assert [item["project_id"] for item in view["items"]] == [_PROJECT]  # type: ignore[index, union-attr]
    # What a caller needs to react survives in the note: Cursor's status and own code.
    assert str(view["agents_error"]).startswith("PERMISSION: ")
    assert str(view["agents_error"]).endswith("[http 403, remote_code feature_unavailable]")
    assert "agents" not in view and view["has_more"] is False


@pytest.mark.parametrize(
    ("status", "remote", "expected"),
    [
        (403, "projects_disabled", "PERMISSION"),
        (403, "user_required", "PERMISSION"),
        (404, "project_not_found", "NOT_FOUND"),
        (409, "project_archived", "CONFLICT"),
    ],
)
async def test_the_projects_error_codes_keep_their_remote_code(status: int, remote: str, expected: str) -> None:
    router = _Router(lambda _request: httpx.Response(status, json={"error": {"code": remote, "message": "No."}}))
    client, _ = await _session(router)
    try:
        result = await client.call_tool("cursor_list_projects", {"project_id": _PROJECT})
    finally:
        await client.__aexit__(None, None, None)
    payload = _error_payload(result)
    assert payload["code"] == expected
    assert payload["remote_code"] == remote
    assert payload["http_status"] == status


async def test_fixture_mode_serves_the_projects_without_a_key() -> None:
    server = build_server(_settings(api_key=None, fixture=True))
    async with Client(server) as client:
        listed = _data(await client.call_tool("cursor_list_projects", {}))
        one = _data(await client.call_tool("cursor_list_projects", {"project_id": FIXTURE_PROJECT_ID}))
        missing = await client.call_tool("cursor_list_projects", {"project_id": "bc-00000000-0000-0000-0000-0000000000ff"})
    assert [item["project_id"] for item in listed["items"]] == [FIXTURE_PROJECT_ID]  # type: ignore[index, union-attr]
    assert one["agents"]  # type: ignore[truthy-bool]
    assert _error_payload(missing)["code"] == "NOT_FOUND"


async def test_an_explicit_agents_page_that_fails_is_an_error_not_a_note() -> None:
    # A cursor taken from the Projects list is not valid for the agents list: Cursor refuses it, and so do we.
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents"):
            return httpx.Response(400, json={"error": {"code": "validation_error", "message": "Bad cursor."}})
        return httpx.Response(200, json=_project())

    client, _ = await _session(_Router(responder))
    try:
        refused = await client.call_tool("cursor_list_projects", {"project_id": _PROJECT, "cursor": "from-projects"})
    finally:
        await client.__aexit__(None, None, None)
    payload = _error_payload(refused)
    assert payload["code"] == "VALIDATION" and payload["remote_code"] == "validation_error"


async def test_the_coordinator_branch_still_needs_a_well_formed_project_id() -> None:
    # The row matches on coordinatorAgentId alone, so its own id must be sane before it builds the next path.
    router = _Router(lambda _request: httpx.Response(200, json=_project("not/an-id", coordinatorAgentId=_COORDINATOR)))
    client, router = await _session(router)
    try:
        refused = await client.call_tool("cursor_list_projects", {"project_id": _COORDINATOR})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(refused)["code"] == "INCOMPATIBLE_RESPONSE"
    assert [call[:2] for call in router.calls] == [("GET", f"/v1/projects/{_COORDINATOR}")]


def test_a_note_for_a_swallowed_failure_keeps_status_code_and_wait() -> None:
    bare = failure(ErrorCode.UPSTREAM, "Down.")
    full = failure(ErrorCode.QUOTA, "Slow down.", http_status=429, remote_code="rate_limit_exceeded", retry_after_seconds=2.5)
    assert failure_note(bare) == "UPSTREAM: Down."
    assert failure_note(full) == "QUOTA: Slow down. [http 429, remote_code rate_limit_exceeded, retry after 2.5s]"


async def test_fixture_project_survives_the_deletion_of_the_seeded_agent() -> None:
    server = build_server(_settings(api_key=None, fixture=True, allow_delete=True))
    async with Client(server) as client:
        before = _data(await client.call_tool("cursor_list_projects", {"project_id": FIXTURE_PROJECT_ID}))
        deleted = await client.call_tool("cursor_delete_agent", {"agent_id": SEEDED_AGENT_ID, "confirm_agent_id": SEEDED_AGENT_ID})
        after = _data(await client.call_tool("cursor_list_projects", {"project_id": FIXTURE_PROJECT_ID}))
        coordinator = _data(await client.call_tool("cursor_get_agent", {"agent_id": FIXTURE_PROJECT_ID}))
        run = _data(await client.call_tool("cursor_get_run", {"agent_id": FIXTURE_PROJECT_ID, "run_id": "run-project-0001"}))
    assert [agent["agent_id"] for agent in before["agents"]] == [SEEDED_AGENT_ID]  # type: ignore[index, union-attr]
    assert deleted.is_error is False
    assert after["agents"] == [] and "agents_error" not in after
    # A Project's id reads as an agent, as it does on the real API.
    assert coordinator["agent_id"] == FIXTURE_PROJECT_ID and run["status"] == "FINISHED"


_OTHER = "bc-55555555-5555-5555-5555-555555555555"


def _fleet_responder(projects: httpx.Response | list[httpx.Response], seen: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    """Two agents (the coordinator and an ordinary one), a run each, and the answer(s) for GET /v1/projects."""
    answers = projects if isinstance(projects, list) else [projects]

    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path == "/v1/projects":
            return answers[min(seen.count("/v1/projects") - 1, len(answers) - 1)]
        if path == "/v1/agents":
            return httpx.Response(
                200, json={"items": [_agent(_COORDINATOR, status="ACTIVE"), _agent(_OTHER, status="ACTIVE")]}
            )
        if path in (f"/v1/agents/{_COORDINATOR}", f"/v1/agents/{_OTHER}"):
            return httpx.Response(200, json=_agent(path.rsplit("/", 1)[-1], status="ACTIVE"))
        if "/runs/" in path:
            return httpx.Response(200, json=_run(status="RUNNING", result=None))
        raise AssertionError(path)

    return responder


def _projects_answer(**project: object) -> httpx.Response:
    return httpx.Response(200, json={"items": [_project(**project)]})


async def test_the_coordinator_is_marked_in_the_agent_list_and_the_projects_are_read_once() -> None:
    seen: list[str] = []
    router = _Router(_fleet_responder(_projects_answer(id=_COORDINATOR, coordinatorAgentId=_COORDINATOR), seen))
    client, _ = await _session(router)
    try:
        first = _data(await client.call_tool("cursor_list_agents", {}))
        second = _data(await client.call_tool("cursor_list_agents", {}))
    finally:
        await client.__aexit__(None, None, None)
    rows = {row["agent_id"]: row for row in first["items"]}  # type: ignore[union-attr]
    assert rows[_COORDINATOR]["coordinator_of_project"] == _COORDINATOR
    assert "coordinator_of_project" not in rows[_OTHER]
    assert second["items"] == first["items"]
    # A fleet poll must not double its traffic: the Projects list is cached for a minute.
    assert seen.count("/v1/projects") == 1


async def test_a_joined_project_marks_the_callers_coordinator_with_the_owners_project_id() -> None:
    seen: list[str] = []
    answer = _projects_answer(id=_PROJECT, coordinatorAgentId=_COORDINATOR)
    client, _ = await _session(_Router(_fleet_responder(answer, seen)))
    try:
        listed = _data(await client.call_tool("cursor_list_agents", {}))
    finally:
        await client.__aexit__(None, None, None)
    rows = {row["agent_id"]: row for row in listed["items"]}  # type: ignore[union-attr]
    assert rows[_COORDINATOR]["coordinator_of_project"] == _PROJECT
    # The owner's coordinator is not one of the caller's agents: nothing is marked by the Project's own id.
    assert _PROJECT not in rows


async def test_one_agent_and_a_supervised_fleet_carry_the_marker_too() -> None:
    seen: list[str] = []
    answer = _projects_answer(id=_COORDINATOR, coordinatorAgentId=_COORDINATOR)
    client, _ = await _session(_Router(_fleet_responder(answer, seen)))
    try:
        one = _data(await client.call_tool("cursor_get_agent", {"agent_id": _COORDINATOR}))
        plain = _data(await client.call_tool("cursor_get_agent", {"agent_id": _OTHER}))
        fleet = _data(await client.call_tool("cursor_supervise", {}))
    finally:
        await client.__aexit__(None, None, None)
    assert one["coordinator_of_project"] == _COORDINATOR and "coordinator_of_project" not in plain
    marked = {row["agent_id"]: row.get("coordinator_of_project") for row in fleet["items"]}  # type: ignore[union-attr]
    assert marked == {_COORDINATOR: _COORDINATOR, _OTHER: None}


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(403, json={"error": {"code": "projects_disabled", "message": "Off for this team."}}),
        httpx.Response(200, json={"items": []}),
        httpx.Response(200, json={"unexpected": "shape"}),
    ],
)
async def test_without_a_readable_projects_list_rows_stay_unmarked_and_the_miss_is_cached(
    answer: httpx.Response,
) -> None:
    seen: list[str] = []
    client, _ = await _session(_Router(_fleet_responder(answer, seen)))
    try:
        first = _data(await client.call_tool("cursor_list_agents", {}))
        _data(await client.call_tool("cursor_get_agent", {"agent_id": _COORDINATOR}))
    finally:
        await client.__aexit__(None, None, None)
    assert all("coordinator_of_project" not in row for row in first["items"])  # type: ignore[union-attr]
    assert seen.count("/v1/projects") == 1


async def test_a_coordinator_on_a_later_page_of_projects_is_found() -> None:
    seen: list[str] = []
    answers = [
        httpx.Response(200, json={"items": [_project(_OTHER, coordinatorAgentId=None)], "nextCursor": "p2"}),
        _projects_answer(id=_COORDINATOR, coordinatorAgentId=_COORDINATOR),
    ]
    client, _ = await _session(_Router(_fleet_responder(answers, seen)))
    try:
        listed = _data(await client.call_tool("cursor_list_agents", {}))
    finally:
        await client.__aexit__(None, None, None)
    rows = {row["agent_id"]: row for row in listed["items"]}  # type: ignore[union-attr]
    assert rows[_COORDINATOR]["coordinator_of_project"] == _COORDINATOR
    assert seen.count("/v1/projects") == 2


async def test_fixture_mode_marks_its_coordinator() -> None:
    server = build_server(_settings(api_key=None, fixture=True))
    async with Client(server) as client:
        one = _data(await client.call_tool("cursor_get_agent", {"agent_id": FIXTURE_PROJECT_ID}))
        seeded = _data(await client.call_tool("cursor_get_agent", {"agent_id": SEEDED_AGENT_ID}))
    assert one["coordinator_of_project"] == FIXTURE_PROJECT_ID
    assert "coordinator_of_project" not in seeded


async def _coordinators(responses: list[Callable[[], httpx.Response]], *, ttl: float, calls: int) -> list[dict[str, str]]:
    from cursor_cloud_mcp.client import CursorCloudClient

    sent: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        return responses[min(len(sent), len(responses)) - 1]()

    client = CursorCloudClient(
        api_key="test-secret-key", transport=httpx.MockTransport(handler), project_cache_ttl_seconds=ttl
    )
    await client.open()
    try:
        with budget.tool_budget(30):
            return [await client.project_coordinators() for _ in range(calls)] + [{"requests": str(len(sent))}]
    finally:
        await client.aclose()


def _page() -> httpx.Response:
    return httpx.Response(200, json={"items": [_project()]})


async def test_a_transient_failure_keeps_the_last_complete_marker_map() -> None:
    def failing() -> httpx.Response:
        return httpx.Response(502, json={"error": {"code": "upstream_error", "message": "bad gateway"}})

    first, second, sent = await _coordinators([_page, failing], ttl=0, calls=2)
    assert first == {_PROJECT: _PROJECT}
    assert second == first  # not replaced by the empty map of the failed read
    assert sent == {"requests": "3"}  # the 502 GET is retried once by the client, then given up


async def test_a_lasting_refusal_is_cached_as_no_projects() -> None:
    def refused() -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "projects_disabled", "message": "off"}})

    first, second, sent = await _coordinators([refused], ttl=60, calls=2)
    assert first == second == {}
    assert sent == {"requests": "1"}


async def test_a_stalled_projects_read_gives_up_quickly(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import time

    import cursor_cloud_mcp.client as client_module
    from cursor_cloud_mcp.client import CursorCloudClient

    monkeypatch.setattr(client_module, "PROJECT_COORDINATOR_MAX_SECONDS", 0.3)

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return _page()

    client = CursorCloudClient(api_key="test-secret-key", transport=httpx.MockTransport(handler))
    await client.open()
    started = time.monotonic()
    try:
        with budget.tool_budget(30):
            assert await client.project_coordinators() == {}
    finally:
        await client.aclose()
    assert time.monotonic() - started < 5
