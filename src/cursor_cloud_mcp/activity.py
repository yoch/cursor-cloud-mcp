"""Activity summary of a run, built from its event stream.

The run object says little: a RUNNING run keeps the ``updatedAt`` of its creation, an ERROR run
carries neither ``error`` nor ``result``, and FINISHED only means the agent ended its turn. The
stream says more: event ids are millisecond timestamps (liveness), and tool calls show the last
command and the background tasks the agent started and awaited. Everything here is the last
*observed* state, never a guarantee about the VM.
"""

import datetime as dt
import json
import re
from typing import Any

from cursor_cloud_mcp.config import ACTIVITY_TEXT_MAX_CHARS, EVENT_TEXT_MAX_CHARS
from cursor_cloud_mcp.models import ActivityView, BackgroundTaskView, RunEventView, ToolCallSummaryView

# Stream ids look like Redis stream ids: "<milliseconds>-<sequence>" (checked 2026-10-06).
_EVENT_ID = re.compile(r"^(\d{12,14})-\d+$")
# Bounds a parsed timestamp must fall within to be trusted: 2020-01-01 to 2100-01-01.
_MIN_MS = 1_577_836_800_000
_MAX_MS = 4_102_444_800_000
_MAX_TASKS = 20


def event_time(event_id: str | None) -> dt.datetime | None:
    """UTC time encoded in an event id, or None if the id does not have the expected shape."""
    if event_id is None:
        return None
    match = _EVENT_ID.match(event_id)
    if match is None:
        return None
    ms = int(match.group(1))
    if not _MIN_MS <= ms <= _MAX_MS:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC)


def iso(moment: dt.datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ActivityTracker:
    """Fed with every stream event, in order; keeps only what the summary needs."""

    def __init__(self) -> None:
        self.last_event_id: str | None = None
        self.scanned = 0
        self._assistant: list[str] = []
        self._assistant_open = False
        self._last_assistant: str | None = None
        self._last_tool: RunEventView | None = None
        self._tasks: dict[str, dict[str, Any]] = {}

    def feed(self, event_id: str | None, kind: str, payload: object, view: RunEventView | None) -> None:
        if event_id:
            self.last_event_id = event_id
        if kind in {"heartbeat", "interaction_update"}:
            return
        self.scanned += 1
        if kind == "assistant":
            text = view.text if view is not None else None
            if not self._assistant_open:
                self._assistant = []
                self._assistant_open = True
            if text:
                self._assistant.append(text)
            return
        if self._assistant_open:
            self._close_assistant()
        if kind == "tool_call" and view is not None:
            self._last_tool = view
            self._track_task(event_id, _tool_payload(payload))

    def summary(self, *, run_terminal: bool | None, now: dt.datetime | None = None) -> ActivityView:
        if self._assistant_open:
            self._close_assistant()
        now = now or dt.datetime.now(dt.UTC)
        last_at = event_time(self.last_event_id)
        tasks = [
            BackgroundTaskView(
                task_id=task_id,
                command=task.get("command"),
                last_state=task.get("state", "unknown"),
                runtime_ms=task.get("runtime_ms"),
                observed_at=iso(event_time(task.get("event_id"))),
            )
            for task_id, task in list(self._tasks.items())[-_MAX_TASKS:]
        ]
        running = sum(1 for task in tasks if task.last_state == "running")
        tool = self._last_tool
        return ActivityView(
            last_event_id=self.last_event_id,
            last_event_at=iso(last_at),
            idle_seconds=None if last_at is None else max(0, int((now - last_at).total_seconds())),
            last_assistant_text=self._last_assistant,
            last_tool_call=None
            if tool is None
            else ToolCallSummaryView(
                name=tool.tool_name,
                status=tool.tool_status,
                args=tool.tool_args,
                result=tool.tool_result,
            ),
            background_tasks=tasks or None,
            unfinished_background_tasks=running if tasks else None,
            run_terminal=run_terminal,
            scanned_events=self.scanned,
        )

    def _close_assistant(self) -> None:
        text = "".join(self._assistant).strip()
        if text:
            # The end of a long message is what tells where the agent stands.
            self._last_assistant = text if len(text) <= ACTIVITY_TEXT_MAX_CHARS else "…" + text[-ACTIVITY_TEXT_MAX_CHARS:]
        self._assistant_open = False

    def _track_task(self, event_id: str | None, payload: dict[str, Any]) -> None:
        """Background launches (run_terminal_cmd) and their observed states (await)."""
        if payload.get("status") != "completed":
            return
        name = payload.get("name")
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        success = result.get("success") if isinstance(result.get("success"), dict) else {}
        if name == "run_terminal_cmd" and (args.get("isBackground") is True or result.get("isBackground") is True):
            task_id = success.get("shellId")
            if task_id is None:
                return
            command = success.get("command") or args.get("command")
            self._tasks[str(task_id)] = {
                "command": _short(command),
                "state": "running",
                "runtime_ms": None,
                "event_id": event_id,
            }
            return
        if name == "await":
            for state, key in (("running", "stillRunning"), ("complete", "complete")):
                observed = success.get(key)
                if isinstance(observed, dict) and observed.get("taskId") is not None:
                    task_id = str(observed["taskId"])
                    task = self._tasks.setdefault(task_id, {"command": None})
                    task.update(state=state, runtime_ms=_int(observed.get("runtimeMs")), event_id=event_id)
                    # Keep insertion order meaningful: the most recently observed task comes last.
                    self._tasks[task_id] = self._tasks.pop(task_id)
                    return


def _tool_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if "name" not in payload and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def _short(value: object) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= EVENT_TEXT_MAX_CHARS else text[:EVENT_TEXT_MAX_CHARS] + "…"


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
