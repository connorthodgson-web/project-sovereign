"""Central orchestrator for Project Sovereign."""

import re
from uuid import uuid4

from agents.adapter import AssistantAgentAdapter
from core.assistant import AssistantLayer
from core.assistant_fast_path import (
    is_explicit_memory_statement,
    is_forget_name_statement,
    is_memory_follow_up_phrase,
    is_memory_lookup,
    is_name_statement,
    is_project_memory_question,
    is_short_personal_fact_statement,
    is_user_memory_question,
)
from core.browser_requests import extract_obvious_browser_request, normalize_transport_text
from core.fast_actions import FastActionHandler
from core.logging import get_logger
from core.models import (
    AgentExecutionStatus,
    AssistantDecision,
    ChatResponse,
    DelegatedAgentState,
    ExecutionEscalation,
    GoalEvaluation,
    LaneSelection,
    ObjectiveStage,
    ObjectiveState,
    RequestMode,
    ReviewStatus,
    SubTask,
    Task,
    TaskOutcome,
    TaskStatus,
)
from core.evaluator import GoalEvaluator
from core.operator_context import OperatorContextService, operator_context
from core.orchestration_graph import SovereignOrchestrationGraph
from core.objective_loop import ObjectiveExecutionLoop, ObjectiveDecisionMaker
from core.planner import Planner
from core.request_trace import current_request_trace, request_trace
from core.router import Router
from core.state import task_state_store


