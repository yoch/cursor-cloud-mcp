"""Runner supervision: pure-JSON errors, stream tail, activity summary, overview, replacing a run."""

import asyncio
import json
import time
from collections.abc import AsyncIterator

import httpx
import pytest
from mcp import Client

from cursor_cloud_mcp.activity import event_time
from cursor_cloud_mcp.server import build_server
from tests.test_tools import (
    _AGENT,
    _RUN,
    Router,
    _agent,
    _data,
    _error_payload,
    _run,
    _session,
    _settings,
)

pytestmark = pytest.mark.anyio

# Shapes recorded from a real stream on 2026-10-06 (values anonymized): event ids are
# millisecond timestamps, background shells are launched by run_terminal_cmd and observed by await.
_T0 = 1791250392023


def _sse(event: str, data: object, at_ms: int) -> bytes:
    return f"id: {at_ms}-0\nevent: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _background_run(*, end: str | None) -> list[bytes]:
    launch_args = {"command": "bash ~/monitor.sh", "isBackground": True}
    launch_result = {
        "success": {"command": "bash ~/monitor.sh", "shellId": 931367, "pid": 165414},
        "isBackground": True,
    }
    chunks = [
        _sse("status", {"status": "RUNNING"}, _T0),
        _sse("interaction_update", {"type": "token-delta"}, _T0 + 1),
        _sse("assistant", {"text": "Launching "}, _T0 + 2),
        _sse("assistant", {"text": "the job."}, _T0 + 3),
        _sse("tool_call", {"callId": "c1", "name": "run_terminal_cmd", "status": "running", "args": launch_args}, _T0 + 4),
        _sse(
            "tool_call",
            {"callId": "c1", "name": "run_terminal_cmd", "status": "completed", "args": launch_args, "result": launch_result},
            _T0 + 5,
        ),
        _sse(
            "tool_call",
            {
                "callId": "c2",
                "name": "await",
                "status": "completed",
                "args": {"taskId": "931367", "blockUntilMs": 900000},
                "result": {"success": {"stillRunning": {"taskId": "931367", "runtimeMs": "900081"}}},
            },
            _T0 + 900_000,
        ),
        _sse("assistant", {"text": "Job still running, ending my turn."}, _T0 + 900_500),
    ]
    if end is not None:
        chunks.append(_sse("status", {"status": end}, _T0 + 901_000))
        chunks.append(_sse("result", {"status": end}, _T0 + 901_001))
        chunks.append(_sse("done", {}, _T0 + 901_002))
    return chunks


class _OpenStream(httpx.AsyncByteStream):
    """Sends its chunks, then stays open like the stream of a run still going."""

    def __init__(self, chunks: list[bytes], *, hang: bool) -> None:
        self._chunks = chunks
        self._hang = hang

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._hang:
            await asyncio.sleep(3600)

    async def aclose(self) -> None:
        return None


def _stream_response(chunks: list[bytes], *, hang: bool = False) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_OpenStream(chunks, hang=hang))


def _router(run: dict[str, object], chunks: list[bytes], *, hang: bool = False) -> Router:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stream"):
            assert "last-event-id" not in request.headers  # a tail always replays from the start
            return _stream_response(chunks, hang=hang)
        return httpx.Response(200, json=run)

    return Router(responder)


async def test_error_text_is_pure_json() -> None:
    not_found = {"error": {"code": "agent_not_found", "message": "Agent not found"}}
    client, _ = await _session(Router(lambda _request: httpx.Response(404, json=not_found)))
    try:
        result = await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN})
    finally:
        await client.__aexit__(None, None, None)
    assert result.is_error is True
    payload = json.loads(result.content[0].text)  # no "Error executing tool" prefix
    assert payload["code"] == "NOT_FOUND"


def test_event_time_reads_millisecond_ids_and_rejects_anything_else() -> None:
    moment = event_time(f"{_T0}-0")
    assert moment is not None and moment.isoformat().startswith("2026-10-06T01:33:12.023")
    assert event_time("1") is None
    assert event_time("abc-0") is None
    assert event_time(f"{_T0}") is None
    assert event_time("99999999999999-0") is None  # beyond 2100: not a timestamp
    assert event_time(None) is None


