"""Adapter for source-backed web search providers."""

from core.models import ToolInvocation
from integrations.search.contracts import SearchProvider, SearchProviderError, SearchRequest
from integrations.search.provider import build_search_provider
from tools.base_tool import BaseTool


class WebSearchTool(BaseTool):
    """Wraps a search provider or search API for research-oriented tasks."""

    name = "web_search_tool"

    def __init__(self, *, search_provider: SearchProvider | None = None) -> None:
        self.search_provider = search_provider

    def supports(self, invocation: ToolInvocation) -> bool:
        return invocation.tool_name == self.name and invocation.action in {"search", "research"}

    def execute(self, invocation: ToolInvocation) -> dict:
        provider = self.search_provider or build_search_provider()
        query = invocation.parameters.get("query") or invocation.parameters.get("objective") or ""
        query = query.strip()
        if not query:
            return {
                "success": False,
                "summary": "Search needs a non-empty query.",
                "error": "Missing search query.",
                "payload": {"query": query, "provider": "none", "sources": []},
            }
        if provider is None or not provider.is_configured():
            return {
                "success": False,
                "summary": "Source-backed search is not configured.",
                "error": "Set SEARCH_PROVIDER=gemini and OPENROUTER_API_KEY before using web_search_tool.",
                "payload": {"query": query, "provider": "none", "sources": []},
            }
        try:
            result = provider.search(SearchRequest(query=query))
        except SearchProviderError as exc:
            return {
                "success": False,
                "summary": "Source-backed search failed.",
                "error": str(exc),
                "payload": {"query": query, "provider": provider.provider_name, "sources": []},
            }
        return {
            "success": result.has_source_evidence,
            "summary": result.answer,
            "error": None if result.has_source_evidence else "Search returned no source evidence.",
            "payload": result.model_dump(),
        }
