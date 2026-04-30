"""Browser-focused agent implementation."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from agents.base_agent import BaseAgent
from app.config import settings
from core.browser_requests import (
    detect_browser_safety_blocker,
    extract_first_url,
    extract_obvious_browser_request,
    resolve_known_browser_target,
    sanitize_url_candidate,
)
from core.logging import get_logger
from core.model_routing import ModelRequestContext
from core.models import (
    AgentExecutionStatus,
    AgentResult,
    BrowserTask,
    SubTask,
    Task,
    ToolEvidence,
    ToolInvocation,
)
from integrations.openrouter_client import OpenRouterClient
from integrations.readiness import build_integration_readiness
from tools.registry import ToolRegistry, build_default_tool_registry


class BrowserAgent(BaseAgent):
    """Handles browser automation and web interaction tasks via external tools."""

    name = "browser_agent"
    supported_tool_names = frozenset({"browser_tool"})

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry | None = None,
        openrouter_client: OpenRouterClient | None = None,
    ) -> None:
        self.logger = get_logger(__name__)
        self.tool_registry = tool_registry or build_default_tool_registry()
        self.openrouter_client = openrouter_client or OpenRouterClient()

    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        self.logger.info("BROWSER_AGENT_START task=%s subtask=%s goal=%r", task.id, subtask.id, task.goal)
        browser_task = self._build_browser_task(task, subtask)
        self.logger.info(
            "BROWSER_TASK_MODEL_CREATED task=%s subtask=%s target=%r resolved_url=%s action=%s extraction=%r",
            task.id,
            subtask.id,
            browser_task.target_site_or_url,
            browser_task.resolved_url,
            browser_task.browser_action,
            browser_task.extraction_objective,
        )

        if browser_task.resolved_url is None and browser_task.backend_used != "browser_use":
            result = self._blocked_resolution_result(task, subtask, browser_task)
            self.logger.info("BROWSER_AGENT_END task=%s subtask=%s status=%s", task.id, subtask.id, result.status.value)
            return result

        safety_result = self._blocked_safety_result(task, subtask, browser_task)
        if safety_result is not None:
            self.logger.info("BROWSER_AGENT_END task=%s subtask=%s status=%s", task.id, subtask.id, safety_result.status.value)
            return safety_result

        normalized_outputs: list[tuple[ToolInvocation, dict[str, Any]]] = []
        first_invocation = self._build_tool_invocation(browser_task, task, subtask)
        subtask.tool_invocation = first_invocation
        first_output = self._execute_browser_invocation(task, subtask, first_invocation)
        normalized_outputs.append((first_invocation, first_output))
        browser_task.action_count = 1
        browser_task.backend_used = self._normalize_backend_name(
            str(first_output["payload"].get("backend", browser_task.backend_used or "playwright"))
        )
        browser_task.evidence.append(dict(first_output["payload"]))
        browser_task.blockers = list(first_output["blockers"])
        self.logger.info(
            "BROWSER_EVIDENCE_CAPTURED task=%s subtask=%s action_count=%s backend=%s success=%s",
            task.id,
            subtask.id,
            browser_task.action_count,
            browser_task.backend_used,
            first_output["success"],
        )

        second_invocation = self._maybe_build_second_invocation(
            browser_task,
            task,
            subtask,
            first_invocation,
            first_output,
        )
        if second_invocation is not None:
            second_output = self._execute_browser_invocation(task, subtask, second_invocation)
            normalized_outputs.append((second_invocation, second_output))
            browser_task.action_count = 2
            browser_task.backend_used = self._normalize_backend_name(
                str(second_output["payload"].get("backend", browser_task.backend_used or "playwright"))
            )
            browser_task.evidence.append(dict(second_output["payload"]))
            if second_output["blockers"]:
                browser_task.blockers = list(second_output["blockers"])
            self.logger.info(
                "BROWSER_EVIDENCE_CAPTURED task=%s subtask=%s action_count=%s backend=%s success=%s",
                task.id,
                subtask.id,
                browser_task.action_count,
                browser_task.backend_used,
                second_output["success"],
            )

        self.logger.info("BROWSER_SYNTHESIS_START task=%s subtask=%s", task.id, subtask.id)
        browser_task.synthesis_result = self._synthesize_answer(task, subtask, browser_task)
        self.logger.info("BROWSER_SYNTHESIS_END task=%s subtask=%s", task.id, subtask.id)

        result = self._build_result(task, subtask, browser_task, normalized_outputs)
        self.logger.info("BROWSER_AGENT_END task=%s subtask=%s status=%s", task.id, subtask.id, result.status.value)
        return result

    def _build_browser_task(self, task: Task, subtask: SubTask) -> BrowserTask:
        invocation = subtask.tool_invocation
        explicit_url = extract_first_url(" ".join([task.goal, subtask.objective, subtask.description]))
        target_site_or_url = None
        resolved_url = explicit_url

        if invocation is not None:
            invocation_url = sanitize_url_candidate(invocation.parameters.get("url"))
            if invocation_url:
                target_site_or_url = invocation_url
                resolved_url = invocation_url

        explicit_match = extract_obvious_browser_request(task.goal) or extract_obvious_browser_request(
            subtask.objective
        )
        if explicit_match is not None:
            target_site_or_url = explicit_match.target or explicit_match.url
            resolved_url = explicit_match.url

        if resolved_url is None:
            known_target, known_url = resolve_known_browser_target(
                " ".join([task.goal, subtask.objective, subtask.description])
            )
            if known_url:
                target_site_or_url = known_target
                resolved_url = known_url

        if resolved_url is None:
            llm_target, llm_url = self._resolve_target_with_llm(task, subtask)
            if llm_target or llm_url:
                target_site_or_url = llm_target or llm_url
                resolved_url = llm_url

        backend_choice = self._select_backend(task, subtask, resolved_url=resolved_url)
        self.logger.info(
            "BROWSER_TARGET_RESOLVED task=%s subtask=%s target=%r resolved_url=%s",
            task.id,
            subtask.id,
            target_site_or_url,
            resolved_url,
        )
        self.logger.info(
            "BROWSER_BACKEND_SELECTED task=%s subtask=%s backend=%s",
            task.id,
            subtask.id,
            backend_choice,
        )
        return BrowserTask(
            original_goal=task.goal,
            target_site_or_url=target_site_or_url or resolved_url,
            resolved_url=resolved_url,
            browser_action=self._infer_action(task, subtask),
            extraction_objective=self._infer_extraction_objective(task, subtask),
            backend_used=backend_choice,
        )

    def _blocked_resolution_result(self, task: Task, subtask: SubTask, browser_task: BrowserTask) -> AgentResult:
        readiness = build_integration_readiness()["integration:browser"]
        blockers = list(browser_task.blockers)
        if not blockers:
            blockers.append("I could not safely resolve the target site from the request.")
        next_actions = [
            "Name the exact site you want me to browse, or share a direct URL.",
        ]
        if browser_task.backend_used == "browser_use":
            next_actions = [
                "Configure Browser Use for open-ended browsing, or retry with a direct site target.",
            ]
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=AgentExecutionStatus.BLOCKED,
            summary="Browser work was recognized, but I could not safely determine a runnable target.",
            tool_name="browser_tool",
            details=[
                f"Goal context: {task.goal}",
                f"Browser objective: {subtask.objective}",
                f"Capability status: {readiness.status}",
                f"Browser task: {browser_task.model_dump()}",
            ],
            evidence=[
                ToolEvidence(
                    tool_name="browser_tool",
                    summary="Browser target resolution failed before execution.",
                    payload={"browser_task": browser_task.model_dump()},
                )
            ],
            blockers=blockers,
            next_actions=next_actions,
        )

    def _blocked_safety_result(self, task: Task, subtask: SubTask, browser_task: BrowserTask) -> AgentResult | None:
        safety_blocker = detect_browser_safety_blocker(
            " ".join([task.goal, subtask.objective, subtask.description, browser_task.resolved_url or ""])
        )
        if safety_blocker is None:
            return None
        browser_task.blockers = [safety_blocker.reason]
        evidence_payload = {
            "browser_task": browser_task.model_dump(),
            "requested_goal": task.goal,
            "requested_url": browser_task.resolved_url,
            "final_url": browser_task.resolved_url,
            "visited_urls": [browser_task.resolved_url] if browser_task.resolved_url else [],
            "extracted_result": "",
            "backend": browser_task.backend_used,
            "blockers": [safety_blocker.reason],
            "blocker": safety_blocker.reason,
            "blocker_category": safety_blocker.category,
        }
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=AgentExecutionStatus.BLOCKED,
            summary=safety_blocker.reason,
            tool_name="browser_tool",
            details=[
                f"Goal context: {task.goal}",
                f"Browser objective: {subtask.objective}",
                f"Browser task: {browser_task.model_dump()}",
            ],
            evidence=[
                ToolEvidence(
                    tool_name="browser_tool",
                    summary=safety_blocker.reason,
                    payload=evidence_payload,
                )
            ],
            blockers=[safety_blocker.reason],
            next_actions=[safety_blocker.next_action],
        )

    def _execute_browser_invocation(
        self,
        task: Task,
        subtask: SubTask,
        invocation: ToolInvocation,
    ) -> dict[str, Any]:
        validation_error = self._validate_invocation(invocation)
        if validation_error is not None:
            return {
                "success": False,
                "summary": "The browser invocation could not be executed safely.",
                "error": validation_error,
                "payload": {},
                "blockers": [validation_error],
                "next_actions": ["Repair the browser invocation and retry."],
            }
        normalized = self.normalize_tool_output(invocation, self.tool_registry.execute(invocation))
        normalized.payload.update(self._normalized_browser_payload(invocation, normalized.payload))
        blockers = self._extract_browser_blockers(normalized)
        return {
            "success": normalized.success,
            "summary": normalized.summary or "",
            "error": normalized.error,
            "payload": normalized.payload,
            "blockers": blockers,
            "next_actions": self._build_next_actions(invocation, normalized),
        }

    def _validate_invocation(self, invocation: ToolInvocation) -> str | None:
        if self.tool_registry.get(invocation.tool_name) is None:
            return f"Unsupported tool invocation: {invocation.tool_name}"
        if invocation.action not in {"open", "inspect", "summarize"}:
            return f"Unsupported browser action: {invocation.action}"
        backend = invocation.parameters.get("backend")
        if backend and backend not in {"playwright", "browser_use"}:
            return f"Unsupported browser backend: {backend}"
        if backend == "playwright" and not invocation.parameters.get("url"):
            return "Playwright browser execution requires a resolved URL."
        return None

    def _build_tool_invocation(
        self,
        browser_task: BrowserTask,
        task: Task,
        subtask: SubTask,
    ) -> ToolInvocation:
        explicit_browser_use = self._explicitly_requests_browser_use(task, subtask)
        parameters = {
            "objective": browser_task.extraction_objective or subtask.objective or task.goal,
            "require_screenshot": "true" if self._explicitly_requests_screenshot(task, subtask) else "false",
            "backend": browser_task.backend_used or "playwright",
            "allow_backend_fallback": "true" if browser_task.resolved_url and browser_task.backend_used != "browser_use" and not explicit_browser_use else "false",
            "max_steps": "6" if browser_task.backend_used == "browser_use" else "2",
        }
        if browser_task.resolved_url:
            parameters["url"] = browser_task.resolved_url
            domain = urlparse(browser_task.resolved_url).netloc
            if domain:
                parameters["allowed_domains"] = domain
        return ToolInvocation(
            tool_name="browser_tool",
            action=browser_task.browser_action,
            parameters=parameters,
        )

    def _maybe_build_second_invocation(
        self,
        browser_task: BrowserTask,
        task: Task,
        subtask: SubTask,
        first_invocation: ToolInvocation,
        first_output: dict[str, Any],
    ) -> ToolInvocation | None:
        reason = self._second_action_reason(browser_task, first_output)
        browser_task.second_action_reasoning = reason
        self.logger.info(
            "BROWSER_SECOND_ACTION_DECISION task=%s subtask=%s second_action=%s reasoning=%r",
            task.id,
            subtask.id,
            bool(reason),
            reason or "No second action was needed.",
        )
        if not reason or browser_task.action_count >= 2:
            return None

        parameters = dict(first_invocation.parameters)
        parameters["objective"] = f"{browser_task.extraction_objective or task.goal} Focus on visible headlines and summary evidence."
        parameters["timeout_ms"] = "30000"
        if browser_task.backend_used == "playwright" and self._browser_use_available() and browser_task.resolved_url:
            parameters["backend"] = "browser_use"
            parameters["allow_backend_fallback"] = "true"
            parameters["max_steps"] = "8"
        else:
            parameters["backend"] = browser_task.backend_used or "playwright"
        return ToolInvocation(
            tool_name="browser_tool",
            action="inspect" if first_invocation.action == "open" else first_invocation.action,
            parameters=parameters,
        )

    def _second_action_reason(
        self,
        browser_task: BrowserTask,
        first_output: dict[str, Any],
    ) -> str | None:
        payload = first_output["payload"]
        headings = [str(item).strip() for item in payload.get("headings", []) if str(item).strip()]
        text_preview = str(payload.get("text_preview", "")).strip()
        objective = (browser_task.extraction_objective or "").lower()
        if not first_output["success"] and payload.get("final_url") and (headings or text_preview):
            return "The first browser action surfaced partial visible content, so I took one more bounded pass to improve synthesis."
        if any(term in objective for term in ("top 5", "top five", "headlines", "stories")) and len(headings) < 3:
            return "The request needs multiple visible headlines, and the first browser pass did not capture enough of them."
        if any(term in objective for term in ("summarize", "summary", "homepage")) and not (
            headings or text_preview
        ):
            return "The first browser pass did not capture enough readable text to synthesize an honest answer."
        return None

    def _synthesize_answer(self, task: Task, subtask: SubTask, browser_task: BrowserTask) -> str:
        llm_answer = self._synthesize_with_llm(task, subtask, browser_task)
        if llm_answer is not None:
            return llm_answer
        return self._synthesize_deterministically(browser_task)

    def _synthesize_with_llm(
        self,
        task: Task,
        subtask: SubTask,
        browser_task: BrowserTask,
    ) -> str | None:
        if not self.openrouter_client.is_configured():
            return None
        prompt = (
            "Answer the user's browser objective using only the provided browser evidence.\n"
            "Do not invent details, links, or steps that are not present in the evidence.\n"
            "If evidence is limited, say that plainly and summarize only what is visible.\n"
            "Prefer a concise Slack-friendly answer with short headings when helpful.\n"
            f"User goal: {task.goal}\n"
            f"Browser objective: {subtask.objective}\n"
            f"Browser task: {json.dumps(browser_task.model_dump(), ensure_ascii=True)}"
        )
        try:
            response = self.openrouter_client.prompt(
                prompt,
                system_prompt=(
                    "You are the browser synthesis layer for Project Sovereign. "
                    "Stay fully grounded in the provided evidence."
                ),
                label="browser_synthesis",
                context=self._model_context_for_browser(
                    task,
                    subtask,
                    browser_task=browser_task,
                    evidence_quality="low" if self._is_insufficient_evidence(browser_task) else "medium",
                ),
            )
            answer = str(response).strip()
            title = str((browser_task.evidence[-1] if browser_task.evidence else {}).get("title", "")).strip()
            if title and title.lower() not in answer.lower():
                answer = f"{title}: {answer}"
            return answer or None
        except (RuntimeError, httpx.HTTPError, ValueError, TypeError):
            return None

    def _synthesize_deterministically(self, browser_task: BrowserTask) -> str:
        latest_payload = browser_task.evidence[-1] if browser_task.evidence else {}
        final_url = str(latest_payload.get("final_url") or browser_task.resolved_url or "").strip()
        title = str(latest_payload.get("title", "")).strip()
        text_preview = str(latest_payload.get("text_preview", "")).strip()
        summary_text = str(latest_payload.get("summary_text", "")).strip()
        headings = self._collect_headings(browser_task)
        objective = (browser_task.extraction_objective or browser_task.original_goal).lower()

        if browser_task.blockers:
            blocker = self._humanize_browser_blocker(browser_task.blockers[0])
            visible = summary_text or text_preview or (headings[0] if headings else "")
            if visible:
                return (
                    f"I could only partially complete the browser task because {blocker.rstrip('.')}. "
                    f"Visible content: {visible}"
                )
            return f"I couldn't fully complete the browser task because {blocker.rstrip('.')}."

        if any(term in objective for term in ("top 5", "top five", "headlines", "stories")) and headings:
            limit = 5 if any(term in objective for term in ("top 5", "top five")) else min(5, len(headings))
            lines = [f"Top items from {title or final_url or 'the page'}:"]
            for index, heading in enumerate(headings[:limit], start=1):
                lines.append(f"{index}. {heading}")
            if final_url:
                lines.append(f"Source: {final_url}")
            return "\n".join(lines)

        if any(term in objective for term in ("summarize", "summary", "homepage")):
            body = summary_text or text_preview
            if body:
                response = body
                if headings:
                    response = f"{response}\nHighlights: {', '.join(headings[:3])}"
                if final_url:
                    response = f"{response}\nSource: {final_url}"
                return response

        if headings:
            response = f"{title or 'Page overview'}: {', '.join(headings[:3])}"
            if final_url:
                response = f"{response}\nSource: {final_url}"
            return response

        if summary_text or text_preview:
            response = summary_text or text_preview
            if final_url:
                response = f"{response}\nSource: {final_url}"
            return response

        if final_url:
            return f"I opened {final_url}, but the page exposed very little readable text to synthesize."
        return "The browser run completed, but it did not expose enough readable evidence to answer confidently."

    def _build_result(
        self,
        task: Task,
        subtask: SubTask,
        browser_task: BrowserTask,
        normalized_outputs: list[tuple[ToolInvocation, dict[str, Any]]],
    ) -> AgentResult:
        latest_output = normalized_outputs[-1][1]
        latest_payload = latest_output["payload"]
        screenshot_path = str(latest_payload.get("screenshot_path", "")).strip() or None
        artifacts = [f"browser:screenshot:{screenshot_path}"] if screenshot_path else []
        blockers = list(browser_task.blockers)
        if not blockers and latest_output["blockers"]:
            blockers = list(latest_output["blockers"])
        if not blockers and self._is_insufficient_evidence(browser_task):
            blockers = ["The page did not expose enough readable evidence to answer confidently."]
        status = AgentExecutionStatus.COMPLETED if latest_output["success"] and not blockers else AgentExecutionStatus.BLOCKED
        summary = browser_task.synthesis_result or latest_output["summary"] or "Browser task finished."
        evidence_payload = {
            "browser_task": browser_task.model_dump(),
            "requested_goal": task.goal,
            "final_url": latest_payload.get("final_url"),
            "requested_url": latest_payload.get("requested_url"),
            "visited_urls": self._visited_urls(browser_task),
            "title": latest_payload.get("title"),
            "status_code": latest_payload.get("status_code"),
            "headings": latest_payload.get("headings"),
            "text_preview": latest_payload.get("text_preview"),
            "summary_text": latest_payload.get("summary_text"),
            "extracted_result": latest_payload.get("extracted_result") or latest_payload.get("summary_text") or latest_payload.get("text_preview"),
            "screenshot_path": latest_payload.get("screenshot_path"),
            "artifacts": latest_payload.get("artifacts") or ([latest_payload.get("screenshot_path")] if latest_payload.get("screenshot_path") else []),
            "backend": latest_payload.get("backend"),
            "headless": latest_payload.get("headless"),
            "local_visible": latest_payload.get("local_visible"),
            "blockers": blockers,
            "blocker": blockers[0] if blockers else None,
            "error": latest_output.get("error"),
        }
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=status,
            summary=summary,
            tool_name="browser_tool",
            details=[
                f"Goal context: {task.goal}",
                f"Browser objective: {subtask.objective}",
                f"Browser task: {browser_task.model_dump()}",
            ],
            artifacts=artifacts,
            evidence=[
                ToolEvidence(
                    tool_name="browser_tool",
                    summary=summary,
                    payload=evidence_payload,
                )
            ],
            blockers=blockers,
            next_actions=(
                []
                if status == AgentExecutionStatus.COMPLETED
                else latest_output["next_actions"]
                or ["Retry with a clearer target page or a page that exposes more readable content."]
            ),
        )

    def _infer_action(self, task: Task, subtask: SubTask) -> str:
        invocation = subtask.tool_invocation
        if invocation is not None and invocation.action in {"open", "inspect", "summarize"}:
            return invocation.action
        text = " ".join([task.goal, subtask.title, subtask.objective]).lower()
        if any(term in text for term in ("headlines", "stories", "summarize", "summary")):
            return "summarize"
        if any(term in text for term in ("inspect", "look at", "what's on", "what is on")):
            return "inspect"
        return "open"

    def _infer_extraction_objective(self, task: Task, subtask: SubTask) -> str:
        combined = " ".join(part for part in [subtask.objective, subtask.description, task.goal] if part).strip()
        if combined:
            return combined
        return task.goal

    def _select_backend(self, task: Task, subtask: SubTask, *, resolved_url: str | None) -> str:
        if self._explicitly_requests_browser_use(task, subtask):
            return "browser_use"
        backend_mode = self._configured_backend_mode()
        if backend_mode in {"playwright", "browser_use"}:
            return backend_mode
        llm_choice = self._select_backend_with_llm(task, subtask, resolved_url=resolved_url)
        if llm_choice in {"playwright", "browser_use"}:
            if llm_choice == "browser_use" and not self._browser_use_available():
                return "browser_use"
            return llm_choice
        text = " ".join([task.goal, subtask.objective, subtask.description]).lower()
        if self._looks_exploratory(text):
            return "browser_use"
        if resolved_url and not self._looks_exploratory(text):
            return "playwright"
        if self._browser_use_available():
            return "browser_use"
        return "playwright"

    def _select_backend_with_llm(
        self,
        task: Task,
        subtask: SubTask,
        *,
        resolved_url: str | None,
    ) -> str | None:
        if not self.openrouter_client.is_configured():
            return None
        prompt = (
            "Choose the best browser backend for this task.\n"
            "Use 'playwright' for direct URL opening, stable summaries, and fast deterministic work.\n"
            "Use 'browser_use' for exploratory browsing, unclear navigation, or multi-step web flows.\n"
            "Return only strict JSON like {\"backend\":\"playwright\",\"reasoning\":\"...\"}.\n"
            f"Goal: {task.goal}\n"
            f"Subtask objective: {subtask.objective}\n"
            f"Resolved URL: {resolved_url}"
        )
        try:
            response = self.openrouter_client.prompt(
                prompt,
                system_prompt="You are a browser-backend selector. Return only valid JSON.",
                label="browser_backend_selection",
                context=self._model_context_for_browser(
                    task,
                    subtask,
                    browser_task=None,
                    evidence_quality="unknown",
                ),
            )
            payload = json.loads(response)
            backend = str(payload.get("backend", "")).strip().lower()
            if backend in {"playwright", "browser_use"}:
                return backend
            return None
        except (RuntimeError, httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError):
            return None

    def _resolve_target_with_llm(self, task: Task, subtask: SubTask) -> tuple[str | None, str | None]:
        if not self.openrouter_client.is_configured():
            return None, None
        prompt = (
            "Resolve the browser target from the user's request.\n"
            "Return strict JSON with keys target, resolved_url, and confidence.\n"
            "Only return a resolved_url when you can infer a safe public homepage or exact URL with high confidence.\n"
            "If you are not confident, return null for resolved_url.\n"
            f"Goal: {task.goal}\n"
            f"Subtask objective: {subtask.objective}"
        )
        try:
            response = self.openrouter_client.prompt(
                prompt,
                system_prompt="You are a careful browser target resolver. Return only valid JSON.",
                label="browser_target_resolution",
                context=self._model_context_for_browser(
                    task,
                    subtask,
                    browser_task=None,
                    evidence_quality="unknown",
                ),
            )
            payload = json.loads(response)
            confidence = str(payload.get("confidence", "")).strip().lower()
            target = str(payload.get("target", "")).strip() or None
            resolved_url = sanitize_url_candidate(payload.get("resolved_url"))
            if confidence not in {"high", "very_high"}:
                return target, None
            return target, resolved_url
        except (RuntimeError, httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError):
            return None, None

    def _build_next_actions(self, invocation: ToolInvocation, normalized_output) -> list[str]:
        if normalized_output.success:
            return []
        combined = " ".join(
            [
                normalized_output.summary or "",
                normalized_output.error or "",
                *[
                    str(item)
                    for item in normalized_output.payload.get("user_action_required", [])
                    if item
                ],
            ]
        ).lower()
        if "browser_enabled" in combined or "disabled" in combined:
            return ["Enable live browser access for this workspace, then retry the browser task."]
        if "no url" in combined or "starting url" in combined or "resolved url" in combined:
            return ["Send the exact URL you want me to open or inspect."]
        if "login" in combined or "sign in" in combined:
            return ["Log in yourself in the visible browser if it is open, then say continue so I can retry the inspection."]
        if "captcha" in combined:
            return ["Complete the CAPTCHA in the visible browser if it is open, then say continue so I can retry the inspection."]
        if "2fa" in combined or "verification code" in combined:
            return ["Complete the verification step in the visible browser if it is open, then say continue so I can retry the inspection."]
        if "payment" in combined or "purchase" in combined:
            return ["Handle the payment or purchase step yourself; I will not automate it."]
        if "sensitive form" in combined:
            return ["Review the sensitive form yourself, or send a non-sensitive page to inspect."]
        if "access denied" in combined or "forbidden" in combined or "unauthorized" in combined:
            return ["Use a public page that allows browser access, or open the page after access is granted."]
        if "browser use" in combined and invocation.parameters.get("backend") == "browser_use":
            return ["Configure Browser Use, or retry with the Playwright backend for a direct URL task."]
        required = normalized_output.payload.get("user_action_required")
        if isinstance(required, list):
            return [str(item) for item in required if str(item).strip()]
        return [f"Retry the browser action for {invocation.parameters.get('url', 'the requested page')}."]

    def _collect_headings(self, browser_task: BrowserTask) -> list[str]:
        seen: set[str] = set()
        headings: list[str] = []
        for payload in browser_task.evidence:
            for item in payload.get("headings", []) or []:
                heading = str(item).strip()
                if not heading or heading in seen:
                    continue
                seen.add(heading)
                headings.append(heading)
        return headings

    def _visited_urls(self, browser_task: BrowserTask) -> list[str]:
        urls: list[str] = []
        for payload in browser_task.evidence:
            raw_urls = payload.get("visited_urls") or []
            if isinstance(raw_urls, list):
                urls.extend(str(item).strip() for item in raw_urls if str(item).strip())
            for key in ("requested_url", "final_url"):
                value = str(payload.get(key) or "").strip()
                if value:
                    urls.append(value)
        if browser_task.resolved_url:
            urls.append(browser_task.resolved_url)
        seen: set[str] = set()
        unique: list[str] = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                unique.append(url)
        return unique

    def _is_insufficient_evidence(self, browser_task: BrowserTask) -> bool:
        payload = browser_task.evidence[-1] if browser_task.evidence else {}
        headings = self._collect_headings(browser_task)
        if headings:
            return False
        objective = (browser_task.extraction_objective or browser_task.original_goal).lower()
        needs_readable_body = any(
            term in objective
            for term in ("summarize", "summary", "homepage", "what's on", "what is on", "report")
        )
        if not needs_readable_body:
            title = str(payload.get("title", "")).strip()
            if title and title.lower() not in {"browser use result", "untitled", "unknown"}:
                return False
        if str(payload.get("text_preview", "")).strip():
            return False
        if str(payload.get("summary_text", "")).strip():
            return False
        return True

    def _normalized_browser_payload(
        self,
        invocation: ToolInvocation,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        requested_url = payload.get("requested_url") or invocation.parameters.get("url")
        final_url = payload.get("final_url")
        return {
            "requested_url": requested_url,
            "final_url": final_url,
            "visited_urls": payload.get("visited_urls") or ([final_url] if final_url else []),
            "title": payload.get("title"),
            "status_code": payload.get("status_code"),
            "headings": payload.get("headings") or [],
            "text_preview": payload.get("text_preview") or "",
            "summary_text": payload.get("summary_text") or "",
            "extracted_result": payload.get("extracted_result") or payload.get("summary_text") or payload.get("text_preview") or "",
            "screenshot_path": payload.get("screenshot_path"),
            "artifacts": payload.get("artifacts") or ([payload.get("screenshot_path")] if payload.get("screenshot_path") else []),
            "backend": payload.get("backend") or invocation.parameters.get("backend") or "playwright",
        }

    def _extract_browser_blockers(self, normalized_output) -> list[str]:
        blockers: list[str] = []
        if normalized_output.error:
            blockers.append(self._humanize_browser_blocker(normalized_output.error))
        payload_blockers = normalized_output.payload.get("blockers")
        if isinstance(payload_blockers, list):
            blockers.extend(self._humanize_browser_blocker(str(item)) for item in payload_blockers if str(item).strip())
        if not normalized_output.success and not blockers:
            blockers.append(self._humanize_browser_blocker(normalized_output.summary or "The browser task did not complete."))
        seen: set[str] = set()
        unique: list[str] = []
        for blocker in blockers:
            if blocker and blocker not in seen:
                seen.add(blocker)
                unique.append(blocker)
        return unique

    def _humanize_browser_blocker(self, blocker: str) -> str:
        cleaned = " ".join(str(blocker).strip().rstrip(".").split())
        lowered = cleaned.lower()
        if "browser_enabled" in lowered or "browser execution is disabled" in lowered:
            return "live browser access is not enabled here"
        if "playwright" in lowered and ("missing" in lowered or "not installed" in lowered):
            return "local browser support is not installed"
        if "chromium" in lowered and ("missing" in lowered or "not installed" in lowered):
            return "the local browser engine is not installed"
        if "browser use" in lowered and ("not installed" in lowered or "not configured" in lowered or "not available" in lowered):
            return "Browser Use is not configured here"
        if "captcha" in lowered or "human verification" in lowered:
            return "the page needs human verification"
        if "2fa" in lowered or "verification code" in lowered or "one-time code" in lowered:
            return "the page needs a verification step from you"
        if "login" in lowered or "sign in" in lowered or "password" in lowered:
            return "the page needs you to log in"
        if "no url" in lowered or "starting url" in lowered or "resolved url" in lowered:
            return "I need the exact URL to open"
        return cleaned or "the browser task did not complete"

    def _normalize_backend_name(self, backend: str) -> str:
        lowered = backend.strip().lower()
        if "browser_use" in lowered:
            return "browser_use"
        return "playwright"

    def _browser_use_available(self) -> bool:
        readiness = build_integration_readiness()["integration:browser_use"]
        return readiness.status == "live"

    def _configured_backend_mode(self) -> str:
        mode = str(getattr(settings, "browser_backend_mode", "auto")).strip().lower()
        if mode not in {"auto", "playwright", "browser_use"}:
            return "auto"
        return mode

    def _explicitly_requests_browser_use(self, task: Task, subtask: SubTask) -> bool:
        text = " ".join([task.goal, subtask.objective, subtask.description]).lower()
        return "browser use" in text or "browser-use" in text

    def _looks_exploratory(self, text: str) -> bool:
        return any(
            phrase in text
            for phrase in (
                "find",
                "look around",
                "figure out",
                "navigate through",
                "click through",
                "multi-step",
                "explore",
                "workflow",
                "log in",
                "login",
            )
        )

    def _explicitly_requests_screenshot(self, task: Task, subtask: SubTask) -> bool:
        text = " ".join([task.goal, subtask.objective, subtask.description]).lower()
        return any(term in text for term in ("screenshot", "screen shot", "capture an image", "visual proof"))

    def _model_context_for_browser(
        self,
        task: Task,
        subtask: SubTask,
        *,
        browser_task: BrowserTask | None,
        evidence_quality: str,
    ) -> ModelRequestContext:
        return ModelRequestContext(
            intent_label="browser_action",
            request_mode=task.request_mode.value,
            selected_lane="browser",
            selected_agent="browser_agent",
            task_complexity="high" if self._looks_exploratory(task.goal.lower()) else "medium",
            risk_level="medium",
            requires_tool_use=True,
            requires_review=True,
            evidence_quality=evidence_quality,
            user_visible_latency_sensitivity="medium",
            cost_sensitivity="medium",
            fallback_allowed=(browser_task.resolved_url is not None if browser_task is not None else True),
        )