async def test_finished_run_reports_a_background_task_last_seen_running() -> None:
    finished = _run(status="FINISHED", result="Job launched.")
    client, router = await _session(_router(finished, _background_run(end="FINISHED")))
    try:
        plain = _data(await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN}))
        view = _data(await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN, "activity": True}))
    finally:
        await client.__aexit__(None, None, None)
    assert "activity" not in plain  # a finished run with a result reads no stream unless asked
    assert sum(1 for call in router.calls if call[1].endswith("/stream")) == 1
    activity = view["activity"]
    assert activity["unfinished_background_tasks"] == 1
    assert activity["background_tasks"] == [
        {
            "task_id": "931367",
            "command": "bash ~/monitor.sh",
            "last_state": "running",
            "runtime_ms": 900081,
            "observed_at": "2026-10-06T01:48:12.023Z",
        }
    ]
    assert activity["last_assistant_text"] == "Job still running, ending my turn."
    assert activity["last_tool_call"]["name"] == "await"
    # The walk stops at the result event, as cursor mode does.
    assert activity["last_event_at"] == "2026-10-06T01:48:13.024Z"
    assert activity["run_terminal"] is True and activity["complete"] is True
    assert activity["idle_seconds"] > 0


async def test_error_run_without_result_gets_its_context_automatically() -> None:
    failed = _run(status="ERROR", result=None)
    client, _ = await _session(_router(failed, _background_run(end="ERROR")))
    try:
        view = _data(await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN}))
    finally:
        await client.__aexit__(None, None, None)
    assert view["status"] == "ERROR" and view["result_present"] is False
    assert view["activity"]["last_tool_call"]["name"] == "await"
    assert view["activity"]["unfinished_background_tasks"] == 1


async def test_activity_error_does_not_fail_the_read() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stream"):
            return httpx.Response(410, json={"error": {"code": "stream_expired", "message": "gone"}})
        return httpx.Response(200, json=_run(status="ERROR", result=None))

    client, _ = await _session(Router(responder))
    try:
        view = _data(await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN}))
    finally:
        await client.__aexit__(None, None, None)
    assert view["status"] == "ERROR"
    assert "activity" not in view
    assert view["activity_error"].startswith("STREAM_EXPIRED")


async def test_tail_returns_the_last_events_of_a_long_replay() -> None:
    chunks = [_sse("status", {"status": "RUNNING"}, _T0)]
    padding = "x" * 400
    for index in range(5000):  # about 2.5 MB, well above the 1 MB cap of cursor mode
        chunks.append(
            _sse(
                "tool_call",
                {"callId": f"c{index}", "name": "read_file", "status": "completed", "args": {"path": f"f{index}", "pad": padding}},
                _T0 + 10 + index,
            )
        )
    chunks += [_sse("result", {"status": "FINISHED", "result": "done"}, _T0 + 6000), _sse("done", {}, _T0 + 6001)]
    assert sum(len(chunk) for chunk in chunks) > 2_000_000
    client, _ = await _session(_router(_run(), chunks))
    try:
        view = _data(
            await client.call_tool("cursor_read_run_events", {"agent_id": _AGENT, "run_id": _RUN, "tail": 3, "max_wait_seconds": 30})
        )
    finally:
        await client.__aexit__(None, None, None)
    assert [event["kind"] for event in view["events"]] == ["tool_call", "tool_call", "result"]
    assert view["events"][1]["call_id"] == "c4999"
    assert view["finished"] is True and view["truncated"] is False
    assert view["scanned_events"] == 5002
    assert view["last_event_id"] == f"{_T0 + 6000}-0"
    assert view["last_event_at"] == "2026-10-06T01:33:18.023Z"


_HEARTBEAT = b"event: heartbeat\ndata: {}\n\n"


