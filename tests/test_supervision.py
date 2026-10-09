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


async def test_activity_keeps_the_last_observed_events_and_widens_the_last_tool_call() -> None:
    long_result = "y" * 3000
    chunks = [
        _sse("status", {"status": "RUNNING"}, _T0),
        _sse(
            "tool_call",
            {"callId": "c1", "name": "shell", "status": "completed", "args": {"cmd": "run"}, "result": {"output": long_result}},
            _T0 + 1,
        ),
        _sse("result", {"status": "FINISHED"}, _T0 + 2),
        _sse("done", {}, _T0 + 3),
    ]
    client, _ = await _session(_router(_run(), chunks))
    try:
        view = _data(await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN, "activity": True}))
    finally:
        await client.__aexit__(None, None, None)
    activity = view["activity"]
    assert [event["kind"] for event in activity["last_events"]] == ["status", "tool_call", "result"]
    assert activity["complete"] is True and "stream_error" not in activity
    tool_event = activity["last_events"][1]
    assert len(tool_event["tool_result"]) == 2000  # ACTIVITY_TOOL_TEXT_MAX_CHARS, marker included
    assert "…[clipped]…" in tool_event["tool_result"] and tool_event["clipped"] is True
    summary_tool = activity["last_tool_call"]
    assert summary_tool["name"] == "shell" and len(summary_tool["result"]) == 2000


async def test_tool_output_limit_widens_the_head_tail_clip_of_events() -> None:
    long_result = "z" * 3000
    chunks = [
        _sse("status", {"status": "RUNNING"}, _T0),
        _sse(
            "tool_call",
            {"callId": "c1", "name": "shell", "status": "completed", "args": {"cmd": "run"}, "result": {"output": long_result}},
            _T0 + 1,
        ),
        _sse("done", {}, _T0 + 2),
    ]
    client, _ = await _session(_router(_run(status="RUNNING", result=None), chunks))
    try:
        default = _data(
            await client.call_tool("cursor_read_run_events", {"agent_id": _AGENT, "run_id": _RUN, "max_wait_seconds": 2})
        )
        widened = _data(
            await client.call_tool(
                "cursor_read_run_events",
                {"agent_id": _AGENT, "run_id": _RUN, "max_wait_seconds": 2, "tool_output_limit": 2000},
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    short = default["events"][1]["tool_result"]
    long = widened["events"][1]["tool_result"]
    assert len(short) == 500 and "…[clipped]…" in short
    assert len(long) == 2000 and "…[clipped]…" in long
    assert long[:200] == short[:200]  # same head, only the window widens


async def test_upstream_omitted_tool_fields_are_reported() -> None:
    # Recorded live on 2026-10-09: an 8 MB terminal output was omitted with truncated: {result: true}.
    chunks = [
        _sse("tool_call", {"callId": "c1", "name": "read", "status": "completed", "result": "short"}, _T0),
        _sse(
            "tool_call",
            {
                "callId": "c2",
                "name": "run_terminal_cmd",
                "status": "completed",
                "args": {"command": "python3 -c \"print('A' * 8000000)\""},
                "truncated": {"result": True},
            },
            _T0 + 1,
        ),
        _sse(
            "tool_call",
            {"callId": "c3", "name": "read_file", "status": "completed", "truncated": {"args": True}},
            _T0 + 2,
        ),
    ]
    client, _ = await _session(_router(_run(), chunks))
    try:
        events = _data(
            await client.call_tool("cursor_read_run_events", {"agent_id": _AGENT, "run_id": _RUN, "max_wait_seconds": 2})
        )
        view = _data(await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN, "activity": True}))
    finally:
        await client.__aexit__(None, None, None)
    first, second, third = events["events"]
    assert first["tool_result"] == "short" and "tool_result_omitted" not in first
    assert "tool_result" not in second and second["tool_result_omitted"] is True
    assert "tool_args_omitted" not in second  # args were present: only the result was omitted
    assert "clipped" not in second  # an upstream omission is not a local clip
    assert third["tool_args_omitted"] is True and "tool_args" not in third
    activity_tool = view["activity"]["last_tool_call"]
    assert activity_tool["name"] == "read_file" and activity_tool["tool_args_omitted"] is True


