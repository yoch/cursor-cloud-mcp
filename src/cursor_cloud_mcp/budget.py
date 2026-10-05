"""Budget absolu d'un appel d'outil. Les sous-opérations reçoivent le temps restant."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_DEADLINE: ContextVar[float | None] = ContextVar("cursor_mcp_tool_deadline", default=None)


@contextmanager
def tool_budget(seconds: float) -> Iterator[None]:
    """Fixe l'échéance de l'appel en cours. Un budget imbriqué ne peut que la raccourcir."""
    now = asyncio.get_running_loop().time()
    current = _DEADLINE.get()
    wanted = now + seconds
    token = _DEADLINE.set(wanted if current is None else min(current, wanted))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def deadline_at(cap_seconds: float) -> float:
    """Échéance absolue : la plus proche entre ``cap_seconds`` et le budget de l'outil."""
    now = asyncio.get_running_loop().time()
    local = now + cap_seconds
    budget = _DEADLINE.get()
    return local if budget is None else min(local, budget)


def remaining(cap_seconds: float) -> float:
    """Temps restant, borné par ``cap_seconds``. Peut être négatif ou nul."""
    return deadline_at(cap_seconds) - asyncio.get_running_loop().time()
