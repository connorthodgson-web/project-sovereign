"""Coverage for structured model routing, escalation, and provider selection."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.codex_cli_agent import CodexCliAgentAdapter
from app.config import settings
from core.assistant import AssistantLayer
from core.evaluator import GoalEvaluator
from core.model_routing import (
    ModelRequestContext,
    ModelRouter,
    ModelTier,
)
from core.models import (
    AgentDescriptor,
    AgentProvider,
    ExecutionEscalation,
    GoalEvaluation,
    RequestMode,
    SubTask,
    Task,
    TaskStatus,
)
from core.planner import Planner
from core.router import Router
from core.supervisor import Supervisor
from integrations.openrouter_client import OpenRouterClient


class _FakeHttpResponse:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeHttpClient:
    def __init__(self, records: list[dict[str, str]], responses: list[str], **kwargs) -> None:
        del kwargs
        self._records = records
        self._responses = responses

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, url: str, *, headers: dict, json: dict) -> _FakeHttpResponse:
        del headers
        self._records.append({"url": url, "model": str(json["model"])})
        return _FakeHttpResponse(self._responses.pop(0))


class _ExplodingClient:
    def is_configured(self) -> bool:
        return True

    def prompt(self, *args, **kwargs) -> str:
        del args, kwargs
        raise AssertionError("This path should not call an LLM.")


class StructuredModelRoutingTests(unittest.TestCase):
    def test_assistant_fast_path_uses_no_heavy_model(self) -> None:
        llm = _ExplodingClient()
        supervisor = Supervisor(
            assistant_layer=AssistantLayer(openrouter_client=llm),
            planner=Planner(openrouter_client=llm),
            router=Router(openrouter_client=llm),
            evaluator=GoalEvaluator(openrouter_client=llm),
        )

        response = supervisor.handle_user_goal("hi")

        self.assertEqual(response.request_mode, RequestMode.ANSWER)
        self.assertEqual(response.planner_mode, "conversation_fast_path")

    def test_memory_fast_path_uses_no_heavy_model(self) -> None:
        llm = _ExplodingClient()
        supervisor = Supervisor(
            assistant_layer=AssistantLayer(openrouter_client=llm),
            planner=Planner(openrouter_client=llm),
            router=Router(openrouter_client=llm),
            evaluator=GoalEvaluator(openrouter_client=llm),
        )

        response = supervisor.handle_user_goal("what do you remember about me?")

        self.assertEqual(response.request_mode, RequestMode.ANSWER)
        self.assertIn(response.planner_mode, {"conversation_memory_fast_path", "conversation"})

    def test_browser_synthesis_starts_on_tier2_for_normal_evidence(self) -> None:
        router = ModelRouter()

        selection = router.select(
            label="browser_synthesis",
            prompt="Summarize the browser evidence.",
            context=ModelRequestContext(
                intent_label="browser_action",
                request_mode="act",
                selected_lane="browser",
                selected_agent="browser_agent",
                task_complexity="medium",
                risk_level="medium",
                requires_tool_use=True,
                requires_review=True,
                evidence_quality="medium",
                user_visible_latency_sensitivity="medium",
                cost_sensitivity="medium",
            ),
        )

        self.assertEqual(selection.tier, ModelTier.TIER_2)

    def test_browser_synthesis_escalates_to_tier3_when_evidence_is_low(self) -> None:
        records: list[dict[str, str]] = []
        responses = ["Grounded summary from limited evidence.", "Grounded summary from limited evidence."]

        def fake_client_factory(**kwargs):
            return _FakeHttpClient(records, responses, **kwargs)

        with (
            patch.object(settings, "openrouter_api_key", "test-openrouter-key"),
            patch.object(settings, "openai_enabled", False),
            patch("integrations.openrouter_client.httpx.Client", fake_client_factory),
        ):
            client = OpenRouterClient(api_key="test-openrouter-key")
            response = client.prompt(
                "Summarize limited browser evidence.",
                label="browser_synthesis",
                context=ModelRequestContext(
                    intent_label="browser_action",
                    request_mode="act",
                    selected_lane="browser",
                    selected_agent="browser_agent",
                    task_complexity="medium",
                    risk_level="medium",
                    requires_tool_use=True,
                    requires_review=True,
                    evidence_quality="low",
                    user_visible_latency_sensitivity="medium",
                    cost_sensitivity="medium",
                ),
            )

        self.assertEqual(response, "Grounded summary from limited evidence.")
        self.assertEqual(len(records), 2)

    def test_planner_uses_tier2_for_normal_planning(self) -> None:
        router = ModelRouter()

        selection = router.select(
            label="planner_create_plan",
            prompt="Break the goal into concrete subtasks.",
            context=ModelRequestContext(
                intent_label="planning",
                request_mode="execute",
                selected_lane="execution_flow",
                selected_agent="planner_agent",
                task_complexity="medium",
                risk_level="medium",
                requires_tool_use=False,
                requires_review=True,
                evidence_quality="unknown",
                user_visible_latency_sensitivity="medium",
                cost_sensitivity="medium",
            ),
        )

        self.assertEqual(selection.tier, ModelTier.TIER_2)

    def test_verifier_can_escalate_to_tier3_after_rejected_evidence(self) -> None:
        router = ModelRouter()

        selection = router.select(
            label="goal_evaluate",
            prompt="Evaluate whether the task is complete after a reviewer rejection.",
            context=ModelRequestContext(
                intent_label="verification",
                request_mode="execute",
                selected_lane="verification",
                selected_agent="verifier_agent",
                task_complexity="high",
                risk_level="high",
                requires_tool_use=True,
                requires_review=True,
                reviewer_rejected=True,
                evidence_quality="low",
                user_visible_latency_sensitivity="medium",
                cost_sensitivity="medium",
            ),
        )

        self.assertEqual(selection.tier, ModelTier.TIER_3)

    def test_codex_prompt_receives_model_guidance_without_extra_model_call(self) -> None:
        descriptor = AgentDescriptor(
            agent_id="codex_cli_agent",
            display_name="Codex CLI Agent",
            provider=AgentProvider.CODEX_CLI,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_settings = SimpleNamespace(
                codex_cli_enabled=False,
                codex_cli_command="codex",
                codex_cli_workspace_root=temp_dir,
                codex_cli_timeout_seconds=30,
                codex_cli_auto_mode=False,
            )
            adapter = CodexCliAgentAdapter(
                descriptor=descriptor,
                runtime_settings=runtime_settings,
                which=lambda _: "codex",
            )

            prompt = adapter._build_bounded_prompt(
                Task(
                    goal="Build a feature, fix the regression, and add tests.",
                    title="Coding task",
                    description="Coding task",
                    escalation_level=ExecutionEscalation.OBJECTIVE_COMPLETION,
                ),
                SubTask(
                    title="Execute bounded coding task",
                    description="Use Codex for bounded coding work.",
                    objective="Build the feature and keep going until it works.",
                    assigned_agent="codex_cli_agent",
                ),
                Path(temp_dir),
            )

        self.assertIn("Reasoning tier guidance:", prompt)
        self.assertIn("Tier rationale:", prompt)

    def test_openai_tier3_disabled_returns_honest_fallback_state(self) -> None:
        with (
            patch.object(settings, "openrouter_api_key", None),
            patch.object(settings, "openai_enabled", False),
            patch.object(settings, "openai_api_key", None),
            patch.object(settings, "openai_model_tier_3", None),
        ):
            client = OpenRouterClient(api_key=None)

            with self.assertRaises(RuntimeError) as exc:
                client.prompt(
                    "Verify a failed critical task.",
                    label="goal_evaluate",
                    context=ModelRequestContext(
                        intent_label="verification",
                        request_mode="execute",
                        selected_lane="verification",
                        selected_agent="verifier_agent",
                        task_complexity="high",
                        risk_level="high",
                        requires_tool_use=True,
                        requires_review=True,
                        verifier_failed=True,
                        evidence_quality="low",
                        user_visible_latency_sensitivity="medium",
                        cost_sensitivity="medium",
                        fallback_allowed=False,
                    ),
                )

        self.assertIn("disabled", str(exc.exception).lower())

    def test_openai_tier3_enabled_uses_direct_provider(self) -> None:
        records: list[dict[str, str]] = []
        responses = ['{"satisfied": false, "reasoning": "Needs more work.", "missing": [], "completion_confidence": 0.3}']

        def fake_client_factory(**kwargs):
            return _FakeHttpClient(records, responses, **kwargs)

        with (
            patch.object(settings, "openrouter_api_key", "test-openrouter-key"),
            patch.object(settings, "openai_enabled", True),
            patch.object(settings, "openai_api_key", "test-openai-key"),
            patch.object(settings, "openai_model_tier_3", "gpt-5.1"),
            patch("integrations.openrouter_client.httpx.Client", fake_client_factory),
        ):
            client = OpenRouterClient(api_key="test-openrouter-key")
            client.prompt(
                "Verify a failed critical task.",
                label="goal_evaluate",
                context=ModelRequestContext(
                    intent_label="verification",
                    request_mode="execute",
                    selected_lane="verification",
                    selected_agent="verifier_agent",
                    task_complexity="high",
                    risk_level="high",
                    requires_tool_use=True,
                    requires_review=True,
                    verifier_failed=True,
                    evidence_quality="low",
                    user_visible_latency_sensitivity="medium",
                    cost_sensitivity="medium",
                ),
            )

        self.assertEqual(len(records), 1)
        self.assertIn("api.openai.com", records[0]["url"])
        self.assertEqual(records[0]["model"], "gpt-5.1")

    def test_successful_request_does_not_make_duplicate_model_calls(self) -> None:
        records: list[dict[str, str]] = []
        responses = ['{"subtasks":[{"title":"Plan","description":"Plan","objective":"Plan","agent_hint":"research_agent","tool_invocation":null}]}']

        def fake_client_factory(**kwargs):
            return _FakeHttpClient(records, responses, **kwargs)

        with (
            patch.object(settings, "openrouter_api_key", "test-openrouter-key"),
            patch.object(settings, "openai_enabled", False),
            patch("integrations.openrouter_client.httpx.Client", fake_client_factory),
        ):
            client = OpenRouterClient(api_key="test-openrouter-key")
            client.prompt(
                "Create a plan.",
                label="planner_create_plan",
                context=ModelRequestContext(
                    intent_label="planning",
                    request_mode="execute",
                    selected_lane="execution_flow",
                    selected_agent="planner_agent",
                    task_complexity="medium",
                    risk_level="medium",
                    requires_tool_use=False,
                    requires_review=True,
                    evidence_quality="unknown",
                    user_visible_latency_sensitivity="medium",
                    cost_sensitivity="medium",
                ),
            )

        self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main()
