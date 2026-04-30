"""Gemini-backed search provider through OpenRouter."""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from app.config import Settings, settings
from integrations.search.contracts import (
    SearchProviderError,
    SearchRequest,
    SearchResult,
    SearchSource,
)


_DEFAULT_GEMINI_SEARCH_MODEL = "google/gemini-2.5-flash"
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


class GeminiSearchProvider:
    """Source-backed research via Gemini on OpenRouter web search."""

    provider_name = "gemini"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_seconds: float | None = None,
        runtime_settings: Settings | None = None,
    ) -> None:
        resolved = runtime_settings or settings
        self.api_key = api_key if api_key is not None else resolved.openrouter_api_key
        self.model = model or resolved.gemini_search_model or _DEFAULT_GEMINI_SEARCH_MODEL
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else float(resolved.search_timeout_seconds)
        )

    def is_configured(self) -> bool:
        return bool(self.api_key and self.model and self.base_url)

    def search(self, request: SearchRequest) -> SearchResult:
        if not self.is_configured():
            raise SearchProviderError(
                "Gemini search is not configured. Set OPENROUTER_API_KEY before retrying."
            )

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are Project Sovereign's source-backed research provider. "
                        "Use OpenRouter web search for current or factual claims. "
                        "Answer concisely, cite source URLs inline when possible, and do not "
                        "claim completion if current sources are unavailable."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Research this with up-to-date web evidence. Include source titles "
                        "and URLs whenever the search tool provides them.\n\n"
                        f"Query: {request.query}"
                    ),
                },
            ],
            "tools": [
                {
                    "type": "openrouter:web_search",
                    "parameters": {
                        "engine": "auto",
                        "max_results": request.max_results,
                        "max_total_results": request.max_results,
                        "search_context_size": "medium",
                    },
                }
            ],
            "max_tokens": 900,
            "temperature": 0.2,
        }
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://project-sovereign.local",
                    "X-Title": "Project Sovereign",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SearchProviderError(f"Gemini search request failed: {exc}") from exc

        data = response.json()
        answer = self._extract_answer(data)
        sources = self._extract_sources(data, answer=answer, max_results=request.max_results)
        return SearchResult(
            query=request.query,
            provider=self.provider_name,
            answer=answer,
            sources=sources,
            raw_metadata={
                "model": data.get("model"),
                "id": data.get("id"),
                "usage": data.get("usage", {}),
            },
        )

    def _extract_message(self, data: Mapping[str, Any]) -> Mapping[str, Any]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return {}
        first = choices[0]
        if not isinstance(first, Mapping):
            return {}
        message = first.get("message")
        return message if isinstance(message, Mapping) else {}

    def _extract_answer(self, data: Mapping[str, Any]) -> str:
        return str(self._extract_message(data).get("content", "")).strip()

    def _extract_sources(
        self,
        data: Mapping[str, Any],
        *,
        answer: str,
        max_results: int,
    ) -> list[SearchSource]:
        sources: list[SearchSource] = []
        seen_urls: set[str] = set()

        message = self._extract_message(data)
        annotations = message.get("annotations")
        if isinstance(annotations, list):
            for item in annotations:
                if not isinstance(item, Mapping):
                    continue
                citation = item.get("url_citation")
                if not isinstance(citation, Mapping):
                    continue
                source = self._source_from_citation(citation)
                if source is None or source.url in seen_urls:
                    continue
                sources.append(source)
                seen_urls.add(source.url)
                if len(sources) >= max_results:
                    return sources

        for title, url in _MARKDOWN_LINK_RE.findall(answer):
            clean_url = url.rstrip(".,;")
            if clean_url in seen_urls:
                continue
            sources.append(SearchSource(title=title.strip() or self._title_from_url(clean_url), url=clean_url))
            seen_urls.add(clean_url)
            if len(sources) >= max_results:
                break

        return sources

    def _source_from_citation(self, citation: Mapping[str, Any]) -> SearchSource | None:
        url = str(citation.get("url", "")).strip()
        if not url:
            return None
        title = str(citation.get("title", "")).strip() or self._title_from_url(url)
        snippet = str(citation.get("content", "")).strip() or None
        return SearchSource(title=title, url=url, snippet=snippet)

    def _title_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        return parsed.netloc or url
