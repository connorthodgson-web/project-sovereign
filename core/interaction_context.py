"""Per-request interaction context shared across transport and operator layers."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class InteractionContext:
    """Transport metadata needed for follow-up delivery and honest runtime behavior."""

    source: str = "local"
    channel_id: str | None = None
    user_id: str | None = None


_interaction_context: ContextVar[InteractionContext | None] = ContextVar(
    "project_sovereign_interaction_context",
    default=None,
)


def get_interaction_context() -> InteractionContext | None:
    """Return the current interaction context when one exists."""

    return _interaction_context.get()


@contextmanager
def bind_interaction_context(context: InteractionContext) -> Iterator[None]:
    """Temporarily bind transport metadata during a backend request."""

    token = _interaction_context.set(context)
    try:
        yield
    finally:
        _interaction_context.reset(token)
