"""Fixes from the second 0.7.1 review: error masking, ASCII validators, coordinator map, 2xx on mutations, stale."""

import secrets

import httpx
import pytest

from cursor_cloud_mcp import budget, redaction
from cursor_cloud_mcp.client import CursorCloudClient
from cursor_cloud_mcp.server import _masked_error
from tests.test_shell_output import _delta, _read
from tests.test_supervision import _T0, _sse
from tests.test_tools import _AGENT, _RUN, Router, _agent, _error_payload, _run, _session

pytestmark = pytest.mark.anyio

_PROJECT = "bc-33333333-3333-3333-3333-333333333333"


def test_masking_keeps_codes_and_ids_but_masks_free_text() -> None:
    value = f"run-{secrets.token_hex(8)}"
    redaction.register(value)
    body = {"code": value, "agent_id": value, "run_id": value, "request_id": value, "message": f"saw {value}"}
    masked = _masked_error(body)
    assert {key: masked[key] for key in ("code", "agent_id", "run_id", "request_id")} == dict.fromkeys(
        ("code", "agent_id", "run_id", "request_id"), value
    )
    assert value not in str(masked["message"])


@pytest.mark.parametrize("event_id", ["é", "1795623681075-0\x07"])
async def test_a_non_ascii_event_id_is_a_json_validation_error(event_id: str) -> None:
    client, _ = await _session(Router(lambda _request: httpx.Response(500)))
    try:
        result = await client.call_tool(
            "cursor_read_run_events", {"agent_id": _AGENT, "run_id": _RUN, "after_event_id": event_id}
        )
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(result)["code"] == "VALIDATION"


@pytest.mark.parametrize("cursor", ["mcp~²~x", "mcp~" + "9" * 5000 + "~x"])
async def test_a_malformed_scan_cursor_is_a_json_validation_error(cursor: str) -> None:
    client, _ = await _session(Router(lambda _request: httpx.Response(200, json={"items": []})))
    try:
        result = await client.call_tool("cursor_list_agents", {"name": "w", "cursor": cursor})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(result)["code"] == "VALIDATION"


async def test_an_unreadable_projects_answer_never_replaces_a_map_already_read() -> None:
    answers = [
        httpx.Response(200, json={"items": [{"id": _PROJECT, "name": "p", "status": "ACTIVE", "coordinatorAgentId": _PROJECT}]}),
        httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>proxy</html>"),
    ]
    sent: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        sent.append(1)
        return answers[min(len(sent), len(answers)) - 1]

    client = CursorCloudClient(api_key="test-secret-key", transport=httpx.MockTransport(handler), project_cache_ttl_seconds=0)
    await client.open()
    try:
        with budget.tool_budget(30):
            first = await client.project_coordinators()
            second = await client.project_coordinators()
    finally:
        await client.aclose()
    assert first == second == {_PROJECT: _PROJECT}


async def test_interleaved_commands_keep_one_tool_call_each() -> None:
    def call(call_id: str, status: str, at: int) -> bytes:
        return _sse("tool_call", {"callId": call_id, "name": "run_terminal_cmd", "status": status, "args": {"c": call_id}}, at)

    chunks = [
        _sse("status", {"status": "RUNNING"}, _T0),
        call("A", "running", _T0 + 1),
        call("B", "running", _T0 + 2),
        _delta("stdout", "a1\n", _T0 + 3, call="A"),
        _delta("stdout", "b1\n", _T0 + 4, call="B"),
        call("A", "completed", _T0 + 5),
        _delta("stdout", "b2\n", _T0 + 6, call="B"),
        call("B", "completed", _T0 + 7),
        _delta("stdout", "", _T0 + 8, call="B"),  # an empty chunk is not an event
        _sse("done", {}, _T0 + 9),
    ]
    for arguments in ({}, {"tail": 20, "max_wait_seconds": 10}):
        view, _ = await _read(chunks, include_shell_output=True, **arguments)
        calls = [(event["call_id"], event["tool_status"]) for event in view["events"] if event["kind"] == "tool_call"]
        assert calls == [("A", "completed"), ("B", "completed")]
        assert [event["stdout"] for event in view["events"] if event["kind"] == "shell_output"] == ["a1\n", "b1\n", "b2\n"]


async def test_a_replaced_running_call_does_not_count_twice_against_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from cursor_cloud_mcp import stream

    monkeypatch.setattr(stream, "TOOL_TEXT_TOTAL_MAX_CHARS", 2500)
    chunks = [_sse("status", {"status": "RUNNING"}, _T0)]
    for index in range(4):  # four calls of about 500 rendered characters each: 2000, under the cap
        args = {"pad": "x" * 2000, "n": index}
        for offset, status in ((0, "running"), (1, "completed")):
            payload = {"callId": f"c{index}", "name": "read_file", "status": status, "args": args}
            chunks.append(_sse("tool_call", payload, _T0 + 10 * index + offset + 1))
    chunks.append(_sse("done", {}, _T0 + 100))
    view, _ = await _read(chunks)
    assert view["truncated"] is False and view["finished"] is True


async def test_an_unexpected_2xx_on_a_follow_up_is_an_unknown_outcome() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_agent())
        return httpx.Response(202, json={"run": _run(status="CREATING", result=None)})

    client, _ = await _session(Router(responder))
    try:
        result = await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "follow-up"})
    finally:
        await client.__aexit__(None, None, None)
    payload = _error_payload(result)
    assert payload["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert payload["http_status"] == 202