async def test_the_global_tool_text_cap_truncates_and_the_cursor_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cursor_cloud_mcp.stream.TOOL_TEXT_TOTAL_MAX_CHARS", 2400)
    padding = "w" * 1000
    chunks = [_sse("status", {"status": "RUNNING"}, _T0)]
    ids: list[str] = []
    for index in range(5):
        chunks.append(
            _sse(
                "tool_call",
                {"callId": f"c{index}", "name": "shell", "status": "completed", "result": {"output": padding + str(index)}},
                _T0 + 1 + index,
            )
        )
        ids.append(f"{_T0 + 1 + index}-0")
    chunks.append(_sse("done", {}, _T0 + 10))

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stream"):
            after = request.headers.get("last-event-id")
            if after in ids:  # what the API does: send only the events after the cursor
                return _stream_response(chunks[ids.index(after) + 2 :])
            return _stream_response(chunks)
        return httpx.Response(200, json=_run(status="RUNNING", result=None))

    client, _ = await _session(Router(responder))
    try:
        first = _data(
            await client.call_tool("cursor_read_run_events", {"agent_id": _AGENT, "run_id": _RUN, "max_wait_seconds": 2})
        )
        second = _data(
            await client.call_tool(
                "cursor_read_run_events",
                {"agent_id": _AGENT, "run_id": _RUN, "max_wait_seconds": 2, "after_event_id": first["last_event_id"]},
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    # Each clipped result costs 500: four tool calls fit under 2400, the fifth is not returned.
    assert [event["kind"] for event in first["events"]] == ["status", "tool_call", "tool_call", "tool_call", "tool_call"]
    assert first["truncated"] is True
    assert first["last_event_id"] == ids[3]
    assert [event["kind"] for event in second["events"]] == ["tool_call", "done"]
    assert second["finished"] is True


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
            if "/run-b/" in path:
                return _stream_response(_background_run(end="FINISHED"))
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
    assert "last_events" not in rows["runner-a"]["activity"]
    assert rows["runner-a"]["activity"]["complete"] is True
    assert rows["runner-b"]["git"]["scope"] == "agent_current_state"
    assert rows["runner-b"]["git"]["branches"][0]["branch"] == "cursor/demo"
    # The headline case: FINISHED with a result, yet its background job was last seen running.
    assert rows["runner-b"]["status"] == "FINISHED" and rows["runner-b"]["activity"]["unfinished_background_tasks"] == 1
    assert rows["runner-c"]["status"] == "ERROR" and rows["runner-c"]["activity"]["run_terminal"] is True
    assert rows["runner-d"]["read_error"].startswith("UPSTREAM")
    summary = view["summary"]
    assert summary["agents"] == 4
    assert summary["by_status"] == {"RUNNING": 1, "FINISHED": 1, "ERROR": 1, "UNREAD": 1}
    assert summary["stale"] == [_A_RUNNING]  # its last event dates from the recorded stream
    assert summary["unfinished_after_end"] == [_B_FINISHED, _C_ERROR]
    assert "incomplete" not in summary
    assert summary["read_errors"] == 1
    streams = [call[1] for call in router.calls if call[1].endswith("/stream")]
    assert len(streams) == 3  # a, b and c; d could not be read


async def test_supervise_keeps_stream_error_rows_partial_and_incomplete() -> None:
    agent = _agent(_B_FINISHED, status="IDLE", latestRunId="run-b")
    errored = [*_background_run(end=None)[:4], _sse("error", {"message": "stream failed"}, _T0 + 5)]

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"items": [agent]})
        if request.url.path.endswith("/stream"):
            return _stream_response(errored)
        return httpx.Response(200, json=_run(id="run-b", agentId=_B_FINISHED, status="ERROR", result=None))

    client, _ = await _session(Router(responder))
    try:
        view = _data(await client.call_tool("cursor_supervise", {"status": "all", "activity": True}))
    finally:
        await client.__aexit__(None, None, None)
    row = view["items"][0]
    assert row["activity"]["stream_error"] is True and row["activity"]["complete"] is False
    assert "last_events" not in row["activity"]  # the overview stays light
    assert view["summary"]["incomplete"] == [_B_FINISHED]
    assert "stale" not in view["summary"]  # a partial summary is not conclusive


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


