"""Task routing for specialized agents."""

from __future__ import annotations

import json

import httpx

from agents.adapter import LocalAgentAdapter, ManagedAgentStubAdapter
from agents.registry import AgentRegistry
from agents.catalog import AgentCatalog, build_agent_catalog
from agents.browser_agent import BrowserAgent
from agents.coding_agent import CodingAgent
from agents.codex_cli_agent import CodexCliAgentAdapter
from agents.communications_agent import CommunicationsAgent
from agents.memory_agent import MemoryAgent
from agents.personal_ops_agent import PersonalOpsAgent
from agents.planner_agent import PlannerAgent
from agents.reminder_agent import ReminderSchedulerAgent
from agents.scheduling_agent import SchedulingPersonalOpsAgent
from agents.research_agent import ResearchAgent
from agents.reviewer_agent import ReviewerAgent
from agents.verifier_agent import VerifierAgent
from core.context_assembly import ContextAssembler
from core.logging import get_logger
from core.model_routing import ModelRequestContext
from core.models import AgentDescriptor, AgentExecutionStatus, AgentProvider, AgentResult, RoutingDecision, SubTask, Task, TaskStatus
from core.personal_ops_intent import looks_like_personal_ops_request
from core.planner import Planner
from core.evaluator import GoalEvaluator
from integrations.openrouter_client import OpenRouterClient
from tools.registry import ToolRegistry, build_default_tool_registry


