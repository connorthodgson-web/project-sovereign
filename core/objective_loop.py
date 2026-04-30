"""Bounded LLM-led objective execution loop for complex tasks."""

from __future__ import annotations

import json
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from core.context_assembly import ContextAssembler
from core.logging import get_logger
from core.model_routing import ModelRequestContext
from core.models import (
    AgentExecutionStatus,
    AgentResult,
    AssistantDecision,
    ExecutionEscalation,
    GoalEvaluation,
    ObjectiveStage,
    SubTask,
    Task,
    TaskOutcome,
    TaskStatus,
    ToolInvocation,
)
from core.state import task_state_store
from core.operator_context import OperatorContextService, operator_context
from integrations.openrouter_client import OpenRouterClient
from tools.tool_policy import ToolCostPolicy, build_tool_cost_policy


class ObjectiveLoopDecision(BaseModel):
    """Structured CEO-loop decision returned by the supervisor LLM."""

    action: str
    reasoning: str
    subtask_id: str | None = None
    title: str | None = None
    description: str | None = None
    objective: str | None = None
    agent_name: str | None = None
    tool_invocation: ToolInvocation | None = None
    final_response: str | None = None
    blockers: list[str] = Field(default_factory=list)


class ObjectiveDecisionMaker(Protocol):
    """Decision boundary for LLM or mocked objective-loop control."""

    def decide(self, task: Task, decision: AssistantDecision, context: dict[str, object]) -> ObjectiveLoopDecision:
        """Choose the next objective-loop action."""


class LlmObjectiveDecisionMaker:
    """Uses the configured LLM to choose the next bounded objective action."""

    def __init__(
        self,
        *,
        openrouter_client: OpenRouterClient | None = None,
        context_assembler: ContextAssembler | None = None,
        tool_policy: ToolCostPolicy | None = None,
    ) -> None:
        self.openrouter_client = openrouter_client or OpenRouterClient()
        self.context_assembler = context_assembler or ContextAssembler()
        self.tool_policy = tool_policy or build_tool_cost_policy(self.context_assembler.capability_catalog)

    def decide(self, task: Task, decision: AssistantDecision, context: dict[str, object]) -> ObjectiveLoopDecision:
        if not self.openrouter_client.is_configured():
            return ObjectiveLoopDecision(
                action="block",
                reasoning="The objective loop needs an LLM decision, but no LLM is configured.",
                blockers=["LLM objective decision maker is unavailable."],
            )

        prompt = (
            f"{self.context_assembler.build('operator', user_message=task.goal).to_prompt_block()}\n"
            f"{self.context_assembler.capability_catalog.summary_block()}\n"
            f"{self.context_assembler.capability_catalog.policy_block()}\n"
            f"tool_policy_assessment: {self.tool_policy.assess(task.goal)}\n"
            "You are Sovereign's CEO/operator control surface. Python is tracking state, evidence, "
            "limits, and safety boundaries; you decide the next action from that state.\n"
            "Do not invent unavailable tools or fixed recipes. Use existing subtasks when they fit, "
            "or create one narrow subtask when the next useful action is missing.\n"
            "Return strict JSON with this shape: "
            '{"action":"execute_subtask|create_subtask|review|verify|finish|block|ask_user",'
            '"reasoning":"...","subtask_id":"...","title":"...","description":"...",'
            '"objective":"...","agent_name":"browser_agent",'
            '"tool_invocation":{"tool_name":"file_tool","action":"write","parameters":{"path":"...","content":"..."}}|null,'
            '"final_response":"...","blockers":["..."]}.\n'
            "Safety: do not choose Manus or disabled premium agents; high-risk outbound messages require user confirmation; "
            "stop on missing credentials, 2FA, CAPTCHA, or required user input.\n"
            "Use reviewer/verifier feedback as input to your next decision, not as a hard deterministic recipe.\n"
            f"Request decision: {decision.model_dump()}\n"
            f"Current objective state: {json.dumps(context, ensure_ascii=True)}"
        )
        try:
            response = self.openrouter_client.prompt(
                prompt,
                system_prompt="Return only valid JSON for the next objective-loop decision.",
                label="objective_loop_decide",
                context=ModelRequestContext(
                    intent_label="objective_loop",
                    request_mode=task.request_mode.value,
                    selected_lane="objective_loop",
                    selected_agent="supervisor",
                    task_complexity="high"
                    if task.escalation_level == ExecutionEscalation.OBJECTIVE_COMPLETION
                    else "medium",
                    risk_level="medium",
                    requires_tool_use=True,
                    requires_review=True,
                    evidence_quality="medium" if task.results else "low",
                    user_visible_latency_sensitivity="medium",
                    cost_sensitivity="medium",
                ),
            )
            payload = json.loads(response)
            return ObjectiveLoopDecision.model_validate(payload)
        except (RuntimeError, httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError) as exc:
            return ObjectiveLoopDecision(
                action="block",
                reasoning=f"The objective-loop LLM decision could not be parsed safely: {exc}",
                blockers=["Objective-loop LLM decision was unavailable or malformed."],
            )


