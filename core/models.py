"""Core shared models for orchestration and API exchange."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    """Lifecycle states for supervisor-managed work."""

    PENDING = "pending"
    PLANNING = "planning"
    PLANNED = "planned"
    ROUTING = "routing"
    ROUTED = "routed"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class AgentExecutionStatus(str, Enum):
    """Honest execution states returned by agents."""

    PLANNED = "planned"
    SIMULATED = "simulated"
    BLOCKED = "blocked"
    COMPLETED = "completed"


class AgentProvider(str, Enum):
    """Execution provider behind an agent adapter."""

    LOCAL = "local"
    OPENAI_AGENTS = "openai_agents"
    MANUS = "manus"
    CODEX_CLI = "codex_cli"
    BROWSER_USE = "browser_use"
    GOOGLE = "google"
    CUSTOM = "custom"


class RequestMode(str, Enum):
    """Top-level assistant handling modes for incoming user requests."""

    ANSWER = "answer"
    ACT = "act"
    EXECUTE = "execute"


class ExecutionEscalation(str, Enum):
    """Granular escalation levels for operator-owned work."""

    CONVERSATIONAL_ADVICE = "conversational_advice"
    SINGLE_ACTION = "single_action"
    BOUNDED_TASK_EXECUTION = "bounded_task_execution"
    OBJECTIVE_COMPLETION = "objective_completion"


class ObjectiveStage(str, Enum):
    """High-level lifecycle for a goal the operator may keep owning."""

    INTAKE = "intake"
    PLANNING = "planning"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    ADAPTING = "adapting"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class ReviewStatus(str, Enum):
    """Review/verification state for meaningful work."""

    NOT_NEEDED = "not_needed"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    VERIFIED = "verified"
    FAILED = "failed"


class BaseEvidence(BaseModel):
    """Structured evidence collected during real or reviewed execution."""

    kind: str
    tool_name: str
    verification_notes: list[str] = Field(default_factory=list)


class FileEvidence(BaseEvidence):
    """Concrete evidence emitted by the workspace file tool."""

    kind: Literal["file"] = "file"
    tool_name: Literal["file_tool"] = "file_tool"
    operation: Literal["write", "read", "list"]
    requested_path: str | None = None
    normalized_path: str | None = None
    workspace_root: str | None = None
    actual_path: str | None = None
    file_path: str | None = None
    content_preview: str | None = None
    listed_entries: list[str] = Field(default_factory=list)


class ToolEvidence(BaseEvidence):
    """Fallback evidence shape for future non-file tools."""

    kind: Literal["tool"] = "tool"
    tool_name: str
    summary: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


EvidenceItem = Annotated[FileEvidence | ToolEvidence, Field(discriminator="kind")]


class BrowserTask(BaseModel):
    """Structured browser-task state tracked across resolution, execution, and synthesis."""

    original_goal: str
    target_site_or_url: str | None = None
    resolved_url: str | None = None
    browser_action: str = "open"
    extraction_objective: str | None = None
    backend_used: Literal["playwright", "browser_use"] | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    synthesis_result: str | None = None
    blockers: list[str] = Field(default_factory=list)
    second_action_reasoning: str | None = None
    action_count: int = 0


class ReminderStatus(str, Enum):
    """Lifecycle states for scheduled reminder delivery."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELED = "canceled"


class ReminderScheduleKind(str, Enum):
    """Supported reminder schedule styles."""

    ONE_TIME = "one_time"
    RECURRING = "recurring"


class ToolInvocation(BaseModel):
    """Structured tool call prepared during planning and consumed by execution."""

    tool_name: str
    action: str
    parameters: dict[str, str] = Field(default_factory=dict)


class NormalizedToolOutput(BaseModel):
    """Minimal tool output contract executor-style agents can rely on."""

    success: bool
    error: str | None = None
    summary: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class GoalEvaluation(BaseModel):
    """Bounded goal-evaluation result used by the supervisor loop."""

    satisfied: bool
    reasoning: str
    missing: list[str] = Field(default_factory=list)
    should_continue: bool = False
    blocked: bool = False
    needs_review: bool = False
    completion_confidence: float = 0.0
    next_action: str | None = None


