"""Search provider factory."""

from __future__ import annotations

from app.config import Settings, settings
from integrations.search.contracts import SearchProvider
from integrations.search.gemini_provider import GeminiSearchProvider


def build_search_provider(runtime_settings: Settings | None = None) -> SearchProvider | None:
    """Return the configured source-backed search provider, if any."""

    resolved = runtime_settings or settings
    provider = (resolved.search_provider or "").strip().lower()
    if not resolved.search_enabled and not provider:
        return None

    if provider in {"", "gemini"}:
        return GeminiSearchProvider(runtime_settings=resolved)

    return None