class Supervisor:
    """Top-level orchestrator coordinating planning, routing, and execution."""

    max_iterations = 3

    def __init__(
        self,
        *,
        assistant_layer: AssistantLayer | None = None,
        planner: Planner | None = None,
        router: Router | None = None,
        evaluator: GoalEvaluator | None = None,
        operator_context_service: OperatorContextService | None = None,
        fast_action_handler: FastActionHandler | None = None,
        objective_decision_maker: ObjectiveDecisionMaker | None = None,
        enable_objective_loop: bool | None = None,
    ) -> None:
        self.logger = get_logger(__name__)
        self.operator_context = operator_context_service or operator_context
        resolved_router = router or Router()
        self.assistant_layer = assistant_layer or AssistantLayer(
            operator_context_service=self.operator_context
        )
        self.router = resolved_router
        self.planner = planner or Planner(
            agent_registry=self.router.agent_registry,
        )
        resolved_fast_actions = fast_action_handler or FastActionHandler(
            operator_context_service=self.operator_context,
            reminder_service=self._reminder_service_from_router(self.router),
            tool_registry=getattr(self.router, "tool_registry", None),
        )
        self.fast_action_handler = resolved_fast_actions
        self.assistant_adapter = AssistantAgentAdapter(assistant_layer=self.assistant_layer)
        self.evaluator = evaluator or GoalEvaluator(
            openrouter_client=getattr(self.router, "openrouter_client", None)
        )
        verifier_adapter = self.router.agent_registry.get("verifier_agent")
        reviewer_adapter = self.router.agent_registry.get("reviewer_agent")
        self.objective_loop = None
        if verifier_adapter is not None and reviewer_adapter is not None:
            self.objective_loop = ObjectiveExecutionLoop(
                router=self.router,
                verifier_adapter=verifier_adapter,
                reviewer_adapter=reviewer_adapter,
                evaluator=self.evaluator,
                decision_maker=objective_decision_maker,
                operator_context_service=self.operator_context,
            )
        self.enable_objective_loop = (
            enable_objective_loop
            if enable_objective_loop is not None
            else objective_decision_maker is not None
            or bool(getattr(getattr(self.router, "openrouter_client", None), "is_configured", lambda: False)())
        )
        self._last_browser_goal: str | None = None
        self.orchestration_graph = SovereignOrchestrationGraph(
            assistant_layer=self.assistant_layer,
            assistant_adapter=self.assistant_adapter,
            fast_action_handler=self.fast_action_handler,
            memory_backend=getattr(self.operator_context.memory_store, "name", "local"),
            lane_selector=self._select_lane,
            plan_task_flow=self._plan_task_flow,
            execute_task_flow=self._execute_task_flow,
            review_task_flow=self._review_task_flow,
            decide_replan=self._decide_replan,
            verify_task_flow=self._verify_task_flow,
            objective_loop_flow=self._objective_loop_flow if self.enable_objective_loop else None,
            evaluate_task=self._evaluate_task_flow,
            finalize_task_flow=self._finalize_task_flow,
        )

    def handle_user_goal(self, goal: str) -> ChatResponse:
        """Handle a user goal through the assistant-preserving LangGraph substrate."""
        transport_normalized_goal = normalize_transport_text(goal)
        normalized_goal = " ".join(transport_normalized_goal.split())
        continuation_goal = self.operator_context.resume_pending_question_if_answer(normalized_goal)
        objective_resume = self._try_resume_pending_objective(normalized_goal)
        if objective_resume is not None:
            return objective_resume
        pending_clarification = self._try_pending_question_clarification(normalized_goal)
        if continuation_goal is None and pending_clarification is not None:
            return pending_clarification
        resolved_goal = self._resolve_browser_retry_goal(continuation_goal or normalized_goal)
        browser_request = extract_obvious_browser_request(resolved_goal)
        with request_trace() as trace:
            self.logger.info(
                "ROUTE_START raw_goal=%r normalized_goal=%r resolved_goal=%r",
                goal,
                normalized_goal,
                resolved_goal,
            )
            self.logger.info(
                "SUPERVISOR_RECEIVED raw_goal=%r normalized_goal=%r resolved_goal=%r browser_retry=%s browser_action=%s browser_url=%s",
                goal,
                normalized_goal,
                resolved_goal,
                normalized_goal != resolved_goal,
                browser_request.action if browser_request is not None else None,
                browser_request.url if browser_request is not None else None,
            )
            decide_fast = getattr(self.assistant_layer, "decide_without_llm", self.assistant_layer.decide)
            fast_path_decision = decide_fast(resolved_goal)
            llm_available = bool(
                getattr(getattr(self.assistant_layer, "openrouter_client", None), "is_configured", lambda: False)()
            )
            can_bypass_langgraph = self._can_bypass_langgraph(resolved_goal, fast_path_decision) and (
                not llm_available or self._can_pre_bypass_with_llm(resolved_goal, fast_path_decision)
            )
            self.operator_context.record_user_message(
                normalized_goal,
                allow_llm_capture=not can_bypass_langgraph,
            )
            response = (
                self._try_pre_orchestration_fast_path(resolved_goal, fast_path_decision)
                if can_bypass_langgraph
                else None
            )
            if response is None:
                response = self.orchestration_graph.invoke(resolved_goal)
            if self._response_has_browser_result(response):
                self._last_browser_goal = resolved_goal
            trace.set_metadata("planner_mode", response.planner_mode)
            trace.set_metadata("request_mode", response.request_mode.value)
            trace.set_metadata("task_status", response.status.value)
            if trace.metadata.get("intent_label"):
                self.logger.info("ROUTE_INTENT_CLASSIFICATION intent_label=%s", trace.metadata["intent_label"])
            assistant_path = trace.assistant_path or response.planner_mode or response.request_mode.value
            self.logger.info(
                "OPENROUTER_CALLED=%s",
                trace.openrouter_calls > 0,
            )
            self.logger.info(
                "SUPERVISOR_TRACE assistant_path=%s planner_mode=%s request_mode=%s openrouter_called=%s openrouter_labels=%s memory_read=%s memory_read_ops=%s memory_write=%s memory_write_ops=%s total_latency_ms=%s",
                assistant_path,
                response.planner_mode,
                response.request_mode.value,
                trace.openrouter_calls > 0,
                trace.openrouter_labels or ["none"],
                bool(trace.memory_reads),
                trace.memory_reads or ["none"],
                bool(trace.memory_writes),
                trace.memory_writes or ["none"],
                trace.total_latency_ms(),
            )
            self.logger.info("LATENCY_MS=%s", trace.total_latency_ms())
            return response

    def _try_pending_question_clarification(self, user_message: str) -> ChatResponse | None:
        state = self.operator_context.get_short_term_state()
        pending = state.pending_question
        if pending is None or state.lifecycle_state != "active":
            return None
        normalized = " ".join(user_message.lower().strip().split())
        if not normalized:
            return None
        if self._looks_like_new_request_while_pending(normalized, pending.question):
            return None
        if not self._looks_like_attempted_pending_answer(normalized, pending.question):
            return None

        reply = self._pending_question_retry_prompt(pending.missing_field, pending.question)
        self.operator_context.record_user_message(user_message, allow_llm_capture=False)
        self.operator_context.record_assistant_reply(reply)
        return ChatResponse(
            task_id=f"answer-{uuid4()}",
            status=TaskStatus.BLOCKED,
            planner_mode="conversation_clarify",
            request_mode=RequestMode.ANSWER,
            escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
            response=reply,
            outcome=TaskOutcome(blocked=1, total_subtasks=0),
            subtasks=[],
            results=[],
        )

    def _looks_like_new_request_while_pending(self, normalized: str, question: str | None) -> bool:
        if question and normalized.rstrip("?") == " ".join(question.lower().strip().split()).rstrip("?"):
            return False
        if normalized.endswith("?"):
            return True
        return normalized.startswith(
            (
                "add ",
                "create ",
                "schedule ",
                "remind me ",
                "delete ",
                "cancel ",
                "move ",
                "update ",
                "change ",
                "send ",
                "email ",
                "open ",
                "check ",
                "research ",
                "build ",
                "write ",
            )
        )

    def _looks_like_attempted_pending_answer(self, normalized: str, question: str | None) -> bool:
        if question and normalized.rstrip("?") == " ".join(question.lower().strip().split()).rstrip("?"):
            return True
        if normalized.startswith(("to ", "for ", "at ", "on ")):
            return True
        if normalized in {"banana", "later idk", "idk", "not sure", "mars", "to mars"}:
            return True
        return len(normalized.split()) <= 4

    def _pending_question_retry_prompt(self, missing_field: str, question: str | None) -> str:
        lowered_field = missing_field.lower()
        lowered_question = (question or "").lower()
        if "calendar" in lowered_field:
            if "day" in lowered_question:
                return "I need a real day for that event, like tomorrow or Friday."
            if "time" in lowered_question:
                return "I need a real time for that event, like 6 PM or 7 to 8 PM."
            return question or "What day and time should I use for that event?"
        if "reminder" in lowered_field:
            if "time" in lowered_question:
                return "I need a real time for that reminder, like 8 AM or tomorrow at 6 PM."
            return question or "What should the reminder say?"
        return question or "Can you clarify what you mean?"

    def _try_resume_pending_objective(self, user_answer: str) -> ChatResponse | None:
        state = self.operator_context.get_short_term_state()
        pending_action = state.pending_action if isinstance(state.pending_action, dict) else None
        if not pending_action or pending_action.get("resume_target") != "objective_loop":
            return None
        task_id = str(pending_action.get("pending_task_id") or pending_action.get("objective_id") or "")
        if not task_id or self.objective_loop is None:
            return None
        task = task_state_store.get_task(task_id)
        if task is None:
            return None
        if task.objective_state is not None:
            task.objective_state.blocked = False
            task.objective_state.blocked_reasons = []
            task.objective_state.recent_decisions.append(f"user_answer: {user_answer}")
            task.objective_state.stage = ObjectiveStage.ADAPTING
            task_state_store.update_objective_state(task.id, task.objective_state)
        task.status = TaskStatus.RUNNING
        task.summary = f"User answered the pending objective question: {user_answer}"
        task_state_store.update_task(task)
        decision = AssistantDecision(
            mode=RequestMode.EXECUTE,
            escalation_level=task.escalation_level,
            reasoning="Resume the same objective after the user answered the pending question.",
            should_use_tools=True,
            intent_label="objective_loop_resume",
        )
        result = self.objective_loop.run(task, decision)
        resumed_task = result["task"]
        outcome = result["outcome"]
        evaluation = result["evaluation"]
        evaluation_mode = str(result["evaluation_mode"])
        if resumed_task.status == TaskStatus.COMPLETED:
            self.operator_context.consume_short_term_state()
        return self._finalize_task_flow(resumed_task, decision, outcome, evaluation, evaluation_mode)

    def _try_pre_orchestration_fast_path(
        self,
        normalized_goal: str,
        decision: AssistantDecision | None = None,
    ) -> ChatResponse | None:
        """Serve obvious assistant and small-action requests before LangGraph starts."""
        decision = decision or self.assistant_layer.decide_without_llm(normalized_goal)
        if not self._can_bypass_langgraph(normalized_goal, decision):
            return None

        lane_selection = self._select_lane(normalized_goal, decision)
        if lane_selection.lane == "planning":
            return None

        trace = current_request_trace()
        if trace is not None:
            trace.set_metadata("intent_label", decision.intent_label)
        self.logger.info(
            "ROUTE_INTENT_CLASSIFICATION mode=%s escalation=%s intent_label=%s should_use_tools=%s requires_follow_up=%s",
            decision.mode.value,
            decision.escalation_level.value,
            decision.intent_label,
            decision.should_use_tools,
            decision.requires_minimal_follow_up,
        )
        self.logger.info(
            "ROUTE_SELECTED lane=%s agent=%s intent_label=%s",
            lane_selection.lane,
            lane_selection.agent_id,
            decision.intent_label,
        )
        self.logger.info(
            "LANE_SELECTED lane=%s agent=%s mode=%s escalation=%s",
            lane_selection.lane,
            lane_selection.agent_id,
            decision.mode.value,
            decision.escalation_level.value,
        )
        self.logger.info(
            "AGENT_SELECTED lane=%s agent=%s",
            lane_selection.lane,
            lane_selection.agent_id,
        )

        if lane_selection.lane == "assistant":
            self.logger.info(
                "AGENT_ADAPTER_START agent_id=%s provider=local mode=answer",
                lane_selection.agent_id,
            )
            response = self.assistant_adapter.build_response(normalized_goal, decision)
            self.logger.info(
                "AGENT_ADAPTER_END agent_id=%s status=%s tool=%s",
                lane_selection.agent_id,
                response.status.value,
                None,
            )
            return response

        self.logger.info(
            "AGENT_ADAPTER_START agent_id=%s provider=local mode=act",
            lane_selection.agent_id,
        )
        response = self.fast_action_handler.handle(normalized_goal, decision)
        if response is None:
            self.logger.info(
                "AGENT_ADAPTER_END agent_id=%s status=%s tool=%s",
                lane_selection.agent_id,
                "fallthrough",
                None,
            )
            return None
        self.logger.info(
            "AGENT_ADAPTER_END agent_id=%s status=%s tool=%s",
            lane_selection.agent_id,
            response.status.value,
            response.results[0].tool_name if response.results else None,
        )
        return response

    def _can_bypass_langgraph(self, normalized_goal: str, decision: AssistantDecision) -> bool:
        if self.fast_action_handler.can_handle_browser_request(normalized_goal):
            return False
        if decision.mode == RequestMode.ANSWER:
            return True
        if decision.mode != RequestMode.ACT:
            return False
        lowered = normalized_goal.lower()
        return any(
            (
                self.fast_action_handler.can_handle_local_file_request(normalized_goal),
                self.assistant_layer._looks_like_explicit_reminder_request(lowered),
                self.fast_action_handler._looks_like_calendar_read_request(lowered),
                self.fast_action_handler._looks_like_cancel_reminder_request(lowered),
                self.fast_action_handler._looks_like_update_reminder_request(lowered),
                self.fast_action_handler._looks_like_calendar_event_request(lowered),
                self.fast_action_handler._looks_like_calendar_delete_request(lowered),
                self.fast_action_handler._looks_like_calendar_update_request(lowered),
                self.fast_action_handler._looks_like_unavailable_email_request(lowered),
                self.fast_action_handler._looks_like_personal_ops_request(lowered),
            )
        )

    def _can_pre_bypass_with_llm(self, normalized_goal: str, decision: AssistantDecision) -> bool:
        del normalized_goal
        return decision.mode == RequestMode.ANSWER and decision.intent_label == "chat"

    def _plan_task_flow(
        self,
        normalized_goal: str,
        decision: AssistantDecision,
        existing_task: Task | None,
        replan_count: int,
    ) -> dict[str, object]:
        if existing_task is None:
            task = Task(
                goal=normalized_goal,
                title=self._build_task_title(normalized_goal),
                description=normalized_goal,
                status=TaskStatus.PLANNING,
                request_mode=decision.mode,
                escalation_level=decision.escalation_level,
                objective_state=self._initial_objective_state(normalized_goal, decision.escalation_level),
            )
            task_state_store.add_task(task)
            self.operator_context.task_started(task)
        else:
            task = existing_task
            task.status = TaskStatus.PLANNING
            task_state_store.update_task(task)

        self._set_objective_stage(task, ObjectiveStage.PLANNING, should_continue=decision.mode != RequestMode.ANSWER)
        planning_subtask = SubTask(
            title="Create structured plan",
            description="Build a structured execution plan with candidate agents and review coverage.",
            objective=f"Plan the execution for: {task.goal}",
            assigned_agent="planner_agent",
        )
        planner_adapter = self.router.agent_registry.get("planner_agent")
        if planner_adapter is None:
            raise RuntimeError("Planner agent adapter is not registered.")

        self.logger.info("PLANNER_AGENT_START task=%s replan_count=%s", task.id, replan_count)
        planning_result = planner_adapter.run(task, planning_subtask)
        self.logger.info(
            "PLANNER_AGENT_END task=%s status=%s",
            task.id,
            planning_result.status.value,
        )
        planning_payload = planning_result.evidence[0].payload if planning_result.evidence else {}
        subtasks = [
            SubTask.model_validate(item)
            for item in planning_payload.get("subtasks", [])
        ]
        planner_mode = str(planning_payload.get("planner_mode", "deterministic"))
        task.planner_mode = planner_mode
        task.subtasks = self._merge_subtasks(task.subtasks, subtasks)
        self.logger.info("Planned task %s using %s mode", task.id, planner_mode)
        for subtask in task.subtasks:
            self.logger.info(
                "SUPERVISOR_SUBTASK task=%s subtask=%s agent=%s tool=%s action=%s",
                task.id,
                subtask.id,
                subtask.assigned_agent,
                subtask.tool_invocation.tool_name if subtask.tool_invocation else None,
                subtask.tool_invocation.action if subtask.tool_invocation else None,
            )
        task_state_store.set_subtasks(task.id, task.subtasks, planner_mode=planner_mode)
        task_state_store.update_status(task.id, TaskStatus.ROUTING)
        task = task_state_store.get_task(task.id) or task
        self._sync_objective_state_for_plan(task)
        task_state_store.update_task(task)
        return {"task": task, "iteration_count": replan_count}

    def _execute_task_flow(
        self,
        task: Task,
        decision: AssistantDecision,
    ) -> dict[str, object]:
        task.status = TaskStatus.RUNNING
        task_state_store.update_task(task)
        iterations = 0
        while iterations < self._iteration_budget(task):
            task = task_state_store.get_task(task.id) or task
            subtask = self._next_subtask(task, include_review=False)
            if subtask is None:
                break

            self._set_objective_stage(task, ObjectiveStage.EXECUTING, active_subtask_ids=[subtask.id], should_continue=True)
            subtask.status = TaskStatus.RUNNING
            task_state_store.update_subtask(task.id, subtask)
            routed_subtask, result = self.router.route_subtask(task, subtask)
            task_state_store.update_subtask(task.id, routed_subtask)
            self._append_result(task, result)
            self._record_delegated_agent_result(task, routed_subtask, result)
            self.operator_context.task_progress(task, result)
            iterations += 1
            task = task_state_store.get_task(task.id) or task
            if result.status == AgentExecutionStatus.BLOCKED:
                self._set_objective_stage(
                    task,
                    ObjectiveStage.BLOCKED,
                    blocked=True,
                    blocked_reasons=result.blockers or [result.summary],
                    should_continue=False,
                )
                break
        return {
            "task": task_state_store.get_task(task.id) or task,
            "iteration_count": iterations,
        }

    def _review_task_flow(
        self,
        task: Task,
        decision: AssistantDecision,
    ) -> dict[str, object]:
        iterations = 0
        if decision.escalation_level != ExecutionEscalation.SINGLE_ACTION:
            while True:
                task = task_state_store.get_task(task.id) or task
                review_subtask = self._next_subtask(task, include_execution=False)
                if review_subtask is None:
                    break
                self._set_objective_stage(task, ObjectiveStage.REVIEWING, active_subtask_ids=[review_subtask.id], should_continue=True)
                review_subtask.status = TaskStatus.RUNNING
                task_state_store.update_subtask(task.id, review_subtask)
                self.logger.info("REVIEWER_AGENT_START task=%s subtask=%s", task.id, review_subtask.id)
                routed_subtask, result = self.router.route_subtask(task, review_subtask)
                self.logger.info(
                    "REVIEWER_AGENT_END task=%s subtask=%s status=%s",
                    task.id,
                    review_subtask.id,
                    result.status.value,
                )
                task_state_store.update_subtask(task.id, routed_subtask)
                self._append_result(task, result)
                self._record_delegated_agent_result(task, routed_subtask, result)
                self.operator_context.task_progress(task, result)
                iterations += 1
                if result.status == AgentExecutionStatus.BLOCKED:
                    break

        evaluation, evaluation_mode = self.evaluator.evaluate(task_state_store.get_task(task.id) or task)
        self.logger.info(
            "COMPLETION_CONFIDENCE task=%s confidence=%s",
            task.id,
            evaluation.completion_confidence,
        )
        self._apply_evaluation(task, evaluation)
        return {
            "task": task_state_store.get_task(task.id) or task,
            "evaluation": evaluation,
            "evaluation_mode": evaluation_mode,
            "iteration_count": iterations,
        }

    def _verify_task_flow(
        self,
        task: Task,
        decision: AssistantDecision,
    ) -> dict[str, object]:
        verifier_adapter = self.router.agent_registry.get("verifier_agent")
        if verifier_adapter is None:
            raise RuntimeError("Verifier agent adapter is not registered.")
        verifier_subtask = SubTask(
            title="Verify final outcome",
            description="Perform the final anti-fake-completion check.",
            objective=f"Verify the final outcome for: {task.goal}",
            assigned_agent="verifier_agent",
        )
        self.logger.info("VERIFIER_AGENT_START task=%s", task.id)
        verifier_result = verifier_adapter.run(task, verifier_subtask)
        self.logger.info(
            "VERIFIER_AGENT_END task=%s status=%s",
            task.id,
            verifier_result.status.value,
        )
        payload = verifier_result.evidence[0].payload if verifier_result.evidence else {}
        evaluation = GoalEvaluation(
            satisfied=bool(payload.get("done", False)),
            reasoning=str(payload.get("reasoning", verifier_result.summary)),
            missing=[str(item) for item in payload.get("missing", [])],
            should_continue=bool(payload.get("should_continue", False)),
            blocked=bool(payload.get("blocked", False)),
            needs_review=bool(payload.get("needs_review", False)),
            completion_confidence=float(payload.get("completion_confidence", 0.0)),
            next_action=str(payload.get("next_action")) if payload.get("next_action") else None,
        )
        evaluation_mode = str(payload.get("evaluation_mode", "deterministic"))
        outcome = self._build_outcome(task)
        task.status = self._derive_task_status(task, evaluation)
        self._finalize_objective_state(task, evaluation)
        task_state_store.update_task(task)
        self.logger.info("FINAL_VERIFICATION_STATUS status=%s", task.status.value)
        return {
            "task": task,
            "outcome": outcome,
            "evaluation": evaluation,
            "evaluation_mode": evaluation_mode,
        }

    def _objective_loop_flow(
        self,
        task: Task,
        decision: AssistantDecision,
    ) -> dict[str, object]:
        if self.objective_loop is None:
            evaluation = GoalEvaluation(
                satisfied=False,
                reasoning="Objective loop is unavailable because reviewer/verifier adapters are not registered.",
                missing=["Reviewer and verifier adapters"],
                should_continue=False,
                blocked=True,
                completion_confidence=0.0,
            )
            task.status = TaskStatus.BLOCKED
            task_state_store.update_task(task)
            return {
                "task": task,
                "outcome": self._build_outcome(task),
                "evaluation": evaluation,
                "evaluation_mode": "objective_loop_unavailable",
                "iteration_count": 0,
            }
        return self.objective_loop.run(task, decision)

    def _evaluate_task_flow(self, task: Task) -> tuple[GoalEvaluation, str]:
        evaluation, evaluation_mode = self.evaluator.evaluate(task)
        self._apply_evaluation(task, evaluation)
        task.status = self._derive_task_status(task, evaluation)
        self._finalize_objective_state(task, evaluation)
        task_state_store.update_task(task)
        return evaluation, evaluation_mode

    def _finalize_task_flow(
        self,
        task: Task,
        decision: AssistantDecision,
        outcome: TaskOutcome,
        evaluation: GoalEvaluation,
        evaluation_mode: str,
    ) -> ChatResponse:
        task.summary = self.assistant_layer.compose_task_response(
            task,
            decision,
            outcome,
            evaluation,
            evaluation_mode,
        )
        task_state_store.update_task(task)
        self.operator_context.task_finished(task)
        self.operator_context.record_assistant_reply(task.summary)
        return ChatResponse(
            task_id=task.id,
            status=task.status,
            planner_mode=task.planner_mode,
            request_mode=task.request_mode,
            escalation_level=task.escalation_level,
            response=task.summary,
            outcome=outcome,
            subtasks=task.subtasks,
            results=task.results,
        )

    def should_send_progress(self, user_message: str) -> bool:
        decision = self.assistant_layer.decide_without_llm(user_message)
        return not self.fast_action_handler.should_hide_progress(user_message, decision)

    def _reminder_service_from_router(self, router: Router):
        registry = getattr(router, "agent_registry", None)
        if registry is None:
            return None
        reminder_adapter = registry.get("reminder_agent")
        if reminder_adapter is None:
            reminder_adapter = registry.get("reminder_scheduler_agent")
        local_agent = getattr(reminder_adapter, "agent", None)
        reminder_backend = getattr(local_agent, "reminder_adapter", None)
        return getattr(reminder_backend, "service", None)

    def _select_lane(
        self,
        normalized_goal: str,
        decision: AssistantDecision,
    ) -> LaneSelection:
        lowered = normalized_goal.lower()
        if decision.mode == RequestMode.ANSWER:
            if self._is_memory_lane_request(lowered):
                self.logger.info("ROUTE_MEMORY lane=assistant agent=memory_agent")
                return LaneSelection(
                    lane="assistant",
                    agent_id="memory_agent",
                    reasoning="Memory updates and follow-ups should stay on the fast conversational memory lane.",
                )
            self.logger.info("ROUTE_FAST_ASSISTANT lane=assistant agent=assistant_agent")
            return LaneSelection(
                lane="assistant",
                agent_id="assistant_agent",
                reasoning="Lightweight conversational requests should stay on the assistant lane.",
            )
        if decision.mode == RequestMode.ACT:
            if (
                self.fast_action_handler._looks_like_calendar_read_request(lowered)
                or self.fast_action_handler._looks_like_calendar_event_request(lowered)
                or self.fast_action_handler._looks_like_calendar_delete_request(lowered)
                or self.fast_action_handler._looks_like_calendar_update_request(lowered)
                or self.fast_action_handler._looks_like_referent_scheduling_action(lowered)
            ):
                self.logger.info("ROUTE_SCHEDULING lane=fast_action agent=scheduling_agent")
                return LaneSelection(
                    lane="fast_action",
                    agent_id="scheduling_agent",
                    reasoning="Calendar and scheduling actions are owned by the Scheduling / Personal Ops Agent.",
                )
            if self.assistant_layer._looks_like_explicit_reminder_request(lowered):
                self.logger.info("ROUTE_REMINDER lane=fast_action agent=scheduling_agent")
                return LaneSelection(
                    lane="fast_action",
                    agent_id="scheduling_agent",
                    reasoning="Reminder requests are owned by the Scheduling / Personal Ops Agent without heavy planning.",
                )
            if self.fast_action_handler._looks_like_personal_ops_request(normalized_goal):
                self.logger.info("ROUTE_PERSONAL_OPS lane=fast_action agent=personal_ops_agent")
                return LaneSelection(
                    lane="fast_action",
                    agent_id="personal_ops_agent",
                    reasoning="Personal lists, notes, and routine placeholders are owned by Personal Ops.",
                )
            if self.fast_action_handler.can_handle_browser_request(normalized_goal):
                self.logger.info("ROUTE_BROWSER lane=fast_action agent=browser_agent")
                return LaneSelection(
                    lane="fast_action",
                    agent_id="browser_agent",
                    reasoning="Direct browser requests should use the bounded browser fast lane.",
                )
            if self.fast_action_handler.can_handle_local_file_request(normalized_goal):
                self.logger.info("ROUTE_LOCAL_FILE lane=fast_action agent=assistant_agent")
                return LaneSelection(
                    lane="fast_action",
                    agent_id="assistant_agent",
                    reasoning="Simple workspace file actions should use the local fast file lane.",
                )
            if self.fast_action_handler._looks_like_unavailable_email_request(lowered):
                self.logger.info("ROUTE_EMAIL_UNAVAILABLE lane=fast_action agent=communications_agent")
                return LaneSelection(
                    lane="fast_action",
                    agent_id="communications_agent",
                    reasoning="Known-unavailable email delivery should fail fast with setup guidance instead of entering planning.",
                )
            if self._looks_like_communications_request(lowered):
                self.logger.info("ROUTE_PLANNER lane=execution_flow agent=planner_agent")
                return LaneSelection(
                    lane="execution_flow",
                    agent_id="planner_agent",
                    reasoning="Outbound messaging and email requests should route through the planner-backed communications execution flow.",
                )
            self.logger.info("ROUTE_FAST_ASSISTANT lane=fast_action agent=assistant_agent")
            return LaneSelection(
                lane="fast_action",
                agent_id="assistant_agent",
                reasoning="Single-step actions should attempt the fast action lane first.",
            )
        self.logger.info("ROUTE_PLANNER lane=execution_flow agent=planner_agent")
        return LaneSelection(
            lane="execution_flow",
            agent_id="planner_agent",
            reasoning="Complex tasks should route through the planner/review/verifier execution flow.",
        )

    def _is_memory_lane_request(self, lowered: str) -> bool:
        normalized = " ".join(lowered.split())
        return any(
            (
                is_name_statement(normalized),
                is_explicit_memory_statement(normalized),
                is_forget_name_statement(normalized),
                is_short_personal_fact_statement(normalized),
                is_user_memory_question(normalized),
                is_project_memory_question(normalized),
                is_memory_lookup(normalized),
                is_memory_follow_up_phrase(normalized),
            )
        )

    def _looks_like_communications_request(self, lowered: str) -> bool:
        normalized = " ".join(lowered.split())
        if "slack" in normalized and any(
            term in normalized for term in ("send", "message", "dm", "direct message", "notify")
        ):
            return True
        if "email" in normalized and any(
            term in normalized for term in ("send", "draft", "reply")
        ):
            return True
        return False

    def _next_subtask(
        self,
        task: Task,
        *,
        include_review: bool = True,
        include_execution: bool = True,
    ):
        for subtask in task.subtasks:
            if self._has_result(task, subtask.id) or subtask.status == TaskStatus.BLOCKED:
                continue
            if not include_review and subtask.assigned_agent == "reviewer_agent":
                continue
            if not include_execution and subtask.assigned_agent != "reviewer_agent":
                continue
            if all(self._dependency_satisfied(task, dependency_id) for dependency_id in subtask.depends_on):
                return subtask
        return None

    def _merge_subtasks(self, existing: list[SubTask], incoming: list[SubTask]) -> list[SubTask]:
        merged = list(existing)
        signatures = {
            (subtask.title, subtask.objective, subtask.assigned_agent)
            for subtask in existing
        }
        for subtask in incoming:
            signature = (subtask.title, subtask.objective, subtask.assigned_agent)
            if signature in signatures:
                continue
            signatures.add(signature)
            merged.append(subtask)
        return merged

    def _append_result(self, task: Task, result) -> None:
        task = task_state_store.get_task(task.id) or task
        results = list(task.results)
        results.append(result)
        task.results = results
        task_state_store.replace_results(task.id, results)

    def _has_result(self, task: Task, subtask_id: str) -> bool:
        return any(result.subtask_id == subtask_id for result in task.results)

    def _dependency_satisfied(self, task: Task, dependency_id: str) -> bool:
        for result in task.results:
            if result.subtask_id == dependency_id:
                return result.status != AgentExecutionStatus.BLOCKED
        return False

    def _derive_task_status(self, task: Task, evaluation: GoalEvaluation) -> TaskStatus:
        if any(result.status == AgentExecutionStatus.BLOCKED for result in task.results):
            return TaskStatus.BLOCKED
        if evaluation.satisfied:
            return TaskStatus.COMPLETED
        if evaluation.blocked:
            return TaskStatus.BLOCKED
        if not evaluation.satisfied:
            return TaskStatus.RUNNING
        if not evaluation.should_continue and task.escalation_level == ExecutionEscalation.OBJECTIVE_COMPLETION:
            return TaskStatus.RUNNING
        if task.results and all(
            result.status == AgentExecutionStatus.COMPLETED for result in task.results
        ):
            return TaskStatus.COMPLETED
        return TaskStatus.RUNNING

    def _decide_replan(self, task: Task, evaluation: GoalEvaluation, replan_count: int) -> bool:
        if replan_count >= 1:
            return False
        if evaluation.satisfied or evaluation.blocked:
            return False
        if task.escalation_level != ExecutionEscalation.OBJECTIVE_COMPLETION:
            return False
        if not evaluation.should_continue:
            return False
        return any(
            result.agent == "reviewer_agent" and result.status == AgentExecutionStatus.BLOCKED
            for result in task.results
        )

    def _build_outcome(self, task: Task) -> TaskOutcome:
        return TaskOutcome(
            completed=sum(
                1 for result in task.results if result.status == AgentExecutionStatus.COMPLETED
            ),
            blocked=sum(
                1 for result in task.results if result.status == AgentExecutionStatus.BLOCKED
            ),
            simulated=sum(
                1 for result in task.results if result.status == AgentExecutionStatus.SIMULATED
            ),
            planned=sum(
                1 for result in task.results if result.status == AgentExecutionStatus.PLANNED
            ),
            total_subtasks=len(task.subtasks),
        )

    def _build_task_title(self, goal: str) -> str:
        trimmed = goal.strip().rstrip(".")
        if len(trimmed) <= 72:
            return trimmed
        return f"{trimmed[:69]}..."

    def _initial_objective_state(
        self,
        goal: str,
        escalation_level: ExecutionEscalation,
    ) -> ObjectiveState:
        review_status = (
            ReviewStatus.NOT_NEEDED
            if escalation_level == ExecutionEscalation.SINGLE_ACTION
            else ReviewStatus.PENDING
        )
        return ObjectiveState(
            objective=goal,
            escalation_level=escalation_level,
            stage=ObjectiveStage.INTAKE,
            review_status=review_status,
            should_continue=escalation_level != ExecutionEscalation.CONVERSATIONAL_ADVICE,
        )

    def _sync_objective_state_for_plan(self, task: Task) -> None:
        if task.objective_state is None:
            return
        task.objective_state.active_subtask_ids = [subtask.id for subtask in task.subtasks[:1]]
        task.objective_state.delegated_agents = self._delegated_agents_from_subtasks(task)
        task_state_store.update_objective_state(task.id, task.objective_state)

    def _delegated_agents_from_subtasks(self, task: Task) -> list[DelegatedAgentState]:
        records: dict[str, DelegatedAgentState] = {}
        for subtask in task.subtasks:
            if not subtask.assigned_agent:
                continue
            record = records.setdefault(
                subtask.assigned_agent,
                DelegatedAgentState(
                    agent_name=subtask.assigned_agent,
                    role=self._agent_role(subtask.assigned_agent),
                ),
            )
            record.subtask_ids.append(subtask.id)
        return list(records.values())

    def _agent_role(self, agent_name: str) -> str:
        definition = self.router.agent_catalog.by_name(agent_name)
        if definition is None:
            return "execution"
        return definition.kind

    def _record_delegated_agent_result(self, task: Task, subtask, result) -> None:
        if task.objective_state is None:
            return
        existing = next(
            (
                lane
                for lane in task.objective_state.delegated_agents
                if lane.agent_name == (subtask.assigned_agent or result.agent)
            ),
            None,
        )
        if existing is None:
            existing = DelegatedAgentState(
                agent_name=subtask.assigned_agent or result.agent,
                role=self._agent_role(subtask.assigned_agent or result.agent),
                subtask_ids=[subtask.id],
            )
            task.objective_state.delegated_agents.append(existing)
        existing.status = subtask.status
        existing.evidence.extend(self._result_evidence_lines(result))
        existing.notes.append(result.summary)
        task.objective_state.evidence_log.extend(self._result_evidence_lines(result))
        task_state_store.update_objective_state(task.id, task.objective_state)

    def _result_evidence_lines(self, result) -> list[str]:
        lines = [result.summary]
        for evidence in result.evidence:
            if getattr(evidence, "verification_notes", None):
                lines.extend(evidence.verification_notes)
        return [line for line in lines if line]

    def _set_objective_stage(
        self,
        task: Task,
        stage: ObjectiveStage,
        *,
        active_subtask_ids: list[str] | None = None,
        blocked: bool | None = None,
        blocked_reasons: list[str] | None = None,
        should_continue: bool | None = None,
    ) -> None:
        if task.objective_state is None:
            return
        task.objective_state.stage = stage
        if active_subtask_ids is not None:
            task.objective_state.active_subtask_ids = active_subtask_ids
        if blocked is not None:
            task.objective_state.blocked = blocked
        if blocked_reasons is not None:
            task.objective_state.blocked_reasons = blocked_reasons
        if should_continue is not None:
            task.objective_state.should_continue = should_continue
        task_state_store.update_objective_state(task.id, task.objective_state)

    def _apply_evaluation(self, task: Task, evaluation: GoalEvaluation) -> None:
        if task.objective_state is None:
            return
        task.objective_state.completion_confidence = evaluation.completion_confidence
        task.objective_state.last_evaluation_reasoning = evaluation.reasoning
        task.objective_state.should_continue = evaluation.should_continue
        task.objective_state.blocked = evaluation.blocked
        if evaluation.blocked and evaluation.missing:
            task.objective_state.blocked_reasons = evaluation.missing
        if evaluation.needs_review:
            task.objective_state.review_status = ReviewStatus.PENDING
        elif evaluation.satisfied:
            task.objective_state.review_status = ReviewStatus.VERIFIED
        task_state_store.update_objective_state(task.id, task.objective_state)

    def _finalize_objective_state(self, task: Task, evaluation: GoalEvaluation) -> None:
        if task.objective_state is None:
            return
        if task.status == TaskStatus.COMPLETED:
            task.objective_state.stage = ObjectiveStage.COMPLETED
            task.objective_state.review_status = (
                ReviewStatus.NOT_NEEDED
                if task.escalation_level == ExecutionEscalation.SINGLE_ACTION
                else ReviewStatus.VERIFIED
            )
            task.objective_state.should_continue = False
        elif task.status == TaskStatus.BLOCKED:
            task.objective_state.stage = ObjectiveStage.BLOCKED
            task.objective_state.review_status = ReviewStatus.FAILED
            task.objective_state.should_continue = False
        else:
            task.objective_state.stage = ObjectiveStage.ADAPTING
            task.objective_state.should_continue = evaluation.should_continue
        task_state_store.update_objective_state(task.id, task.objective_state)

    def _iteration_budget(self, task: Task) -> int:
        if task.escalation_level == ExecutionEscalation.OBJECTIVE_COMPLETION:
            return max(self.max_iterations, len(task.subtasks) + 1)
        return self.max_iterations

    def _resolve_browser_retry_goal(self, normalized_goal: str) -> str:
        lowered = normalized_goal.lower().strip()
        if lowered not in {"try again", "retry", "please try again", "run that again", "do that again"}:
            return normalized_goal
        if self._last_browser_goal:
            return self._last_browser_goal
        for task in task_state_store.list_tasks():
            if self._is_browser_task(task):
                return task.goal
        return normalized_goal

    def _response_has_browser_result(self, response: ChatResponse) -> bool:
        return any(result.tool_name == "browser_tool" for result in response.results)

    def _is_browser_task(self, task: Task) -> bool:
        if any(result.tool_name == "browser_tool" for result in task.results):
            return True
        if any(
            subtask.assigned_agent == "browser_agent"
            or (subtask.tool_invocation and subtask.tool_invocation.tool_name == "browser_tool")
            for subtask in task.subtasks
        ):
            return True
        return bool(re.search(r"((?:https?|file)://[^\s)]+)", task.goal, flags=re.IGNORECASE))


supervisor = Supervisor()
