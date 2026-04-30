"""Focused coverage for the thin LangGraph orchestration substrate."""

from __future__ import annotations

import unittest

from agents.adapter import AssistantAgentAdapter
from core.interaction_context import InteractionContext, bind_interaction_context
from core.models import (
    AssistantDecision,
    ChatResponse,
    ExecutionEscalation,
    GoalEvaluation,
    LaneSelection,
    RequestMode,
    Task,
    TaskOutcome,
    TaskStatus,
)
from core.orchestration_graph import SovereignOrchestrationGraph


class GraphAssistantStub:
    def __init__(self, decision: AssistantDecision, *, answer_text: str = "answer") -> None:
        self.decision = decision
        self.answer_text = answer_text
        self.decide_calls = 0
        self.answer_calls = 0

    def decide(self, _: str) -> AssistantDecision:
        self.decide_calls += 1
        return self.decision

    def build_answer_response(self, _: str, decision: AssistantDecision) -> ChatResponse:
        self.answer_calls += 1
        return ChatResponse(
            task_id="answer-1",
            status=TaskStatus.COMPLETED,
            planner_mode="conversation",
            request_mode=decision.mode,
            escalation_level=decision.escalation_level,
            response=self.answer_text,
            outcome=TaskOutcome(total_subtasks=0),
            subtasks=[],
            results=[],
        )


class FastActionStub:
    def __init__(self, response: ChatResponse | None) -> None:
        self.response = response
        self.calls = 0

    def handle(self, _: str, __: AssistantDecision) -> ChatResponse | None:
        self.calls += 1
        return self.response


class QueuedAssistantStub:
    def __init__(self, decisions: list[AssistantDecision]) -> None:
        self.decisions = decisions
        self.decide_calls = 0

    def decide(self, _: str) -> AssistantDecision:
        decision = self.decisions[self.decide_calls]
        self.decide_calls += 1
        return decision

    def build_answer_response(self, message: str, decision: AssistantDecision) -> ChatResponse:
        return ChatResponse(
            task_id=f"answer-{self.decide_calls}",
            status=TaskStatus.COMPLETED,
            planner_mode="conversation",
            request_mode=decision.mode,
            escalation_level=decision.escalation_level,
            response=f"answer:{message}",
            outcome=TaskOutcome(total_subtasks=0),
            subtasks=[],
            results=[],
        )