async def test_tail_stops_at_the_heartbeat_of_a_run_still_going() -> None:
    chunks = [*_background_run(end=None), _HEARTBEAT]
    client, _ = await _session(_router(_run(status="RUNNING", result=None), chunks, hang=True))
    started = time.monotonic()
    try:
        view = _data(
            await client.call_tool("cursor_read_run_events", {"agent_id": _AGENT, "run_id": _RUN, "tail": 2, "max_wait_seconds": 20})
        )
    finally:
        await client.__aexit__(None, None, None)
    # The heartbeat proves the replay has been delivered: no need to wait for the deadline.
    assert time.monotonic() - started < 5
    assert view["finished"] is False and view["truncated"] is False
    assert [event["kind"] for event in view["events"]] == ["tool_call", "assistant"]


async def test_silence_alone_does_not_end_the_walk() -> None:
    # No heartbeat, no live event, no result: the replay may not be over, the walk says so.
    client, _ = await _session(_router(_run(status="RUNNING", result=None), _background_run(end=None), hang=True))
    started = time.monotonic()
    try:
        view = _data(
            await client.call_tool("cursor_read_run_events", {"agent_id": _AGENT, "run_id": _RUN, "tail": 2, "max_wait_seconds": 3})
        )
    finally:
        await client.__aexit__(None, None, None)
    assert time.monotonic() - started >= 2.5
    assert view["truncated"] is True
    assert [event["kind"] for event in view["events"]] == ["tool_call", "assistant"]


async def test_tail_stops_at_live_events_even_without_a_pause() -> None:
    now_ms = int(time.time() * 1000)

    class _Chatty(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield _sse("status", {"status": "RUNNING"}, _T0)
            for index in range(10_000):  # a live agent streaming without pause
                yield _sse("assistant", {"text": "."}, now_ms + index)
                await asyncio.sleep(0.01)

        async def aclose(self) -> None:
            return None

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Chatty())

    client, _ = await _session(Router(responder))
    started = time.monotonic()
    try:
        view = _data(
            await client.call_tool("cursor_read_run_events", {"agent_id": _AGENT, "run_id": _RUN, "tail": 5, "max_wait_seconds": 20})
        )
    finally:
        await client.__aexit__(None, None, None)
    assert time.monotonic() - started < 5
    assert view["truncated"] is False


async def test_tail_and_cursor_are_exclusive() -> None:
    client, router = await _session(_router(_run(), []))
    try:
        refused = await client.call_tool(
            "cursor_read_run_events", {"agent_id": _AGENT, "run_id": _RUN, "tail": 5, "after_event_id": f"{_T0}-0"}
        )
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(refused)["code"] == "VALIDATION"
    assert router.calls == []


_A_RUNNING = "bc-aaaaaaaa-0000-0000-0000-000000000001"
_B_FINISHED = "bc-aaaaaaaa-0000-0000-0000-000000000002"
_C_ERROR = "bc-aaaaaaaa-0000-0000-0000-000000000003"
_D_BROKEN = "bc-aaaaaaaa-0000-0000-0000-000000000004"


def _fleet_router() -> Router:
    agents = [
        _agent(_A_RUNNING, name="runner-a", status="ACTIVE", latestRunId="run-a"),
        _agent(_B_FINISHED, name="runner-b", status="IDLE", latestRunId="run-b"),
        _agent(_C_ERROR, name="runner-c", status="IDLE", latestRunId="run-c"),
        _agent(_D_BROKEN, name="runner-d", status="ACTIVE", latestRunId="run-d"),
        _agent("bc-aaaaaaaa-0000-0000-0000-000000000005", name="other", status="ACTIVE", latestRunId="run-e"),
    ]
    runs = {
        "run-a": _run(id="run-a", agentId=_A_RUNNING, status="RUNNING", result=None),
        "run-b": _run(id="run-b", agentId=_B_FINISHED, status="FINISHED", result="all done"),
        "run-c": _run(id="run-c", agentId=_C_ERROR, status="ERROR", result=None),
        "run-e": _run(id="run-e", agentId="bc-aaaaaaaa-0000-0000-0000-000000000005", status="RUNNING", result=None),
    }

    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/agents":
            return httpx.Response(200, json={"items": agents})
        if path.endswith("/stream"):
            if "/run-a/" in path:
                return _stream_response([*_background_run(end=None), _HEARTBEAT], hang=True)
            return _stream_response(_background_run(end="ERROR"))
        run_id = path.rsplit("/", 1)[-1]
        if run_id == "run-d":
            return httpx.Response(500, json={"error": {"code": "internal_error", "message": "boom"}})
        return httpx.Response(200, json=runs[run_id])

    return Router(responder)


