"""Shared inbound transport path for all user-facing Sovereign clients."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from core.browser_requests import normalize_transport_text
from core.interaction_context import InteractionContext, bind_interaction_context
from core.models import ChatResponse
from core.supervisor import supervisor


TransportKind = Literal["dashboard", "slack", "ios", "local"]


@dataclass(frozen=True)
class OperatorMessage:
    """Normalized message envelope passed from transports into the CEO loop."""

    message: str
    transport: TransportKind = "local"
    channel_id: str | None = None
    user_id: str | None = None


def normalize_operator_message_text(message: str) -> str:
    """Apply transport-agnostic text normalization before the operator sees input."""

    return " ".join(normalize_transport_text(message).split())


def handle_operator_message(
    message: OperatorMessage,
    *,
    backend_handler: Callable[[str], ChatResponse] | None = None,
) -> ChatResponse:
    """Run one user message through the canonical CEO/operator entrypoint."""

    normalized = normalize_operator_message_text(message.message)
    handler = backend_handler or supervisor.handle_user_goal
    context = InteractionContext(
        source=message.transport,
        channel_id=message.channel_id,
        user_id=message.user_id,
    )
    with bind_interaction_context(context):
        return handler(normalized)