# --- Review fixes ---------------------------------------------------------------------------


def _paged_agents(count: int, *, status: str = "ACTIVE") -> Router:
    """Agents served 100 per page, with real cursors; each agent's run is RUNNING without result."""
    agents = [
        _agent(f"bc-cccccccc-0000-0000-0000-{index:012d}", name=f"runner-{index}", status=status, latestRunId=f"run-{index}")
        for index in range(count)
    ]

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            assert request.url.params["limit"] == "100"
            start = int(request.url.params.get("cursor", "0"))
            payload: dict[str, object] = {"items": agents[start : start + 100]}
            if start + 100 < count:
                payload["nextCursor"] = str(start + 100)
            return httpx.Response(200, json=payload)
        run_id = request.url.path.rsplit("/", 1)[-1]
        agent_id = request.url.path.split("/")[3]
        return httpx.Response(200, json=_run(id=run_id, agentId=agent_id, status="RUNNING", result=None))

    return Router(responder)


async def test_supervise_limit_is_exact_and_the_cursor_loses_nothing() -> None:
    client, _ = await _session(_paged_agents(12))
    seen: list[str] = []
    try:
        cursor = None
        for _round in range(4):
            args: dict[str, object] = {"limit": 5}
            if cursor is not None:
                args["cursor"] = cursor
            view = _data(await client.call_tool("cursor_supervise", args))
            assert len(view["items"]) <= 5
            seen += [row["name"] for row in view["items"]]
            cursor = view.get("next_cursor")
            if cursor is None:
                break
    finally:
        await client.__aexit__(None, None, None)
    assert seen == [f"runner-{index}" for index in range(12)]  # every match, once, in order


async def test_supervise_default_limit_is_enforced() -> None:
    client, _ = await _session(_paged_agents(120))
    try:
        view = _data(await client.call_tool("cursor_supervise", {}))
    finally:
        await client.__aexit__(None, None, None)
    assert len(view["items"]) == 50
    assert view["has_more"] is True and view["next_cursor"].startswith("mcp~50~")


async def test_a_malformed_scan_cursor_is_refused() -> None:
    client, router = await _session(_paged_agents(3))
    try:
        refused = await client.call_tool("cursor_supervise", {"cursor": "mcp~abc~0"})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(refused)["code"] == "VALIDATION"
    assert router.calls == []


async def test_name_search_resumes_inside_a_page() -> None:
    client, _ = await _session(_paged_agents(12, status="IDLE"))
    try:
        first = _data(await client.call_tool("cursor_list_agents", {"name": "runner", "limit": 4}))
        second = _data(
            await client.call_tool("cursor_list_agents", {"name": "runner", "limit": 4, "cursor": first["next_cursor"]})
        )
    finally:
        await client.__aexit__(None, None, None)
    assert [item["name"] for item in first["items"]] == [f"runner-{index}" for index in range(4)]
    assert [item["name"] for item in second["items"]] == [f"runner-{index}" for index in range(4, 8)]


def test_get_run_budget_stays_under_the_client_timeout() -> None:
    from cursor_cloud_mcp.server import get_run_budget

    assert get_run_budget(0) == 90.0
    assert get_run_budget(60) == 95.0  # 60 + 45 would exceed the 100 s of the example clients


async def test_activity_false_never_reads_the_stream() -> None:
    client, router = await _session(_router(_run(status="ERROR", result=None), _background_run(end="ERROR")))
    try:
        view = _data(await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN, "activity": False}))
    finally:
        await client.__aexit__(None, None, None)
    assert "activity" not in view
    assert not any(call[1].endswith("/stream") for call in router.calls)


