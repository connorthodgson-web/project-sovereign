"""Concrete browser backends and runtime availability helpers."""

from __future__ import annotations

import asyncio
import importlib.metadata
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from app.config import Settings, settings
from core.browser_requests import detect_browser_safety_blocker
from integrations.browser.contracts import (
    BrowserAdapter,
    BrowserExecutionRequest,
    BrowserExecutionResult,
)

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on optional runtime package
    PlaywrightError = Exception
    PlaywrightTimeoutError = Exception
    sync_playwright = None

try:
    from browser_use_sdk import AsyncBrowserUse
except ImportError:  # pragma: no cover - depends on optional runtime package
    AsyncBrowserUse = None


@dataclass(frozen=True)
class BrowserRuntimeSupport:
    """Local snapshot of available browser backends."""

    playwright_available: bool
    browser_use_sdk_available: bool
    chromium_binary_available: bool = False
    chromium_executable_path: str | None = None
    playwright_version: str | None = None

    @property
    def any_backend_available(self) -> bool:
        return (
            (self.playwright_available and self.chromium_binary_available)
            or self.browser_use_sdk_available
        )

    def user_action_requirements(self) -> tuple[str, ...]:
        requirements: list[str] = []
        if not self.playwright_available:
            requirements.extend(
                [
                    f'Install the Playwright Python package with `"{sys.executable}" -m pip install playwright`.',
                    f'Install a Playwright browser binary with `"{sys.executable}" -m playwright install chromium`.',
                ]
            )
        elif not self.chromium_binary_available:
            requirements.append(
                f'Install a Playwright Chromium browser binary with `"{sys.executable}" -m playwright install chromium`.'
            )
        return tuple(requirements)


def detect_browser_runtime_support() -> BrowserRuntimeSupport:
    """Return which browser execution packages are currently available."""

    chromium_binary_available = False
    chromium_executable_path: str | None = None
    playwright_version: str | None = None
    if sync_playwright is not None:
        try:
            playwright_version = importlib.metadata.version("playwright")
        except Exception:  # pragma: no cover - defensive metadata lookup
            playwright_version = None

        # Keep readiness checks cheap. Starting Playwright here can hang in
        # restricted desktop runtimes, so the actual browser adapter performs
        # concrete launch diagnostics only when execution is requested.
        chromium_binary_available = True

    return BrowserRuntimeSupport(
        playwright_available=sync_playwright is not None,
        browser_use_sdk_available=AsyncBrowserUse is not None,
        chromium_binary_available=chromium_binary_available,
        chromium_executable_path=chromium_executable_path,
        playwright_version=playwright_version,
    )


@dataclass(frozen=True)
class BrowserRuntimeDiagnostic:
    """Concrete runtime diagnostic for the local Playwright path."""

    python_executable: str
    playwright_import_ok: bool
    playwright_version: str | None
    chromium_executable_path: str | None
    chromium_binary_exists: bool
    chromium_launch_ok: bool
    example_navigation_ok: bool
    page_title: str | None
    body_snippet: str | None
    browser_closed_cleanly: bool
    likely_cause: str
    recommended_commands: tuple[str, ...] = ()
    raw_error: str | None = None


