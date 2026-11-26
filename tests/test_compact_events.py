"""cursor_read_run_events(compact=true): the command and its outputs, without Cursor's internal fields."""

import pytest

from cursor_cloud_mcp.stream import compact_args, compact_result
from tests.test_supervision import _T0, _router, _sse
from tests.test_tools import _AGENT, _RUN, _data, _run, _session

pytestmark = pytest.mark.anyio

_ARGS = {
    "command": "make test",
    "parsingResult": {"executableCommands": [{"name": "make", "args": [{"type": "word", "value": "test"}]}]},
    "toolCallId": "toolu_0001",
    "timeout": 600000,
}
_RESULT = {"success": {"command": "make test", "exitCode": 2, "stdout": "ran 3\n", "stderr": "1 failed\n", "executionTime": 812}}


def _chunks() -> list[bytes]:
    return [
        _sse("status", {"status": "RUNNING"}, _T0),
        _sse("tool_call", {"callId": "c1", "name": "shell", "status": "running", "args": _ARGS}, _T0 + 1),
        _sse("tool_call", {"callId": "c1", "name": "shell", "status": "completed", "args": _ARGS, "result": _RESULT}, _T0 + 2),
        _sse("done", {}, _T0 + 3),
    ]


@pytest.mark.parametrize("tail", [None, 10])
async def test_compact_keeps_the_command_and_its_outputs(tail: int | None) -> None:
    args: dict[str, object] = {"agent_id": _AGENT, "run_id": _RUN, "compact": True, "max_wait_seconds": 3}
    if tail is not None:
        args["tail"] = tail
    client, _ = await _session(_router(_run(status="RUNNING", result=None), _chunks()))
    try:
        view = _data(await client.call_tool("cursor_read_run_events", args))
    finally:
        await client.__aexit__(None, None, None)
    calls = [event for event in view["events"] if event["kind"] == "tool_call"]
    assert len(calls) == 1  # the running and completed states still merge
    assert calls[0]["tool_args"] == "make test"
    assert calls[0]["tool_result"] == "[success] exit_code=2\nstdout:\nran 3\nstderr:\n1 failed"
    assert "parsingResult" not in str(view)


async def test_without_compact_the_payload_is_unchanged() -> None:
    client, _ = await _session(_router(_run(status="RUNNING", result=None), _chunks()))
    try:
        view = _data(await client.call_tool("cursor_read_run_events", {"agent_id": _AGENT, "run_id": _RUN, "max_wait_seconds": 3}))
    finally:
        await client.__aexit__(None, None, None)
    call = next(event for event in view["events"] if event["kind"] == "tool_call")
    assert "parsingResult" in call["tool_args"]


def test_unknown_shapes_are_left_as_they_are() -> None:
    launch = {"success": {"command": "bash ~/monitor.sh", "shellId": 931367, "pid": 165414}, "isBackground": True}
    assert compact_result(launch) == launch  # no exit code, no output: nothing recognized, nothing hidden
    assert compact_args({"path": "a.py", "contents": "x"}) == {"path": "a.py", "contents": "x"}
    assert compact_args({"command": "bash ~/monitor.sh", "isBackground": True}) == "[background] bash ~/monitor.sh"
    assert compact_result("plain text") == "plain text"
    assert compact_result({"failure": {"exitCode": 127, "stderr": "sh: nope: not found"}}) == (
        "[failure] exit_code=127\nstderr:\nsh: nope: not found"
    )


def test_calls_without_call_id_keep_their_full_arguments() -> None:
    from cursor_cloud_mcp.stream import SseEvent, _simplify

    payload = {"name": "shell", "status": "completed", "args": {"command": "make", "cwd": "/a"}, "result": _RESULT}
    view = _simplify(SseEvent(event="tool_call", data="{}", event_id="1-0"), payload, compact=True)
    assert "/a" in (view.tool_args or "")  # matched on its args: two such calls must not merge
    assert view.tool_result is not None and view.tool_result.startswith("[success]")
