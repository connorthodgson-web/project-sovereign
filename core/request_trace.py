"""Per-request tracing for latency and path visibility."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter


@dataclass
class RequestTrace:
    started_at: float = field(default_factory=perf_counter)
    assistant_path: str | None = None
    openrouter_calls: int = 0
    openrouter_labels: list[str] = field(default_factory=list)
    model_selections: list[str] = field(default_factory=list)
    escalation_events: list[str] = field(default_factory=list)
    memory_reads: list[str] = field(default_factory=list)
    memory_writes: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)

    def record_openrouter(self, label: str | None = None) -> None:
        self.openrouter_calls += 1
        if label:
            self._append_unique(self.openrouter_labels, label)

    def record_memory_read(self, operation: str) -> None:
        self._append_unique(self.memory_reads, operation)

    def record_memory_write(self, operation: str) -> None:
        self._append_unique(self.memory_writes, operation)

    def record_model_selection(self, selection: str) -> None:
        self._append_unique(self.model_selections, selection)

    def record_escalation(self, event: str) -> None:
        self._append_unique(self.escalation_events, event)

    def set_path(self, path: str) -> None:
        self.assistant_path = path

    def set_metadata(self, key: str, value: str) -> None:
        self.metadata[key] = value

    def total_latency_ms(self) -> int:
        return int((perf_counter() - self.started_at) * 1000)

    def _append_unique(self, values: list[str], value: str) -> None:
        if value not in values:
            values.append(value)


_CURRENT_TRACE: ContextVar[RequestTrace | None] = ContextVar("request_trace", default=None)


def current_request_trace() -> RequestTrace | None:
    return _CURRENT_TRACE.get()


@contextmanager
def request_trace() -> RequestTrace:
    trace = RequestTrace()
    token = _CURRENT_TRACE.set(trace)
    try:
        yield trace
    finally:
        _CURRENT_TRACE.reset(token)