class RoutingDecision(BaseModel):
    """Structured output for bounded subtask-to-agent classification."""

    agent_name: str
    strategy: str
    reasoning: str


class AgentDescriptor(BaseModel):
    """Shared metadata describing an executable local or managed agent adapter."""

    agent_id: str
    display_name: str
    provider: AgentProvider
    capabilities: list[str] = Field(default_factory=list)
    cost_tier: str = "standard"
    risk_level: str = "low"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    evidence_schema: dict[str, Any] = Field(default_factory=dict)
    supports_async: bool = False
    requires_credentials: bool = False
    enabled: bool = True
    aliases: list[str] = Field(default_factory=list)


class LaneSelection(BaseModel):
    """Fresh per-request routing choice for the CEO/supervisor graph."""

    lane: str
    agent_id: str | None = None
    reasoning: str


class AssistantDecision(BaseModel):
    """Structured request interpretation used by the assistant layer."""

    mode: RequestMode
    escalation_level: ExecutionEscalation = ExecutionEscalation.CONVERSATIONAL_ADVICE
    reasoning: str
    should_use_tools: bool = False
    requires_minimal_follow_up: bool = False
    intent_label: str = "assistant"
    follow_up_prompt: str | None = None


class DelegatedAgentState(BaseModel):
    """A durable record of an agent lane participating in a task."""

    agent_name: str
    role: str
    status: TaskStatus = TaskStatus.PENDING
    subtask_ids: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ObjectiveState(BaseModel):
    """Operator-owned execution state for multi-step tasks and objectives."""

    objective: str
    escalation_level: ExecutionEscalation
    stage: ObjectiveStage = ObjectiveStage.INTAKE
    active_subtask_ids: list[str] = Field(default_factory=list)
    delegated_agents: list[DelegatedAgentState] = Field(default_factory=list)
    blocked: bool = False
    blocked_reasons: list[str] = Field(default_factory=list)
    evidence_log: list[str] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.NOT_NEEDED
    completion_confidence: float = 0.0
    should_continue: bool = False
    requires_user_input: bool = False
    last_evaluation_reasoning: str | None = None
    iteration_count: int = 0
    retry_count: int = 0
    recent_decisions: list[str] = Field(default_factory=list)
    tool_calls_attempted: list[str] = Field(default_factory=list)
    reviewer_feedback: list[str] = Field(default_factory=list)
    verifier_feedback: list[str] = Field(default_factory=list)


class SubTask(BaseModel):
    """A routed unit of work created by the planner."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str
    description: str
    objective: str
    assigned_agent: str | None = None
    tool_invocation: ToolInvocation | None = None
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class AgentResult(BaseModel):
    """Normalized structured result returned by a worker agent."""

    subtask_id: str
    agent: str
    status: AgentExecutionStatus
    summary: str
    tool_name: str | None = None
    details: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class Task(BaseModel):
    """Top-level task tracked by the supervisor."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    goal: str
    title: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    request_mode: RequestMode = RequestMode.EXECUTE
    escalation_level: ExecutionEscalation = ExecutionEscalation.BOUNDED_TASK_EXECUTION
    planner_mode: str = "deterministic"
    summary: str | None = None
    subtasks: list[SubTask] = Field(default_factory=list)
    results: list[AgentResult] = Field(default_factory=list)
    objective_state: ObjectiveState | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class TaskOutcome(BaseModel):
    """Rolled-up execution counts for a task."""

    completed: int = 0
    blocked: int = 0
    simulated: int = 0
    planned: int = 0
    total_subtasks: int = 0


class ChatRequest(BaseModel):
    """User input payload for the chat endpoint."""

    message: str = Field(min_length=1)
    transport: Literal["dashboard", "slack", "ios", "local"] = "dashboard"
    channel_id: str | None = None
    user_id: str | None = None


class ChatResponse(BaseModel):
    """Supervisor response shape for the chat endpoint."""

    task_id: str
    status: TaskStatus
    planner_mode: str
    request_mode: RequestMode = RequestMode.EXECUTE
    escalation_level: ExecutionEscalation = ExecutionEscalation.BOUNDED_TASK_EXECUTION
    response: str
    outcome: TaskOutcome
    subtasks: list[SubTask]
    results: list[AgentResult]