class Router:
    """Assign subtasks to the most appropriate specialized agent."""

    def __init__(
        self,
        *,
        openrouter_client: OpenRouterClient | None = None,
        tool_registry: ToolRegistry | None = None,
        context_assembler: ContextAssembler | None = None,
        agent_catalog: AgentCatalog | None = None,
        reminder_agent: ReminderSchedulerAgent | None = None,
        agent_registry: AgentRegistry | None = None,
    ) -> None:
        self.logger = get_logger(__name__)
        self.openrouter_client = openrouter_client or OpenRouterClient()
        self.tool_registry = tool_registry or build_default_tool_registry()
        self.agent_catalog = agent_catalog or build_agent_catalog()
        self.context_assembler = context_assembler or ContextAssembler(
            tool_registry=self.tool_registry,
            agent_catalog=self.agent_catalog,
        )
        self.agent_registry = agent_registry or self._build_agent_registry(
            reminder_agent=reminder_agent
        )

    def assign_agent(self, subtask: SubTask) -> RoutingDecision:
        if subtask.assigned_agent and self.agent_registry.get(subtask.assigned_agent) is not None:
            return RoutingDecision(
                agent_name=subtask.assigned_agent,
                strategy="explicit",
                reasoning="Planner provided a supported explicit agent assignment.",
            )

        llm_decision = self._classify_with_llm(subtask)
        if llm_decision is not None:
            return llm_decision

        return self._classify_deterministically(subtask)

    def _classify_with_llm(self, subtask: SubTask) -> RoutingDecision | None:
        if not self.openrouter_client.is_configured():
            return None

        prompt = (
            f"{self.context_assembler.build('router', goal=subtask.objective).to_prompt_block()}\n"
            "Choose the best agent for the subtask from this fixed set only:\n"
            f"{', '.join(self.available_agents())}\n"
            "Return strict JSON with the shape "
            '{"agent_name":"coding_agent","reasoning":"..."}.'
            "\nPrefer the narrowest currently supported agent and do not invent new agents.\n"
            "Treat scaffolded capabilities honestly. If the work targets a scaffolded tool, choose the agent that should own the blocked or planned path.\n"
            f"Title: {subtask.title}\n"
            f"Description: {subtask.description}\n"
            f"Objective: {subtask.objective}\n"
            f"Tool invocation: {subtask.tool_invocation.model_dump() if subtask.tool_invocation else None}"
        )

        try:
            response = self.openrouter_client.prompt(
                prompt,
                system_prompt=(
                    "You are a bounded router. Return only valid JSON and choose exactly one supported agent."
                ),
                label="router_agent_select",
                context=ModelRequestContext(
                    intent_label="routing",
                    request_mode="execute",
                    selected_lane="execution_flow",
                    selected_agent="router",
                    task_complexity="medium",
                    risk_level="medium",
                    requires_tool_use=subtask.tool_invocation is not None,
                    requires_review=False,
                    evidence_quality="unknown",
                    user_visible_latency_sensitivity="medium",
                    cost_sensitivity="medium",
                ),
            )
            payload = json.loads(response)
            agent_name = str(payload.get("agent_name", "")).strip()
            reasoning = str(payload.get("reasoning", "")).strip()
            if self.agent_registry.get(agent_name) is None or not reasoning:
                return None
            return RoutingDecision(
                agent_name=agent_name,
                strategy="openrouter",
                reasoning=reasoning,
            )
        except (RuntimeError, httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError):
            return None

    def _classify_deterministically(self, subtask: SubTask) -> RoutingDecision:
        if subtask.tool_invocation is not None:
            if subtask.tool_invocation.tool_name == "browser_tool":
                return RoutingDecision(
                    agent_name="browser_agent",
                    strategy="structured_fallback",
                    reasoning="Structured browser tool invocation should execute through the browser agent.",
                )
            if subtask.tool_invocation.tool_name == "web_search_tool":
                return RoutingDecision(
                    agent_name="research_agent",
                    strategy="structured_fallback",
                    reasoning="Structured source-backed search invocations should execute through the research agent.",
                )
            if subtask.tool_invocation.tool_name in {"file_tool", "runtime_tool"}:
                return RoutingDecision(
                    agent_name="coding_agent",
                    strategy="structured_fallback",
                    reasoning="Structured tool invocation targets a coding-agent-supported tool.",
                )
        lowered = f"{subtask.title} {subtask.description} {subtask.objective}".lower()
        if looks_like_personal_ops_request(lowered):
            return RoutingDecision(
                agent_name="personal_ops_agent",
                strategy="personal_ops_fallback",
                reasoning="Personal list/note and routine work is owned by the unified Personal Ops Agent.",
            )
        if any(token in lowered for token in ("remember", "memory", "recall", "stored preference", "saved preference", "prior context")):
            return RoutingDecision(
                agent_name="memory_agent",
                strategy="deterministic",
                reasoning="Memory and context recall work is owned by the Memory Agent.",
            )
        if any(token in lowered for token in ("review", "verify", "critique", "quality", "correctness")):
            return RoutingDecision(
                agent_name="reviewer_agent",
                strategy="deterministic",
                reasoning="Review and verification work is owned by the Reviewer Agent.",
            )
        if any(token in lowered for token in ("remind me", "reminder", "calendar", "event", "appointment", "meeting", "google tasks", "task list", "to-do", "todo")):
            return RoutingDecision(
                agent_name="scheduling_agent",
                strategy="scheduling_fallback",
                reasoning="Scheduling, calendar, reminders, and Google Tasks work is owned by the Scheduling / Personal Ops Agent.",
            )
        return RoutingDecision(
            agent_name="research_agent",
            strategy="safe_fallback",
            reasoning=(
                "LLM routing was unavailable and no structured tool invocation was attached, "
                "so the conservative fallback is interpretation-first research."
            ),
        )

    def available_agents(self) -> list[str]:
        excluded = {"planner_agent", "verifier_agent"}
        return sorted(
            adapter.agent_id
            for adapter in self.agent_registry.list_agents()
            if adapter.agent_id not in excluded
        )

    def route_subtask(self, task: Task, subtask: SubTask) -> tuple[SubTask, AgentResult]:
        decision = self.assign_agent(subtask)
        adapter = self.agent_registry.get(decision.agent_name)
        agent_name = adapter.agent_id if adapter is not None else decision.agent_name
        subtask.assigned_agent = agent_name
        subtask.status = TaskStatus.ROUTED
        subtask.notes.append(f"Routed to {agent_name}.")
        subtask.notes.append(
            f"Routing decision ({decision.strategy}): {decision.reasoning}"
        )
        self.logger.info(
            "ROUTER_EXECUTE task=%s subtask=%s agent=%s strategy=%s tool=%s action=%s",
            task.id,
            subtask.id,
            agent_name,
            decision.strategy,
            subtask.tool_invocation.tool_name if subtask.tool_invocation else None,
            subtask.tool_invocation.action if subtask.tool_invocation else None,
        )
        self.logger.info(
            "TOOL_SELECTED agent=%s tool=%s action=%s",
            agent_name,
            subtask.tool_invocation.tool_name if subtask.tool_invocation else None,
            subtask.tool_invocation.action if subtask.tool_invocation else None,
        )
        self.logger.info(
            "AGENT_SELECTED task=%s subtask=%s agent=%s provider=%s",
            task.id,
            subtask.id,
            agent_name,
            adapter.descriptor.provider.value if adapter is not None else "unknown",
        )
        if adapter is None:
            result = AgentResult(
                subtask_id=subtask.id,
                agent=agent_name,
                status=AgentExecutionStatus.BLOCKED,
                summary=f"No agent adapter is registered for {agent_name}.",
                blockers=[f"Missing adapter registration for {agent_name}."],
                next_actions=["Register an agent adapter that can execute this subtask."],
            )
        else:
            result = adapter.run(task, subtask)

        if result.status == AgentExecutionStatus.COMPLETED:
            subtask.status = TaskStatus.COMPLETED
            subtask.notes.append("Agent marked this subtask completed.")
        elif result.status == AgentExecutionStatus.BLOCKED:
            subtask.status = TaskStatus.BLOCKED
            subtask.notes.append("Agent reported an execution blocker.")
        else:
            subtask.status = TaskStatus.RUNNING
            subtask.notes.append(
                f"Agent returned {result.status.value}; work was prepared but not fully executed."
            )

        if result.evidence:
            self.logger.info(
                "TOOL_OR_MANAGED_AGENT_EVIDENCE task=%s subtask=%s agent=%s evidence_items=%s",
                task.id,
                subtask.id,
                agent_name,
                len(result.evidence),
            )
        return subtask, result

    def _build_agent_registry(
        self,
        *,
        reminder_agent: ReminderSchedulerAgent | None,
    ) -> AgentRegistry:
        registry = AgentRegistry()
        shared_planner = Planner(
            openrouter_client=self.openrouter_client,
            tool_registry=self.tool_registry,
            context_assembler=self.context_assembler,
            agent_catalog=self.agent_catalog,
            agent_registry=registry,
        )
        shared_evaluator = GoalEvaluator(openrouter_client=self.openrouter_client)
        registry.register(
            LocalAgentAdapter(
                descriptor=AgentDescriptor(
                    agent_id="browser_agent",
                    display_name="Browser Agent",
                    provider=AgentProvider.LOCAL,
                    capabilities=["browser", "web_navigation", "page_inspection", "tool:browser_tool"],
                    cost_tier="standard",
                    risk_level="medium",
                    input_schema={"task": "Task", "subtask": "SubTask"},
                    output_schema={"result": "AgentResult"},
                    evidence_schema={"tool_name": "browser_tool"},
                    supports_async=False,
                    requires_credentials=False,
                    enabled=True,
                ),
                agent=BrowserAgent(tool_registry=self.tool_registry, openrouter_client=self.openrouter_client),
            )
        )
        registry.register(
            LocalAgentAdapter(
                descriptor=AgentDescriptor(
                    agent_id="planner_agent",
                    display_name="Planner Agent",
                    provider=AgentProvider.LOCAL,
                    capabilities=["planning", "delegation", "task_decomposition"],
                    cost_tier="standard",
                    risk_level="low",
                    input_schema={"task": "Task", "subtask": "SubTask"},
                    output_schema={"result": "AgentResult"},
                    evidence_schema={"type": "planning"},
                    aliases=["planning_agent"],
                ),
                agent=PlannerAgent(planner=shared_planner),
            )
        )
        registry.register(
            LocalAgentAdapter(
                descriptor=AgentDescriptor(
                    agent_id="coding_agent",
                    display_name="Coding Agent",
                    provider=AgentProvider.LOCAL,
                    capabilities=["coding", "files", "runtime", "tool:file_tool", "tool:runtime_tool"],
                    cost_tier="standard",
                    risk_level="medium",
                    input_schema={"task": "Task", "subtask": "SubTask"},
                    output_schema={"result": "AgentResult"},
                    evidence_schema={"tool_name": "file_tool|runtime_tool"},
                ),
                agent=CodingAgent(tool_registry=self.tool_registry),
            )
        )
        registry.register(
            LocalAgentAdapter(
                descriptor=AgentDescriptor(
                    agent_id="communications_agent",
                    display_name="Communications Agent",
                    provider=AgentProvider.LOCAL,
                    capabilities=["messaging", "email", "gmail", "notifications", "tool:slack_messaging_tool"],
                    cost_tier="standard",
                    risk_level="low for reads; high for send/delete/archive/forward",
                    input_schema={"task": "Task", "subtask": "SubTask"},
                    output_schema={"result": "AgentResult"},
                    evidence_schema={"type": "communication"},
                ),
                agent=CommunicationsAgent(tool_registry=self.tool_registry),
            )
        )
        registry.register(
            LocalAgentAdapter(
                descriptor=AgentDescriptor(
                    agent_id="memory_agent",
                    display_name="Memory Agent",
                    provider=AgentProvider.LOCAL,
                    capabilities=["memory", "recall", "context"],
                    cost_tier="low",
                    risk_level="low",
                    input_schema={"task": "Task", "subtask": "SubTask"},
                    output_schema={"result": "AgentResult"},
                    evidence_schema={"type": "memory"},
                ),
                agent=MemoryAgent(),
            )
        )
        registry.register(
            LocalAgentAdapter(
                descriptor=AgentDescriptor(
                    agent_id="scheduling_agent",
                    display_name="Scheduling / Personal Ops Agent",
                    provider=AgentProvider.LOCAL,
                    capabilities=["calendar", "google_calendar", "google_tasks", "tasks", "reminders", "scheduler", "follow_ups", "personal_ops", "tool:reminder_scheduler"],
                    cost_tier="low",
                    risk_level="medium",
                    input_schema={"task": "Task", "subtask": "SubTask"},
                    output_schema={"result": "AgentResult"},
                    evidence_schema={"tool_name": "google_calendar|google_tasks|reminder_scheduler"},
                    aliases=["reminder_agent", "reminder_scheduler_agent"],
                ),
                agent=SchedulingPersonalOpsAgent(
                    reminder_adapter=(reminder_agent.reminder_adapter if reminder_agent is not None else None)
                ),
            )
        )
        registry.register(
            LocalAgentAdapter(
                descriptor=AgentDescriptor(
                    agent_id="personal_ops_agent",
                    display_name="Personal Ops Agent",
                    provider=AgentProvider.LOCAL,
                    capabilities=["personal_ops", "personal_lists", "personal_notes", "proactive_routines"],
                    cost_tier="low",
                    risk_level="low for lists/notes; medium for delegated admin actions",
                    input_schema={"task": "Task", "subtask": "SubTask"},
                    output_schema={"result": "AgentResult"},
                    evidence_schema={"tool_name": "personal_ops_lists|proactive_routines"},
                    aliases=["personal_ops"],
                ),
                agent=PersonalOpsAgent(),
            )
        )
        registry.register(
            LocalAgentAdapter(
                descriptor=AgentDescriptor(
                    agent_id="research_agent",
                    display_name="Research Agent",
                    provider=AgentProvider.LOCAL,
                    capabilities=[
                        "research",
                        "current_info",
                        "documentation_lookup",
                        "comparison_research",
                        "analysis",
                        "planning_support",
                        "tool:web_search_tool",
                    ],
                    cost_tier="standard",
                    risk_level="low",
                    input_schema={"task": "Task", "subtask": "SubTask"},
                    output_schema={"result": "AgentResult"},
                    evidence_schema={"tool_name": "web_search_tool", "requires_sources": True},
                ),
                agent=ResearchAgent(),
            )
        )
        registry.register(
            LocalAgentAdapter(
                descriptor=AgentDescriptor(
                    agent_id="reviewer_agent",
                    display_name="Reviewer Agent",
                    provider=AgentProvider.LOCAL,
                    capabilities=["review", "verification", "quality", "quality_gate"],
                    cost_tier="standard",
                    risk_level="low",
                    input_schema={"task": "Task", "subtask": "SubTask"},
                    output_schema={"result": "AgentResult"},
                    evidence_schema={"type": "review"},
                ),
                agent=ReviewerAgent(tool_registry=self.tool_registry),
            )
        )
        registry.register(
            LocalAgentAdapter(
                descriptor=AgentDescriptor(
                    agent_id="verifier_agent",
                    display_name="Verifier Agent",
                    provider=AgentProvider.LOCAL,
                    capabilities=["final_verification", "quality_gate", "anti_fake_completion"],
                    cost_tier="standard",
                    risk_level="low",
                    input_schema={"task": "Task", "subtask": "SubTask"},
                    output_schema={"result": "AgentResult"},
                    evidence_schema={"type": "verification"},
                ),
                agent=VerifierAgent(evaluator=shared_evaluator),
            )
        )
        registry.register(
            ManagedAgentStubAdapter(
                descriptor=AgentDescriptor(
                    agent_id="openai_agents_agent",
                    display_name="OpenAI Agents SDK Agent",
                    provider=AgentProvider.OPENAI_AGENTS,
                    capabilities=["managed_reasoning", "tool_orchestration"],
                    cost_tier="premium",
                    risk_level="medium",
                    input_schema={"task": "Task", "subtask": "SubTask"},
                    output_schema={"result": "AgentResult"},
                    evidence_schema={"type": "managed_agent"},
                    supports_async=True,
                    requires_credentials=True,
                    enabled=False,
                ),
                required_settings=("openai_agents_api_key",),
                status_note="This is a readiness stub for the future OpenAI Agents SDK integration.",
            )
        )
        registry.register(
            ManagedAgentStubAdapter(
                descriptor=AgentDescriptor(
                    agent_id="manus_agent",
                    display_name="Manus Agent",
                    provider=AgentProvider.MANUS,
                    capabilities=["managed_browser", "managed_execution"],
                    cost_tier="premium",
                    risk_level="medium",
                    input_schema={"task": "Task", "subtask": "SubTask"},
                    output_schema={"result": "AgentResult"},
                    evidence_schema={"type": "managed_agent"},
                    supports_async=True,
                    requires_credentials=True,
                    enabled=False,
                ),
                required_settings=("manus_api_key",),
                status_note="This is a readiness stub for a future Manus-backed adapter.",
            )
        )
        registry.register(
            CodexCliAgentAdapter(
                descriptor=AgentDescriptor(
                    agent_id="codex_cli_agent",
                    display_name="Codex CLI Agent",
                    provider=AgentProvider.CODEX_CLI,
                    capabilities=["coding", "managed_coding", "workspace_edits", "code_review"],
                    cost_tier="standard",
                    risk_level="medium",
                    input_schema={"task": "Task", "subtask": "SubTask"},
                    output_schema={"result": "AgentResult"},
                    evidence_schema={"tool_name": "codex_cli"},
                    supports_async=True,
                    requires_credentials=False,
                    enabled=False,
                ),
            )
        )
        registry.register(
            ManagedAgentStubAdapter(
                descriptor=AgentDescriptor(
                    agent_id="browser_use_agent",
                    display_name="Browser Use Agent",
                    provider=AgentProvider.BROWSER_USE,
                    capabilities=["browser", "open_ended_browser"],
                    cost_tier="premium",
                    risk_level="medium",
                    input_schema={"task": "Task", "subtask": "SubTask"},
                    output_schema={"result": "AgentResult"},
                    evidence_schema={"type": "managed_agent"},
                    supports_async=True,
                    requires_credentials=True,
                    enabled=False,
                ),
                required_settings=("browser_use_api_key",),
                status_note="This is a readiness stub for a future Browser Use adapter.",
            )
        )
        return registry
