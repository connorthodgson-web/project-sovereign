"""Structured capability metadata for tools and adjacent runtime integrations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from agents.catalog import AgentCatalog, build_agent_catalog
from integrations.readiness import IntegrationReadiness, build_integration_readiness
from tools.registry import ToolRegistry, build_default_tool_registry


class CapabilityDefinition(BaseModel):
    """Editable capability metadata loaded from disk."""

    capability_id: str | None = None
    name: str
    display_name: str | None = None
    category: str
    cost_tier: str = "standard"
    risk_level: str = "low"
    complexity_fit: list[str] = Field(default_factory=list)
    summary: str
    status: str
    backing_component: str | None = None
    owner_agent: str | None = None
    strengths: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    requires_credentials: bool = False
    escalation_targets: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    suited_for: list[str] = Field(default_factory=list)
    avoid_when: list[str] = Field(default_factory=list)
    honesty_notes: list[str] = Field(default_factory=list)
    execution_semantics: list[str] = Field(default_factory=list)
    failure_semantics: list[str] = Field(default_factory=list)
    evidence_expectations: list[str] = Field(default_factory=list)
    config_requirements: list[str] = Field(default_factory=list)


class CapabilitySnapshot(BaseModel):
    """Runtime-aware capability state exposed to prompts and user-facing answers."""

    capability_id: str
    name: str
    display_name: str
    category: str
    cost_tier: str
    risk_level: str
    complexity_fit: list[str] = Field(default_factory=list)
    summary: str
    status: str
    configured: bool = False
    enabled: bool = False
    owner_agent: str | None = None
    strengths: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    requires_credentials: bool = False
    escalation_targets: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    suited_for: list[str] = Field(default_factory=list)
    avoid_when: list[str] = Field(default_factory=list)
    honesty_notes: list[str] = Field(default_factory=list)
    execution_semantics: list[str] = Field(default_factory=list)
    failure_semantics: list[str] = Field(default_factory=list)
    evidence_expectations: list[str] = Field(default_factory=list)
    config_requirements: list[str] = Field(default_factory=list)
    missing_config: list[str] = Field(default_factory=list)

    @property
    def is_live(self) -> bool:
        return self.status == "live"

    def short_line(self) -> str:
        return (
            f"{self.capability_id} ({self.status}; cost={self.cost_tier}; "
            f"risk={self.risk_level}): {self.summary}"
        )

    def plain_status(self) -> str:
        labels = {
            "live": "live",
            "scaffolded": "partly built",
            "configured_but_disabled": "configured but disabled",
            "unavailable": "needs setup",
            "planned": "planned",
        }
        return labels.get(self.status, self.status.replace("_", " "))

    def plain_line(self) -> str:
        owner = f" via {self.owner_agent.replace('_', ' ')}" if self.owner_agent else ""
        return f"{self.display_name} is {self.plain_status()}{owner}: {self.summary}"


class CEOCapabilityContext(BaseModel):
    """Plain-language runtime capability view for the CEO/operator."""

    capabilities: list[CapabilitySnapshot]
    agents: list[dict[str, object]]

    def snapshot_for(self, capability_name: str) -> CapabilitySnapshot | None:
        for snapshot in self.capabilities:
            if capability_name in {snapshot.name, snapshot.capability_id}:
                return snapshot
        return None

    def status_groups(self) -> dict[str, list[CapabilitySnapshot]]:
        groups = {
            "live": [],
            "configured_but_disabled": [],
            "scaffolded": [],
            "unavailable": [],
            "planned": [],
        }
        for snapshot in self.capabilities:
            groups.setdefault(snapshot.status, []).append(snapshot)
        return groups

    def prompt_block(self) -> str:
        sections = ["ceo_capability_context:"]
        for snapshot in self.capabilities:
            sections.append(f"- {snapshot.plain_line()}")
            if snapshot.missing_config:
                sections.append(f"  setup_needed: {', '.join(snapshot.missing_config)}")
            if snapshot.evidence_expectations:
                sections.append(
                    f"  evidence_needed: {', '.join(snapshot.evidence_expectations[:4])}"
                )
            if snapshot.limitations:
                sections.append(f"  limits: {', '.join(snapshot.limitations[:3])}")
        sections.append("ceo_agent_context:")
        for agent in self.agents:
            sections.append(
                f"- {agent['display_name']} is {agent['status']}: {agent['summary']}"
            )
            capabilities = agent.get("capabilities") or []
            if capabilities:
                sections.append(f"  handles: {', '.join(str(item) for item in capabilities)}")
        return "\n".join(sections)

    def agent_lines(self) -> list[str]:
        return [
            f"{agent['display_name']} ({agent['status']}): {agent['summary']}"
            for agent in self.agents
        ]


@dataclass
class CapabilityCatalog:
    """Loads definitions from disk and resolves runtime-aware status."""

    manifest_path: Path
    tool_registry: ToolRegistry
    agent_catalog: AgentCatalog

    def definitions(self) -> list[CapabilityDefinition]:
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        items = payload.get("capabilities", [])
        return [CapabilityDefinition.model_validate(item) for item in items]

    def snapshots(self) -> list[CapabilitySnapshot]:
        return [self._resolve(definition) for definition in self.definitions()]

    def live(self) -> list[CapabilitySnapshot]:
        return [item for item in self.snapshots() if item.is_live]

    def non_live(self) -> list[CapabilitySnapshot]:
        return [item for item in self.snapshots() if not item.is_live]

    def summary_block(self) -> str:
        sections = ["capability_state:"]
        for snapshot in self.snapshots():
            sections.append(f"- {snapshot.short_line()}")
            if snapshot.owner_agent:
                sections.append(f"  owner_agent: {snapshot.owner_agent}")
            if snapshot.complexity_fit:
                sections.append(f"  complexity_fit: {', '.join(snapshot.complexity_fit)}")
            if snapshot.strengths:
                sections.append(f"  strengths: {', '.join(snapshot.strengths[:3])}")
            if snapshot.limitations:
                sections.append(f"  limitations: {', '.join(snapshot.limitations[:2])}")
            if snapshot.escalation_targets:
                sections.append(f"  escalation_targets: {', '.join(snapshot.escalation_targets)}")
            if snapshot.honesty_notes:
                sections.extend(f"  note: {note}" for note in snapshot.honesty_notes[:2])
            if snapshot.missing_config:
                sections.append(f"  missing_config: {', '.join(snapshot.missing_config)}")
        return "\n".join(sections)

    def policy_block(self) -> str:
        return "\n".join(
            [
                "tool_cost_policy:",
                "- prefer free/local/cheap tools first: assistant/direct answers, memory, reminders, and local file operations when they fit the request.",
                "- simple direct browser work should start with the local Playwright-backed browser path.",
                "- stronger browser backends such as Browser Use are optional escalation targets for complex open-ended browser workflows when enabled, not the default.",
                "- premium managed agents such as Manus should not be used for trivial tasks and must not be selected while disabled or unconfigured.",
                "- escalate only for complexity, repeated failure, or an explicit user request, and only when the target capability exists and is enabled.",
                "- if a requested premium capability is unavailable, explain the blocker plainly instead of pretending execution happened.",
            ]
        )

    def user_visible_lines(self) -> tuple[list[str], list[str]]:
        live = [snapshot.short_line() for snapshot in self.live()]
        non_live = [snapshot.short_line() for snapshot in self.non_live()]
        return live, non_live

    def grouped_lines(self) -> dict[str, list[str]]:
        groups = {
            "live": [],
            "scaffolded": [],
            "configured_but_disabled": [],
            "unavailable": [],
            "planned": [],
        }
        for snapshot in self.snapshots():
            groups.setdefault(snapshot.status, []).append(snapshot.short_line())
        return groups

    def ceo_context(self) -> CEOCapabilityContext:
        snapshots = self.snapshots()
        snapshot_by_id = {
            key: snapshot
            for snapshot in snapshots
            for key in (snapshot.capability_id, snapshot.name)
        }
        agents: list[dict[str, object]] = []
        for definition in self.agent_catalog.definitions:
            owned = []
            for capability in definition.owns_capabilities:
                snapshot = snapshot_by_id.get(capability)
                if snapshot is not None:
                    owned.append(f"{snapshot.display_name} ({snapshot.plain_status()})")
                else:
                    owned.append(capability.replace("_", " "))
            agents.append(
                {
                    "name": definition.name,
                    "display_name": definition.name.replace("_", " ").title(),
                    "kind": definition.kind,
                    "status": definition.status,
                    "summary": definition.summary,
                    "capabilities": owned,
                    "scope": list(definition.execution_scope),
                }
            )
        return CEOCapabilityContext(capabilities=snapshots, agents=agents)

    def owner_for(self, capability_name: str) -> str | None:
        for snapshot in self.snapshots():
            if capability_name in {snapshot.name, snapshot.capability_id}:
                return snapshot.owner_agent
        return None

    def activation_requirements_for(self, capability_name: str) -> list[str]:
        for snapshot in self.snapshots():
            if capability_name in {snapshot.name, snapshot.capability_id}:
                return snapshot.config_requirements + snapshot.missing_config
        return []

    def snapshot_for(self, capability_name: str) -> CapabilitySnapshot | None:
        for snapshot in self.snapshots():
            if capability_name in {snapshot.name, snapshot.capability_id}:
                return snapshot
        return None

    def _resolve(self, definition: CapabilityDefinition) -> CapabilitySnapshot:
        status = definition.status
        notes = list(definition.honesty_notes)
        missing_config: list[str] = []

        configured = False
        enabled = False
        readiness = self._resolve_backing_component(definition.backing_component)
        if readiness is not None:
            status = readiness.status
            configured = readiness.configured
            enabled = readiness.enabled
            notes.extend(readiness.notes)
            missing_config = list(readiness.missing_fields)

        owner_agent = definition.owner_agent
        if owner_agent is None:
            owner = self.agent_catalog.capability_owner(definition.name)
            owner_agent = owner.name if owner else None

        return CapabilitySnapshot(
            capability_id=definition.capability_id or definition.name,
            name=definition.name,
            display_name=definition.display_name or definition.name.replace("_", " ").title(),
            category=definition.category,
            cost_tier=definition.cost_tier,
            risk_level=definition.risk_level,
            complexity_fit=definition.complexity_fit,
            summary=definition.summary,
            status=status,
            configured=configured,
            enabled=enabled,
            owner_agent=owner_agent,
            strengths=definition.strengths or definition.suited_for,
            limitations=definition.limitations or definition.avoid_when,
            requires_credentials=definition.requires_credentials,
            escalation_targets=definition.escalation_targets,
            inputs=definition.inputs,
            outputs=definition.outputs,
            suited_for=definition.suited_for,
            avoid_when=definition.avoid_when,
            honesty_notes=notes,
            execution_semantics=definition.execution_semantics,
            failure_semantics=definition.failure_semantics,
            evidence_expectations=definition.evidence_expectations,
            config_requirements=definition.config_requirements,
            missing_config=missing_config,
        )

    def _resolve_backing_component(
        self,
        backing_component: str | None,
    ) -> IntegrationReadiness | None:
        if backing_component == "tool:file_tool":
            return IntegrationReadiness(
                backing_component=backing_component,
                status="live" if self.tool_registry.get("file_tool") else "unavailable",
                configured=True,
                enabled=True,
            )
        if backing_component == "tool:runtime_tool":
            return IntegrationReadiness(
                backing_component=backing_component,
                status="live" if self.tool_registry.get("runtime_tool") else "unavailable",
                configured=True,
                enabled=True,
            )
        if backing_component == "tool:browser_tool":
            return IntegrationReadiness(
                backing_component=backing_component,
                status="live" if self.tool_registry.get("browser_tool") else "unavailable",
                configured=bool(self.tool_registry.get("browser_tool")),
                enabled=bool(self.tool_registry.get("browser_tool")),
            )
        if backing_component == "tool:web_search_tool":
            return build_integration_readiness().get("integration:search")
        return build_integration_readiness().get(backing_component or "")


def build_capability_catalog(
    *,
    manifest_path: str | Path | None = None,
    tool_registry: ToolRegistry | None = None,
    agent_catalog: AgentCatalog | None = None,
) -> CapabilityCatalog:
    """Create the runtime-aware capability catalog."""

    path = (
        Path(manifest_path)
        if manifest_path
        else Path(__file__).resolve().parent.parent / "prompts" / "capabilities" / "tools.json"
    )
    return CapabilityCatalog(
        manifest_path=path,
        tool_registry=tool_registry or build_default_tool_registry(),
        agent_catalog=agent_catalog or build_agent_catalog(),
    )
