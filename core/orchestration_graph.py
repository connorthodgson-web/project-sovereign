"""LangGraph CEO spine for assistant, planning, execution, review, and verification."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypedDict
from uuid import uuid4
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from agents.adapter import AssistantAgentAdapter
from core.fast_actions import FastActionHandler
from core.interaction_context import get_interaction_context
from core.logging import get_logger
from core.models import AssistantDecision, ChatResponse, GoalEvaluation, LaneSelection, Task, TaskOutcome
from core.request_trace import current_request_trace


class SovereignGraphState(TypedDict, total=False):
    """Lean graph state for the CEO supervisor spine."""

    user_message: str
    normalized_message: str
    memory_backend: str
    decision: AssistantDecision
    lane_selection: LaneSelection
    current_task: Task
    outcome: TaskOutcome
    evaluation: GoalEvaluation
    evaluation_mode: str
    response_payload: ChatResponse
    iteration_count: int
    replan_requested: bool
    replan_count: int


class SovereignOrchestrationGraph:
    """LangGraph spine with fast paths plus planner/reviewer/verifier stages."""

    def __init__(
        self,
        *,
        assistant_layer,
        assistant_adapter: AssistantAgentAdapter,
        fast_action_handler: FastActionHandler,
        memory_backend: str,
        lane_selector: Callable[[str, AssistantDecision], LaneSelection],
        plan_task_flow: Callable[[str, AssistantDecision, Task | None, int], dict[str, object]] | None = None,
        execute_task_flow: Callable[[Task, AssistantDecision], dict[str, object]] | None = None,
        review_task_flow: Callable[[Task, AssistantDecision], dict[str, object]] | None = None,
        decide_replan: Callable[[Task, GoalEvaluation, int], bool] | None = None,
        verify_task_flow: Callable[[Task, AssistantDecision], dict[str, object]] | None = None,
        objective_loop_flow: Callable[[Task, AssistantDecision], dict[str, object]] | None = None,
        evaluate_task: Callable[[Task], tuple[GoalEvaluation, str]],
        finalize_task_flow: Callable[
            [Task, AssistantDecision, TaskOutcome, GoalEvaluation, str],
            ChatResponse,
        ],
    ) -> None:
        self.logger = get_logger(__name__)
        self.assistant_layer = assistant_layer
        self.assistant_adapter = assistant_adapter
        self.fast_action_handler = fast_action_handler
        self.memory_backend = memory_backend
        self.lane_selector = lane_selector
        self.plan_task_flow = plan_task_flow or self._default_plan_task_flow
        self.execute_task_flow = execute_task_flow or self._default_execute_task_flow
        self.review_task_flow = review_task_flow or self._default_review_task_flow
        self.decide_replan = decide_replan or (lambda _task, _evaluation, _count: False)
        self.verify_task_flow = verify_task_flow or self._default_verify_task_flow
        self.objective_loop_flow = objective_loop_flow
        self.evaluate_task = evaluate_task
        self.finalize_task_flow = finalize_task_flow
        self.checkpointer = MemorySaver()
        self._graph = self._build_graph()

    @property
    def checkpoint_enabled(self) -> bool:
        return True

    def invoke(self, user_message: str) -> ChatResponse:
        normalized_message = " ".join(user_message.split())
        self.logger.info("LANGGRAPH_START message=%r normalized=%r", user_message, normalized_message)
        result = self._graph.invoke(
            {
                "user_message": user_message,
                "normalized_message": normalized_message,
                "memory_backend": self.memory_backend,
                "iteration_count": 0,
                "replan_count": 0,
            },
            config=self._invoke_config(),
        )
        response = result.get("response_payload")
        if response is None:
            raise RuntimeError("LangGraph orchestration finished without a response payload.")
        self.logger.info(
            "FINAL_RESPONSE planner_mode=%s request_mode=%s status=%s",
            response.planner_mode,
            response.request_mode.value,
            response.status.value,
        )
        return response

    def _build_graph(self):
        graph = StateGraph(SovereignGraphState)
        graph.add_node("start", self._start_node)
        graph.add_node("select_lane", self._select_lane_node)
        graph.add_node("assistant_or_fast_action", self._assistant_or_fast_action_node)
        graph.add_node("planning", self._planning_node)
        graph.add_node("objective_loop", self._objective_loop_node)
        graph.add_node("agent_execution", self._agent_execution_node)
        graph.add_node("review", self._review_node)
        graph.add_node("replan_decision", self._replan_decision_node)
        graph.add_node("verifier", self._verifier_node)
        graph.add_node("final_response", self._final_response_node)

        graph.add_edge(START, "start")
        graph.add_edge("start", "select_lane")
        graph.add_conditional_edges(
            "select_lane",
            self._route_after_lane_selection,
            {
                "assistant_or_fast_action": "assistant_or_fast_action",
                "planning": "planning",
            },
        )
        graph.add_conditional_edges(
            "assistant_or_fast_action",
            self._route_after_fast_path,
            {
                "final_response": "final_response",
                "planning": "planning",
            },
        )
        graph.add_conditional_edges(
            "planning",
            self._route_after_planning,
            {
                "objective_loop": "objective_loop",
                "agent_execution": "agent_execution",
            },
        )
        graph.add_edge("objective_loop", "final_response")
        graph.add_edge("agent_execution", "review")
        graph.add_edge("review", "replan_decision")
        graph.add_conditional_edges(
            "replan_decision",
            self._route_after_replan_decision,
            {
                "planning": "planning",
                "verifier": "verifier",
            },
        )
        graph.add_edge("verifier", "final_response")
        graph.add_edge("final_response", END)
        return graph.compile(checkpointer=self.checkpointer)

    def _start_node(self, state: SovereignGraphState) -> SovereignGraphState:
        # Checkpointing keeps thread identity stable across Slack turns, but each
        # user message needs a clean per-turn execution envelope.
        return {
            "user_message": state.get("user_message", ""),
            "normalized_message": state["normalized_message"],
            "memory_backend": state.get("memory_backend", self.memory_backend),
            "decision": None,
            "lane_selection": None,
            "current_task": None,
            "outcome": None,
            "evaluation": None,
            "evaluation_mode": None,
            "response_payload": None,
            "iteration_count": 0,
            "replan_requested": False,
            "replan_count": 0,
        }

    def _select_lane_node(self, state: SovereignGraphState) -> SovereignGraphState:
        normalized_message = state["normalized_message"]
        decision = self.assistant_layer.decide(normalized_message)
        lane_selection = self.lane_selector(normalized_message, decision)
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
        return {
            "decision": decision,
            "lane_selection": lane_selection,
        }

    def _assistant_or_fast_action_node(self, state: SovereignGraphState) -> SovereignGraphState:
        decision = state["decision"]
        normalized_message = state["normalized_message"]
        lane_selection = state["lane_selection"]
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
            response = self.assistant_adapter.build_response(normalized_message, decision)
            self.logger.info(
                "AGENT_ADAPTER_END agent_id=%s status=%s tool=%s",
                lane_selection.agent_id,
                response.status.value,
                None,
            )
            return {
                "response_payload": response
            }

        if lane_selection.lane == "fast_action":
            self.logger.info(
                "AGENT_ADAPTER_START agent_id=%s provider=local mode=act",
                lane_selection.agent_id,
            )
            response = self.fast_action_handler.handle(normalized_message, decision)
            if response is not None:
                self.logger.info(
                    "AGENT_ADAPTER_END agent_id=%s status=%s tool=%s",
                    lane_selection.agent_id,
                    response.status.value,
                    response.results[0].tool_name if response.results else None,
                )
                return {"response_payload": response}
            self.logger.info(
                "AGENT_ADAPTER_END agent_id=%s status=%s tool=%s",
                lane_selection.agent_id,
                "fallthrough",
                None,
            )
        return {}

    def _planning_node(self, state: SovereignGraphState) -> SovereignGraphState:
        decision = state["decision"]
        task = state.get("current_task")
        replan_count = state.get("replan_count", 0)
        planning_state = self.plan_task_flow(
            state["normalized_message"],
            decision,
            task,
            replan_count,
        )
        return {
            "current_task": planning_state["task"],
            "iteration_count": planning_state.get("iteration_count", state.get("iteration_count", 0)),
        }

    def _agent_execution_node(self, state: SovereignGraphState) -> SovereignGraphState:
        task = state.get("current_task")
        decision = state.get("decision")
        if task is None or decision is None:
            raise RuntimeError("Execution node is missing planning state.")
        execution_state = self.execute_task_flow(task, decision)
        return {
            "current_task": execution_state["task"],
            "iteration_count": execution_state["iteration_count"],
        }

    def _objective_loop_node(self, state: SovereignGraphState) -> SovereignGraphState:
        task = state.get("current_task")
        decision = state.get("decision")
        if task is None or decision is None or self.objective_loop_flow is None:
            raise RuntimeError("Objective loop node is missing required state.")
        loop_state = self.objective_loop_flow(task, decision)
        return {
            "current_task": loop_state["task"],
            "outcome": loop_state["outcome"],
            "evaluation": loop_state["evaluation"],
            "evaluation_mode": loop_state["evaluation_mode"],
            "iteration_count": loop_state.get("iteration_count", state.get("iteration_count", 0)),
        }

    def _review_node(self, state: SovereignGraphState) -> SovereignGraphState:
        task = state.get("current_task")
        decision = state.get("decision")
        if task is None or decision is None:
            raise RuntimeError("Review node is missing execution state.")
        review_state = self.review_task_flow(task, decision)
        evaluation = review_state.get("evaluation")
        evaluation_mode = review_state.get("evaluation_mode", "deterministic")
        if evaluation is None:
            evaluation, evaluation_mode = self.evaluate_task(review_state["task"])
        return {
            "current_task": review_state["task"],
            "evaluation": evaluation,
            "evaluation_mode": evaluation_mode,
            "iteration_count": review_state.get("iteration_count", state.get("iteration_count", 0)),
        }

    def _replan_decision_node(self, state: SovereignGraphState) -> SovereignGraphState:
        task = state.get("current_task")
        evaluation = state.get("evaluation")
        if task is None or evaluation is None:
            raise RuntimeError("Replan decision node is missing evaluation state.")
        replan_count = state.get("replan_count", 0)
        should_replan = self.decide_replan(task, evaluation, replan_count)
        self.logger.info(
            "REPLAN_DECISION task=%s should_replan=%s iteration_count=%s replan_count=%s",
            task.id,
            should_replan,
            state.get("iteration_count", 0),
            replan_count,
        )
        return {
            "replan_requested": should_replan,
            "replan_count": replan_count + (1 if should_replan else 0),
        }

    def _verifier_node(self, state: SovereignGraphState) -> SovereignGraphState:
        task = state.get("current_task")
        decision = state.get("decision")
        if task is None or decision is None:
            raise RuntimeError("Verifier node is missing task state.")
        verification_state = self.verify_task_flow(task, decision)
        return {
            "current_task": verification_state["task"],
            "outcome": verification_state["outcome"],
            "evaluation": verification_state["evaluation"],
            "evaluation_mode": verification_state["evaluation_mode"],
        }

    def _final_response_node(self, state: SovereignGraphState) -> SovereignGraphState:
        if state.get("response_payload") is not None:
            return {}

        task = state.get("current_task")
        decision = state.get("decision")
        outcome = state.get("outcome")
        evaluation = state.get("evaluation")
        evaluation_mode = state.get("evaluation_mode", "deterministic")
        if task is None or decision is None or outcome is None or evaluation is None:
            raise RuntimeError("LangGraph final response node is missing required execution state.")

        return {
            "response_payload": self.finalize_task_flow(
                task,
                decision,
                outcome,
                evaluation,
                evaluation_mode,
            )
        }

    def _route_after_lane_selection(self, state: SovereignGraphState) -> str:
        lane_selection = state["lane_selection"]
        if lane_selection.lane in {"assistant", "fast_action"}:
            return "assistant_or_fast_action"
        return "planning"

    def _route_after_fast_path(self, state: SovereignGraphState) -> str:
        if state.get("response_payload") is not None:
            return "final_response"
        return "planning"

    def _route_after_planning(self, state: SovereignGraphState) -> str:
        decision = state.get("decision")
        if (
            self.objective_loop_flow is not None
            and decision is not None
            and decision.mode.value == "execute"
        ):
            return "objective_loop"
        return "agent_execution"

    def _route_after_replan_decision(self, state: SovereignGraphState) -> str:
        if state.get("replan_requested"):
            return "planning"
        return "verifier"

    def _default_plan_task_flow(
        self,
        normalized_message: str,
        decision: AssistantDecision,
        task: Task | None,
        replan_count: int,
    ) -> dict[str, object]:
        del decision, replan_count
        return {
            "task": task or Task(goal=normalized_message, title=normalized_message, description=normalized_message),
            "iteration_count": 0,
        }

    def _default_execute_task_flow(
        self,
        task: Task,
        decision: AssistantDecision,
    ) -> dict[str, object]:
        del decision
        return {"task": task, "iteration_count": 0}

    def _default_review_task_flow(
        self,
        task: Task,
        decision: AssistantDecision,
    ) -> dict[str, object]:
        del decision
        evaluation, evaluation_mode = self.evaluate_task(task)
        return {
            "task": task,
            "evaluation": evaluation,
            "evaluation_mode": evaluation_mode,
            "iteration_count": 0,
        }

    def _default_verify_task_flow(
        self,
        task: Task,
        decision: AssistantDecision,
    ) -> dict[str, object]:
        del decision
        evaluation, evaluation_mode = self.evaluate_task(task)
        return {
            "task": task,
            "outcome": TaskOutcome(total_subtasks=len(task.subtasks)),
            "evaluation": evaluation,
            "evaluation_mode": evaluation_mode,
        }

    def _invoke_config(self) -> dict[str, dict[str, str]]:
        interaction = get_interaction_context()
        if interaction is None:
            thread_id = f"sovereign-local-thread:{uuid4()}"
        else:
            parts = [
                interaction.source or "local",
                interaction.channel_id or "no-channel",
                interaction.user_id or "no-user",
            ]
            thread_id = ":".join(parts)
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "supervisor",
            }
        }
