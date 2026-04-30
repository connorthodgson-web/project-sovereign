"""Adapter for real browser-backed page inspection and evidence capture."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from app.config import settings
from core.browser_requests import sanitize_url_candidate
from core.logging import get_logger
from core.models import ToolInvocation
from integrations.browser.contracts import BrowserExecutionRequest
from integrations.browser.runtime import BrowserExecutionService
from tools.base_tool import BaseTool


class BrowserTool(BaseTool):
    """Wraps the live browser execution service behind a bounded tool contract."""

    name = "browser_tool"

    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        execution_service: BrowserExecutionService | None = None,
    ) -> None:
        self.logger = get_logger(__name__)
        self.workspace_root = Path(workspace_root or settings.workspace_root).resolve()
        self.execution_service = execution_service or BrowserExecutionService(
            workspace_root=self.workspace_root
        )

    def supports(self, invocation: ToolInvocation) -> bool:
        return invocation.tool_name == self.name and invocation.action in {
            "open",
            "inspect",
            "summarize",
        }

    def execute(self, invocation: ToolInvocation) -> dict:
        raw_url = invocation.parameters.get("url")
        url = self._normalize_url(raw_url)
        self.logger.info(
            "BROWSER_TOOL_START action=%s raw_url=%r final_url=%s objective=%r",
            invocation.action,
            raw_url,
            url,
            invocation.parameters.get("objective"),
        )
        request = BrowserExecutionRequest(
            action=invocation.action,
            objective=invocation.parameters.get("objective")
            or invocation.parameters.get("prompt")
            or f"{invocation.action} the requested page",
            start_url=url,
            preferred_backend=invocation.parameters.get("backend"),
            allow_backend_fallback=self._parameter_flag(
                invocation.parameters.get("allow_backend_fallback"),
                default=True,
            ),
            allowed_domains=self._parameter_list(invocation.parameters.get("allowed_domains")),
            require_screenshot=self._parameter_flag(
                invocation.parameters.get("require_screenshot"),
                default=False,
            ),
            screenshot_policy=self._screenshot_policy(),
            headless=self._parameter_flag(
                invocation.parameters.get("headless"),
                default=self._default_headless(),
            ),
            local_visible=not self._parameter_flag(
                invocation.parameters.get("headless"),
                default=self._default_headless(),
            ),
            timeout_ms=self._parameter_int(
                invocation.parameters.get("timeout_ms"),
                default=20000,
            ),
            max_steps=self._parameter_int(
                invocation.parameters.get("max_steps"),
                default=20,
            ),
        )
        result = self.execution_service.execute(request)
        payload = {
            "backend": result.backend,
            "action": invocation.action,
            "requested_url": url,
            **result.structured_result,
            "evidence": result.evidence,
            "user_action_required": result.user_action_required,
        }
        error = result.blockers[0] if result.blockers else None
        response = {
            "success": result.success,
            "summary": result.summary,
            "error": error,
            "payload": payload,
        }
        self.logger.info(
            "BROWSER_TOOL_END success=%s backend=%s final_url=%s error=%r",
            result.success,
            result.backend,
            payload.get("final_url") or url,
            error,
        )
        return response

    def _normalize_url(self, raw_url: str | None) -> str | None:
        candidate = sanitize_url_candidate(raw_url)
        if not candidate:
            return None
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https", "file"}:
            return candidate
        if parsed.scheme:
            return None
        if "." in candidate and " " not in candidate:
            return f"https://{candidate}"
        return None

    def _parameter_flag(self, value: str | None, *, default: bool) -> bool:
        if value is None:
            return default
        return value.strip().lower() not in {"false", "0", "no"}

    def _parameter_int(self, value: str | None, *, default: int) -> int:
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            return default

    def _parameter_list(self, value: str | None) -> list[str]:
        if value is None:
            return []
        return [item.strip() for item in value.split(",") if item.strip()]

    def _screenshot_policy(self) -> str:
        policy = str(getattr(settings, "browser_save_screenshots", "on_failure")).strip().lower()
        if policy not in {"never", "on_failure", "always"}:
            return "on_failure"
        return policy

    def _default_headless(self) -> bool:
        if bool(getattr(settings, "browser_visible", False)):
            return False
        if bool(getattr(settings, "browser_show_window", False)):
            return False
        return bool(getattr(settings, "browser_headless", True))