async def test_terminal_activity_is_cached_with_a_current_idle_time() -> None:
    client, router = await _session(_router(_run(status="ERROR", result=None), _background_run(end="ERROR")))
    try:
        first = _data(await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN}))
        await asyncio.sleep(1.1)
        second = _data(await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN}))
    finally:
        await client.__aexit__(None, None, None)
    assert sum(1 for call in router.calls if call[1].endswith("/stream")) == 1
    assert second["activity"]["unfinished_background_tasks"] == 1
    assert second["activity"]["idle_seconds"] > first["activity"]["idle_seconds"]


async def test_a_stream_error_returns_a_partial_summary_and_is_not_cached() -> None:
    errored = [*_background_run(end=None)[:4], _sse("error", {"message": "stream failed"}, _T0 + 5)]
    streams = iter([errored, _background_run(end="ERROR")])
    run = _run(status="ERROR", result=None)

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stream"):
            return _stream_response(next(streams))
        return httpx.Response(200, json=run)

    client, router = await _session(Router(responder))
    try:
        first = _data(await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN}))
        second = _data(await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN}))
    finally:
        await client.__aexit__(None, None, None)
    activity = first["activity"]
    assert activity["stream_error"] is True and activity["complete"] is False
    assert "activity_error" not in first
    # The error event itself stays visible in the observed tail: the flag is not the only diagnosis.
    assert activity["last_events"][-1]["kind"] == "error"
    assert activity["last_events"][-1]["text"] == "stream failed"
    assert activity["last_assistant_text"] == "Launching the job."
    assert sum(1 for call in router.calls if call[1].endswith("/stream")) == 2  # retried, not cached
    assert second["activity"]["complete"] is True
    assert second["activity"]["unfinished_background_tasks"] == 1
    assert "stream_error" not in second["activity"]


async def test_statuses_survive_replays_that_do_not_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cursor_cloud_mcp.server.SUPERVISE_ACTIVITY_BUDGET_SECONDS", 4.0)
    monkeypatch.setattr("cursor_cloud_mcp.supervision.SUPERVISE_ACTIVITY_BUDGET_SECONDS", 4.0)
    agents = [
        _agent(f"bc-dddddddd-0000-0000-0000-{index:012d}", status="ACTIVE", latestRunId=f"run-{index}")
        for index in range(20)
    ]

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"items": agents})
        if request.url.path.endswith("/stream"):
            # Old events, no heartbeat, no result: the walk can never prove it reached the end.
            return _stream_response(_background_run(end=None), hang=True)
        run_id = request.url.path.rsplit("/", 1)[-1]
        agent_id = request.url.path.split("/")[3]
        return httpx.Response(200, json=_run(id=run_id, agentId=agent_id, status="RUNNING", result=None))

    client, _ = await _session(Router(responder))
    try:
        view = _data(await client.call_tool("cursor_supervise", {"activity": True}))
    finally:
        await client.__aexit__(None, None, None)
    assert all(row["status"] == "RUNNING" for row in view["items"])  # status reads never starved
    assert len(view["summary"]["incomplete"]) == 20
    assert "stale" not in view["summary"]  # partial walks are not conclusive


def test_unfinished_tasks_are_counted_beyond_the_listed_twenty() -> None:
    from cursor_cloud_mcp.activity import ActivityTracker
    from cursor_cloud_mcp.models import RunEventView

    tracker = ActivityTracker()
    for task in range(25):
        payload = {
            "name": "run_terminal_cmd",
            "status": "completed",
            "args": {"isBackground": True},
            "result": {"success": {"shellId": task, "command": f"job {task}"}},
        }
        view = RunEventView(event_id=f"{_T0 + task}-0", kind="tool_call", tool_name="run_terminal_cmd", tool_status="completed")
        tracker.feed(f"{_T0 + task}-0", "tool_call", payload, view)
    for task in range(5, 25):  # the 20 newest are awaited to completion; the 5 oldest never are
        payload = {"name": "await", "status": "completed", "result": {"success": {"complete": {"taskId": str(task), "runtimeMs": "1"}}}}
        view = RunEventView(event_id=f"{_T0 + 100 + task}-0", kind="tool_call", tool_name="await", tool_status="completed")
        tracker.feed(f"{_T0 + 100 + task}-0", "tool_call", payload, view)
    summary = tracker.summary(run_terminal=True)
    assert summary.unfinished_background_tasks == 5
    assert summary.background_tasks_total == 25
    assert summary.background_tasks is not None and len(summary.background_tasks) == 20