def collect_browser_runtime_diagnostic() -> BrowserRuntimeDiagnostic:
    """Run a focused Playwright diagnostic against the local runtime."""

    python_executable = sys.executable
    playwright_support = detect_browser_runtime_support()
    playwright_import_ok = sync_playwright is not None
    chromium_launch_ok = False
    example_navigation_ok = False
    browser_closed_cleanly = False
    page_title: str | None = None
    body_snippet: str | None = None
    raw_error: str | None = None
    likely_cause = "browser_runtime_ready"
    commands: list[str] = []

    if not playwright_import_ok:
        likely_cause = "missing_playwright_package"
        commands = [
            f'"{python_executable}" -m pip install playwright',
            f'"{python_executable}" -m playwright install chromium',
        ]
    elif not playwright_support.chromium_binary_available:
        likely_cause = "missing_chromium_browser"
        commands = [f'"{python_executable}" -m playwright install chromium']
    else:
        browser = None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                chromium_launch_ok = True
                page = browser.new_page()
                page.goto("https://example.com", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(500)
                page_title = page.title()
                body_snippet = " ".join(page.locator("body").inner_text().split())[:240]
                example_navigation_ok = True
                browser.close()
                browser_closed_cleanly = True
                browser = None
                likely_cause = "browser_runtime_ready"
        except PlaywrightError as exc:
            raw_error = str(exc)
            likely_cause, commands = _classify_playwright_error(raw_error, python_executable)
        except Exception as exc:  # pragma: no cover - defensive boundary
            raw_error = str(exc)
            likely_cause = "unexpected_browser_runtime_error"
        finally:
            if browser is not None:
                try:
                    browser.close()
                    browser_closed_cleanly = True
                except Exception:
                    browser_closed_cleanly = False

    return BrowserRuntimeDiagnostic(
        python_executable=python_executable,
        playwright_import_ok=playwright_import_ok,
        playwright_version=playwright_support.playwright_version,
        chromium_executable_path=playwright_support.chromium_executable_path,
        chromium_binary_exists=playwright_support.chromium_binary_available,
        chromium_launch_ok=chromium_launch_ok,
        example_navigation_ok=example_navigation_ok,
        page_title=page_title,
        body_snippet=body_snippet,
        browser_closed_cleanly=browser_closed_cleanly,
        likely_cause=likely_cause,
        recommended_commands=tuple(commands),
        raw_error=raw_error,
    )


def _classify_playwright_error(error_text: str, python_executable: str) -> tuple[str, list[str]]:
    """Translate a Playwright launch failure into a likely cause and commands."""

    lowered = error_text.lower()
    if "executable doesn't exist" in lowered or "browser_type.launch" in lowered and "install" in lowered:
        return (
            "missing_chromium_browser",
            [f'"{python_executable}" -m playwright install chromium'],
        )
    if "cannot find module" in lowered or "no module named" in lowered:
        return (
            "missing_playwright_package",
            [
                f'"{python_executable}" -m pip install playwright',
                f'"{python_executable}" -m playwright install chromium',
            ],
        )
    if "protocol error" in lowered or "target page, context or browser has been closed" in lowered:
        return (
            "protocol_error_or_stale_browser_runtime",
            [f'"{python_executable}" -m playwright install chromium'],
        )
    if "failed to launch" in lowered:
        return (
            "chromium_launch_failed",
            [f'"{python_executable}" -m playwright install chromium'],
        )
    return ("unexpected_browser_runtime_error", [])


def _build_browser_failure(
    *,
    summary: str,
    backend: str,
    blockers: list[str],
    likely_cause: str,
    commands: list[str],
    diagnostic: BrowserRuntimeDiagnostic | None = None,
) -> BrowserExecutionResult:
    """Create a structured browser failure with precise runtime metadata."""

    structured_result: dict[str, object] = {
        "likely_cause": likely_cause,
        "python_executable": sys.executable,
    }
    if diagnostic is not None:
        structured_result["diagnostic"] = {
            "python_executable": diagnostic.python_executable,
            "playwright_import_ok": diagnostic.playwright_import_ok,
            "playwright_version": diagnostic.playwright_version,
            "chromium_executable_path": diagnostic.chromium_executable_path,
            "chromium_binary_exists": diagnostic.chromium_binary_exists,
            "chromium_launch_ok": diagnostic.chromium_launch_ok,
            "example_navigation_ok": diagnostic.example_navigation_ok,
            "page_title": diagnostic.page_title,
            "body_snippet": diagnostic.body_snippet,
            "browser_closed_cleanly": diagnostic.browser_closed_cleanly,
            "likely_cause": diagnostic.likely_cause,
            "raw_error": diagnostic.raw_error,
        }
    return BrowserExecutionResult(
        success=False,
        summary=summary,
        backend=backend,
        structured_result=structured_result,
        blockers=blockers,
        user_action_required=commands,
    )


class BrowserUseCloudAdapter(BrowserAdapter):
    """Optional Browser Use provider adapter for safe multi-step browser runs."""

    backend_name = "browser_use"

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url

    def is_available(self) -> bool:
        return AsyncBrowserUse is not None and bool(self.api_key)

    def execute(self, request: BrowserExecutionRequest) -> BrowserExecutionResult:
        if AsyncBrowserUse is None:
            return BrowserExecutionResult(
                success=False,
                summary="Browser Use SDK is not installed in this runtime.",
                backend=self.backend_name,
                blockers=["Install `browser-use-sdk` to enable Browser Use cloud execution."],
                user_action_required=[
                    "Install the `browser-use-sdk` package in this environment.",
                ],
            )
        if not self.api_key:
            return BrowserExecutionResult(
                success=False,
                summary="Browser Use cloud execution is not configured.",
                backend=self.backend_name,
                blockers=["BROWSER_USE_API_KEY is missing."],
                user_action_required=[
                    "Add `BROWSER_USE_API_KEY` to the environment or .env file.",
                ],
            )

        async def _run() -> object:
            client = AsyncBrowserUse(api_key=self.api_key, base_url=self.base_url)
            try:
                run = await client.run(
                    request.objective,
                    start_url=request.start_url,
                    allowed_domains=request.allowed_domains or None,
                    max_steps=request.max_steps,
                )
                return getattr(run, "output", None)
            finally:
                close = getattr(client, "close", None)
                if close is not None:
                    await close()

        try:
            output = asyncio.run(_run())
        except RuntimeError as exc:
            return BrowserExecutionResult(
                success=False,
                summary="Browser Use cloud execution could not start.",
                backend=self.backend_name,
                blockers=[str(exc)],
            )
        except Exception as exc:  # pragma: no cover - network/service failure surface
            return BrowserExecutionResult(
                success=False,
                summary="Browser Use cloud execution failed.",
                backend=self.backend_name,
                blockers=[str(exc)],
            )

        raw_result = (
            dict(output)
            if isinstance(output, dict)
            else {"output": str(output).strip()} if output is not None else {}
        )
        structured_result = self._normalize_output(raw_result, request)
        summary = structured_result.get("summary_text") or structured_result.get("text_preview")
        if not summary:
            summary = "Browser Use cloud execution completed."
        return BrowserExecutionResult(
            success=True,
            summary=str(summary),
            backend=self.backend_name,
            structured_result=structured_result,
            evidence=["Browser Use cloud returned structured output."],
        )

    def _normalize_output(
        self,
        raw_result: dict[str, object],
        request: BrowserExecutionRequest,
    ) -> dict[str, object]:
        final_url = self._first_text(
            raw_result,
            "final_url",
            "current_url",
            "url",
            "last_url",
        ) or request.start_url
        title = self._first_text(raw_result, "title", "page_title")
        summary_text = self._first_text(raw_result, "summary_text", "summary", "result", "output")
        text_preview = self._first_text(raw_result, "text_preview", "text", "content") or summary_text
        screenshot_path = self._first_text(raw_result, "screenshot_path", "screenshot")
        visited_urls = raw_result.get("visited_urls") or raw_result.get("urls") or []
        if isinstance(visited_urls, str):
            visited_urls = [line.strip() for line in visited_urls.splitlines() if line.strip()]
        if not isinstance(visited_urls, list):
            visited_urls = []
        normalized_visited_urls = [str(item).strip() for item in visited_urls if str(item).strip()]
        if final_url and final_url not in normalized_visited_urls:
            normalized_visited_urls.append(final_url)
        headings = raw_result.get("headings") or raw_result.get("titles") or []
        if isinstance(headings, str):
            headings = [line.strip() for line in headings.splitlines() if line.strip()]
        if not isinstance(headings, list):
            headings = []
        return {
            "requested_goal": request.objective,
            "requested_url": request.start_url,
            "final_url": final_url,
            "visited_urls": normalized_visited_urls,
            "title": title or "Browser Use result",
            "summary_text": str(summary_text or "").strip(),
            "text_preview": str(text_preview or "").strip()[:320],
            "extracted_result": str(summary_text or text_preview or "").strip(),
            "headings": [str(item).strip() for item in headings if str(item).strip()][:5],
            "screenshot_path": screenshot_path,
            "artifacts": [screenshot_path] if screenshot_path else [],
            "headless": request.headless,
            "local_visible": request.local_visible or not request.headless,
            "raw_browser_use_result": raw_result,
        }

    def _first_text(self, payload: dict[str, object], *keys: str) -> str | None:
        for key in keys:
            value = payload.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None


class PlaywrightBrowserAdapter(BrowserAdapter):
    """Deterministic local browser adapter for URL-based page inspection."""

    backend_name = "playwright"

    def __init__(self, *, workspace_root: str | Path | None = None) -> None:
        root = Path(workspace_root or settings.workspace_root)
        self.workspace_root = root.resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def execute(self, request: BrowserExecutionRequest) -> BrowserExecutionResult:
        if sync_playwright is None:
            python_executable = sys.executable
            return _build_browser_failure(
                summary="Playwright is not installed in this runtime.",
                backend=self.backend_name,
                blockers=[
                    f"The Playwright Python package is missing for interpreter: {python_executable}"
                ],
                likely_cause="missing_playwright_package",
                commands=[
                    f'"{python_executable}" -m pip install playwright',
                    f'"{python_executable}" -m playwright install chromium',
                ],
            )

        start_url = (request.start_url or "").strip()
        if not start_url:
            return BrowserExecutionResult(
                success=False,
                summary="A starting URL is required for the current local browser path.",
                backend=self.backend_name,
                blockers=["No URL was provided for the browser action."],
                user_action_required=[
                    "Provide a direct URL for the page you want opened or inspected.",
                ],
            )

        support = detect_browser_runtime_support()
        if not support.chromium_binary_available:
            python_executable = sys.executable
            return _build_browser_failure(
                summary="Chromium is not installed for the current Playwright runtime.",
                backend=self.backend_name,
                blockers=[
                    f"Playwright can import in {python_executable}, but no Chromium browser binary was found."
                ],
                likely_cause="missing_chromium_browser",
                commands=[f'"{python_executable}" -m playwright install chromium'],
            )

        screenshot_path = self._build_screenshot_path(start_url)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=request.headless)
                try:
                    page = browser.new_page()
                    response = page.goto(start_url, wait_until="domcontentloaded", timeout=request.timeout_ms)
                    page.wait_for_timeout(800)
                    title = page.title()
                    final_url = page.url
                    extracted = page.evaluate(
                        """() => {
                            const headingNodes = Array.from(document.querySelectorAll('h1, h2, h3'));
                            const headings = headingNodes
                                .map((node) => (node.innerText || '').trim())
                                .filter(Boolean)
                                .slice(0, 6);
                            const metaDescription = document.querySelector('meta[name="description"]')?.content || '';
                            const bodyText = (document.body?.innerText || '').trim();
                            return {
                                headings,
                                metaDescription,
                                bodyText,
                            };
                        }"""
                    )
                    page_text = self._normalize_whitespace(str(extracted.get("bodyText", "")))
                    meta_description = self._normalize_whitespace(
                        str(extracted.get("metaDescription", ""))
                    )
                    headings = [
                        self._normalize_whitespace(str(item))
                        for item in extracted.get("headings", [])
                        if str(item).strip()
                    ]
                    status_code = response.status if response is not None else None
                    sensitive_form_markers = page.evaluate(
                        """() => {
                            const inputs = Array.from(document.querySelectorAll('input, textarea, select'));
                            return inputs.map((node) => {
                                const name = node.getAttribute('name') || '';
                                const id = node.getAttribute('id') || '';
                                const type = node.getAttribute('type') || '';
                                const autocomplete = node.getAttribute('autocomplete') || '';
                                const label = node.getAttribute('aria-label') || '';
                                return `${type} ${name} ${id} ${autocomplete} ${label}`.trim();
                            }).filter(Boolean);
                        }"""
                    )
                    blockers = self._detect_page_blockers(
                        title=title,
                        page_text=page_text,
                        url=final_url,
                        status_code=status_code,
                        form_markers=[
                            str(item)
                            for item in sensitive_form_markers
                            if str(item).strip()
                        ],
                    )
                    should_save_screenshot = self._should_save_screenshot(
                        request,
                        blocked=bool(blockers),
                    )
                    if should_save_screenshot:
                        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(screenshot_path), full_page=True)
                finally:
                    browser.close()
        except PlaywrightTimeoutError:
            return BrowserExecutionResult(
                success=False,
                summary=f"Opening {start_url} timed out.",
                backend=self.backend_name,
                blockers=[f"The page did not finish loading within {request.timeout_ms} ms."],
                user_action_required=[
                    "Retry with a simpler page, a stable public URL, or a longer timeout.",
                ],
            )
        except PlaywrightError as exc:
            message = str(exc)
            likely_cause, commands = _classify_playwright_error(message, sys.executable)
            diagnostic = collect_browser_runtime_diagnostic()
            summary = "The local browser runtime could not launch."
            if likely_cause == "missing_chromium_browser":
                summary = "Chromium is not installed for the Python runtime running Sovereign."
            elif likely_cause == "protocol_error_or_stale_browser_runtime":
                summary = "Playwright hit a protocol error while launching Chromium."
            elif likely_cause == "chromium_launch_failed":
                summary = "Chromium failed to launch from the current Playwright runtime."
            blockers = [message]
            if diagnostic.python_executable:
                blockers.append(f"Python executable: {diagnostic.python_executable}")
            if diagnostic.chromium_executable_path:
                blockers.append(
                    f"Chromium executable path: {diagnostic.chromium_executable_path}"
                )
            return _build_browser_failure(
                summary=summary,
                backend=self.backend_name,
                blockers=blockers,
                likely_cause=likely_cause,
                commands=commands,
                diagnostic=diagnostic,
            )
        except Exception as exc:  # pragma: no cover - defensive boundary
            diagnostic = collect_browser_runtime_diagnostic()
            return _build_browser_failure(
                summary="The local browser run failed.",
                backend=self.backend_name,
                blockers=[str(exc), f"Python executable: {diagnostic.python_executable}"],
                likely_cause="unexpected_browser_runtime_error",
                commands=list(diagnostic.recommended_commands),
                diagnostic=diagnostic,
            )

        preview = meta_description or page_text
        concise_summary = self._summarize_page(title=title, preview=preview)
        saved_screenshot_path = str(screenshot_path) if should_save_screenshot else None
        structured_result = {
            "requested_goal": request.objective,
            "requested_url": start_url,
            "final_url": final_url,
            "visited_urls": [final_url] if final_url else [start_url],
            "title": title,
            "status_code": status_code,
            "summary_text": concise_summary,
            "text_preview": self._truncate(preview, 320),
            "extracted_result": concise_summary,
            "headings": headings[:5],
            "meta_description": meta_description or None,
            "screenshot_path": saved_screenshot_path,
            "artifacts": [saved_screenshot_path] if saved_screenshot_path else [],
            "headless": request.headless,
            "local_visible": request.local_visible or not request.headless,
        }

        if blockers:
            return BrowserExecutionResult(
                success=False,
                summary=f"Opened {final_url}, but the page is blocked.",
                backend=self.backend_name,
                structured_result=structured_result,
                evidence=[
                    f"Page title: {title}",
                    f"Screenshot: {screenshot_path}" if saved_screenshot_path else "Screenshot not saved.",
                ],
                blockers=blockers,
                user_action_required=self._blocked_page_actions(blockers),
            )

        return BrowserExecutionResult(
            success=True,
            summary=f"Opened {final_url} and captured page evidence for '{title or final_url}'.",
            backend=self.backend_name,
            structured_result=structured_result,
            evidence=[
                f"Page title: {title}",
                f"Screenshot: {screenshot_path}" if saved_screenshot_path else "Screenshot not saved.",
                f"Summary: {concise_summary}",
            ],
        )

    def _build_screenshot_path(self, url: str) -> Path:
        parsed = urlparse(url)
        host = re.sub(r"[^a-zA-Z0-9.-]+", "-", parsed.netloc or "page").strip("-") or "page"
        path_hint = re.sub(r"[^a-zA-Z0-9]+", "-", parsed.path.strip("/")) or "home"
        filename = f"{host}-{path_hint}.png"
        return self.workspace_root / ".sovereign" / "browser_artifacts" / filename

    def _should_save_screenshot(
        self,
        request: BrowserExecutionRequest,
        *,
        blocked: bool,
    ) -> bool:
        policy = request.screenshot_policy.strip().lower()
        if policy not in {"never", "on_failure", "always"}:
            policy = "on_failure"
        if policy == "never":
            return False
        if request.require_screenshot or policy == "always":
            return True
        return policy == "on_failure" and blocked

    def _normalize_whitespace(self, value: str) -> str:
        return " ".join(value.split())

    def _truncate(self, value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return f"{value[: limit - 3]}..."

    def _summarize_page(self, *, title: str, preview: str) -> str:
        clean_preview = self._truncate(preview, 220) if preview else ""
        if title and clean_preview:
            return f"{title}: {clean_preview}"
        if title:
            return title
        return clean_preview or "The page loaded, but it did not expose much readable text."

    def _detect_page_blockers(
        self,
        *,
        title: str,
        page_text: str,
        url: str,
        status_code: int | None,
        form_markers: list[str],
    ) -> list[str]:
        combined = f"{title} {page_text} {url} {' '.join(form_markers)}".lower()
        blockers: list[str] = []
        if status_code in {401, 403, 407}:
            blockers.append("The site denied access to the browser session.")
        if any(token in combined for token in ("captcha", "verify you are human", "cf-challenge", "cloudflare")):
            blockers.append("The page is blocked by CAPTCHA or human verification.")
        if any(
            token in combined
            for token in ("two-factor", "2fa", "verification code", "one-time code", "authenticator app")
        ):
            blockers.append("The page is asking for 2FA or a verification code.")
        if any(
            token in combined
            for token in (
                "access denied",
                "forbidden",
                "unauthorized",
                "not authorized",
                "auth wall",
                "authentication required",
            )
        ):
            blockers.append("The site denied access to the browser session.")
        if any(
            token in combined
            for token in (
                "log in",
                "login",
                "sign in",
                "signin",
                "password",
                "username",
                "email address",
            )
        ):
            blockers.append("The page requires a login before I can inspect it.")
        if any(
            token in combined
            for token in (
                "checkout",
                "payment",
                "purchase",
                "credit card",
                "card number",
                "billing",
                "place order",
                "buy now",
            )
        ):
            blockers.append("The page is asking for payment or purchase details.")
        if any(
            token in combined
            for token in (
                "social security",
                "ssn",
                "date of birth",
                "medical",
                "bank account",
                "routing number",
                "tax id",
            )
        ):
            blockers.append("The page contains a sensitive form that needs user review.")
        return blockers

    def _blocked_page_actions(self, blockers: list[str]) -> list[str]:
        joined = " ".join(blockers).lower()
        if "captcha" in joined:
            return [
                "Complete the CAPTCHA or provide a path that does not require human verification.",
            ]
        if "2fa" in joined or "verification code" in joined:
            return [
                "Complete the 2FA step or provide the required verification code through a supported secure path.",
            ]
        if "login" in joined:
            return ["Open the page after logging in yourself, or provide a public page that does not require sign-in."]
        if "payment" in joined or "purchase" in joined:
            return ["Handle the payment or purchase step yourself; I will not automate it."]
        if "sensitive form" in joined:
            return ["Review and complete the sensitive form yourself, or provide a non-sensitive page to inspect."]
        return ["Retry with a page that allows automated browser access."]


class BrowserExecutionService:
    """Coordinator that keeps browser backends modular and swappable."""

    def __init__(
        self,
        *,
        runtime_settings: Settings | None = None,
        workspace_root: str | Path | None = None,
        playwright_adapter: PlaywrightBrowserAdapter | None = None,
        browser_use_adapter: BrowserUseCloudAdapter | None = None,
    ) -> None:
        self.runtime_settings = runtime_settings or settings
        self.workspace_root = workspace_root or self.runtime_settings.workspace_root
        self.playwright_adapter = playwright_adapter or PlaywrightBrowserAdapter(
            workspace_root=self.workspace_root
        )
        self.browser_use_adapter = browser_use_adapter or BrowserUseCloudAdapter(
            api_key=self.runtime_settings.browser_use_api_key,
            base_url=self.runtime_settings.browser_base_url,
        )

    def execute(self, request: BrowserExecutionRequest) -> BrowserExecutionResult:
        if not self.runtime_settings.browser_enabled:
            return BrowserExecutionResult(
                success=False,
                summary="Browser execution is disabled for this runtime.",
                backend="none",
                blockers=["BROWSER_ENABLED is false."],
                user_action_required=[
                    "Set `BROWSER_ENABLED=true` to allow live browser execution in this runtime.",
                ],
            )

        safety_blocker = detect_browser_safety_blocker(
            " ".join(part for part in (request.objective, request.start_url or "") if part)
        )
        if safety_blocker is not None:
            preferred = (request.preferred_backend or "none").strip().lower() or "none"
            if preferred not in {"playwright", "browser_use"}:
                preferred = "none"
            return BrowserExecutionResult(
                success=False,
                summary=safety_blocker.reason,
                backend=preferred,
                structured_result={
                    "requested_goal": request.objective,
                    "requested_url": request.start_url,
                    "final_url": request.start_url,
                    "visited_urls": [request.start_url] if request.start_url else [],
                    "blocker_category": safety_blocker.category,
                },
                blockers=[safety_blocker.reason],
                user_action_required=[safety_blocker.next_action],
            )

        backend_mode = self._backend_mode()
        preferred_backend = (request.preferred_backend or "").strip().lower()
        if backend_mode in {"playwright", "browser_use"} and not preferred_backend:
            preferred_backend = backend_mode
        wants_browser_use = preferred_backend == "browser_use"
        wants_playwright = preferred_backend == "playwright"

        if wants_browser_use:
            if self._browser_use_available():
                browser_use_result = self.browser_use_adapter.execute(request)
                if (
                    not browser_use_result.success
                    and request.allow_backend_fallback
                    and request.start_url
                    and backend_mode != "browser_use"
                ):
                    return self.playwright_adapter.execute(
                        request.model_copy(update={"preferred_backend": "playwright"})
                    )
                return browser_use_result
            if request.allow_backend_fallback and request.start_url:
                return self.playwright_adapter.execute(
                    request.model_copy(update={"preferred_backend": "playwright"})
                )
            return BrowserExecutionResult(
                success=False,
                summary="Browser Use was selected for this task, but it is not available in this runtime.",
                backend="browser_use",
                blockers=[
                    "Browser Use is not installed or not configured for the current runtime."
                ],
                user_action_required=[
                    "Install and configure Browser Use, or retry with the Playwright backend."
                ],
            )

        if request.start_url and (wants_playwright or not wants_browser_use):
            return self.playwright_adapter.execute(request)

        if self._browser_use_available():
            return self.browser_use_adapter.execute(request)

        support = detect_browser_runtime_support()
        return BrowserExecutionResult(
            success=False,
            summary="No usable browser execution backend is available for this request.",
            backend="none",
            blockers=[
                "A URL-driven Playwright path or Browser Use cloud configuration is required.",
            ],
            user_action_required=list(support.user_action_requirements())
            + ["Add `BROWSER_USE_API_KEY` if you want open-ended Browser Use cloud tasks."],
        )

    def _backend_mode(self) -> str:
        mode = str(getattr(self.runtime_settings, "browser_backend_mode", "auto")).strip().lower()
        if mode not in {"auto", "playwright", "browser_use"}:
            return "auto"
        return mode

    def _browser_use_available(self) -> bool:
        return bool(getattr(self.runtime_settings, "browser_use_enabled", False)) and self.browser_use_adapter.is_available()
