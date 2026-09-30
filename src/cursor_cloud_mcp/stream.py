"""Lecture bornée du flux SSE d'un run et attente par relectures."""

import asyncio
import json
from dataclasses import dataclass

import httpx

from cursor_cloud_mcp.client import CursorCloudClient, read_bounded
from cursor_cloud_mcp.config import EVENT_TEXT_MAX_CHARS, RESULT_DEFAULT_LIMIT, STREAM_MAX_BYTES
from cursor_cloud_mcp.errors import ErrorCode, failure
from cursor_cloud_mcp.models import RunEventView, RunEventsView, WaitRunView
from cursor_cloud_mcp.present import run_view
from cursor_cloud_mcp.validation import require_event_id, require_segment

_KEEP = {"status", "assistant", "tool_call", "thinking", "result", "error", "done"}
_IGNORE = {"heartbeat", "interaction_update"}
_POLL_SECONDS = 5.0


@dataclass
class SseEvent:
    event_id: str | None
    event: str
    data: str


class SseParser:
    """Parseur incrémental. Un événement coupé entre deux morceaux reste en attente."""

    def __init__(self) -> None:
        self._buffer = ""
        self._event_id: str | None = None
        self._event = "message"
        self._data: list[str] = []

    def feed(self, text: str) -> list[SseEvent]:
        self._buffer += text
        found: list[SseEvent] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line == "":
                emitted = self._emit()
                if emitted is not None:
                    found.append(emitted)
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "id":
                self._event_id = value
            elif field == "event":
                self._event = value or "message"
            elif field == "data":
                self._data.append(value)
        return found

    def _emit(self) -> SseEvent | None:
        if not self._data and self._event == "message":
            return None
        event = SseEvent(event_id=self._event_id, event=self._event, data="\n".join(self._data))
        self._event = "message"
        self._data = []
        return event


async def read_run_events(
    client: CursorCloudClient,
    *,
    agent_id: str,
    run_id: str,
    after_event_id: str | None,
    max_wait_seconds: float,
    max_events: int,
    include_thinking: bool,
) -> RunEventsView:
    require_segment(agent_id, label="agent_id")
    require_segment(run_id, label="run_id")
    if after_event_id is not None:
        after_event_id = require_event_id(after_event_id)
    headers = {"Accept": "text/event-stream"}
    if after_event_id is not None:
        headers["Last-Event-ID"] = after_event_id
    path = client.run_stream_path(agent_id, run_id)
    async with client.stream_get(path, headers=headers, deadline=max_wait_seconds) as response:
        if response.status_code in {301, 302, 303, 307, 308}:
            raise failure(ErrorCode.INCOMPATIBLE_RESPONSE, "Redirection du flux refusée.")
        if response.status_code != 200:
            raw = await _read_error_body(response)
            raise client.error_from_response(response, raw)
        retention = _retention(response)
        return await _collect(
            response,
            agent_id=agent_id,
            run_id=run_id,
            max_wait_seconds=max_wait_seconds,
            max_events=max_events,
            include_thinking=include_thinking,
            retention_seconds=retention,
        )


async def wait_run(
    client: CursorCloudClient,
    *,
    agent_id: str,
    run_id: str,
    max_wait_seconds: float,
) -> WaitRunView:
    require_segment(agent_id, label="agent_id")
    require_segment(run_id, label="run_id")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_seconds
    last: WaitRunView | None = None
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            if last is None:
                raise failure(ErrorCode.TIMEOUT, "Délai d'attente dépassé avant la première lecture.")
            return last.model_copy(update={"timed_out": True})
        remote = await client.get_run(agent_id, run_id, deadline=min(client.deadline_seconds, remaining))
        view = run_view(remote, offset=0, limit=RESULT_DEFAULT_LIMIT)
        last = WaitRunView(**view.model_dump(), timed_out=False)
        if view.terminal is True:
            return last
        remaining = deadline - loop.time()
        if remaining < _POLL_SECONDS:
            return last.model_copy(update={"timed_out": True})
        await asyncio.sleep(_POLL_SECONDS)


