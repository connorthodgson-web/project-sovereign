"""Shared safety checks for durable memory providers."""

from __future__ import annotations

import re


MEMORY_SECRET_PATTERN = re.compile(
    r"("
    r"password|passwd|secret|client[_ -]?secret|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|"
    r"token\s*(?:is|=|:)|bearer\s+[a-z0-9_\-\.]+|"
    r"sk-[a-z0-9_\-]{8,}|xox[baprs]-[a-z0-9\-]{8,}|gh[pousr]_[a-z0-9_]{16,}|"
    r"AIza[0-9A-Za-z_\-]{16,}|[a-f0-9]{32,}"
    r")",
    re.IGNORECASE,
)


def looks_secret_like(text: str) -> bool:
    """Return whether text looks like a credential that must stay out of memory."""

    return bool(MEMORY_SECRET_PATTERN.search(text))
