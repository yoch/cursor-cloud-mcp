"""Live shell output on the run stream: opt-in header, shell-tool-call-delta events, merging and clipping."""

import httpx
import pytest

from tests.test_supervision import _T0, _sse, _stream_response
from tests.test_tools import _AGENT, _RUN, Router, _data, _run, _session

pytestmark = pytest.mark.anyio

_HEADER = "x-cursor-sdk-shell-tool-call-delta"
_CALL = "call-1"
_ARGS = {"command": "for i in 1 2; do echo tick $i; echo warn $i >&2; sleep 2; done"}


def _delta(stream: str, content: str, at: int, call: str = _CALL) -> bytes:
    # Shape recorded from a real stream on 2026-11-25 (values anonymized).
    payload = {"type": "shell-tool-call-delta", "callId": call, "modelCallId": "m-1", "stream": stream, "content": content}
    return _sse("interaction_update", payload, at)


def _shell_run() -> list[bytes]:
    return [
        _sse("status", {"status": "RUNNING"}, _T0),
        _sse("interaction_update", {"type": "token-delta", "tokens": 30}, _T0 + 1),
        _sse("tool_call", {"callId": _CALL, "name": "run_terminal_cmd", "status": "running", "args": _ARGS}, _T0 + 2),
        _sse("interaction_update", {"type": "tool-call-started", "callId": _CALL}, _T0 + 3),
        _delta("stdout", "tick 1\n", _T0 + 4),
        _delta("stderr", "warn 1\n", _T0 + 5),
        _delta("stdout", "tick 2\n", _T0 + 6),
        _delta("stderr", "warn 2\n", _T0 + 7),
        _sse(
            "tool_call",
            {
                "callId": _CALL,
                "name": "run_terminal_cmd",
                "status": "completed",
                "args": _ARGS,
                "result": {"success": {"stdout": "tick 1\ntick 2\n", "stderr": "warn 1\nwarn 2\n"}},
            },
            _T0 + 8,
        ),
        _sse("assistant", {"text": "OK"}, _T0 + 9),
        _sse("result", {"status": "FINISHED", "text": "OK"}, _T0 + 10),
        _sse("done", {}, _T0 + 11),
    ]


def _recording_router(chunks: list[bytes]) -> tuple[Router, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stream"):
            seen.append(request)
            return _stream_response(chunks)
        return httpx.Response(200, json=_run())

    return Router(responder), seen


async def _read(chunks: list[bytes], **arguments: object) -> tuple[dict, list[httpx.Request]]:
    router, seen = _recording_router(chunks)
    client, _ = await _session(router)
    try:
        view = _data(
            await client.call_tool(
                "cursor_read_run_events", {"agent_id": _AGENT, "run_id": _RUN, "max_wait_seconds": 5, **arguments}
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    return view, seen


async def test_shell_output_is_opt_in() -> None:
    view, seen = await _read(_shell_run())
    assert _HEADER not in seen[0].headers
    assert "shell_output" not in [event["kind"] for event in view["events"]]


async def test_shell_deltas_of_one_command_form_one_event() -> None:
    view, seen = await _read(_shell_run(), include_shell_output=True)
    assert seen[0].headers[_HEADER] == "true"
    kinds = [event["kind"] for event in view["events"]]
    # The finished tool_call replaces the running one, after the output it printed.
    assert kinds == ["status", "shell_output", "tool_call", "assistant", "result"]
    assert view["events"][2]["tool_status"] == "completed"
    shell = view["events"][1]
    assert shell["call_id"] == _CALL
    assert shell["stdout"] == "tick 1\ntick 2\n"
    assert shell["stderr"] == "warn 1\nwarn 2\n"
    assert shell["event_id"] == f"{_T0 + 7}-0"  # the cursor of the last delta merged in
    assert "clipped" not in shell and "text" not in shell
    assert view["finished"] is True


async def test_long_shell_output_keeps_its_end() -> None:
    chunks = [_sse("status", {"status": "RUNNING"}, _T0)]
    chunks += [_delta("stdout", f"line {index:04d}\n", _T0 + 1 + index) for index in range(200)]
    chunks += [_delta("stdout", "other\n", _T0 + 300, call="call-2"), _sse("done", {}, _T0 + 301)]
    view, _ = await _read(chunks, include_shell_output=True, tool_output_limit=100)
    first, second = view["events"][1], view["events"][2]
    assert len(first["stdout"]) == 100 and first["stdout"].startswith("…")
    assert first["stdout"].endswith("line 0199\n")
    assert first["clipped"] is True
    assert second["call_id"] == "call-2" and second["stdout"] == "other\n"


async def test_tail_mode_keeps_shell_output_too() -> None:
    view, seen = await _read(_shell_run(), tail=4, include_shell_output=True, max_wait_seconds=10)
    assert seen[0].headers[_HEADER] == "true"
    assert [event["kind"] for event in view["events"]] == ["shell_output", "tool_call", "assistant", "result"]
    assert view["events"][0]["stdout"] == "tick 1\ntick 2\n"


async def test_other_interaction_updates_stay_ignored() -> None:
    chunks = [
        _sse("status", {"status": "RUNNING"}, _T0),
        _sse("interaction_update", {"type": "shell-tool-call-delta", "stream": "pty", "content": "x"}, _T0 + 1),
        _sse("interaction_update", {"type": "text-delta", "text": "hi"}, _T0 + 2),
        _sse("interaction_update", "not json", _T0 + 3),
        _sse("done", {}, _T0 + 4),
    ]
    view, _ = await _read(chunks, include_shell_output=True)
    assert [event["kind"] for event in view["events"]] == ["status", "done"]
    assert view["last_event_id"] == f"{_T0 + 4}-0"


async def test_merged_chunks_count_against_the_cap_by_what_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    from cursor_cloud_mcp import stream

    monkeypatch.setattr(stream, "TOOL_TEXT_TOTAL_MAX_CHARS", 1000)
    chunks = [_sse("status", {"status": "RUNNING"}, _T0)]
    # 5000 characters printed, far above the cap, but one event of at most 100 is kept.
    chunks += [_delta("stdout", "x" * 49 + "\n", _T0 + 1 + index) for index in range(100)]
    chunks.append(_sse("done", {}, _T0 + 200))
    view, _ = await _read(chunks, include_shell_output=True, tool_output_limit=100)
    assert view["truncated"] is False and view["finished"] is True
    assert [event["kind"] for event in view["events"]] == ["status", "shell_output", "done"]


async def test_chunks_without_a_call_id_are_never_merged() -> None:
    chunks = [
        _sse("status", {"status": "RUNNING"}, _T0),
        _sse("interaction_update", {"type": "shell-tool-call-delta", "stream": "stdout", "content": "a\n"}, _T0 + 1),
        _sse("interaction_update", {"type": "shell-tool-call-delta", "stream": "stdout", "content": "b\n"}, _T0 + 2),
        _sse("done", {}, _T0 + 3),
    ]
    view, _ = await _read(chunks, include_shell_output=True)
    assert [event.get("stdout") for event in view["events"]] == [None, "a\n", "b\n", None]