async def test_ignored_events_are_not_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    from cursor_cloud_mcp import stream

    parsed: list[str] = []
    original = stream._payload

    def counting(data: str) -> object:
        parsed.append(data)
        return original(data)

    monkeypatch.setattr(stream, "_payload", counting)
    chunks = [
        _sse("status", {"status": "RUNNING"}, _T0),
        *[_sse("interaction_update", {"type": "token-delta"}, _T0 + 1 + index) for index in range(10)],
        _HEARTBEAT,
    ]
    client, _ = await _session(_router(_run(status="RUNNING", result=None), chunks, hang=True))
    try:
        await client.call_tool("cursor_read_run_events", {"agent_id": _AGENT, "run_id": _RUN, "tail": 5})
    finally:
        await client.__aexit__(None, None, None)
    assert len(parsed) == 1  # only the status event; ten updates and the heartbeat untouched


def test_same_id_ignores_case_only_for_prefixed_uuids() -> None:
    from cursor_cloud_mcp.validation import same_id

    assert same_id("run-abcdef12-abcd-abcd-abcd-abcdef123456", "run-ABCDEF12-ABCD-ABCD-ABCD-ABCDEF123456")
    assert same_id("bc-abcdef12-abcd-abcd-abcd-abcdef123456", "bc-ABCDEF12-abcd-ABCD-abcd-ABCDEF123456")
    assert not same_id("run-Ab12", "run-aB12")  # not a UUID: case may matter
    assert not same_id("run-abcdef12-abcd-abcd-abcd-abcdef123456", "run-abcdef12-abcd-abcd-abcd-abcdef123457")


async def test_follow_up_is_annotated_destructive() -> None:
    client, _ = await _session(Router(lambda _request: httpx.Response(500)))
    try:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    finally:
        await client.__aexit__(None, None, None)
    follow_up = tools["cursor_create_run"].annotations
    creation = tools["cursor_create_agent"].annotations
    assert follow_up is not None and follow_up.destructive_hint is True  # replace_active cancels a run
    assert creation is not None and creation.destructive_hint is False


async def test_expired_streams_are_not_counted_as_incomplete() -> None:
    agents = [_agent(_B_FINISHED, status="IDLE", latestRunId="run-b")]

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"items": agents})
        if request.url.path.endswith("/stream"):
            return httpx.Response(410, json={"error": {"code": "stream_expired", "message": "gone"}})
        return httpx.Response(200, json=_run(id="run-b", agentId=_B_FINISHED, status="FINISHED", result="done"))

    client, _ = await _session(Router(responder))
    try:
        view = _data(await client.call_tool("cursor_supervise", {"status": "all", "activity": True}))
    finally:
        await client.__aexit__(None, None, None)
    assert view["items"][0]["activity_error"].startswith("STREAM_EXPIRED")
    assert "incomplete" not in view["summary"]



class _SlowClose(httpx.AsyncByteStream):
    """Delivers its events, then takes longer to close than the 0.5 s grace allows."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        await asyncio.sleep(3600)

    async def aclose(self) -> None:
        await asyncio.sleep(2)


async def test_a_slow_close_does_not_lose_the_events_already_read() -> None:
    chunks = _background_run(end=None)

    def responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_SlowClose(chunks))

    client, _ = await _session(Router(responder))
    try:
        view = _data(
            await client.call_tool("cursor_read_run_events", {"agent_id": _AGENT, "run_id": _RUN, "max_wait_seconds": 2})
        )
    finally:
        await client.__aexit__(None, None, None)
    # The read stopped at its deadline on a live stream; closing overran, the events are still returned.
    assert view["truncated"] is True
    assert [event["kind"] for event in view["events"]][:2] == ["status", "assistant"]
    assert view["last_event_id"] == f"{_T0 + 900_500}-0"
