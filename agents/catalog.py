"""Durable catalog of Sovereign's standing and future subagent roles."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentDefinition:
    """Editable definition for an operator-owned agent role."""

    name: str
    kind: str
    summary: str
    status: str
    owns_capabilities: tuple[str, ...] = ()
    execution_scope: tuple[str, ...] = ()
    prompt_files: tuple[str, ...] = ()
    tool_families: tuple[str, ...] = ()

    def short_line(self) -> str:
        return f"{self.name} ({self.status}): {self.summary}"


@dataclass
class AgentCatalog:
    """Lookup and summary helpers for the CEO operator's subagent map."""

    definitions: list[AgentDefinition] = field(default_factory=list)

    def by_name(self, name: str) -> AgentDefinition | None:
        for definition in self.definitions:
            if definition.name == name:
                return definition
        return None

    def available_agent_names(self) -> list[str]:
        return [definition.name for definition in self.definitions if definition.status != "planned"]

    def capability_owner(self, capability_name: str) -> AgentDefinition | None:
        for definition in self.definitions:
            if capability_name in definition.owns_capabilities:
                return definition
        return None

    def summary_block(self) -> str:
        sections = ["agent_roles:"]
        for definition in self.definitions:
            sections.append(f"- {definition.short_line()}")
            if definition.owns_capabilities:
                sections.append(
                    f"  owns: {', '.join(definition.owns_capabilities)}"
                )
        return "\n".join(sections)

    def user_visible_lines(self) -> list[str]:
        return [definition.short_line() for definition in self.definitions]


def build_agent_catalog() -> AgentCatalog:
    """Return the standing and scaffolded subagent model for Sovereign."""

    return AgentCatalog(
        definitions=[
            AgentDefinition(
                name="supervisor",
                kind="operator",
                summary="Main CEO-style operator that owns planning, delegation, review, and final user communication.",
                status="live",
                owns_capabilities=("operator_orchestration", "slack_transport", "llm_reasoning", "browser_execution"),
                execution_scope=("goal interpretation", "delegation", "final synthesis"),
                prompt_files=(
                    "instructions/operator_identity.md",
                    "instructions/operator.md",
                    "instructions/delegation_contract.md",
                ),
            ),
            AgentDefinition(
                name="planning_agent",
                kind="standing",
                summary="Builds internal execution plans with dependencies, evidence expectations, and honest blockers.",
                status="live",
                owns_capabilities=("plan_generation",),
                execution_scope=("plan decomposition", "dependency mapping"),
                prompt_files=("instructions/planning_agent.md",),
            ),
            AgentDefinition(
                name="research_agent",
                kind="standing",
                summary="Owns source-backed search, current-info research, documentation lookup, comparisons, and research synthesis.",
                status="live",
                owns_capabilities=(
                    "research_synthesis",
                    "web_search_tool",
                    "current_info",
                    "documentation_lookup",
                    "comparison_research",
                    "semantic_retrieval",
                ),
                execution_scope=("source-backed search", "research", "retrieval"),
                prompt_files=("instructions/research_agent.md",),
            ),
            AgentDefinition(
                name="browser_agent",
                kind="standing",
                summary="Future complex-browser workflow owner for portal work, QA flows, and richer evidence loops; direct simple browser execution is a separate live tool path.",
                status="scaffolded",
                owns_capabilities=("browser_use_browser",),
                execution_scope=("complex browser workflows", "portal automation", "browser evidence review"),
                prompt_files=("instructions/browser_agent.md",),
            ),
            AgentDefinition(
                name="coding_agent",
                kind="standing",
                summary="Owns file and runtime execution plus future code/runtime operations.",
                status="live",
                owns_capabilities=("file_tool", "runtime_tool", "coding_runtime"),
                execution_scope=("workspace operations", "runtime commands"),
                prompt_files=("instructions/coding_agent.md",),
                tool_families=("file_tool", "runtime_tool"),
            ),
            AgentDefinition(
                name="codex_cli_agent",
                kind="managed",
                summary="Owns bounded frontier coding work through the local Codex CLI with reviewable evidence capture.",
                status="live",
                owns_capabilities=("managed_coding", "bounded_workspace_edits", "codex_cli_execution"),
                execution_scope=("feature work", "debugging", "refactors", "tests"),
            ),
            AgentDefinition(
                name="personal_ops_agent",
                kind="standing",
                summary="Internal parent domain for life/admin work: reminders, calendar, Gmail, personal lists/notes, and future routines.",
                status="live",
                owns_capabilities=("personal_ops", "personal_lists_notes", "proactive_routines"),
                execution_scope=("personal admin", "structured user lists", "routine manifests", "life-ops delegation"),
                prompt_files=("instructions/personal_ops_agent.md",),
                tool_families=("personal_ops_lists", "proactive_routines"),
            ),
            AgentDefinition(
                name="communications_agent",
                kind="standing",
                summary="Personal Ops submodule for Gmail/email, outbound Slack, notifications, and future SMS/Discord communication channels.",
                status="live",
                owns_capabilities=("messaging_notifications", "slack_outbound_delivery", "email_delivery", "gmail"),
                execution_scope=("email", "mailbox operations", "notifications", "cross-channel messaging"),
                prompt_files=("instructions/communications_agent.md",),
            ),
            AgentDefinition(
                name="scheduling_agent",
                kind="standing",
                summary="Personal Ops submodule for calendar events, reminders, task to-dos, scheduling questions, and time-based personal operations.",
                status="live",
                owns_capabilities=("reminder_scheduler", "google_calendar", "google_tasks"),
                execution_scope=("reminders", "calendar lookup", "calendar updates", "task to-dos", "schedules", "recurring jobs"),
                prompt_files=("instructions/scheduling_agent.md", "instructions/reminder_agent.md"),
                tool_families=("google_calendar", "google_tasks", "reminder_scheduler"),
            ),
            AgentDefinition(
                name="memory_agent",
                kind="standing",
                summary="Owns memory capture, retrieval coordination, and long-term context hygiene.",
                status="live",
                owns_capabilities=("memory_capture", "knowledge_memory"),
                execution_scope=("memory capture", "memory retrieval"),
                prompt_files=("instructions/memory_agent.md",),
            ),
            AgentDefinition(
                name="reviewer_agent",
                kind="standing",
                summary="Reviews intermediate outputs for honesty, evidence quality, and obvious execution gaps.",
                status="live",
                owns_capabilities=("review_pass",),
                execution_scope=("review", "critique"),
                prompt_files=("instructions/reviewer_agent.md",),
            ),
            AgentDefinition(
                name="verifier_agent",
                kind="standing",
                summary="Reserved final-quality verifier for stronger completion checks on higher-stakes work.",
                status="scaffolded",
                owns_capabilities=("final_verification",),
                execution_scope=("final verification", "anti-fake-completion"),
                prompt_files=("instructions/verifier_agent.md",),
            ),
            AgentDefinition(
                name="openclaw_bridge_agent",
                kind="bridge",
                summary="Future adapter/bridge owner for OpenClaw or similar external agent runtimes.",
                status="scaffolded",
                owns_capabilities=("openclaw_bridge",),
                execution_scope=("external runtime bridging", "tool-runtime mediation"),
                prompt_files=("instructions/openclaw_bridge_agent.md",),
            ),
            AgentDefinition(
                name="provider_router_agent",
                kind="future",
                summary="Future owner for frontier model/provider routing policy and provider-aware execution choices.",
                status="planned",
                owns_capabilities=("frontier_model_routing",),
                execution_scope=("provider routing", "model policy"),
                prompt_files=("instructions/provider_router_agent.md",),
            ),
        ]
    )
