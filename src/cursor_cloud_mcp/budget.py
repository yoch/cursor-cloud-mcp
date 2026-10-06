"""Absolute budget of a tool call. Sub-operations receive the remaining time."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_DEADLINE: ContextVar[float | None] = ContextVar("cursor_mcp_tool_deadline", default=None)


@contextmanager
def tool_budget(seconds: float) -> Iterator[None]:
    """Set the deadline of the current call. A nested budget can only shorten it."""
    now = asyncio.get_running_loop().time()
    current = _DEADLINE.get()
    wanted = now + seconds
    token = _DEADLINE.set(wanted if current is None else min(current, wanted))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def deadline_at(cap_seconds: float) -> float:
    """Absolute deadline: the nearer of ``cap_seconds`` and the tool budget."""
    now = asyncio.get_running_loop().time()
    local = now + cap_seconds
    budget = _DEADLINE.get()
    return local if budget is None else min(local, budget)


def remaining(cap_seconds: float) -> float:
    """Remaining time, capped by ``cap_seconds``. May be negative or zero."""
    return deadline_at(cap_seconds) - asyncio.get_running_loop().time()
