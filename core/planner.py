"""Goal decomposition primitives for the supervisor."""

from __future__ import annotations

import json
import re
import sys

import httpx

from agents.registry import AgentRegistry
from agents.catalog import AgentCatalog, build_agent_catalog
from core.logging import get_logger
from core.model_routing import ModelRequestContext
from core.context_assembly import ContextAssembler
from core.invocation_builders import (
    BrowserToolInvocationBuilder,
    BuiltInvocation,
    FileToolInvocationBuilder,
    InvocationBuilder,
    RuntimeToolInvocationBuilder,
    SlackMessagingInvocationBuilder,
)
from core.models import ExecutionEscalation, SubTask, ToolInvocation
from integrations.openrouter_client import OpenRouterClient
from tools.tool_policy import ToolCostPolicy, build_tool_cost_policy
from tools.registry import ToolRegistry, build_default_tool_registry


class Planner:
    """Break top-level goals into executable subtasks."""

    def __init__(
        self,
        *,
        openrouter_client: OpenRouterClient | None = None,
        tool_registry: ToolRegistry | None = None,
        invocation_builders: list[InvocationBuilder] | None = None,
        agent_tool_support: dict[str, frozenset[str]] | None = None,
        context_assembler: ContextAssembler | None = None,
        agent_catalog: AgentCatalog | None = None,
        agent_registry: AgentRegistry | None = None,
        tool_policy: ToolCostPolicy | None = None,
    ) -> None:
        self.logger = get_logger(__name__)
        self.openrouter_client = openrouter_client or OpenRouterClient()
        self.tool_registry = tool_registry or build_default_tool_registry()
        self.agent_catalog = agent_catalog or build_agent_catalog()
        self.agent_registry = agent_registry
        self.context_assembler = context_assembler or ContextAssembler(
            tool_registry=self.tool_registry,
            agent_catalog=self.agent_catalog,
        )
        self.invocation_builders = invocation_builders or [
            SlackMessagingInvocationBuilder(),
            BrowserToolInvocationBuilder(),
            FileToolInvocationBuilder(),
            RuntimeToolInvocationBuilder(),
        ]
        self.tool_policy = tool_policy or build_tool_cost_policy(self.context_assembler.capability_catalog)
        self.agent_tool_support = agent_tool_support or {
            "browser_agent": frozenset({"browser_tool"}),
            "codex_cli_agent": frozenset(),
            "coding_agent": frozenset({"file_tool", "runtime_tool"}),
            "communications_agent": frozenset({"slack_messaging_tool"}),
            "reminder_agent": frozenset(),
            "reminder_scheduler_agent": frozenset(),
            "memory_agent": frozenset(),
            "research_agent": frozenset({"web_search_tool"}),
            "reviewer_agent": frozenset(),
            "planner_agent": frozenset(),
            "verifier_agent": frozenset(),
        }

    def create_plan(
        self,
        goal: str,
        *,
        escalation_level: ExecutionEscalation = ExecutionEscalation.BOUNDED_TASK_EXECUTION,
    ) -> tuple[list[SubTask], str]:
        """Return subtasks and the planner mode used."""
        mixed_tool_plan = self._build_mixed_browser_file_plan(
            goal,
            escalation_level=escalation_level,
        )
        if mixed_tool_plan is not None:
            self.logger.info("PLANNER_PATH goal=%r planner_path=deterministic_mixed_tools", goal)
            return mixed_tool_plan, "deterministic"
        forced_browser_invocation = self._build_forced_browser_invocation(goal)
        if forced_browser_invocation is not None:
            self.logger.info(
                "BROWSER_REQUEST_DETECTED goal=%r action=%s url=%s path=fast_path",
                goal,
                forced_browser_invocation.invocation.action,
                forced_browser_invocation.invocation.parameters.get("url"),
            )
            self.logger.info("PLANNER_PATH goal=%r planner_path=fast_browser", goal)
            return self._create_tool_plan(
                goal,
                forced_browser_invocation,
                escalation_level=escalation_level,
            ), "deterministic"
        if self._looks_like_reminder_goal(goal):
            self.logger.info("PLANNER_PATH goal=%r planner_path=deterministic_reminder", goal)
            return self._create_reminder_plan(goal, escalation_level=escalation_level), "deterministic"
        coding_artifact_plan = self._build_bounded_coding_artifact_plan(
            goal,
            escalation_level=escalation_level,
        )
        if coding_artifact_plan is not None:
            planner_path = (
                "deterministic_codex_cli"
                if any(subtask.assigned_agent == "codex_cli_agent" for subtask in coding_artifact_plan)
                else "deterministic_local_coding_artifact"
            )
            self.logger.info("PLANNER_PATH goal=%r planner_path=%s", goal, planner_path)
            return coding_artifact_plan, "deterministic"
        llm_plan = self._create_llm_plan(goal, escalation_level=escalation_level)
        if llm_plan is not None:
            self.logger.info("PLANNER_PATH goal=%r planner_path=openrouter", goal)
            return self._finalize_plan(llm_plan), "openrouter"
        built_invocation = self._build_supported_invocation(goal)
        if built_invocation is not None:
            self.logger.info("PLANNER_PATH goal=%r planner_path=deterministic_tool", goal)
            return self._create_tool_plan(
                goal,
                built_invocation,
                escalation_level=escalation_level,
            ), "deterministic"
        if self._should_delegate_to_codex_cli(goal):
            self.logger.info("ROUTE_CODEX_FALLBACK goal=%r", goal)
            self.logger.info("PLANNER_PATH goal=%r planner_path=deterministic_codex_cli_fallback", goal)
            return self._create_codex_cli_plan(goal, escalation_level=escalation_level), "deterministic_fallback"
        self.logger.info("PLANNER_PATH goal=%r planner_path=deterministic_fallback", goal)
        return self._finalize_plan(
            self._create_fallback_plan(goal, escalation_level=escalation_level)
        ), "deterministic"

    def _create_llm_plan(
        self,
        goal: str,
        *,
        escalation_level: ExecutionEscalation,
    ) -> list[SubTask] | None:
        if not self.openrouter_client.is_configured():
            return None

        prompt = (
            f"{self.context_assembler.build('planning_agent', goal=goal).to_prompt_block()}\n"
            f"Escalation level: {escalation_level.value}\n"
            "Break the goal into concrete subtasks for a modular operator system.\n"
            "Return strict JSON with the shape "
            '{"subtasks":[{"title":"...","description":"...","objective":"...","agent_hint":"...",'
            '"tool_invocation":{"tool_name":"file_tool","action":"write","parameters":{"path":"...","content":"..."}}|null}]}.'
            "\n"
            f"Only use these agent_hint values: {', '.join(self._planner_available_agents())}.\n"
            'Only use tool_invocation for supported browser_tool actions open, inspect, summarize, '
            "web_search_tool actions search and research, file_tool actions write, read, list, "
            "runtime_tool action run, or slack_messaging_tool actions send_channel_message and send_dm.\n"
            "Use live capabilities as the primary execution path. If a capability is scaffolded, represent it honestly in subtask text without inventing execution.\n"
            "Tool cost policy:\n"
            "- prefer free/local/cheap tools for simple work\n"
            "- use premium managed agents only for complexity, repeated failure, or explicit request, and only when enabled\n"
            "- represent mixed tool requests as a sequence instead of collapsing them into one capability\n"
            "Plan sizing guidance:\n"
            "- single_action: 2 to 3 subtasks max, minimal scaffolding\n"
            "- bounded_task_execution: 3 to 4 subtasks, contained execution\n"
            "- objective_completion: 4 to 5 subtasks, include explicit review/adapt coverage\n"
            f"Goal: {goal}"
        )

        try:
            response = self.openrouter_client.prompt(
                prompt,
                system_prompt=(
                    "You are a planning component. Return only valid JSON without markdown."
                ),
                label="planner_create_plan",
                context=ModelRequestContext(
                    intent_label="planning",
                    request_mode="execute",
                    selected_lane="execution_flow",
                    selected_agent="planner_agent",
                    task_complexity=(
                        "high"
                        if escalation_level == ExecutionEscalation.OBJECTIVE_COMPLETION
                        else "medium"
                    ),
                    risk_level=(
                        "high"
                        if escalation_level == ExecutionEscalation.OBJECTIVE_COMPLETION
                        else "medium"
                    ),
                    requires_tool_use=False,
                    requires_review=escalation_level != ExecutionEscalation.SINGLE_ACTION,
                    evidence_quality="unknown",
                    user_visible_latency_sensitivity="medium",
                    cost_sensitivity="medium",
                ),
            )
            payload = json.loads(response)
            items = payload.get("subtasks", [])
            subtasks: list[SubTask] = []
            for item in items:
                title = str(item.get("title", "")).strip()
                description = str(item.get("description", "")).strip()
                objective = str(item.get("objective", "")).strip()
                agent_hint = str(item.get("agent_hint", "")).strip() or None
                tool_invocation = self._parse_tool_invocation(item.get("tool_invocation"))
                if not title or not description or not objective:
                    continue
                subtasks.append(
                    SubTask(
                        title=title,
                        description=description,
                        objective=objective,
                        assigned_agent=agent_hint,
                        tool_invocation=tool_invocation,
                    )
                )
            min_subtasks, max_subtasks = self._subtask_bounds(escalation_level)
            if min_subtasks <= len(subtasks) <= max_subtasks:
                return self._finalize_plan(subtasks[:max_subtasks])
            return None
        except (RuntimeError, httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError):
            return None

    def _create_fallback_plan(
        self,
        goal: str,
        *,
        escalation_level: ExecutionEscalation,
    ) -> list[SubTask]:
        """Create a simplified fallback plan when LLM planning is unavailable."""
        execution_agent = self._select_primary_agent(goal)
        subtasks = [
            SubTask(
                title=f"Handle: {goal[:50]}",
                description="Execute the goal using the best available capability path.",
                objective=goal,
                assigned_agent=execution_agent,
            )
        ]
        if escalation_level != ExecutionEscalation.SINGLE_ACTION:
            subtasks.append(
                SubTask(
                    title="Review result",
                    description="Verify the execution was honest and evidence-backed.",
                    objective=f"Review the result for: {goal}",
                    assigned_agent="reviewer_agent",
                )
            )
        return self._finalize_plan(subtasks)

    def _create_reminder_plan(
        self,
        goal: str,
        *,
        escalation_level: ExecutionEscalation,
    ) -> list[SubTask]:
        return self._finalize_plan(
            [
                SubTask(
                    title="Capture reminder context",
                    description="Persist the reminder request and its delivery context for the current run.",
                    objective=f"Record the reminder request for: {goal}",
                    assigned_agent="memory_agent",
                ),
                SubTask(
                    title="Schedule reminder delivery",
                    description="Parse the one-time reminder request and schedule durable outbound follow-up delivery.",
                    objective=f"Schedule the reminder for: {goal}",
                    assigned_agent="reminder_agent",
                ),
                SubTask(
                    title="Review reminder scheduling evidence",
                    description="Verify the reminder was actually scheduled with durable state and delivery metadata.",
                    objective=f"Review the reminder scheduling result for: {goal}",
                    assigned_agent="reviewer_agent",
                ),
            ]
        )

    def _create_tool_plan(
        self,
        goal: str,
        built_invocation: BuiltInvocation,
        *,
        escalation_level: ExecutionEscalation,
    ) -> list[SubTask]:
        subtasks = [
            SubTask(
                title="Capture goal context",
                description="Normalize and persist the incoming file goal for the current run.",
                objective=f"Record the workspace file task for: {goal}",
                assigned_agent="memory_agent",
            ),
            SubTask(
                title=built_invocation.execution_title,
                description=built_invocation.execution_description,
                objective=built_invocation.execution_objective,
                assigned_agent=built_invocation.execution_agent,
                tool_invocation=built_invocation.invocation,
            ),
        ]
        if escalation_level != ExecutionEscalation.SINGLE_ACTION:
            subtasks.append(
                SubTask(
                    title=built_invocation.review_title,
                    description=built_invocation.review_description,
                    objective=built_invocation.review_objective,
                    assigned_agent="reviewer_agent",
                )
            )
        return self._finalize_plan(subtasks)

    def _build_mixed_browser_file_plan(
        self,
        goal: str,
        *,
        escalation_level: ExecutionEscalation,
    ) -> list[SubTask] | None:
        policy_decision = self.tool_policy.assess(goal)
        sequence = policy_decision.capability_sequence
        if not (
            any(item in sequence for item in ("playwright_browser", "browser_use_browser"))
            and "file_tool" in sequence
        ):
            return None
        browser_invocation = self._build_forced_browser_invocation(goal)
        if browser_invocation is None:
            return None
        file_builder = next(
            (builder for builder in self.invocation_builders if isinstance(builder, FileToolInvocationBuilder)),
            None,
        )
        if file_builder is None or not file_builder.can_build(goal):
            return None
        file_invocation = file_builder.build(goal)
        subtasks = [
            SubTask(
                title="Capture mixed tool context",
                description="Record that this request needs a browser-to-file capability sequence.",
                objective=f"Record the mixed browser and file task for: {goal}",
                assigned_agent="memory_agent",
                notes=[f"Tool policy: {policy_decision.rationale}"],
            ),
            SubTask(
                title=browser_invocation.execution_title,
                description=browser_invocation.execution_description,
                objective=browser_invocation.execution_objective,
                assigned_agent=browser_invocation.execution_agent,
                tool_invocation=browser_invocation.invocation,
                notes=["Capability sequence step: browser evidence first."],
            ),
            SubTask(
                title="Save browser findings to file",
                description="Use the workspace file tool after browser evidence is available so the request is not treated as browser-only.",
                objective=f"Save the browser findings requested by: {goal}",
                assigned_agent=file_invocation.execution_agent,
                tool_invocation=file_invocation.invocation,
                notes=["Capability sequence step: file artifact second."],
            ),
        ]
        if escalation_level != ExecutionEscalation.SINGLE_ACTION:
            subtasks.append(
                SubTask(
                    title="Review mixed tool result",
                    description="Verify both browser evidence and the requested file artifact are represented before completion.",
                    objective=f"Review the browser plus file result for: {goal}",
                    assigned_agent="reviewer_agent",
                )
            )
        return self._finalize_plan(subtasks)

    def _create_codex_cli_plan(
        self,
        goal: str,
        *,
        escalation_level: ExecutionEscalation,
    ) -> list[SubTask]:
        subtasks = [
            SubTask(
                title="Capture coding context",
                description="Persist the coding goal and scope before delegated Codex execution.",
                objective=f"Record the managed coding task for: {goal}",
                assigned_agent="memory_agent",
            ),
            SubTask(
                title="Execute bounded coding task",
                description="Use the managed Codex CLI lane for bounded coding work inside the approved workspace root.",
                objective=goal,
                assigned_agent="codex_cli_agent",
            ),
        ]
        if escalation_level != ExecutionEscalation.SINGLE_ACTION:
            subtasks.append(
                SubTask(
                    title="Review Codex execution evidence",
                    description="Inspect Codex exit status, output, diff evidence, and any tests run before treating the work as complete.",
                    objective=f"Review the Codex execution result for: {goal}",
                    assigned_agent="reviewer_agent",
                )
            )
        return self._finalize_plan(subtasks)

    def _build_bounded_coding_artifact_plan(
        self,
        goal: str,
        *,
        escalation_level: ExecutionEscalation,
    ) -> list[SubTask] | None:
        artifact = self._infer_local_coding_artifact(goal)
        if artifact is None:
            return None
        if self._codex_cli_ready() and not artifact["prefer_local"]:
            return self._create_codex_cli_plan(goal, escalation_level=escalation_level)

        write_invocation = ToolInvocation(
            tool_name="file_tool",
            action="write",
            parameters={
                "path": artifact["path"],
                "content": artifact["content"],
            },
        )
        if not self._validate_invocation_for_agent(write_invocation, "coding_agent"):
            return None

        subtasks = [
            SubTask(
                title="Capture coding context",
                description="Persist the bounded coding goal and target artifact before execution.",
                objective=f"Record the local coding task for: {goal}",
                assigned_agent="memory_agent",
            ),
            SubTask(
                title="Create requested coding artifact",
                description="Write the requested bounded artifact into the workspace with concrete file evidence.",
                objective=f"Create {artifact['path']} for: {goal}",
                assigned_agent="coding_agent",
                tool_invocation=write_invocation,
            ),
        ]

        if artifact.get("run_command"):
            run_invocation = ToolInvocation(
                tool_name="runtime_tool",
                action="run",
                parameters={"command": artifact["run_command"]},
            )
            if not self._validate_invocation_for_agent(run_invocation, "coding_agent"):
                return None
            subtasks.append(
                SubTask(
                    title="Run requested script",
                    description="Run the generated script inside the workspace and capture stdout, stderr, and exit code.",
                    objective=f"Verify the generated script for: {goal}",
                    assigned_agent="coding_agent",
                    tool_invocation=run_invocation,
                )
            )

        subtasks.append(
            SubTask(
                title="Review coding artifact evidence",
                description="Verify created file paths plus runtime output before treating the coding request as complete.",
                objective=f"Review local coding artifact evidence for: {goal}",
                assigned_agent="reviewer_agent",
            )
        )
        return self._finalize_plan(subtasks)

    def _infer_local_coding_artifact(self, goal: str) -> dict[str, str | bool] | None:
        lowered = " ".join(goal.lower().split())
        if (
            self._looks_like_browser_execution_goal(lowered)
            or self._looks_like_reminder_goal(goal)
            or self._looks_like_communications_goal(lowered)
        ):
            return None
        action_terms = ("build", "create", "make", "write", "generate")
        if not any(term in lowered for term in action_terms):
            return None

        if "readme" in lowered and "file" in lowered:
            return {
                "path": self._extract_named_artifact_path(goal, default_path="README.md", default_extension=".md"),
                "content": self._readme_content(goal),
                "run_command": "",
                "prefer_local": True,
            }

        if "python" in lowered and "script" in lowered:
            path = self._extract_named_artifact_path(
                goal,
                default_path=self._default_python_script_name(lowered),
                default_extension=".py",
            )
            runtime_path = path if "/" in path or "\\" in path else f"created_items/{path}"
            return {
                "path": path,
                "content": self._python_script_content(lowered),
                "run_command": f'"{sys.executable}" "{runtime_path}"',
                "prefer_local": self._looks_like_tiny_local_script(lowered),
            }

        return None

    def _extract_named_artifact_path(
        self,
        goal: str,
        *,
        default_path: str,
        default_extension: str,
    ) -> str:
        cleaned = goal.replace('"', "").replace("'", "")
        explicit_path_match = re.search(
            r"\b(?:at|in|to|called|named)\s+([A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+)\b",
            cleaned,
            flags=re.IGNORECASE,
        )
        if explicit_path_match:
            return explicit_path_match.group(1).strip(".,:;")
        named_match = re.search(
            r"\b(?:called|named)\s+([A-Za-z0-9_./\\-]+)\b",
            cleaned,
            flags=re.IGNORECASE,
        )
        if named_match:
            candidate = named_match.group(1).strip(".,:;")
            if "." not in candidate.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]:
                return f"{candidate}{default_extension}"
            return candidate
        return default_path

    def _default_python_script_name(self, lowered_goal: str) -> str:
        if "quadratic" in lowered_goal:
            return "quadratic.py"
        if "hello" in lowered_goal:
            return "hello.py"
        return "script.py"

    def _looks_like_tiny_local_script(self, lowered_goal: str) -> bool:
        return any(term in lowered_goal for term in ("tiny", "simple", "hello", "quadratic"))

    def _python_script_content(self, lowered_goal: str) -> str:
        if "quadratic" in lowered_goal:
            return (
                "import cmath\n\n"
                "def solve_quadratic(a, b, c):\n"
                "    if a == 0:\n"
                "        raise ValueError(\"a must not be zero for a quadratic equation\")\n"
                "    discriminant = b * b - 4 * a * c\n"
                "    root = cmath.sqrt(discriminant)\n"
                "    return ((-b + root) / (2 * a), (-b - root) / (2 * a))\n\n"
                "def main():\n"
                "    roots = solve_quadratic(1, -3, 2)\n"
                "    print(f\"Roots: {roots[0]}, {roots[1]}\")\n\n"
                "if __name__ == \"__main__\":\n"
                "    main()\n"
            )
        return (
            "def main():\n"
            "    print(\"hello\")\n\n"
            "if __name__ == \"__main__\":\n"
            "    main()\n"
        )

    def _readme_content(self, goal: str) -> str:
        title = "Project Sovereign"
        if "simple" in goal.lower():
            return f"# {title}\n\nA simple README created by Project Sovereign.\n"
        return f"# {title}\n\nCreated by Project Sovereign.\n"

    def _codex_cli_ready(self) -> bool:
        if self.agent_registry is None:
            return False
        adapter = self.agent_registry.get("codex_cli_agent")
        return bool(adapter is not None and getattr(adapter, "enabled", False))

    def _select_primary_agent(self, goal: str) -> str:
        candidates = self._candidate_agent_ids_for_text(goal)
        return candidates[0] if candidates else "research_agent"

    def _looks_like_browser_execution_goal(self, description: str) -> bool:
        browser_terms = ("browser", "browse", "website", "web", "ui", "page", "site")
        execution_terms = ("open", "navigate", "test", "click", "fill", "inspect", "log in", "login")
        return any(term in description for term in browser_terms) and any(
            term in description for term in execution_terms
        )

    def _looks_like_research_or_planning_goal(self, description: str) -> bool:
        if any(
            phrase in description
            for phrase in (
                "research",
                "compare",
                "comparison",
                "current",
                "latest",
                "recent",
                "news",
                "documentation",
                "docs",
                "look up",
                "source",
                "sources",
                "options",
                "help me plan",
                "create a plan",
                "make a plan",
                "plan for",
                "next step",
                "brainstorm",
                "investigate",
            )
        ):
            return True
        return False

    def _looks_like_communications_goal(self, description: str) -> bool:
        return any(
            keyword in description
            for keyword in ("send", "email", "message", "notify", "calendar invite", "text")
        )

    def _build_execution_title(self, agent_name: str) -> str:
        titles = {
            "browser_agent": "Prepare browser execution path",
            "codex_cli_agent": "Prepare managed Codex execution path",
            "communications_agent": "Prepare communication delivery path",
            "reminder_scheduler_agent": "Prepare reminder scheduling path",
            "reminder_agent": "Prepare reminder scheduling path",
            "memory_agent": "Prepare memory retrieval path",
            "reviewer_agent": "Prepare review execution path",
            "research_agent": "Prepare interpretation and research path",
            "coding_agent": "Prepare implementation path",
        }
        return titles.get(agent_name, "Prepare execution path")

    def _looks_like_reminder_goal(self, goal: str) -> bool:
        lowered = goal.lower()
        return "remind me" in lowered or any(
            marker in lowered for marker in ("set a reminder", "schedule a reminder", "cancel reminder")
        )

    def _build_supported_invocation(self, goal: str) -> BuiltInvocation | None:
        for builder in self.invocation_builders:
            if builder.can_build(goal):
                built_invocation = builder.build(goal)
                if self._validate_invocation_for_agent(
                    built_invocation.invocation,
                    built_invocation.execution_agent,
                ):
                    return built_invocation
        return None

    def _build_forced_browser_invocation(self, goal: str) -> BuiltInvocation | None:
        for builder in self.invocation_builders:
            if not isinstance(builder, BrowserToolInvocationBuilder):
                continue
            if not builder.can_build(goal):
                continue
            built_invocation = builder.build(goal)
            if self._validate_invocation_for_agent(
                built_invocation.invocation,
                built_invocation.execution_agent,
            ):
                return built_invocation
        return None

    def _parse_tool_invocation(self, payload: object) -> ToolInvocation | None:
        if not isinstance(payload, dict):
            return None
        tool_name = str(payload.get("tool_name", "")).strip()
        action = str(payload.get("action", "")).strip()
        parameters = payload.get("parameters", {})
        if not isinstance(parameters, dict):
            return None
        normalized_parameters = {
            str(key): str(value) for key, value in parameters.items() if value is not None
        }
        invocation = ToolInvocation(tool_name=tool_name, action=action, parameters=normalized_parameters)
        if not self.tool_registry.supports_invocation(invocation):
            return None
        return invocation

    def _validate_subtasks(self, subtasks: list[SubTask]) -> list[SubTask]:
        for subtask in subtasks:
            invocation = subtask.tool_invocation
            if invocation is None:
                continue
            if self._validate_invocation_for_agent(invocation, subtask.assigned_agent):
                continue
            subtask.notes.append(
                f"Planner validation rejected tool invocation {invocation.tool_name}:{invocation.action} for agent {subtask.assigned_agent or 'unassigned'}."
            )
            subtask.tool_invocation = None
        return subtasks

    def candidate_agent_ids_for_subtask(self, subtask: SubTask) -> list[str]:
        if subtask.assigned_agent and self._agent_exists(subtask.assigned_agent):
            return [subtask.assigned_agent]
        if subtask.tool_invocation is not None and self.agent_registry is not None:
            candidates = self.agent_registry.candidates_for_capability(
                f"tool:{subtask.tool_invocation.tool_name}"
            )
            candidate_ids = [adapter.agent_id for adapter in candidates]
            if candidate_ids:
                return candidate_ids
        return self._candidate_agent_ids_for_text(
            " ".join([subtask.title, subtask.description, subtask.objective])
        )

    def _validate_invocation_for_agent(
        self,
        invocation: ToolInvocation,
        agent_name: str | None,
    ) -> bool:
        if not self.tool_registry.supports_invocation(invocation):
            return False
        if agent_name is None:
            return True
        supported_tools = self.agent_tool_support.get(agent_name)
        if supported_tools is None:
            return True
        return invocation.tool_name in supported_tools

    def _finalize_plan(self, subtasks: list[SubTask]) -> list[SubTask]:
        linked = self._link_dependencies(subtasks)
        validated = self._validate_subtasks(linked)
        return self._decorate_with_registry_candidates(validated)

    def _link_dependencies(self, subtasks: list[SubTask]) -> list[SubTask]:
        if len(subtasks) >= 2:
            subtasks[1].depends_on = [subtasks[0].id]
        if len(subtasks) >= 3:
            subtasks[2].depends_on = [subtasks[0].id, subtasks[1].id]
        if len(subtasks) >= 4:
            subtasks[3].depends_on = [subtasks[1].id, subtasks[2].id]
        if len(subtasks) >= 5:
            subtasks[4].depends_on = [subtasks[2].id, subtasks[3].id]
        return subtasks

    def _subtask_bounds(self, escalation_level: ExecutionEscalation) -> tuple[int, int]:
        bounds = {
            ExecutionEscalation.SINGLE_ACTION: (2, 3),
            ExecutionEscalation.BOUNDED_TASK_EXECUTION: (3, 4),
            ExecutionEscalation.OBJECTIVE_COMPLETION: (4, 5),
        }
        return bounds.get(escalation_level, (3, 4))

    def _execution_description_for_escalation(
        self,
        escalation_level: ExecutionEscalation,
    ) -> str:
        if escalation_level == ExecutionEscalation.SINGLE_ACTION:
            return "Handle the requested action directly without creating heavy execution scaffolding."
        if escalation_level == ExecutionEscalation.OBJECTIVE_COMPLETION:
            return "Drive the primary execution lane for an owned objective and surface what remains."
        return "Route the goal toward the most relevant contained execution path for this operator loop."

    def _decorate_with_registry_candidates(self, subtasks: list[SubTask]) -> list[SubTask]:
        for subtask in subtasks:
            candidate_ids = self.candidate_agent_ids_for_subtask(subtask)
            if candidate_ids:
                if not subtask.assigned_agent or not self._agent_exists(subtask.assigned_agent):
                    subtask.assigned_agent = candidate_ids[0]
                subtask.notes.append(f"Planner candidates: {', '.join(candidate_ids)}")
        return subtasks

    def _candidate_agent_ids_for_text(self, text: str) -> list[str]:
        if self._looks_like_serious_coding_goal(text):
            return ["codex_cli_agent", "coding_agent"]
        capabilities = self._infer_capabilities(text)
        if self.agent_registry is None:
            return self._fallback_candidates_for_capabilities(capabilities)

        candidate_ids: list[str] = []
        for capability in capabilities:
            for adapter in self.agent_registry.candidates_for_capability(capability):
                if adapter.agent_id not in candidate_ids:
                    candidate_ids.append(adapter.agent_id)
        return candidate_ids or self._fallback_candidates_for_capabilities(capabilities)

    def _infer_capabilities(self, text: str) -> list[str]:
        lowered = text.lower()
        capabilities: list[str] = []
        if self._looks_like_research_or_planning_goal(lowered):
            capabilities.append("research")
        rules = (
            (r"\b(remind|schedule|follow up|follow-up|later|tomorrow|mins?|hours?)\b", "reminders"),
            (r"\b(browser|browse|site|website|page|ui|navigate|open|inspect|click|login|log in)\b", "browser"),
            (r"\b(review|verify|validate|qa|quality|audit|double-check|check)\b", "review"),
            (r"\b(memory|remember|recall|store|retrieve|context)\b", "memory"),
            (r"\b(email|message|notify|text|slack|telegram)\b", "messaging"),
            (r"\b(create|write|edit|update|implement|run|code|file|runtime)\b", "coding"),
            (r"\b(research|investigate|compare|comparison|current|latest|recent|news|documentation|docs|source|sources|options|analyze|summarize|plan)\b", "research"),
        )
        for pattern, capability in rules:
            if capability not in capabilities and re.search(pattern, lowered):
                capabilities.append(capability)
        return capabilities or ["research"]

    def _fallback_candidates_for_capabilities(self, capabilities: list[str]) -> list[str]:
        mapping = {
            "reminders": ["scheduling_agent"],
            "calendar": ["scheduling_agent"],
            "browser": ["browser_agent"],
            "review": ["reviewer_agent"],
            "memory": ["memory_agent"],
            "messaging": ["communications_agent"],
            "coding": ["codex_cli_agent", "coding_agent"],
            "research": ["research_agent"],
        }
        candidate_ids: list[str] = []
        for capability in capabilities:
            for agent_id in mapping.get(capability, []):
                if agent_id not in candidate_ids:
                    candidate_ids.append(agent_id)
        return candidate_ids or ["research_agent"]

    def _planner_available_agents(self) -> list[str]:
        if self.agent_registry is None:
            return [
                "browser_agent",
                "coding_agent",
                "communications_agent",
                "memory_agent",
                "scheduling_agent",
                "research_agent",
                "reviewer_agent",
                "verifier_agent",
            ]
        excluded = {"planner_agent"}
        return [
            adapter.agent_id
            for adapter in self.agent_registry.list_agents(include_disabled=False)
            if adapter.agent_id not in excluded
        ]

    def _agent_exists(self, agent_id: str) -> bool:
        if self.agent_registry is None:
            return True
        return self.agent_registry.get(agent_id) is not None

    def _should_delegate_to_codex_cli(self, goal: str) -> bool:
        lowered = goal.lower()
        if any(term in lowered for term in ("build", "implement", "refactor", "debug", "fix")) and any(
            term in lowered
            for term in ("test", "tests", "failing", "regression", "module", "endpoint", "feature", "auth")
        ):
            return True
        return (
            self._looks_like_serious_coding_goal(goal)
            and not self._looks_like_browser_execution_goal(lowered)
            and not self._looks_like_reminder_goal(goal)
        )

    def _looks_like_serious_coding_goal(self, text: str) -> bool:
        lowered = text.lower()
        if self._looks_like_browser_execution_goal(lowered):
            return False
        if self._looks_like_reminder_goal(lowered) or self._looks_like_communications_goal(lowered):
            return False
        if any(term in lowered for term in ("build", "implement", "refactor", "debug", "fix")) and any(
            term in lowered for term in ("test", "tests", "failing", "regression")
        ):
            return True
        patterns = (
            r"\b(build|implement|ship|refactor|debug|fix|repair|investigate)\b",
            r"\b(test|tests|test suite|failing test|broken build)\b",
            r"\b(feature|bug|regression|codebase|module|function|integration|endpoint|api|service)\b",
        )
        matches = sum(1 for pattern in patterns if re.search(pattern, lowered))
        if "create a file" in lowered or lowered.startswith("run "):
            return False
        return matches >= 2 or ("fix" in lowered and "test" in lowered)