async def test_supervise_reports_each_runner_and_isolates_failures() -> None:
    client, router = await _session(_fleet_router())
    try:
        view = _data(
            await client.call_tool("cursor_supervise", {"status": "all", "name": "runner", "activity": True})
        )
    finally:
        await client.__aexit__(None, None, None)
    rows = {row["name"]: row for row in view["items"]}
    assert set(rows) == {"runner-a", "runner-b", "runner-c", "runner-d"}
    assert rows["runner-a"]["status"] == "RUNNING"
    assert rows["runner-a"]["activity"]["unfinished_background_tasks"] == 1
    assert rows["runner-a"]["activity"]["last_tool_call"] == {"name": "await", "status": "completed"}
    assert "background_tasks" not in rows["runner-a"]["activity"]  # overview: signals, not texts
    assert "activity" not in rows["runner-b"]  # finished with a result: no stream read
    assert rows["runner-c"]["status"] == "ERROR" and rows["runner-c"]["activity"]["run_terminal"] is True
    assert rows["runner-d"]["read_error"].startswith("UPSTREAM")
    summary = view["summary"]
    assert summary["agents"] == 4
    assert summary["by_status"] == {"RUNNING": 1, "FINISHED": 1, "ERROR": 1, "UNREAD": 1}
    assert summary["stale"] == [_A_RUNNING]  # its last event dates from the recorded stream
    assert summary["unfinished_after_end"] == [_C_ERROR]
    assert summary["read_errors"] == 1
    streams = [call[1] for call in router.calls if call[1].endswith("/stream")]
    assert len(streams) == 2


async def test_supervise_keeps_active_agents_by_default() -> None:
    client, _ = await _session(_fleet_router())
    try:
        view = _data(await client.call_tool("cursor_supervise", {}))
    finally:
        await client.__aexit__(None, None, None)
    assert sorted(row["agent_status"] for row in view["items"]) == ["ACTIVE", "ACTIVE", "ACTIVE"]
    assert all("activity" not in row for row in view["items"])


async def test_supervise_reads_runs_in_parallel_but_bounded() -> None:
    agents = [
        _agent(f"bc-bbbbbbbb-0000-0000-0000-{index:012d}", status="ACTIVE", latestRunId=f"run-{index}")
        for index in range(20)
    ]
    state = {"now": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"items": agents})
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.05)
        state["now"] -= 1
        run_id = request.url.path.rsplit("/", 1)[-1]
        agent_id = request.url.path.split("/")[3]
        return httpx.Response(200, json=_run(id=run_id, agentId=agent_id, status="RUNNING", result=None))

    server = build_server(_settings(), transport=httpx.MockTransport(handler))
    started = time.monotonic()
    async with Client(server) as client:
        view = _data(await client.call_tool("cursor_supervise", {}))
    assert len(view["items"]) == 20
    assert 1 < state["peak"] <= 8
    assert time.monotonic() - started < 1.0  # 20 reads of 50 ms, not one after another


