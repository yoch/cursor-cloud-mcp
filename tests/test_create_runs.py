"""cursor_create_runs: one templated follow-up per agent, a row each, never resent."""

import httpx
import pytest

from tests.test_tools import Router, _agent, _data, _error_payload, _run, _session

pytestmark = pytest.mark.anyio

_A, _B, _C = (f"bc-{c * 8}-0000-0000-0000-000000000001" for c in "abc")


def _fleet(post_status: dict[str, int] | None = None) -> Router:
    statuses = post_status or {}

    def responder(request: httpx.Request) -> httpx.Response:
        agent_id = request.url.path.split("/")[3]
        if request.method == "GET" and request.url.path == f"/v1/agents/{agent_id}":
            if statuses.get(agent_id) == 404:
                return httpx.Response(404, json={"error": {"code": "agent_not_found", "message": "gone"}})
            return httpx.Response(200, json=_agent(agent_id, status="IDLE", latestRunId=f"run-old-{agent_id[3]}"))
        if request.method == "POST" and request.url.path.endswith("/runs"):
            status = statuses.get(agent_id, 201)
            if status != 201:
                return httpx.Response(status, json={"error": {"code": "internal_error", "message": "boom"}})
            return httpx.Response(201, json={"run": _run(id=f"run-new-{agent_id[3]}", agentId=agent_id, status="CREATING", result=None)})
        raise AssertionError(f"{request.method} {request.url.path}")

    return Router(responder)


def _posts(router: Router) -> list[tuple[str, object]]:
    return [(path.split("/")[3], body) for method, path, body in router.calls if method == "POST"]


async def test_each_agent_gets_its_own_worker_in_the_prompt() -> None:
    client, router = await _session(_fleet())
    try:
        view = _data(
            await client.call_tool(
                "cursor_create_runs",
                {
                    "targets": [{"agent_id": _A, "worker": "w01"}, {"agent_id": _B, "worker": "w02"}],
                    "prompt": "Check job {worker}: push to fleet/{worker}.",
                },
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    assert [(row["agent_id"], row["worker"], row["outcome"], row["run_id"]) for row in view["items"]] == [
        (_A, "w01", "sent", "run-new-a"),
        (_B, "w02", "sent", "run-new-b"),
    ]
    assert view["summary"] == {"sent": 2, "not_sent": 0, "unknown": 0}
    prompts = {agent: body["prompt"]["text"] for agent, body in _posts(router)}  # type: ignore[index]
    assert prompts == {_A: "Check job w01: push to fleet/w01.", _B: "Check job w02: push to fleet/w02."}


async def test_one_failure_never_stops_the_others_and_nothing_is_resent() -> None:
    client, router = await _session(_fleet({_B: 404, _C: 500}))
    try:
        view = _data(
            await client.call_tool(
                "cursor_create_runs",
                {"targets": [{"agent_id": _A}, {"agent_id": _B}, {"agent_id": _C}], "prompt": "Push your chunks."},
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    rows = {row["agent_id"]: row for row in view["items"]}
    assert rows[_A]["outcome"] == "sent"
    assert rows[_B]["outcome"] == "not_sent" and rows[_B]["error_code"] == "NOT_FOUND"
    # A 5xx on the POST: the run may exist. Reported, never retried.
    assert rows[_C]["outcome"] == "unknown" and rows[_C]["error_code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert view["summary"] == {"sent": 1, "not_sent": 1, "unknown": 1}
    assert sorted(agent for agent, _body in _posts(router)) == [_A, _C]  # one POST each, B never reached it


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"targets": [{"agent_id": _A}, {"agent_id": _A.upper().replace("BC-", "bc-")}], "prompt": "x"}, "twice"),
        ({"targets": [{"agent_id": _A, "worker": "w01"}, {"agent_id": _B}], "prompt": "job {worker}"}, "worker value"),
        ({"targets": [{"agent_id": _A, "worker": "w01"}], "prompt": "no placeholder"}, "no {worker}"),
        ({"targets": [{"agent_id": "not-an-id"}], "prompt": "x"}, "bc-<uuid>"),
        ({"targets": [{"agent_id": _A}], "prompt": "x", "delivery": "steer", "replace_active": True}, "steer"),
    ],
)
async def test_a_batch_that_cannot_be_served_whole_sends_nothing(args: dict[str, object], needle: str) -> None:
    client, router = await _session(_fleet())
    try:
        refused = await client.call_tool("cursor_create_runs", args)
    finally:
        await client.__aexit__(None, None, None)
    payload = _error_payload(refused)
    assert payload["code"] == "VALIDATION" and needle in payload["message"]
    assert router.calls == []


async def test_writes_off_refuses_the_batch() -> None:
    client, router = await _session(_fleet(), allow_writes=False)
    try:
        refused = await client.call_tool("cursor_create_runs", {"targets": [{"agent_id": _A}], "prompt": "x"})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(refused)["code"] == "READ_ONLY"
    assert router.calls == []


async def test_the_batch_is_annotated_like_a_follow_up() -> None:
    client, _router = await _session(_fleet())
    try:
        listed = {tool.name: tool for tool in (await client.list_tools()).tools}
    finally:
        await client.__aexit__(None, None, None)
    annotations = listed["cursor_create_runs"].annotations
    assert annotations is not None and annotations.destructive_hint is True and annotations.read_only_hint is False