async def _collect(
    response: httpx.Response,
    *,
    agent_id: str,
    run_id: str,
    max_wait_seconds: float,
    max_events: int,
    include_thinking: bool,
    retention_seconds: int | None,
) -> RunEventsView:
    parser = SseParser()
    events: list[RunEventView] = []
    last_event_id: str | None = None
    run_status: str | None = None
    finished = False
    truncated = False
    total = 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_seconds
    try:
        async with asyncio.timeout_at(deadline):
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if total > STREAM_MAX_BYTES:
                    truncated = True
                    break
                decoded = chunk.decode("utf-8", errors="replace")
                for raw_event in parser.feed(decoded):
                    if raw_event.event_id:
                        last_event_id = raw_event.event_id
                    if raw_event.event in _IGNORE:
                        continue
                    if raw_event.event == "thinking" and not include_thinking:
                        continue
                    if raw_event.event not in _KEEP:
                        continue
                    view = _simplify(raw_event)
                    if view.status is not None and view.kind in {"status", "result", "error"}:
                        run_status = view.status
                    events.append(view)
                    if view.kind in {"result", "error", "done"}:
                        finished = True
                        break
                    if len(events) >= max_events:
                        truncated = True
                        break
                if finished or truncated:
                    break
    except (TimeoutError, httpx.TimeoutException):
        truncated = not finished
    return RunEventsView(
        agent_id=agent_id,
        run_id=run_id,
        events=events,
        last_event_id=last_event_id,
        finished=finished,
        run_status=run_status,
        retention_seconds=retention_seconds,
        truncated=truncated,
    )


def _simplify(event: SseEvent) -> RunEventView:
    payload = _payload(event.data)
    text = _text_of(payload, event.data)
    status = _string_field(payload, "status")
    if event.event == "tool_call":
        return RunEventView(
            event_id=event.event_id,
            kind="tool_call",
            status=status,
            tool_name=_string_field(payload, "name"),
            tool_status=status,
            tool_args=_clip(payload.get("args") if isinstance(payload, dict) else None),
            tool_result=_clip(payload.get("result") if isinstance(payload, dict) else None),
        )
    if event.event == "status":
        return RunEventView(event_id=event.event_id, kind="status", text=text, status=status)
    if event.event == "thinking":
        return RunEventView(event_id=event.event_id, kind="thinking", text=text, status=status)
    if event.event == "result":
        return RunEventView(event_id=event.event_id, kind="result", text=text, status=status)
    if event.event == "error":
        return RunEventView(event_id=event.event_id, kind="error", text=text, status=status)
    if event.event == "done":
        return RunEventView(event_id=event.event_id, kind="done", text=text, status=status)
    return RunEventView(event_id=event.event_id, kind="assistant", text=text, status=status)


def _payload(data: str) -> object:
    if not data:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return data


def _text_of(payload: object, raw: str) -> str | None:
    if isinstance(payload, str):
        return _clip(payload)
    if isinstance(payload, dict):
        for key in ("text", "message", "delta"):
            value = payload.get(key)
            if isinstance(value, str):
                return _clip(value)
        if payload:
            return _clip(payload)
    if raw:
        return _clip(raw)
    return None


def _string_field(payload: object, key: str) -> str | None:
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


def _clip(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    if len(text) > EVENT_TEXT_MAX_CHARS:
        return text[:EVENT_TEXT_MAX_CHARS] + "…"
    return text


def _retention(response: httpx.Response) -> int | None:
    raw = response.headers.get("x-cursor-stream-retention-seconds")
    if raw is None or not raw.isdigit():
        return None
    return int(raw)


async def _read_error_body(response: httpx.Response) -> bytes:
    try:
        return await read_bounded(response, 64_000)
    except Exception:
        return b""