def _replace_router(statuses: list[str], *, cancel_status: int = 200) -> Router:
    """Agent ACTIVE on run-old; each GET of run-old returns the next status of ``statuses``."""
    sequence = iter(statuses)
    state = {"last": statuses[0]}

    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == f"/v1/agents/{_AGENT}":
            return httpx.Response(200, json=_agent(_AGENT, status="ACTIVE", latestRunId="run-old"))
        if request.method == "GET" and path.endswith("/runs/run-old"):
            state["last"] = next(sequence, state["last"])
            return httpx.Response(200, json=_run(id="run-old", status=state["last"], result=None))
        if path.endswith("/runs/run-old/cancel"):
            if cancel_status != 200:
                return httpx.Response(cancel_status, json={"error": {"code": "run_not_cancellable", "message": "ended"}})
            return httpx.Response(200, json={"id": "run-old"})
        if request.method == "POST" and path.endswith("/runs"):
            return httpx.Response(201, json={"run": _run(id="run-new", status="CREATING", result=None)})
        raise AssertionError(f"{request.method} {path}")

    return Router(responder)


def _posts(router: Router) -> list[str]:
    return [call[1] for call in router.calls if call[0] == "POST"]


async def test_replace_active_cancels_then_follows_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cursor_cloud_mcp.sessions.CANCEL_REREAD_PAUSE_SECONDS", 0)
    router = _replace_router(["RUNNING", "RUNNING", "CANCELLED"])
    client, _ = await _session(router)
    try:
        view = _data(
            await client.call_tool(
                "cursor_create_run", {"agent_id": _AGENT, "prompt": "new direction", "replace_active": True}
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    assert view["replaced_run_id"] == "run-old" and view["run_id"] == "run-new"
    # One cancel, then exactly one follow-up, sent only after CANCELLED was re-read.
    assert _posts(router) == [f"/v1/agents/{_AGENT}/runs/run-old/cancel", f"/v1/agents/{_AGENT}/runs"]
    follow_up = next(index for index, call in enumerate(router.calls) if call[1] == f"/v1/agents/{_AGENT}/runs")
    assert any(call[1].endswith("/runs/run-old") for call in router.calls[:follow_up][-1:])


async def test_replace_active_sends_nothing_if_the_run_does_not_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cursor_cloud_mcp.sessions.CANCEL_REREAD_PAUSE_SECONDS", 0)
    router = _replace_router(["RUNNING"])
    client, _ = await _session(router)
    try:
        refused = await client.call_tool(
            "cursor_create_run", {"agent_id": _AGENT, "prompt": "new direction", "replace_active": True}
        )
    finally:
        await client.__aexit__(None, None, None)
    payload = _error_payload(refused)
    assert payload["code"] == "AGENT_BUSY" and payload["run_id"] == "run-old"
    assert _posts(router) == [f"/v1/agents/{_AGENT}/runs/run-old/cancel"]


async def test_replace_active_on_an_ended_run_just_follows_up() -> None:
    router = _replace_router(["FINISHED"])
    client, _ = await _session(router)
    try:
        view = _data(
            await client.call_tool(
                "cursor_create_run", {"agent_id": _AGENT, "prompt": "next", "replace_active": True}
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    assert "replaced_run_id" not in view
    assert _posts(router) == [f"/v1/agents/{_AGENT}/runs"]


async def test_replace_active_race_with_the_end_of_the_run() -> None:
    # Read RUNNING, but it ends before the cancel arrives: the API refuses the cancel.
    router = _replace_router(["RUNNING", "FINISHED"], cancel_status=409)
    client, _ = await _session(router)
    try:
        view = _data(
            await client.call_tool(
                "cursor_create_run", {"agent_id": _AGENT, "prompt": "next", "replace_active": True}
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    assert "replaced_run_id" not in view
    assert _posts(router)[-1] == f"/v1/agents/{_AGENT}/runs"


async def test_replace_active_validates_the_model_before_cancelling() -> None:
    router = _replace_router(["RUNNING"])
    original = router.responder

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"items": [{"id": "composer-2.5", "displayName": "Composer"}]})
        return original(request)

    router.responder = responder
    client, _ = await _session(router)
    try:
        refused = await client.call_tool(
            "cursor_create_run",
            {"agent_id": _AGENT, "prompt": "next", "replace_active": True, "model_id": "no-such-model"},
        )
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(refused)["code"] == "VALIDATION"
    assert _posts(router) == []  # the running run was not stopped for a rejected request