class ObjectiveExecutionLoop:
    """Bounded loop where the LLM chooses and Python executes safely."""

    stop_markers = ("captcha", "2fa", "two-factor", "missing credentials", "credential", "login required")
    high_risk_agents = {"communications_agent"}
    high_risk_tools = {"slack_messaging_tool", "email_tool", "messaging_tool"}

    def __init__(
        self,
        *,
        router,
        verifier_adapter,
        reviewer_adapter,
        evaluator,
        decision_maker: ObjectiveDecisionMaker | None = None,
        operator_context_service: OperatorContextService | None = None,
        max_iterations: int = 6,
        max_retries: int = 2,
    ) -> None:
        self.logger = get_logger(__name__)
        self.router = router
        self.verifier_adapter = verifier_adapter
        self.reviewer_adapter = reviewer_adapter
        self.evaluator = evaluator
        self.operator_context = operator_context_service or operator_context
        self.decision_maker = decision_maker or LlmObjectiveDecisionMaker(
            openrouter_client=getattr(router, "openrouter_client", None),
            context_assembler=getattr(router, "context_assembler", None),
        )
        self.max_iterations = max_iterations
        self.max_retries = max_retries

    def run(self, task: Task, decision: AssistantDecision) -> dict[str, object]:
        iterations = 0
        retry_count = 0
        evaluation = GoalEvaluation(
            satisfied=False,
            reasoning="Objective loop has not reached final verification yet.",
            should_continue=True,
            completion_confidence=0.0,
        )
        evaluation_mode = "objective_loop"
        task.status = TaskStatus.RUNNING
        self._set_stage(task, ObjectiveStage.EXECUTING)

        while iterations < self.max_iterations and retry_count <= self.max_retries:
            task = task_state_store.get_task(task.id) or task
            self._sync_loop_counts(task, iterations, retry_count)
            loop_context = self._build_context(task, iterations=iterations, retry_count=retry_count)
            loop_decision = self.decision_maker.decide(task, decision, loop_context)
            self._record_decision(task, loop_decision)
            self.logger.info(
                "OBJECTIVE_LOOP_DECISION task=%s iteration=%s action=%s reasoning=%r",
                task.id,
                iterations,
                loop_decision.action,
                loop_decision.reasoning,
            )

            guardrail = self._guardrail(loop_decision)
            if guardrail is not None:
                task.status = TaskStatus.BLOCKED
                evaluation = guardrail
                break

            if loop_decision.action in {"block", "ask_user", "blocked_user_input"}:
                task.status = TaskStatus.BLOCKED
                if loop_decision.action in {"ask_user", "blocked_user_input"}:
                    self.operator_context.set_pending_question(
                        original_user_intent=task.goal,
                        missing_field="objective_user_input",
                        expected_answer_type="text",
                        resume_target="objective_loop",
                        tool_or_agent="supervisor",
                        pending_task_id=task.id,
                        objective_id=task.id,
                        question=loop_decision.final_response or (loop_decision.blockers or ["What do you need me to know?"])[0],
                    )
                evaluation = GoalEvaluation(
                    satisfied=False,
                    reasoning=loop_decision.reasoning,
                    missing=loop_decision.blockers or ["User input or unavailable capability is required."],
                    should_continue=False,
                    blocked=True,
                    completion_confidence=0.0,
                    next_action=loop_decision.final_response,
                )
                break

            if loop_decision.action == "finish":
                verifier_evaluation, evaluation_mode = self._run_verifier(task)
                evaluation = verifier_evaluation
                if evaluation.satisfied or evaluation.blocked:
                    task.status = self._status_for_evaluation(evaluation)
                    break
                retry_count += 1
                iterations += 1
                continue

            if loop_decision.action == "verify":
                evaluation, evaluation_mode = self._run_verifier(task)
                if evaluation.satisfied or evaluation.blocked:
                    task.status = self._status_for_evaluation(evaluation)
                    break
                retry_count += 1
                iterations += 1
                continue

            if loop_decision.action == "review":
                result = self._run_review(task)
                if result.status == AgentExecutionStatus.BLOCKED:
                    retry_count += 1
                iterations += 1
                continue

            if loop_decision.action in {"execute_subtask", "create_subtask"}:
                subtask = self._resolve_execution_subtask(task, loop_decision)
                if subtask is None:
                    retry_count += 1
                    iterations += 1
                    continue
                result = self._execute_subtask(task, subtask)
                if self._is_user_input_blocker(result):
                    task.status = TaskStatus.BLOCKED
                    evaluation = GoalEvaluation(
                        satisfied=False,
                        reasoning=result.summary,
                        missing=result.blockers or [result.summary],
                        should_continue=False,
                        blocked=True,
                        completion_confidence=0.0,
                        next_action=(result.next_actions or [None])[0],
                    )
                    break
                if result.status == AgentExecutionStatus.BLOCKED:
                    retry_count += 1
                iterations += 1
                continue

            retry_count += 1
            iterations += 1

        else:
            task.status = TaskStatus.BLOCKED
            evaluation = GoalEvaluation(
                satisfied=False,
                reasoning="The bounded objective loop stopped at its iteration or retry limit.",
                missing=["A successful LLM-selected execution path within loop limits"],
                should_continue=False,
                blocked=True,
                completion_confidence=0.0,
                next_action="Narrow the objective or resolve the repeated blocker.",
            )

        task = task_state_store.get_task(task.id) or task
        if task.status not in {TaskStatus.COMPLETED, TaskStatus.BLOCKED}:
            task.status = self._status_for_evaluation(evaluation)
        self._finalize_state(task, evaluation, iterations, retry_count)
        task_state_store.update_task(task)
        return {
            "task": task,
            "outcome": self._build_outcome(task),
            "evaluation": evaluation,
            "evaluation_mode": evaluation_mode,
            "iteration_count": iterations,
        }

    def _resolve_execution_subtask(self, task: Task, decision: ObjectiveLoopDecision) -> SubTask | None:
        if decision.action == "execute_subtask":
            for subtask in task.subtasks:
                if subtask.id == decision.subtask_id:
                    return subtask
            return None
        subtask = SubTask(
            title=decision.title or "Execute LLM-selected objective step",
            description=decision.description or decision.reasoning,
            objective=decision.objective or task.goal,
            assigned_agent=decision.agent_name,
            tool_invocation=decision.tool_invocation,
        )
        task.subtasks.append(subtask)
        task_state_store.set_subtasks(task.id, task.subtasks, planner_mode=task.planner_mode)
        return subtask

    def _execute_subtask(self, task: Task, subtask: SubTask) -> AgentResult:
        self._set_stage(task, ObjectiveStage.EXECUTING, active_subtask_ids=[subtask.id])
        routed_subtask, result = self.router.route_subtask(task, subtask)
        task_state_store.update_subtask(task.id, routed_subtask)
        self._append_result(task, result)
        self._record_agent_result(task, routed_subtask, result)
        return result

    def _run_review(self, task: Task) -> AgentResult:
        review_subtask = SubTask(
            title="Review latest objective evidence",
            description="Review the latest execution result and surface feedback for the CEO loop.",
            objective=f"Review the current evidence for: {task.goal}",
            assigned_agent="reviewer_agent",
        )
        task.subtasks.append(review_subtask)
        task_state_store.set_subtasks(task.id, task.subtasks, planner_mode=task.planner_mode)
        self._set_stage(task, ObjectiveStage.REVIEWING, active_subtask_ids=[review_subtask.id])
        result = self.reviewer_adapter.run(task, review_subtask)
        review_subtask.status = TaskStatus.COMPLETED if result.status == AgentExecutionStatus.COMPLETED else TaskStatus.BLOCKED
        task_state_store.update_subtask(task.id, review_subtask)
        self._append_result(task, result)
        self._record_agent_result(task, review_subtask, result)
        if task.objective_state is not None:
            task.objective_state.reviewer_feedback.append(result.summary)
            task_state_store.update_objective_state(task.id, task.objective_state)
        return result

    def _run_verifier(self, task: Task) -> tuple[GoalEvaluation, str]:
        verify_subtask = SubTask(
            title="Verify objective outcome",
            description="Verify whether the current evidence satisfies the user's original goal.",
            objective=f"Verify the objective outcome for: {task.goal}",
            assigned_agent="verifier_agent",
        )
        task.subtasks.append(verify_subtask)
        task_state_store.set_subtasks(task.id, task.subtasks, planner_mode=task.planner_mode)
        result = self.verifier_adapter.run(task, verify_subtask)
        verify_subtask.status = TaskStatus.COMPLETED if result.status == AgentExecutionStatus.COMPLETED else TaskStatus.BLOCKED
        task_state_store.update_subtask(task.id, verify_subtask)
        self._append_result(task, result)
        self._record_agent_result(task, verify_subtask, result)
        if task.objective_state is not None:
            task.objective_state.verifier_feedback.append(result.summary)
            task_state_store.update_objective_state(task.id, task.objective_state)
        payload = result.evidence[0].payload if result.evidence else {}
        evaluation = GoalEvaluation(
            satisfied=bool(payload.get("done", False)),
            reasoning=str(payload.get("reasoning", result.summary)),
            missing=[str(item) for item in payload.get("missing", [])],
            should_continue=bool(payload.get("should_continue", False)),
            blocked=bool(payload.get("blocked", False)),
            needs_review=bool(payload.get("needs_review", False)),
            completion_confidence=float(payload.get("completion_confidence", 0.0)),
            next_action=str(payload.get("next_action")) if payload.get("next_action") else None,
        )
        return evaluation, str(payload.get("evaluation_mode", "objective_loop"))

    def _guardrail(self, decision: ObjectiveLoopDecision) -> GoalEvaluation | None:
        agent_name = decision.agent_name or ""
        invocation = decision.tool_invocation
        if agent_name == "manus_agent":
            return self._blocked_eval("Manus is disabled for this pass and cannot be called.")
        adapter = self.router.agent_registry.get(agent_name) if agent_name else None
        if agent_name and (adapter is None or not adapter.enabled):
            return self._blocked_eval(f"Agent {agent_name} is unavailable or disabled.")
        if agent_name in self.high_risk_agents or (
            invocation is not None and invocation.tool_name in self.high_risk_tools
        ):
            return self._blocked_eval("This high-risk outbound action requires user confirmation first.")
        if invocation is not None:
            if invocation.tool_name.lower().startswith("manus"):
                return self._blocked_eval("Manus is disabled for this pass and cannot be called.")
            if not self.router.tool_registry.supports_invocation(invocation):
                return self._blocked_eval(
                    f"Unsupported tool invocation requested: {invocation.tool_name}:{invocation.action}."
                )
        return None

    def _blocked_eval(self, reason: str) -> GoalEvaluation:
        return GoalEvaluation(
            satisfied=False,
            reasoning=reason,
            missing=[reason],
            should_continue=False,
            blocked=True,
            completion_confidence=0.0,
            next_action=reason,
        )

    def _is_user_input_blocker(self, result: AgentResult) -> bool:
        haystack = " ".join([result.summary, *result.blockers, *result.next_actions]).lower()
        return any(marker in haystack for marker in self.stop_markers)

    def _build_context(self, task: Task, *, iterations: int, retry_count: int) -> dict[str, object]:
        return {
            "original_goal": task.goal,
            "current_plan": [subtask.model_dump() for subtask in task.subtasks],
            "recent_decisions": task.objective_state.recent_decisions if task.objective_state else [],
            "tool_calls_attempted": task.objective_state.tool_calls_attempted if task.objective_state else [],
            "evidence_results": [
                {
                    "agent": result.agent,
                    "status": result.status.value,
                    "tool_name": result.tool_name,
                    "summary": result.summary,
                    "blockers": result.blockers,
                    "next_actions": result.next_actions,
                    "evidence": [item.model_dump() for item in result.evidence],
                }
                for result in task.results
            ],
            "reviewer_feedback": task.objective_state.reviewer_feedback if task.objective_state else [],
            "verifier_feedback": task.objective_state.verifier_feedback if task.objective_state else [],
            "blockers": task.objective_state.blocked_reasons if task.objective_state else [],
            "iteration_count": iterations,
            "retry_count": retry_count,
            "max_iterations": self.max_iterations,
            "max_retries": self.max_retries,
            "available_agents": self.router.agent_registry.list_descriptors(include_disabled=True),
            "available_tools": self.router.tool_registry.list_tool_names(),
        }

    def _record_decision(self, task: Task, decision: ObjectiveLoopDecision) -> None:
        if task.objective_state is None:
            return
        task.objective_state.recent_decisions.append(f"{decision.action}: {decision.reasoning}")
        task.objective_state.recent_decisions = task.objective_state.recent_decisions[-8:]
        if decision.tool_invocation is not None:
            invocation = decision.tool_invocation
            task.objective_state.tool_calls_attempted.append(f"{invocation.tool_name}:{invocation.action}")
        task_state_store.update_objective_state(task.id, task.objective_state)

    def _record_agent_result(self, task: Task, subtask: SubTask, result: AgentResult) -> None:
        if task.objective_state is None:
            return
        task.objective_state.active_subtask_ids = [subtask.id]
        task.objective_state.evidence_log.append(result.summary)
        if result.status == AgentExecutionStatus.BLOCKED:
            task.objective_state.blocked_reasons.extend(result.blockers or [result.summary])
        task_state_store.update_objective_state(task.id, task.objective_state)

    def _append_result(self, task: Task, result: AgentResult) -> None:
        task = task_state_store.get_task(task.id) or task
        task.results = [*task.results, result]
        task_state_store.replace_results(task.id, task.results)

    def _set_stage(
        self,
        task: Task,
        stage: ObjectiveStage,
        *,
        active_subtask_ids: list[str] | None = None,
    ) -> None:
        if task.objective_state is None:
            return
        task.objective_state.stage = stage
        if active_subtask_ids is not None:
            task.objective_state.active_subtask_ids = active_subtask_ids
        task_state_store.update_objective_state(task.id, task.objective_state)

    def _sync_loop_counts(self, task: Task, iterations: int, retry_count: int) -> None:
        if task.objective_state is None:
            return
        task.objective_state.iteration_count = iterations
        task.objective_state.retry_count = retry_count
        task_state_store.update_objective_state(task.id, task.objective_state)

    def _finalize_state(
        self,
        task: Task,
        evaluation: GoalEvaluation,
        iterations: int,
        retry_count: int,
    ) -> None:
        if task.objective_state is None:
            return
        task.objective_state.iteration_count = iterations
        task.objective_state.retry_count = retry_count
        task.objective_state.completion_confidence = evaluation.completion_confidence
        task.objective_state.last_evaluation_reasoning = evaluation.reasoning
        task.objective_state.should_continue = evaluation.should_continue
        task.objective_state.blocked = evaluation.blocked
        if evaluation.blocked and evaluation.missing:
            task.objective_state.blocked_reasons = evaluation.missing
        if task.status == TaskStatus.COMPLETED:
            task.objective_state.stage = ObjectiveStage.COMPLETED
        elif task.status == TaskStatus.BLOCKED:
            task.objective_state.stage = ObjectiveStage.BLOCKED
        else:
            task.objective_state.stage = ObjectiveStage.ADAPTING
        task_state_store.update_objective_state(task.id, task.objective_state)

    def _status_for_evaluation(self, evaluation: GoalEvaluation) -> TaskStatus:
        if evaluation.satisfied:
            return TaskStatus.COMPLETED
        if evaluation.blocked or not evaluation.should_continue:
            return TaskStatus.BLOCKED
        return TaskStatus.RUNNING

    def _build_outcome(self, task: Task) -> TaskOutcome:
        return TaskOutcome(
            completed=sum(1 for result in task.results if result.status == AgentExecutionStatus.COMPLETED),
            blocked=sum(1 for result in task.results if result.status == AgentExecutionStatus.BLOCKED),
            simulated=sum(1 for result in task.results if result.status == AgentExecutionStatus.SIMULATED),
            planned=sum(1 for result in task.results if result.status == AgentExecutionStatus.PLANNED),
            total_subtasks=len(task.subtasks),
        )
