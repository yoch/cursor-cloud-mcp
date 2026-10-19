"""Local journal of the models this MCP sent: the API never returns the model of an agent or a run.

One JSON line per creation or follow-up that set a model, appended to ``models.jsonl`` in the
state directory. Several server processes may append at once (one write per line, O_APPEND).
Only what this MCP sent from this machine is known: a change made elsewhere is not seen.
"""

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass

from cursor_cloud_mcp.models import ModelSelectionView

logger = logging.getLogger(__name__)

_FILE = "models.jsonl"


@dataclass(frozen=True)
class _Entry:
    # The run's createdAt as Cursor returned it, else the local write time (an unconfirmed send):
    # inheritance is decided on Cursor's clock whenever it is known.
    at: dt.datetime
    recorded_at: str
    agent_id: str
    run_id: str | None
    model: ModelSelectionView


class ModelJournal:
    """Append-only record of model selections. ``directory=None`` disables it (reads find nothing)."""

    def __init__(self, directory: str | None) -> None:
        self._path = None if directory is None else os.path.join(directory, _FILE)
        self._stamp: tuple[float, int] | None = None
        self._entries: list[_Entry] = []

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def record(
        self,
        *,
        agent_id: str,
        run_id: str | None,
        run_created_at: str | None,
        model: ModelSelectionView,
        confirmed: bool = True,
    ) -> None:
        """Never raises: the paid call it describes has already been sent."""
        if self._path is None:
            return
        line: dict[str, object] = {
            "ts": _now(),
            "agent_id": agent_id,
            "run_id": run_id,
            "model": model.model_dump(exclude_none=True, exclude={"recorded_at", "inherited", "unconfirmed"}),
        }
        if run_created_at is not None:
            line["run_created_at"] = run_created_at
        if not confirmed:
            line["unconfirmed"] = True
        data = (json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        try:
            os.makedirs(os.path.dirname(self._path), mode=0o700, exist_ok=True)
            handle = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(handle, data)
            finally:
                os.close(handle)
        except OSError as exc:
            logger.warning("model_journal_write_failed=%s", type(exc).__name__)

    def for_agent(self, agent_id: str) -> ModelSelectionView | None:
        """The last model recorded for the agent: the one its next run without model_id keeps."""
        found = [entry for entry in self._load() if entry.agent_id == agent_id]
        return None if not found else _view(found[-1], inherited=False)

    def for_run(self, agent_id: str, run_id: str, created_at: str | None) -> ModelSelectionView | None:
        """The model the run was sent with, else the last one recorded for its agent before it started."""
        entries = [entry for entry in self._load() if entry.agent_id == agent_id]
        exact = [entry for entry in entries if entry.run_id == run_id]
        if exact:
            return _view(exact[-1], inherited=False)
        started = _parse(created_at)
        if started is None:
            return None
        earlier = [entry for entry in entries if entry.at <= started]
        # Latest by Cursor's time; among equal times, the last written.
        return None if not earlier else _view(max(reversed(earlier), key=lambda entry: entry.at), inherited=True)

    def has_agent(self, agent_id: str) -> bool:
        return any(entry.agent_id == agent_id for entry in self._load())

    def _load(self) -> list[_Entry]:
        if self._path is None:
            return []
        try:
            stat = os.stat(self._path)
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("model_journal_read_failed=%s", type(exc).__name__)
            return []
        stamp = (stat.st_mtime, stat.st_size)
        if stamp == self._stamp:
            return self._entries
        try:
            with open(self._path, encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError as exc:
            logger.warning("model_journal_read_failed=%s", type(exc).__name__)
            return []
        self._entries = [entry for entry in (_entry(line) for line in lines) if entry is not None]
        self._stamp = stamp
        return self._entries


def _entry(line: str) -> _Entry | None:
    """A damaged or foreign line is skipped, never fatal."""
    try:
        raw = json.loads(line)
        at = _parse(raw.get("run_created_at")) or _parse(raw["ts"])
        model = ModelSelectionView.model_validate(raw["model"])
        if raw.get("unconfirmed") is True:
            model = model.model_copy(update={"unconfirmed": True})
        agent_id = raw["agent_id"]
        run_id = raw.get("run_id")
    except (ValueError, KeyError, TypeError):
        return None
    if at is None or not isinstance(agent_id, str) or not (run_id is None or isinstance(run_id, str)):
        return None
    return _Entry(at=at, recorded_at=raw["ts"], agent_id=agent_id, run_id=run_id, model=model)


def _view(entry: _Entry, *, inherited: bool) -> ModelSelectionView:
    return entry.model.model_copy(
        update={"source": "local_journal", "recorded_at": entry.recorded_at, "inherited": inherited or None}
    )


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)
