"""Focused coverage for the real browser execution path."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.browser_agent import BrowserAgent
from agents.research_agent import ResearchAgent
from agents.reviewer_agent import ReviewerAgent
from app.config import settings
from core.assistant import AssistantLayer
from core.models import (
    AgentExecutionStatus,
    AgentResult,
    AssistantDecision,
    ExecutionEscalation,
    RequestMode,
    SubTask,
    Task,
    TaskStatus,
    ToolEvidence,
    ToolInvocation,
)
from core.evaluator import GoalEvaluator
from core.operator_context import operator_context
from core.planner import Planner
from core.router import Router
from core.state import task_state_store
from core.supervisor import Supervisor
from integrations.browser.contracts import BrowserExecutionRequest, BrowserExecutionResult
from integrations.browser.runtime import BrowserExecutionService, BrowserUseCloudAdapter
from integrations.readiness import build_integration_readiness
from integrations.search.contracts import SearchRequest, SearchResult, SearchSource
from integrations.search.gemini_provider import GeminiSearchProvider
from tools.base_tool import BaseTool
from tools.browser_tool import BrowserTool
from tools.registry import ToolRegistry, build_default_tool_registry


class BrowserExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        operator_context.pending_confirmations.clear()
        operator_context.short_term_states.clear()
        task_state_store._tasks.clear()

    class FakeBrowserTool(BaseTool):
        name = "browser_tool"

        def execute(self, invocation: ToolInvocation) -> dict:
            backend = invocation.parameters.get("backend", "playwright")
            url = invocation.parameters.get("url")
            objective = invocation.parameters.get("objective", "")
            if backend == "browser_use" and invocation.parameters.get("allow_backend_fallback") == "false":
                return {
                    "success": False,
                    "summary": "Browser Use was selected for this task, but it is not available in this runtime.",
                    "error": "Browser Use is not installed or not configured for the current runtime.",
                    "payload": {
                        "backend": "browser_use",
                        "requested_url": url,
                        "user_action_required": [
                            "Install and configure Browser Use, or retry with the Playwright backend."
                        ],
                    },
                }
            if url == "https://login.example.com":
                return {
                    "success": False,
                    "summary": "Opened https://login.example.com, but the page is blocked.",
                    "error": "The page requires a login before I can inspect it.",
                    "payload": {
                        "backend": backend,
                        "requested_url": url,
                        "final_url": url,
                        "title": "Sign in",
                        "status_code": 200,
                        "headings": ["Sign in"],
                        "text_preview": "Please sign in with your email and password.",
                        "summary_text": "Sign in page.",
                        "screenshot_path": "C:/tmp/login.png",
                        "user_action_required": ["Log in yourself first."],
                    },
                }
            if url == "https://captcha.example.com":
                return {
                    "success": False,
                    "summary": "Opened https://captcha.example.com, but the page is blocked.",
                    "error": "The page is blocked by CAPTCHA or human verification.",
                    "payload": {
                        "backend": backend,
                        "requested_url": url,
                        "final_url": url,
                        "title": "Verify You Are Human",
                        "status_code": 403,
                        "headings": ["Verification required"],
                        "text_preview": "Verify you are human before continuing.",
                        "summary_text": "CAPTCHA challenge.",
                        "screenshot_path": "C:/tmp/captcha.png",
                        "user_action_required": ["Complete the CAPTCHA."],
                    },
                }

            dataset = {
                "https://example.com": {
                    "title": "Example Domain",
                    "final_url": "https://example.com",
                    "headings": ["Example Domain"],
                    "text_preview": "This domain is for use in illustrative examples in documents.",
                    "summary_text": "Example Domain is a reserved page for documentation examples.",
                },
                "https://www.cnn.com": {
                    "title": "CNN",
                    "final_url": "https://www.cnn.com",
                    "headings": [
                        "Story One",
                        "Story Two",
                        "Story Three",
                        "Story Four",
                        "Story Five",
                    ],
                    "text_preview": "Top stories from the CNN homepage.",
                    "summary_text": "CNN homepage with major top stories.",
                },
                "https://cnn.com": {
                    "title": "CNN",
                    "final_url": "https://www.cnn.com",
                    "headings": [
                        "Story One",
                        "Story Two",
                        "Story Three",
                        "Story Four",
                        "Story Five",
                    ],
                    "text_preview": "Top stories from the CNN homepage.",
                    "summary_text": "CNN homepage with major top stories.",
                },
                "https://www.espn.com": {
                    "title": "ESPN",
                    "final_url": "https://www.espn.com",
                    "headings": [
                        "Headline One",
                        "Headline Two",
                        "Headline Three",
                        "Headline Four",
                        "Headline Five",
                    ],
                    "text_preview": "Top headlines from ESPN.",
                    "summary_text": "ESPN homepage with top sports headlines.",
                },
                "https://www.wikipedia.org": {
                    "title": "Wikipedia",
                    "final_url": "https://www.wikipedia.org",
                    "headings": ["Wikipedia", "The Free Encyclopedia"],
                    "text_preview": "Wikipedia is a free encyclopedia that anyone can edit.",
                    "summary_text": "Wikipedia homepage for the free encyclopedia.",
                },
                "https://limited.example.com": {
                    "title": "Limited Example",
                    "final_url": "https://limited.example.com",
                    "headings": [],
                    "text_preview": "",
                    "summary_text": "",
                },
            }
            payload = dataset.get(url or "", dataset["https://example.com"]).copy()
            payload.update(
                {
                    "backend": "playwright" if backend == "browser_use" else backend,
                    "requested_url": url,
                    "screenshot_path": "C:/tmp/fake-browser.png",
                    "user_action_required": [],
                    "objective": objective,
                }
            )
            return {
                "success": True,
                "summary": f"Opened {payload['final_url']} and captured browser evidence.",
                "error": None,
                "payload": payload,
            }

    class FakeSearchProvider:
        provider_name = "fake_search"

        def __init__(self, *, sources: bool = True) -> None:
            self.sources = sources
            self.requests: list[SearchRequest] = []

        def is_configured(self) -> bool:
            return True

        def search(self, request: SearchRequest) -> SearchResult:
            self.requests.append(request)
            return SearchResult(
                query=request.query,
                provider=self.provider_name,
                answer="Fake source-backed research answer.",
                sources=[
                    SearchSource(
                        title="Fake Source",
                        url="https://example.com/research",
                        snippet="A fake source for tests.",
                    )
                ]
                if self.sources
                else [],
            )

    class PromptDispatchOpenRouterClient:
        def is_configured(self) -> bool:
            return True

        def prompt(
            self,
            prompt: str,
            *,
            system_prompt: str | None = None,
            label: str | None = None,
            context=None,
        ) -> str:
            del system_prompt, label, context
            if "Classify how the CEO assistant should handle" in prompt:
                return (
                    '{"mode":"ANSWER","escalation_level":"conversational_advice",'
                    '"reasoning":"hallucinated browser-free answer","should_use_tools":false,'
                    '"requires_minimal_follow_up":false}'
                )
            if "Break the goal into concrete subtasks" in prompt:
                return (
                    '{"subtasks":['
                    '{"title":"Think about the page","description":"Do not use the browser",'
                    '"objective":"Summarize from priors","agent_hint":"research_agent","tool_invocation":null},'
                    '{"title":"Reply","description":"Answer directly","objective":"Send a reply",'
                    '"agent_hint":"research_agent","tool_invocation":null},'
                    '{"title":"Review","description":"Review the answer","objective":"Review the answer",'
                    '"agent_hint":"reviewer_agent","tool_invocation":null}'
                    "]}"
                )
            if "Write the final user-facing reply" in prompt:
                return "Fabricated browser response."
            return '{"agent_name":"research_agent","reasoning":"default"}'

    class PlannerPathOpenRouterClient:
        def is_configured(self) -> bool:
            return True

        def prompt(
            self,
            prompt: str,
            *,
            system_prompt: str | None = None,
            label: str | None = None,
            context=None,
        ) -> str:
            del system_prompt, label, context
            if "Classify how the CEO assistant should handle" in prompt:
                return (
                    '{"mode":"EXECUTE","escalation_level":"bounded_task_execution",'
                    '"reasoning":"Needs interpretation before execution","should_use_tools":true,'
                    '"requires_minimal_follow_up":false}'
                )
            if "Break the goal into concrete subtasks" in prompt:
                return (
                    '{"subtasks":['
                    '{"title":"Investigate the target site","description":"Interpret the request before browsing",'
                    '"objective":"Figure out which site the user means","agent_hint":"research_agent","tool_invocation":null},'
                    '{"title":"Prepare reply","description":"Respond with what is needed",'
                    '"objective":"Explain the missing target URL","agent_hint":"research_agent","tool_invocation":null},'
                    '{"title":"Review","description":"Review the response","objective":"Review the response",'
                    '"agent_hint":"reviewer_agent","tool_invocation":null}'
                    "]}"
                )
            if "Write the final user-facing reply" in prompt:
                return "I need the specific URL before I can browse."
            return '{"agent_name":"research_agent","reasoning":"default"}'

    def _build_local_page(self, directory: str) -> str:
        page = Path(directory) / "page.html"
        page.write_text(
            (
                "<html><head><title>Local Browser Test</title>"
                '<meta name="description" content="A short local page for browser execution tests.">'
                "</head><body><h1>Browser Test Page</h1>"
                "<p>This page confirms the browser execution path is real.</p></body></html>"
            ),
            encoding="utf-8",
        )
        return page.resolve().as_uri()

    def _build_fake_browser_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(self.FakeBrowserTool())
        return registry

    def test_research_agent_uses_configured_search_provider_with_sources(self) -> None:
        provider = self.FakeSearchProvider()
        agent = ResearchAgent(search_provider=provider)
        task = Task(
            goal="research current browser automation options",
            title="Research",
            description="Research",
            request_mode=RequestMode.EXECUTE,
        )
        subtask = SubTask(
            title="Research options",
            description="Research options",
            objective="Compare current browser automation tools.",
            assigned_agent="research_agent",
        )

        result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(result.tool_name, "web_search_tool")
        self.assertEqual(result.evidence[0].payload["provider"], "fake_search")
        self.assertEqual(result.evidence[0].payload["sources"][0]["title"], "Fake Source")
        self.assertIn("Compare current browser automation tools", provider.requests[0].query)

    def test_research_agent_blocks_when_no_search_provider_is_configured(self) -> None:
        with (
            patch.object(settings, "search_enabled", True),
            patch.object(settings, "search_provider", "gemini"),
            patch.object(settings, "openrouter_api_key", None),
        ):
            agent = ResearchAgent()
            task = Task(goal="research current AI news", title="Research", description="Research")
            subtask = SubTask(
                title="Research news",
                description="Research news",
                objective="Research current AI news.",
                assigned_agent="research_agent",
            )

            result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertEqual(result.tool_name, "web_search_tool")
        self.assertIn("SEARCH_PROVIDER=gemini", result.blockers[0])
        self.assertIn("OPENROUTER_API_KEY", result.blockers[0])

    def test_gemini_search_provider_returns_search_result_shape(self) -> None:
        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {
                    "id": "or-search-test",
                    "model": "google/gemini-2.5-flash",
                    "choices": [
                        {
                            "message": {
                                "content": "Gemini found a cited result: [Example](https://example.com/research).",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url_citation": {
                                            "url": "https://example.com/research",
                                            "title": "Example Research",
                                            "content": "A source-backed test citation.",
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"server_tool_use": {"web_search_requests": 1}},
                }

        with patch("integrations.search.gemini_provider.httpx.post", return_value=FakeResponse()) as post:
            provider = GeminiSearchProvider(api_key="test-key", model="google/gemini-2.5-flash", timeout_seconds=1)
            result = provider.search(SearchRequest(query="current browser automation options", max_results=3))

        self.assertEqual(result.query, "current browser automation options")
        self.assertEqual(result.provider, "gemini")
        self.assertIn("Gemini found", result.answer)
        self.assertEqual(result.sources[0].title, "Example Research")
        self.assertEqual(result.sources[0].url, "https://example.com/research")
        self.assertTrue(result.timestamp)
        request_payload = post.call_args.kwargs["json"]
        self.assertEqual(request_payload["tools"][0]["type"], "openrouter:web_search")
        self.assertEqual(request_payload["tools"][0]["parameters"]["max_results"], 3)

    def test_source_less_research_does_not_complete_or_review(self) -> None:
        provider = self.FakeSearchProvider(sources=False)
        agent = ResearchAgent(search_provider=provider)
        task = Task(goal="research current AI news", title="Research", description="Research")
        subtask = SubTask(
            title="Research news",
            description="Research news",
            objective="Research current AI news.",
            assigned_agent="research_agent",
        )

        result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("source", " ".join(result.blockers).lower())

        fake_completed = AgentResult(
            subtask_id=subtask.id,
            agent="research_agent",
            status=AgentExecutionStatus.COMPLETED,
            summary="Fake complete research with no sources.",
            tool_name="web_search_tool",
            evidence=[
                ToolEvidence(
                    tool_name="web_search_tool",
                    summary="No sources.",
                    payload={
                        "query": "research current AI news",
                        "provider": "fake_search",
                        "answer": "No source answer.",
                        "sources": [],
                        "timestamp": "2026-04-29T00:00:00+00:00",
                    },
                )
            ],
        )
        task.results.append(fake_completed)
        review_result = ReviewerAgent().run(
            task,
            SubTask(
                title="Review research",
                description="Review research",
                objective="Review research evidence.",
                assigned_agent="reviewer_agent",
            ),
        )

        self.assertEqual(review_result.status, AgentExecutionStatus.BLOCKED)
        self.assertTrue(any("source titles and urls" in note.lower() for note in review_result.blockers))

    def test_specific_url_checks_still_route_to_browser_agent(self) -> None:
        router = Router(tool_registry=self._build_fake_browser_registry())
        planner = Planner(tool_registry=self._build_fake_browser_registry(), agent_registry=router.agent_registry)

        subtasks, planner_mode = planner.create_plan("check https://example.com and summarize it")

        self.assertEqual(planner_mode, "deterministic")
        browser_subtask = next(subtask for subtask in subtasks if subtask.tool_invocation)
        self.assertEqual(browser_subtask.assigned_agent, "browser_agent")
        self.assertEqual(browser_subtask.tool_invocation.tool_name, "browser_tool")

    def test_general_research_does_not_open_browser_unnecessarily(self) -> None:
        class NoLlmClient:
            def is_configured(self) -> bool:
                return False

        provider = self.FakeSearchProvider()
        registry = self._build_fake_browser_registry()
        no_llm = NoLlmClient()
        router = Router(tool_registry=registry, openrouter_client=no_llm)
        planner = Planner(tool_registry=registry, agent_registry=router.agent_registry, openrouter_client=no_llm)
        supervisor = Supervisor(
            planner=planner,
            router=router,
            assistant_layer=AssistantLayer(openrouter_client=no_llm),
            evaluator=GoalEvaluator(openrouter_client=no_llm),
        )

        with patch("agents.research_agent.build_search_provider", return_value=provider):
            response = supervisor.handle_user_goal("research the latest Python packaging documentation")

        self.assertEqual(response.status.value, "completed")
        self.assertTrue(any(result.tool_name == "web_search_tool" for result in response.results))
        self.assertFalse(any(result.tool_name == "browser_tool" for result in response.results))

    def test_browser_readiness_is_live_when_enabled(self) -> None:
        with patch.object(settings, "browser_enabled", True):
            readiness = build_integration_readiness()

        browser = readiness["integration:browser"]
        self.assertEqual(browser.status, "live")
        self.assertTrue(browser.enabled)
        self.assertTrue(browser.configured)

    def test_browser_tool_returns_real_page_evidence(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "browser_enabled", True),
        ):
            tool = BrowserTool(workspace_root=temp_dir)
            url = self._build_local_page(temp_dir)

            result = tool.execute(
                ToolInvocation(
                    tool_name="browser_tool",
                    action="open",
                    parameters={"url": url, "objective": "Open the local test page"},
                )
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["payload"]["title"], "Local Browser Test")
            self.assertIsNone(result["payload"]["screenshot_path"])
            created_items_pngs = list((Path(temp_dir) / "created_items").glob("**/*.png"))
            self.assertEqual(created_items_pngs, [])
            self.assertIn("Browser Test Page", " ".join(result["payload"]["headings"]))

    def test_browser_tool_saves_screenshot_on_blocked_page_without_created_items_clutter(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "browser_enabled", True),
            patch.object(settings, "browser_save_screenshots", "on_failure"),
        ):
            page = Path(temp_dir) / "blocked.html"
            page.write_text(
                "<html><head><title>Verify You Are Human</title></head>"
                "<body><h1>CAPTCHA required</h1><p>Verify you are human.</p></body></html>",
                encoding="utf-8",
            )
            tool = BrowserTool(workspace_root=temp_dir)

            result = tool.execute(
                ToolInvocation(
                    tool_name="browser_tool",
                    action="open",
                    parameters={
                        "url": page.resolve().as_uri(),
                        "objective": "Open the blocked test page",
                    },
                )
            )

            self.assertFalse(result["success"])
            screenshot_path = Path(result["payload"]["screenshot_path"])
            self.assertTrue(screenshot_path.exists())
            self.assertIn(".sovereign", screenshot_path.parts)
            self.assertIn("browser_artifacts", screenshot_path.parts)
            created_items_pngs = list((Path(temp_dir) / "created_items").glob("**/*.png"))
            self.assertEqual(created_items_pngs, [])

    def test_browser_tool_reports_disabled_runtime_honestly(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "browser_enabled", False),
        ):
            tool = BrowserTool(workspace_root=temp_dir)
            url = self._build_local_page(temp_dir)

            result = tool.execute(
                ToolInvocation(
                    tool_name="browser_tool",
                    action="open",
                    parameters={"url": url, "objective": "Open the local test page"},
                )
            )

            self.assertFalse(result["success"])
            self.assertIn("disabled", result["summary"].lower())
            self.assertIn("BROWSER_ENABLED is false", result["error"])

    def test_browser_tool_respects_visible_mode_config_without_gui(self) -> None:
        class CapturingExecutionService:
            def __init__(self) -> None:
                self.request: BrowserExecutionRequest | None = None

            def execute(self, request: BrowserExecutionRequest) -> BrowserExecutionResult:
                self.request = request
                return BrowserExecutionResult(
                    success=True,
                    summary="Captured request.",
                    backend="playwright",
                    structured_result={
                        "requested_url": request.start_url,
                        "final_url": request.start_url,
                        "title": "Visible Config",
                        "headings": ["Visible Config"],
                        "text_preview": "Visible mode was requested.",
                        "summary_text": "Visible mode was requested.",
                        "headless": request.headless,
                        "local_visible": request.local_visible,
                    },
                )

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "browser_headless", True),
            patch.object(settings, "browser_visible", True),
            patch.object(settings, "browser_show_window", False),
        ):
            service = CapturingExecutionService()
            tool = BrowserTool(workspace_root=temp_dir, execution_service=service)

            result = tool.execute(
                ToolInvocation(
                    tool_name="browser_tool",
                    action="open",
                    parameters={"url": "https://example.com", "objective": "Open visibly"},
                )
            )

        self.assertIsNotNone(service.request)
        self.assertFalse(service.request.headless)
        self.assertTrue(service.request.local_visible)
        self.assertFalse(result["payload"]["headless"])
        self.assertTrue(result["payload"]["local_visible"])

    def test_browser_agent_can_synthesize_and_run_browser_invocation(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "browser_enabled", True),
        ):
            url = self._build_local_page(temp_dir)
            agent = BrowserAgent(tool_registry=build_default_tool_registry())
            task = Task(
                goal=f"Open {url} in the browser and inspect it.",
                title="Open browser page",
                description="Open browser page",
                request_mode=RequestMode.ACT,
            )
            subtask = SubTask(
                title="Inspect page",
                description="Inspect page",
                objective=f"Open {url} and capture browser evidence",
                assigned_agent="browser_agent",
            )

            result = agent.run(task, subtask)

            self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
            self.assertEqual(result.tool_name, "browser_tool")
            self.assertEqual(result.evidence[0].payload["title"], "Local Browser Test")

    def test_browser_agent_summarizes_example_dot_com(self) -> None:
        agent = BrowserAgent(tool_registry=self._build_fake_browser_registry())
        task = Task(
            goal="open https://example.com and summarize it",
            title="Open example.com",
            description="Open example.com",
            request_mode=RequestMode.ACT,
        )
        subtask = SubTask(
            title="Summarize example.com",
            description="Summarize example.com",
            objective="Open https://example.com and summarize it",
            assigned_agent="browser_agent",
        )

        result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertIn("Example Domain", result.summary)
        self.assertEqual(
            result.evidence[0].payload["browser_task"]["resolved_url"],
            "https://example.com",
        )

    def test_browser_agent_resolves_cnn_without_explicit_url(self) -> None:
        agent = BrowserAgent(tool_registry=self._build_fake_browser_registry())
        task = Task(
            goal="open cnn and tell me the top 5 stories",
            title="Open cnn",
            description="Open cnn",
            request_mode=RequestMode.ACT,
        )
        subtask = SubTask(
            title="Check cnn",
            description="Check cnn",
            objective="Open cnn and tell me the top 5 stories",
            assigned_agent="browser_agent",
        )

        result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(
            result.evidence[0].payload["browser_task"]["resolved_url"],
            "https://www.cnn.com",
        )
        self.assertIn("CNN", result.summary)
        self.assertIn("Story One", result.summary)

    def test_browser_agent_handles_direct_cnn_url(self) -> None:
        agent = BrowserAgent(tool_registry=self._build_fake_browser_registry())
        task = Task(
            goal="open https://cnn.com and tell me the top 5 stories",
            title="Open cnn",
            description="Open cnn",
            request_mode=RequestMode.ACT,
        )
        subtask = SubTask(
            title="Check cnn",
            description="Check cnn",
            objective="Open https://cnn.com and tell me the top 5 stories",
            assigned_agent="browser_agent",
        )

        result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(
            result.evidence[0].payload["browser_task"]["resolved_url"],
            "https://cnn.com",
        )
        self.assertIn("Story Five", result.summary)

    def test_browser_agent_resolves_espn_headlines(self) -> None:
        agent = BrowserAgent(tool_registry=self._build_fake_browser_registry())
        task = Task(
            goal="go to espn and tell me the top headlines",
            title="Open espn",
            description="Open espn",
            request_mode=RequestMode.ACT,
        )
        subtask = SubTask(
            title="Check espn",
            description="Check espn",
            objective="Go to espn and tell me the top headlines",
            assigned_agent="browser_agent",
        )

        result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(
            result.evidence[0].payload["browser_task"]["resolved_url"],
            "https://www.espn.com",
        )
        self.assertIn("Headline One", result.summary)

    def test_browser_agent_resolves_wikipedia_homepage(self) -> None:
        agent = BrowserAgent(tool_registry=self._build_fake_browser_registry())
        task = Task(
            goal="open wikipedia and summarize homepage",
            title="Open wikipedia",
            description="Open wikipedia",
            request_mode=RequestMode.ACT,
        )
        subtask = SubTask(
            title="Check wikipedia",
            description="Check wikipedia",
            objective="Open wikipedia and summarize homepage",
            assigned_agent="browser_agent",
        )

        result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(
            result.evidence[0].payload["browser_task"]["resolved_url"],
            "https://www.wikipedia.org",
        )
        self.assertIn("Wikipedia", result.summary)

    def test_browser_evidence_produces_grounded_synthesis(self) -> None:
        agent = BrowserAgent(tool_registry=self._build_fake_browser_registry())
        task = Task(
            goal="open https://example.com and summarize it",
            title="Open example.com",
            description="Open example.com",
            request_mode=RequestMode.ACT,
        )
        subtask = SubTask(
            title="Summarize example.com",
            description="Summarize example.com",
            objective="Open https://example.com and summarize it",
            assigned_agent="browser_agent",
        )

        result = agent.run(task, subtask)
        browser_task = result.evidence[0].payload["browser_task"]

        self.assertEqual(result.summary, browser_task["synthesis_result"])
        self.assertIn("Example Domain", result.summary)

    def test_browser_agent_reports_insufficient_evidence_honestly(self) -> None:
        agent = BrowserAgent(tool_registry=self._build_fake_browser_registry())
        task = Task(
            goal="open https://limited.example.com and summarize homepage",
            title="Open limited page",
            description="Open limited page",
            request_mode=RequestMode.ACT,
        )
        subtask = SubTask(
            title="Summarize limited page",
            description="Summarize limited page",
            objective="Open https://limited.example.com and summarize homepage",
            assigned_agent="browser_agent",
        )

        result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertTrue(any("readable evidence" in blocker.lower() for blocker in result.blockers))

    def test_browser_agent_returns_human_readable_auth_blocker(self) -> None:
        agent = BrowserAgent(tool_registry=self._build_fake_browser_registry())
        task = Task(
            goal="open https://login.example.com and summarize it",
            title="Open login page",
            description="Open login page",
            request_mode=RequestMode.ACT,
        )
        subtask = SubTask(
            title="Summarize login page",
            description="Summarize login page",
            objective="Open https://login.example.com and summarize it",
            assigned_agent="browser_agent",
        )

        result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("login", result.summary.lower())
        self.assertNotIn("adapter", result.summary.lower())
        self.assertNotIn("runtime", result.summary.lower())

    def test_browser_agent_returns_human_readable_captcha_blocker(self) -> None:
        agent = BrowserAgent(tool_registry=self._build_fake_browser_registry())
        task = Task(
            goal="open https://captcha.example.com and summarize it",
            title="Open captcha page",
            description="Open captcha page",
            request_mode=RequestMode.ACT,
        )
        subtask = SubTask(
            title="Summarize captcha page",
            description="Summarize captcha page",
            objective="Open https://captcha.example.com and summarize it",
            assigned_agent="browser_agent",
        )

        result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("captcha", result.summary.lower())
        self.assertNotIn("adapter", result.summary.lower())
        self.assertNotIn("runtime", result.summary.lower())

    def test_browser_blocker_sets_pending_continuation_and_continue_retries(self) -> None:
        class LoginThenSuccessTool(BaseTool):
            name = "browser_tool"

            def __init__(self) -> None:
                self.calls = 0

            def supports(self, invocation: ToolInvocation) -> bool:
                return invocation.tool_name == self.name

            def execute(self, invocation: ToolInvocation) -> dict:
                self.calls += 1
                if self.calls == 1:
                    return {
                        "success": False,
                        "summary": "Opened https://login.example.com, but the page is blocked.",
                        "error": "The page requires a login before I can inspect it.",
                        "payload": {
                            "backend": "playwright",
                            "requested_url": invocation.parameters.get("url"),
                            "final_url": "https://login.example.com",
                            "title": "Sign in",
                            "headings": ["Sign in"],
                            "text_preview": "Please sign in.",
                            "summary_text": "Sign in page.",
                            "screenshot_path": "C:/tmp/login.png",
                            "user_action_required": ["Log in yourself first."],
                        },
                    }
                return {
                    "success": True,
                    "summary": "Opened https://login.example.com and captured browser evidence.",
                    "error": None,
                    "payload": {
                        "backend": "playwright",
                        "requested_url": invocation.parameters.get("url"),
                        "final_url": "https://login.example.com/dashboard",
                        "title": "Dashboard",
                        "headings": ["Dashboard"],
                        "text_preview": "Private dashboard after user login.",
                        "summary_text": "Dashboard page after user login.",
                        "screenshot_path": "C:/tmp/dashboard.png",
                        "user_action_required": [],
                    },
                }

        registry = ToolRegistry()
        registry.register(LoginThenSuccessTool())
        router = Router(tool_registry=registry)
        planner = Planner(tool_registry=registry, agent_registry=router.agent_registry)
        supervisor = Supervisor(
            planner=planner,
            router=router,
            assistant_layer=AssistantLayer(),
        )

        first = supervisor.handle_user_goal("open https://login.example.com and summarize it")
        pending = operator_context.get_short_term_state().pending_question
        second = supervisor.handle_user_goal("continue")

        self.assertEqual(first.status, TaskStatus.BLOCKED)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.resume_target, "browser_continuation")
        self.assertEqual(second.status, TaskStatus.COMPLETED)
        self.assertIn("Dashboard", second.response)
        self.assertNotIn("resume_target", second.response)
        self.assertNotIn("pending_action", second.response)

    def test_reviewer_rejects_browser_result_without_page_evidence(self) -> None:
        task = Task(
            goal="open https://example.com and summarize it",
            title="Open example.com",
            description="Open example.com",
            request_mode=RequestMode.EXECUTE,
        )
        execution_subtask = SubTask(
            id="browser-no-evidence",
            title="Weak browser result",
            description="Weak browser result",
            objective="Open https://example.com",
            assigned_agent="browser_agent",
        )
        task.subtasks.append(execution_subtask)
        task.results.append(
            AgentResult(
                subtask_id=execution_subtask.id,
                agent="browser_agent",
                status=AgentExecutionStatus.COMPLETED,
                summary="Browser task completed.",
                tool_name="browser_tool",
                evidence=[
                    ToolEvidence(
                        tool_name="browser_tool",
                        summary="Generic browser success.",
                        payload={
                            "browser_task": {"synthesis_result": "Generic browser success."},
                            "final_url": "https://example.com",
                            "title": "",
                            "headings": [],
                            "text_preview": "",
                            "summary_text": "",
                        },
                    )
                ],
            )
        )
        reviewer = ReviewerAgent(tool_registry=self._build_fake_browser_registry())
        review_subtask = SubTask(
            title="Review browser evidence",
            description="Review browser evidence",
            objective="Review the browser evidence",
            assigned_agent="reviewer_agent",
        )

        result = reviewer.run(task, review_subtask)

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertTrue(any("readable visible page content" in note.lower() for note in result.blockers))

    def test_evaluator_does_not_complete_simulated_browser_success(self) -> None:
        task = Task(
            goal="open https://example.com and summarize it",
            title="Open example.com",
            description="Open example.com",
            request_mode=RequestMode.ACT,
            escalation_level=ExecutionEscalation.SINGLE_ACTION,
        )
        task.results.append(
            AgentResult(
                subtask_id="fake-browser",
                agent="browser_agent",
                status=AgentExecutionStatus.COMPLETED,
                summary="I opened the page and summarized it.",
                tool_name="browser_tool",
                evidence=[
                    ToolEvidence(
                        tool_name="browser_tool",
                        summary="I opened the page.",
                        payload={
                            "final_url": "https://example.com",
                            "title": "",
                            "headings": [],
                            "text_preview": "",
                            "summary_text": "",
                        },
                    )
                ],
            )
        )

        class NoOpenRouterClient:
            def is_configured(self) -> bool:
                return False

        evaluation, mode = GoalEvaluator(openrouter_client=NoOpenRouterClient()).evaluate(task)

        self.assertEqual(mode, "deterministic")
        self.assertFalse(evaluation.satisfied)
        self.assertTrue(evaluation.needs_review)

    def test_browser_use_missing_returns_honest_block(self) -> None:
        class FakePlaywrightAdapter:
            def execute(self, request: BrowserExecutionRequest) -> BrowserExecutionResult:
                del request
                return BrowserExecutionResult(
                    success=True,
                    summary="Playwright fallback ran.",
                    backend="playwright",
                    structured_result={"final_url": "https://example.com"},
                )

        class FakeBrowserUseAdapter:
            def is_available(self) -> bool:
                return False

            def execute(self, request: BrowserExecutionRequest) -> BrowserExecutionResult:
                del request
                raise AssertionError("Browser Use should not execute when unavailable.")

        service = BrowserExecutionService(
            playwright_adapter=FakePlaywrightAdapter(),
            browser_use_adapter=FakeBrowserUseAdapter(),
        )

        with patch.object(settings, "browser_enabled", True):
            result = service.execute(
                BrowserExecutionRequest(
                    objective="Explore the target site",
                    preferred_backend="browser_use",
                    allow_backend_fallback=False,
                )
            )

        self.assertFalse(result.success)
        self.assertEqual(result.backend, "browser_use")
        self.assertIn("not available", result.summary.lower())

    def test_2fa_browser_request_returns_human_in_loop_blocker(self) -> None:
        class FailingIfCalledPlaywrightAdapter:
            def execute(self, request: BrowserExecutionRequest) -> BrowserExecutionResult:
                del request
                raise AssertionError("Safety blocker should stop before browser execution.")

        service = BrowserExecutionService(playwright_adapter=FailingIfCalledPlaywrightAdapter())

        with patch.object(settings, "browser_enabled", True):
            result = service.execute(
                BrowserExecutionRequest(
                    objective="Open https://example.com and enter the 2FA verification code",
                    start_url="https://example.com",
                    preferred_backend="playwright",
                )
            )

        self.assertFalse(result.success)
        self.assertEqual(result.backend, "playwright")
        self.assertIn("2FA", result.summary)
        self.assertTrue(any("continue" in action.lower() for action in result.user_action_required))

    def test_browser_use_disabled_explicit_request_returns_config_needed_message(self) -> None:
        agent = BrowserAgent(tool_registry=self._build_fake_browser_registry())
        task = Task(
            goal="use Browser Use to open https://example.com and summarize it",
            title="Use Browser Use",
            description="Use Browser Use",
            request_mode=RequestMode.ACT,
        )
        subtask = SubTask(
            title="Use Browser Use",
            description="Use Browser Use",
            objective="Use Browser Use to open https://example.com and summarize it",
            assigned_agent="browser_agent",
        )

        with patch.object(settings, "browser_use_enabled", False):
            result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("Browser Use", result.summary)
        self.assertEqual(result.evidence[0].payload["browser_task"]["backend_used"], "browser_use")
        self.assertTrue(any("Configure Browser Use" in action for action in result.next_actions))

    def test_browser_use_disabled_simple_browser_task_uses_playwright(self) -> None:
        agent = BrowserAgent(tool_registry=self._build_fake_browser_registry())
        task = Task(
            goal="open https://example.com and summarize it",
            title="Open example.com",
            description="Open example.com",
            request_mode=RequestMode.ACT,
        )
        subtask = SubTask(
            title="Summarize example.com",
            description="Summarize example.com",
            objective="Open https://example.com and summarize it",
            assigned_agent="browser_agent",
        )

        with patch.object(settings, "browser_use_enabled", False):
            result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(result.evidence[0].payload["browser_task"]["backend_used"], "playwright")
        self.assertEqual(result.evidence[0].payload["browser_task"]["resolved_url"], "https://example.com")

    def test_browser_use_enabled_mock_selects_browser_use_for_complex_workflow(self) -> None:
        class BrowserUseCapturingTool(BaseTool):
            name = "browser_tool"

            def execute(self, invocation: ToolInvocation) -> dict:
                backend = invocation.parameters.get("backend")
                return {
                    "success": True,
                    "summary": "Browser Use captured complex workflow evidence.",
                    "error": None,
                    "payload": {
                        "backend": backend,
                        "requested_url": invocation.parameters.get("url"),
                        "final_url": "https://www.cnn.com",
                        "title": "CNN",
                        "headings": ["Story One", "Story Two", "Story Three"],
                        "text_preview": "Browser Use explored the page and found top stories.",
                        "summary_text": "Browser Use workflow completed.",
                        "screenshot_path": None,
                        "user_action_required": [],
                    },
                }

        registry = ToolRegistry()
        registry.register(BrowserUseCapturingTool())
        agent = BrowserAgent(tool_registry=registry)
        task = Task(
            goal="find the top stories on cnn through a multi-step browser workflow",
            title="Explore cnn",
            description="Explore cnn",
            request_mode=RequestMode.ACT,
        )
        subtask = SubTask(
            title="Explore cnn",
            description="Explore cnn",
            objective="Find the top stories on cnn through a multi-step browser workflow",
            assigned_agent="browser_agent",
        )

        with patch.object(agent, "_browser_use_available", return_value=True):
            result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(result.evidence[0].payload["browser_task"]["backend_used"], "browser_use")
        self.assertEqual(result.evidence[0].payload["final_url"], "https://www.cnn.com")

    def test_browser_use_adapter_normalizes_evidence_shape(self) -> None:
        adapter = BrowserUseCloudAdapter(api_key="test")

        normalized = adapter._normalize_output(
            {
                "url": "https://example.com/finished",
                "page_title": "Finished Page",
                "summary": "The workflow completed with visible evidence.",
                "titles": ["First visible heading", "Second visible heading"],
            },
            BrowserExecutionRequest(
                objective="Complete the browser workflow",
                start_url="https://example.com",
                preferred_backend="browser_use",
            ),
        )

        self.assertEqual(normalized["requested_url"], "https://example.com")
        self.assertEqual(normalized["final_url"], "https://example.com/finished")
        self.assertEqual(normalized["title"], "Finished Page")
        self.assertEqual(normalized["summary_text"], "The workflow completed with visible evidence.")
        self.assertEqual(normalized["headings"], ["First visible heading", "Second visible heading"])
        self.assertIn("screenshot_path", normalized)

    def test_browser_use_failure_falls_back_to_playwright_when_safe(self) -> None:
        class FakePlaywrightAdapter:
            def execute(self, request: BrowserExecutionRequest) -> BrowserExecutionResult:
                self.request = request
                return BrowserExecutionResult(
                    success=True,
                    summary="Playwright fallback ran.",
                    backend="playwright",
                    structured_result={
                        "requested_url": request.start_url,
                        "final_url": request.start_url,
                        "title": "Fallback Page",
                        "headings": ["Fallback"],
                        "text_preview": "Fallback evidence.",
                        "summary_text": "Fallback evidence.",
                        "screenshot_path": None,
                    },
                )

        class FailingBrowserUseAdapter:
            def is_available(self) -> bool:
                return True

            def execute(self, request: BrowserExecutionRequest) -> BrowserExecutionResult:
                del request
                return BrowserExecutionResult(
                    success=False,
                    summary="Browser Use failed.",
                    backend="browser_use",
                    blockers=["Browser Use service failed."],
                )

        service = BrowserExecutionService(
            playwright_adapter=FakePlaywrightAdapter(),
            browser_use_adapter=FailingBrowserUseAdapter(),
        )

        with (
            patch.object(settings, "browser_enabled", True),
            patch.object(settings, "browser_use_enabled", True),
            patch.object(settings, "browser_backend_mode", "auto"),
        ):
            result = service.execute(
                BrowserExecutionRequest(
                    objective="Summarize direct page",
                    start_url="https://example.com",
                    preferred_backend="browser_use",
                    allow_backend_fallback=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.backend, "playwright")
        self.assertEqual(result.structured_result["title"], "Fallback Page")

    def test_browser_use_does_not_save_screenshots_into_created_items(self) -> None:
        class FakeBrowserUseAdapter:
            def is_available(self) -> bool:
                return True

            def execute(self, request: BrowserExecutionRequest) -> BrowserExecutionResult:
                return BrowserExecutionResult(
                    success=True,
                    summary="Browser Use ran.",
                    backend="browser_use",
                    structured_result={
                        "requested_url": request.start_url,
                        "final_url": request.start_url,
                        "title": "Browser Use Page",
                        "headings": ["Browser Use Page"],
                        "text_preview": "Browser Use evidence.",
                        "summary_text": "Browser Use evidence.",
                        "screenshot_path": None,
                    },
                )

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "browser_enabled", True),
            patch.object(settings, "browser_use_enabled", True),
        ):
            service = BrowserExecutionService(
                workspace_root=temp_dir,
                browser_use_adapter=FakeBrowserUseAdapter(),
            )
            tool = BrowserTool(workspace_root=temp_dir, execution_service=service)

            result = tool.execute(
                ToolInvocation(
                    tool_name="browser_tool",
                    action="summarize",
                    parameters={
                        "url": "https://example.com",
                        "backend": "browser_use",
                        "objective": "Summarize with Browser Use",
                    },
                )
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["payload"]["backend"], "browser_use")
            created_items_pngs = list((Path(temp_dir) / "created_items").glob("**/*.png"))
            self.assertEqual(created_items_pngs, [])

    def test_browser_result_can_be_reviewed_after_execution(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "browser_enabled", True),
        ):
            url = self._build_local_page(temp_dir)
            tool_registry = build_default_tool_registry()
            browser_agent = BrowserAgent(tool_registry=tool_registry)
            reviewer_agent = ReviewerAgent(tool_registry=tool_registry)
            execution_subtask = SubTask(
                id="browser-exec",
                title="Inspect page",
                description="Inspect page",
                objective=f"Open {url} and capture browser evidence",
                assigned_agent="browser_agent",
            )
            task = Task(
                goal=f"Open {url} in the browser and inspect it.",
                title="Open browser page",
                description="Open browser page",
                request_mode=RequestMode.EXECUTE,
                subtasks=[execution_subtask],
            )

            execution_result = browser_agent.run(task, execution_subtask)
            task.results.append(execution_result)
            review_subtask = SubTask(
                title="Review browser evidence",
                description="Review browser evidence",
                objective="Review the browser evidence",
                assigned_agent="reviewer_agent",
            )
            review_result = reviewer_agent.run(task, review_subtask)

            self.assertEqual(execution_result.status, AgentExecutionStatus.COMPLETED)
            self.assertEqual(review_result.status, AgentExecutionStatus.COMPLETED)
            self.assertTrue(review_result.evidence[0].verification_notes)

    def test_supervisor_executes_browser_flow_end_to_end(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "browser_enabled", True),
            patch.object(settings, "openrouter_api_key", None),
            patch.object(settings, "openai_enabled", False),
            patch.object(settings, "openai_api_key", None),
        ):
            url = self._build_local_page(temp_dir)
            supervisor = Supervisor()

            response = supervisor.handle_user_goal(
                f"Open {url} in the browser and summarize the page."
            )

            self.assertEqual(response.status.value, "completed")
            browser_result = next(result for result in response.results if result.tool_name == "browser_tool")
            self.assertEqual(browser_result.evidence[0].payload["title"], "Local Browser Test")
            self.assertIsNone(browser_result.evidence[0].payload["screenshot_path"])
            self.assertIn("local browser test", response.response.lower())

    def test_supervisor_handles_natural_browser_aliases_with_fake_backend(self) -> None:
        registry = self._build_fake_browser_registry()
        router = Router(tool_registry=registry)
        planner = Planner(tool_registry=registry, agent_registry=router.agent_registry)
        supervisor = Supervisor(
            planner=planner,
            router=router,
            assistant_layer=AssistantLayer(),
        )

        response = supervisor.handle_user_goal("open cnn and tell me the top 5 stories")

        self.assertEqual(response.status.value, "completed")
        self.assertIn("Story One", response.response)
        browser_result = next(result for result in response.results if result.tool_name == "browser_tool")
        self.assertEqual(
            browser_result.evidence[0].payload["browser_task"]["resolved_url"],
            "https://www.cnn.com",
        )

    def test_browser_request_followed_by_reminder_routes_to_correct_lane(self) -> None:
        registry = self._build_fake_browser_registry()
        router = Router(tool_registry=registry)
        planner = Planner(tool_registry=registry, agent_registry=router.agent_registry)
        supervisor = Supervisor(
            planner=planner,
            router=router,
            assistant_layer=AssistantLayer(),
        )

        first_response = supervisor.handle_user_goal("open wikipedia and summarize homepage")
        second_response = supervisor.handle_user_goal("remind me in 5 minutes to check email")

        self.assertEqual(first_response.request_mode, RequestMode.ACT)
        self.assertTrue(any(result.tool_name == "browser_tool" for result in first_response.results))
        self.assertEqual(second_response.request_mode, RequestMode.ACT)
        self.assertEqual(second_response.planner_mode, "fast_action")
        self.assertFalse(any(result.tool_name == "browser_tool" for result in second_response.results))

    def test_browser_request_followed_by_assistant_chat_stays_assistant(self) -> None:
        registry = self._build_fake_browser_registry()
        router = Router(tool_registry=registry)
        planner = Planner(tool_registry=registry, agent_registry=router.agent_registry)
        supervisor = Supervisor(
            planner=planner,
            router=router,
            assistant_layer=AssistantLayer(),
        )

        first_response = supervisor.handle_user_goal("open wikipedia and summarize homepage")
        second_response = supervisor.handle_user_goal("thanks")

        self.assertEqual(first_response.request_mode, RequestMode.ACT)
        self.assertTrue(any(result.tool_name == "browser_tool" for result in first_response.results))
        self.assertEqual(second_response.request_mode, RequestMode.ANSWER)
        self.assertFalse(any(result.tool_name == "browser_tool" for result in second_response.results))

    def test_assistant_overrides_llm_answer_for_direct_browser_request(self) -> None:
        assistant = AssistantLayer()
        with patch.object(
            assistant,
            "_decide_with_llm",
            return_value=AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning="hallucinated conversational answer",
                should_use_tools=False,
                requires_minimal_follow_up=True,
            ),
        ):
            decision = assistant.decide("open https://example.com and summarize it")

        self.assertEqual(decision.mode, RequestMode.ACT)
        self.assertTrue(decision.should_use_tools)
        self.assertFalse(decision.requires_minimal_follow_up)

    def test_go_to_url_is_treated_as_direct_browser_request(self) -> None:
        assistant = AssistantLayer()
        with patch.object(
            assistant,
            "_decide_with_llm",
            return_value=AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning="hallucinated conversational answer",
                should_use_tools=False,
                requires_minimal_follow_up=False,
            ),
        ):
            decision = assistant.decide("go to https://example.com")

        self.assertEqual(decision.mode, RequestMode.ACT)
        self.assertTrue(decision.should_use_tools)

    def test_slack_raw_link_is_sanitized_for_direct_browser_request(self) -> None:
        assistant = AssistantLayer()

        decision = assistant.decide("open <https://example.com> and summarize it")

        self.assertEqual(decision.mode, RequestMode.ACT)
        self.assertTrue(decision.should_use_tools)

    def test_slack_labeled_link_is_sanitized_for_direct_browser_request(self) -> None:
        assistant = AssistantLayer()

        decision = assistant.decide("summarize <https://example.com|example.com>")

        self.assertEqual(decision.mode, RequestMode.ACT)
        self.assertTrue(decision.should_use_tools)

    def test_openrouter_configured_direct_url_request_still_executes_browser_tool(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "browser_enabled", True),
        ):
            url = self._build_local_page(temp_dir)
            llm = self.PromptDispatchOpenRouterClient()
            supervisor = Supervisor(
                assistant_layer=AssistantLayer(openrouter_client=llm),
                planner=Planner(openrouter_client=llm),
                router=Router(openrouter_client=llm),
                evaluator=GoalEvaluator(openrouter_client=llm),
            )

            response = supervisor.handle_user_goal(f"summarize {url}")

            self.assertEqual(response.status.value, "completed")
            self.assertEqual(response.planner_mode, "fast_action")
            self.assertTrue(any(result.tool_name == "browser_tool" for result in response.results))
            browser_result = next(result for result in response.results if result.tool_name == "browser_tool")
            self.assertEqual(browser_result.evidence[0].payload["title"], "Local Browser Test")
            self.assertNotIn("fabricated browser response", response.response.lower())

    def test_direct_url_with_trailing_punctuation_still_executes_browser_tool(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "browser_enabled", True),
            patch.object(settings, "openrouter_api_key", None),
            patch.object(settings, "openai_enabled", False),
            patch.object(settings, "openai_api_key", None),
        ):
            url = self._build_local_page(temp_dir)
            supervisor = Supervisor()

            response = supervisor.handle_user_goal(f"open {url}.")

            self.assertEqual(response.status.value, "completed")
            browser_result = next(result for result in response.results if result.tool_name == "browser_tool")
            self.assertEqual(browser_result.evidence[0].payload["title"], "Local Browser Test")
            self.assertEqual(browser_result.evidence[0].payload["requested_url"], url)

    def test_ambiguous_browser_request_without_url_asks_for_clarification(self) -> None:
        llm = self.PlannerPathOpenRouterClient()
        supervisor = Supervisor(
            assistant_layer=AssistantLayer(openrouter_client=llm),
            planner=Planner(openrouter_client=llm),
            router=Router(openrouter_client=llm),
            evaluator=GoalEvaluator(openrouter_client=llm),
        )

        response = supervisor.handle_user_goal("open the website and summarize it")

        self.assertEqual(response.planner_mode, "conversation_clarify")
        self.assertEqual(response.request_mode, RequestMode.ANSWER)
        self.assertFalse(any(result.tool_name == "browser_tool" for result in response.results))
        self.assertIn("what exactly do you want me to act on", response.response.lower())

    def test_supervisor_retry_reuses_last_browser_goal(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "browser_enabled", True),
            patch.object(settings, "openrouter_api_key", None),
            patch.object(settings, "openai_enabled", False),
            patch.object(settings, "openai_api_key", None),
        ):
            url = self._build_local_page(temp_dir)
            supervisor = Supervisor()

            first_response = supervisor.handle_user_goal(
                f"Open {url} in the browser and summarize the page."
            )
            retry_response = supervisor.handle_user_goal("try again")

            self.assertEqual(first_response.status.value, "completed")
            self.assertEqual(retry_response.status.value, "completed")
            self.assertTrue(any(result.tool_name == "browser_tool" for result in retry_response.results))

    def test_supervisor_retry_reuses_normalized_previous_browser_goal(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "browser_enabled", True),
            patch.object(settings, "openrouter_api_key", None),
            patch.object(settings, "openai_enabled", False),
            patch.object(settings, "openai_api_key", None),
        ):
            url = self._build_local_page(temp_dir)
            supervisor = Supervisor()

            first_response = supervisor.handle_user_goal(
                f"open <{url}|Local Browser Test> and summarize it"
            )
            retry_response = supervisor.handle_user_goal("try again")

            self.assertEqual(first_response.status.value, "completed")
            self.assertEqual(retry_response.status.value, "completed")
            self.assertTrue(any(result.tool_name == "browser_tool" for result in retry_response.results))
            browser_result = next(result for result in retry_response.results if result.tool_name == "browser_tool")
            self.assertEqual(browser_result.evidence[0].payload["requested_url"], url)

    def test_browser_tool_logs_final_sanitized_url(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "browser_enabled", False),
        ):
            tool = BrowserTool(workspace_root=temp_dir)
            url = self._build_local_page(temp_dir)

            with self.assertLogs("tools.browser_tool", level="INFO") as logs:
                tool.execute(
                    ToolInvocation(
                        tool_name="browser_tool",
                        action="open",
                        parameters={"url": f"<{url}|local page>"},
                    )
                )

            combined = "\n".join(logs.output)
            self.assertIn("BROWSER_TOOL_START", combined)
            self.assertIn(f"raw_url='<{url}|local page>'", combined)
            self.assertIn(f"final_url={url}", combined)


if __name__ == "__main__":
    unittest.main()