class LangGraphOrchestrationTests(unittest.TestCase):
    def test_answer_path_stays_shallow_and_returns_direct_reply(self) -> None:
        assistant = GraphAssistantStub(
            AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning="conversational",
                should_use_tools=False,
            ),
            answer_text="Hi. What can I help with?",
        )
        assistant_adapter = AssistantAgentAdapter(assistant_layer=assistant)
        fast_actions = FastActionStub(response=None)
        plan_calls: list[str] = []

        graph = SovereignOrchestrationGraph(
            assistant_layer=assistant,
            assistant_adapter=assistant_adapter,
            fast_action_handler=fast_actions,
            memory_backend="hybrid",
            lane_selector=lambda message, decision: LaneSelection(
                lane="assistant",
                agent_id="assistant_agent",
                reasoning=f"assistant:{message}:{decision.mode.value}",
            ),
            plan_task_flow=lambda message, decision, task, replan_count: plan_calls.append(message) or {
                "task": task or Task(goal=message, title=message, description=message),
                "iteration_count": 0,
            },
            evaluate_task=lambda task: (GoalEvaluation(satisfied=False, reasoning=task.goal), "deterministic"),
            finalize_task_flow=lambda *args: self.fail("Finalize should not run for answer mode."),
        )

        response = graph.invoke("hi")

        self.assertEqual(response.response, "Hi. What can I help with?")
        self.assertEqual(assistant.decide_calls, 1)
        self.assertEqual(assistant.answer_calls, 1)
        self.assertEqual(fast_actions.calls, 0)
        self.assertEqual(plan_calls, [])

    def test_act_path_can_fall_through_to_execution_when_fast_path_declines(self) -> None:
        assistant = GraphAssistantStub(
            AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="small action",
                should_use_tools=True,
            )
        )
        assistant_adapter = AssistantAgentAdapter(assistant_layer=assistant)
        fast_actions = FastActionStub(response=None)
        execution_log: list[str] = []
        finalize_log: list[str] = []

        def plan_task_flow(
            message: str,
            decision: AssistantDecision,
            task: Task | None,
            replan_count: int,
        ) -> dict[str, object]:
            del decision, replan_count
            return {
                "task": task or Task(
                    goal=message,
                    title=message,
                    description=message,
                    status=TaskStatus.COMPLETED,
                ),
                "iteration_count": 0,
            }

        def execute_task_flow(task: Task, decision: AssistantDecision) -> dict[str, object]:
            execution_log.append(f"{task.goal}:{decision.mode.value}")
            task.status = TaskStatus.COMPLETED
            return {
                "task": task,
                "iteration_count": 1,
            }

        def verify_task_flow(task: Task, _decision: AssistantDecision) -> dict[str, object]:
            return {
                "task": task,
                "outcome": TaskOutcome(completed=1, total_subtasks=1),
                "evaluation": GoalEvaluation(
                    satisfied=True,
                    reasoning="completed",
                    should_continue=False,
                    completion_confidence=0.9,
                ),
                "evaluation_mode": "deterministic",
            }

        def finalize_task_flow(
            task: Task,
            decision: AssistantDecision,
            outcome: TaskOutcome,
            evaluation: GoalEvaluation,
            evaluation_mode: str,
        ) -> ChatResponse:
            finalize_log.append(f"{task.goal}:{evaluation_mode}:{outcome.completed}:{evaluation.satisfied}")
            return ChatResponse(
                task_id="task-1",
                status=task.status,
                planner_mode="graph-test",
                request_mode=decision.mode,
                escalation_level=decision.escalation_level,
                response="Executed after fast-path fallback.",
                outcome=outcome,
                subtasks=[],
                results=[],
            )

        graph = SovereignOrchestrationGraph(
            assistant_layer=assistant,
            assistant_adapter=assistant_adapter,
            fast_action_handler=fast_actions,
            memory_backend="local",
            lane_selector=lambda _message, _decision: LaneSelection(
                lane="fast_action",
                agent_id="assistant_agent",
                reasoning="test fast action",
            ),
            plan_task_flow=plan_task_flow,
            execute_task_flow=execute_task_flow,
            verify_task_flow=verify_task_flow,
            finalize_task_flow=finalize_task_flow,
            evaluate_task=lambda task: (
                GoalEvaluation(satisfied=True, reasoning=f"verified {task.goal}", should_continue=False),
                "deterministic",
            ),
        )

        response = graph.invoke("create hello.txt")

        self.assertEqual(response.response, "Executed after fast-path fallback.")
        self.assertEqual(fast_actions.calls, 1)
        self.assertEqual(execution_log, ["create hello.txt:act"])
        self.assertEqual(finalize_log, ["create hello.txt:deterministic:1:True"])

    def test_graph_enables_in_memory_checkpointing_without_touching_memory_backend(self) -> None:
        assistant = GraphAssistantStub(
            AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning="conversational",
                should_use_tools=False,
            )
        )
        assistant_adapter = AssistantAgentAdapter(assistant_layer=assistant)
        graph = SovereignOrchestrationGraph(
            assistant_layer=assistant,
            assistant_adapter=assistant_adapter,
            fast_action_handler=FastActionStub(response=None),
            memory_backend="zep",
            lane_selector=lambda _message, _decision: LaneSelection(
                lane="assistant",
                agent_id="assistant_agent",
                reasoning="test assistant lane",
            ),
            plan_task_flow=lambda message, decision, task, replan_count: {
                "task": task or Task(goal=message, title=message, description=message),
                "iteration_count": 0,
            },
            evaluate_task=lambda task: (GoalEvaluation(satisfied=False, reasoning=task.goal), "deterministic"),
            finalize_task_flow=lambda *args: self.fail("Finalize should not run for answer mode."),
        )

        self.assertTrue(graph.checkpoint_enabled)
        self.assertEqual(graph.memory_backend, "zep")
        self.assertIsNotNone(graph.checkpointer)

    def test_checkpointed_thread_does_not_reuse_prior_response_payload(self) -> None:
        assistant = QueuedAssistantStub(
            [
                AssistantDecision(
                    mode=RequestMode.ANSWER,
                    escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                    reasoning="first answer",
                    should_use_tools=False,
                ),
                AssistantDecision(
                    mode=RequestMode.ACT,
                    escalation_level=ExecutionEscalation.SINGLE_ACTION,
                    reasoning="second action",
                    should_use_tools=True,
                ),
            ]
        )
        assistant_adapter = AssistantAgentAdapter(assistant_layer=assistant)
        fast_actions = FastActionStub(
            response=ChatResponse(
                task_id="action-1",
                status=TaskStatus.COMPLETED,
                planner_mode="fast_action",
                request_mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                response="second turn action result",
                outcome=TaskOutcome(total_subtasks=1, completed=1),
                subtasks=[],
                results=[],
            )
        )
        graph = SovereignOrchestrationGraph(
            assistant_layer=assistant,
            assistant_adapter=assistant_adapter,
            fast_action_handler=fast_actions,
            memory_backend="local",
            lane_selector=lambda _message, decision: LaneSelection(
                lane="assistant" if decision.mode == RequestMode.ANSWER else "fast_action",
                agent_id="assistant_agent",
                reasoning="test lane",
            ),
            plan_task_flow=lambda message, decision, task, replan_count: {
                "task": task or Task(goal=message, title=message, description=message),
                "iteration_count": 0,
            },
            evaluate_task=lambda task: (GoalEvaluation(satisfied=False, reasoning=task.goal), "deterministic"),
            finalize_task_flow=lambda *args: self.fail("Finalize should not run for this regression."),
        )

        with bind_interaction_context(InteractionContext(source="slack", channel_id="D123", user_id="U123")):
            first = graph.invoke("hi")
            second = graph.invoke("create a task")

        self.assertEqual(first.response, "answer:hi")
        self.assertEqual(second.response, "second turn action result")
        self.assertEqual(fast_actions.calls, 1)


if __name__ == "__main__":
    unittest.main()
