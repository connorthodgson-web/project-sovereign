"""Research-focused agent implementation."""

from __future__ import annotations

from agents.base_agent import BaseAgent
from core.models import AgentExecutionStatus, AgentResult, SubTask, Task, ToolEvidence
from integrations.search.contracts import SearchProvider, SearchProviderError, SearchRequest
from integrations.search.provider import build_search_provider


class ResearchAgent(BaseAgent):
    """Handles web research, retrieval, and information synthesis tasks."""

    name = "research_agent"
    supported_tool_names = frozenset({"web_search_tool"})

    def __init__(self, *, search_provider: SearchProvider | None = None) -> None:
        self.search_provider = search_provider

    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        provider = self.search_provider or build_search_provider()
        if provider is None or not provider.is_configured():
            return self._missing_provider_result(task, subtask)

        query = self._build_search_query(task, subtask)
        try:
            search_result = provider.search(SearchRequest(query=query))
        except SearchProviderError as exc:
            return self._blocked_search_result(task, subtask, query=query, blocker=str(exc))

        if not search_result.has_source_evidence:
            return self._blocked_search_result(
                task,
                subtask,
                query=query,
                blocker="Search completed without source titles and URLs, so it cannot count as finished research.",
                provider=search_result.provider,
                payload=search_result.model_dump(),
            )

        sources = [source.model_dump() for source in search_result.sources]
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=AgentExecutionStatus.COMPLETED,
            summary=search_result.answer,
            tool_name="web_search_tool",
            details=[
                f"Goal context: {task.goal}",
                f"Research objective: {subtask.objective}",
                f"Search query: {search_result.query}",
                f"Provider: {search_result.provider}",
                f"Timestamp: {search_result.timestamp}",
                f"Sources: {'; '.join(f'{source.title} ({source.url})' for source in search_result.sources)}",
            ],
            artifacts=[f"research:evidence:{task.id}"],
            evidence=[
                ToolEvidence(
                    tool_name="web_search_tool",
                    summary=search_result.answer,
                    payload={
                        "query": search_result.query,
                        "provider": search_result.provider,
                        "answer": search_result.answer,
                        "sources": sources,
                        "timestamp": search_result.timestamp,
                        "raw_metadata": search_result.raw_metadata,
                    },
                )
            ],
        )

    def _missing_provider_result(self, task: Task, subtask: SubTask) -> AgentResult:
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=AgentExecutionStatus.BLOCKED,
            summary="Source-backed research is not configured yet.",
            tool_name="web_search_tool",
            details=[
                f"Goal context: {task.goal}",
                f"Research objective: {subtask.objective}",
                "Search needs a configured provider before research can count as complete.",
                "Set SEARCH_PROVIDER=gemini and OPENROUTER_API_KEY in the runtime environment.",
            ],
            blockers=[
                "No source-backed search provider is configured. Set SEARCH_PROVIDER=gemini and OPENROUTER_API_KEY, then retry."
            ],
            next_actions=[
                "Configure a search provider through environment variables; do not store API keys in memory."
            ],
        )

    def _blocked_search_result(
        self,
        task: Task,
        subtask: SubTask,
        *,
        query: str,
        blocker: str,
        provider: str = "unknown",
        payload: dict[str, object] | None = None,
    ) -> AgentResult:
        evidence_payload = {
            "query": query,
            "provider": provider,
            "answer": "",
            "sources": [],
            "timestamp": "",
        }
        if payload:
            evidence_payload.update(payload)
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=AgentExecutionStatus.BLOCKED,
            summary="Source-backed research could not be completed.",
            tool_name="web_search_tool",
            details=[
                f"Goal context: {task.goal}",
                f"Research objective: {subtask.objective}",
                f"Search query: {query}",
                blocker,
            ],
            evidence=[
                ToolEvidence(
                    tool_name="web_search_tool",
                    summary=blocker,
                    payload=evidence_payload,
                )
            ],
            blockers=[blocker],
            next_actions=["Retry with a configured provider that returns source titles and URLs."],
        )

    def _build_search_query(self, task: Task, subtask: SubTask) -> str:
        if subtask.tool_invocation and subtask.tool_invocation.tool_name == "web_search_tool":
            query = (
                subtask.tool_invocation.parameters.get("query")
                or subtask.tool_invocation.parameters.get("objective")
                or ""
            ).strip()
            if query:
                return query
        objective = subtask.objective.strip()
        if objective and objective != task.goal:
            return f"{objective}\n\nOriginal user goal: {task.goal}"
        return task.goal.strip()
