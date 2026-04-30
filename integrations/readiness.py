"""Runtime readiness snapshots for live, scaffolded, and future integrations."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings, settings
from integrations.browser.runtime import detect_browser_runtime_support
from integrations.gmail_client import GmailClient
from integrations.google_calendar_client import GoogleCalendarClient
from integrations.reminders.service import _APSCHEDULER_IMPORT_ERROR
from integrations.tasks.google_client import GoogleTasksClient


@dataclass(frozen=True)
class IntegrationReadiness:
    """Normalized readiness state for one integration-backed capability."""

    backing_component: str
    status: str
    configured: bool
    enabled: bool
    required_fields: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def is_live(self) -> bool:
        return self.status == "live"


def _calendar_scope_readiness_note(scopes: str, live: bool) -> str:
    normalized = " ".join(scopes.lower().split())
    has_full_calendar_scope = (
        "https://www.googleapis.com/auth/calendar" in normalized
        and "calendar.readonly" not in normalized
    )
    if live and has_full_calendar_scope:
        return "Calendar read/write is live; create/update/delete still require confirmation."
    if live:
        return "Calendar read is live, but write readiness depends on full non-readonly calendar scope."
    if has_full_calendar_scope:
        return "Full calendar scope is configured, but credentials/token/runtime readiness still determine live use."
    return "Calendar write actions require the full non-readonly Google Calendar scope plus a live token."


def build_integration_readiness(
    runtime_settings: Settings | None = None,
) -> dict[str, IntegrationReadiness]:
    """Return readiness truth for the operator's current and future integrations."""

    resolved = runtime_settings or settings
    browser_support = detect_browser_runtime_support()

    def snapshot(
        backing_component: str,
        *,
        required_fields: tuple[str, ...] = (),
        enabled: bool = True,
        live_when_configured: bool = False,
        default_status: str = "scaffolded",
        notes: tuple[str, ...] = (),
    ) -> IntegrationReadiness:
        configured = resolved.is_configured(*required_fields) if required_fields else False
        missing = tuple(
            field_name.upper() for field_name in required_fields if not getattr(resolved, field_name)
        )

        if configured and enabled and live_when_configured:
            status = "live"
        elif configured and not enabled:
            status = "configured_but_disabled"
        elif configured and enabled:
            status = default_status
        else:
            status = default_status

        return IntegrationReadiness(
            backing_component=backing_component,
            status=status,
            configured=configured,
            enabled=enabled,
            required_fields=required_fields,
            missing_fields=missing,
            notes=notes,
        )

    snapshots = {
        "integration:slack_transport": snapshot(
            "integration:slack_transport",
            required_fields=("slack_bot_token", "slack_app_token"),
            enabled=resolved.slack_enabled,
            live_when_configured=True,
            default_status="scaffolded",
            notes=("Slack stays first-class, but only counts as live when credentials and runtime enablement both exist.",),
        ),
        "integration:slack_outbound": snapshot(
            "integration:slack_outbound",
            required_fields=("slack_bot_token",),
            enabled=resolved.slack_enabled,
            live_when_configured=True,
            default_status="scaffolded",
            notes=("Outbound Slack delivery is separate from inbound transport and only counts as live when the bot token is available.",),
        ),
        "integration:llm_reasoning": snapshot(
            "integration:llm_reasoning",
            required_fields=("openrouter_api_key",),
            enabled=True,
            live_when_configured=True,
            default_status="unavailable",
            notes=("Deterministic fallbacks should remain usable when the LLM provider is missing.",),
        ),
        "integration:browser": snapshot(
            "integration:browser",
            required_fields=(),
            enabled=resolved.browser_enabled,
            live_when_configured=True,
            default_status="unavailable",
            notes=(
                "Direct browser execution is live for simple URL inspection when Playwright is installed and BROWSER_ENABLED=true.",
                "The complex browser agent remains scaffolded/future-facing and should not be described as live.",
            ),
        ),
        "integration:browser_use": IntegrationReadiness(
            backing_component="integration:browser_use",
            status=(
                "live"
                if (
                    resolved.browser_use_enabled
                    and resolved.browser_use_api_key
                    and browser_support.browser_use_sdk_available
                )
                else "configured_but_disabled"
                if resolved.browser_use_api_key and not resolved.browser_use_enabled
                else "unavailable"
                if resolved.browser_use_enabled
                else "planned"
            ),
            configured=bool(resolved.browser_use_api_key and browser_support.browser_use_sdk_available),
            enabled=resolved.browser_use_enabled,
            required_fields=("browser_use_enabled", "browser_use_api_key"),
            missing_fields=tuple(
                field
                for field, missing in (
                    ("BROWSER_USE_ENABLED", not resolved.browser_use_enabled),
                    ("BROWSER_USE_API_KEY", not resolved.browser_use_api_key),
                    ("BROWSER_USE_SDK", not browser_support.browser_use_sdk_available),
                )
                if missing
            ),
            notes=(
                "Browser Use is an optional stronger browser backend for complex open-ended browser workflows.",
                "It only runs when BROWSER_USE_ENABLED=true, BROWSER_USE_API_KEY is present, and the Browser Use SDK is installed.",
                "Visible local browser preferences are passed through the shared browser request contract when the provider supports them.",
            ),
        ),
        "integration:email": snapshot(
            "integration:email",
            required_fields=("email_provider", "email_api_key", "email_from_address"),
            enabled=resolved.email_enabled,
            default_status="scaffolded",
            notes=("Email stays scaffolded until a provider adapter and delivery confirmation path are implemented.",),
        ),
        "integration:gmail": IntegrationReadiness(
            backing_component="integration:gmail",
            status=(
                "live"
                if (gmail_readiness := GmailClient(runtime_settings=resolved).readiness()).live
                else "configured_but_disabled"
                if gmail_readiness.configured and not gmail_readiness.enabled
                else "scaffolded"
                if gmail_readiness.can_run_local_auth
                else "unavailable"
            ),
            configured=gmail_readiness.configured,
            enabled=gmail_readiness.enabled,
            required_fields=(
                "gmail_enabled",
                "gmail_credentials_path",
                "gmail_token_path",
                "gmail_scopes",
            ),
            missing_fields=tuple(
                field
                for field, missing in (
                    ("GMAIL_ENABLED", not resolved.gmail_enabled),
                    ("GMAIL_CREDENTIALS_PATH", not GmailClient(runtime_settings=resolved)._credentials_path().exists()),
                    ("GMAIL_TOKEN_PATH", not GmailClient(runtime_settings=resolved)._token_path().exists()),
                    ("GMAIL_DEPS", any("dependencies are missing" in blocker for blocker in gmail_readiness.blockers)),
                )
                if missing
            ),
            notes=(
                "Gmail is owned by the unified Communications Agent.",
                "Gmail is live only when enabled, dependencies are installed, credentials exist, and a token exists.",
                "Credentials without a token mean local OAuth can be run, but mailbox actions are not live yet.",
                "Token-ready Gmail may read/search/draft directly; send/delete/archive/forward and bulk changes require confirmation.",
                "Read/search/draft operations may run directly; send/delete/archive/forward and bulk mailbox changes require explicit confirmation.",
            ),
        ),
        "integration:google_calendar": IntegrationReadiness(
            backing_component="integration:google_calendar",
            status=(
                "live"
                if (google_readiness := GoogleCalendarClient(runtime_settings=resolved).readiness()).live
                else "configured_but_disabled"
                if google_readiness.configured and not google_readiness.enabled
                else "scaffolded"
                if google_readiness.can_run_local_auth
                else "unavailable"
            ),
            configured=google_readiness.configured,
            enabled=google_readiness.enabled,
            required_fields=(
                "google_calendar_enabled",
                "google_calendar_credentials_path",
                "google_calendar_token_path",
                "google_calendar_scopes",
            ),
            missing_fields=tuple(
                field
                for field, missing in (
                    ("GOOGLE_CALENDAR_ENABLED", not resolved.google_calendar_enabled),
                    ("GOOGLE_CALENDAR_CREDENTIALS_PATH", not GoogleCalendarClient(runtime_settings=resolved)._credentials_path().exists()),
                    ("GOOGLE_CALENDAR_TOKEN_PATH", not GoogleCalendarClient(runtime_settings=resolved)._token_path().exists()),
                    ("GOOGLE_CALENDAR_DEPS", any("dependencies are missing" in blocker for blocker in google_readiness.blockers)),
                )
                if missing
            ),
            notes=(
                "Google Calendar is owned by the Scheduling / Personal Ops Agent.",
                "Calendar is live only when enabled, dependencies are installed, credentials exist, and a token exists.",
                _calendar_scope_readiness_note(resolved.google_calendar_scopes, google_readiness.live),
            ),
        ),
        "integration:google_tasks": IntegrationReadiness(
            backing_component="integration:google_tasks",
            status=(
                "live"
                if (tasks_readiness := GoogleTasksClient(runtime_settings=resolved).readiness()).live
                else "configured_but_disabled"
                if tasks_readiness.configured and not tasks_readiness.enabled
                else "scaffolded"
                if tasks_readiness.can_run_local_auth
                else "unavailable"
            ),
            configured=tasks_readiness.configured,
            enabled=tasks_readiness.enabled,
            required_fields=(
                "google_tasks_enabled",
                "google_tasks_credentials_path",
                "google_tasks_token_path",
                "google_tasks_scopes",
            ),
            missing_fields=tuple(
                field
                for field, missing in (
                    ("GOOGLE_TASKS_ENABLED", not resolved.google_tasks_enabled),
                    ("GOOGLE_TASKS_CREDENTIALS_PATH", not GoogleTasksClient(runtime_settings=resolved)._credentials_path().exists()),
                    ("GOOGLE_TASKS_TOKEN_PATH", not GoogleTasksClient(runtime_settings=resolved)._token_path().exists()),
                    ("GOOGLE_TASKS_DEPS", any("dependencies are missing" in blocker for blocker in tasks_readiness.blockers)),
                )
                if missing
            ),
            notes=(
                "Google Tasks is owned by the Scheduling / Personal Ops Agent.",
                "Tasks are live only when enabled, dependencies are installed, credentials exist, and a token exists.",
                "Tasks are to-do items with optional due dates; listing, creating, and completing tasks are supported.",
            ),
        ),
        "integration:reminders": IntegrationReadiness(
            backing_component="integration:reminders",
            status=(
                "live"
                if (
                    resolved.reminders_enabled
                    and (resolved.scheduler_backend or "").lower() == "apscheduler"
                    and _APSCHEDULER_IMPORT_ERROR is None
                )
                else "configured_but_disabled"
                if resolved.scheduler_backend and not resolved.reminders_enabled
                else "scaffolded"
            ),
            configured=(
                (resolved.scheduler_backend or "").lower() == "apscheduler"
                and _APSCHEDULER_IMPORT_ERROR is None
            ),
            enabled=resolved.reminders_enabled,
            required_fields=("scheduler_backend",),
            missing_fields=()
            if (
                (resolved.scheduler_backend or "").lower() == "apscheduler"
                and _APSCHEDULER_IMPORT_ERROR is None
            )
            else ("SCHEDULER_BACKEND",)
            if not resolved.scheduler_backend
            else ("APSCHEDULER_PACKAGE",)
            if _APSCHEDULER_IMPORT_ERROR is not None
            else ("SCHEDULER_BACKEND",),
            notes=(
                "Reminder scheduling is only live when APScheduler is installed, configured, and runtime delivery is enabled.",
            ),
        ),
        "integration:messaging": snapshot(
            "integration:messaging",
            required_fields=("messaging_provider", "messaging_api_key"),
            enabled=resolved.messaging_enabled,
            default_status="scaffolded",
            notes=("Cross-channel notifications remain scaffolded until a delivery adapter exists.",),
        ),
        "integration:semantic_retrieval": snapshot(
            "integration:semantic_retrieval",
            required_fields=("retrieval_backend",),
            enabled=resolved.semantic_retrieval_enabled,
            default_status="scaffolded",
            notes=("Keyword retrieval is the current fallback until a real retrieval backend is attached.",),
        ),
        "integration:search": IntegrationReadiness(
            backing_component="integration:search",
            status=(
                "live"
                if (resolved.search_provider or "").lower() in {"", "gemini"}
                and bool(resolved.openrouter_api_key)
                else "unavailable"
            ),
            configured=bool(resolved.openrouter_api_key),
            enabled=bool(resolved.openrouter_api_key) or resolved.search_enabled or bool(resolved.search_provider),
            required_fields=("search_provider", "openrouter_api_key"),
            missing_fields=tuple(
                field
                for field, missing in (
                    ("SEARCH_PROVIDER", bool(resolved.search_provider) and (resolved.search_provider or "").lower() != "gemini"),
                    ("OPENROUTER_API_KEY", not resolved.openrouter_api_key),
                )
                if missing
            ),
            notes=(
                "Search is for finding and summarizing source-backed information, not opening or interacting with a specific page.",
                "API keys must come from environment variables or a secrets layer, not ordinary memory.",
            ),
        ),
        "integration:openclaw": snapshot(
            "integration:openclaw",
            required_fields=("openclaw_base_url",),
            enabled=resolved.openclaw_enabled,
            default_status="scaffolded",
            notes=("OpenClaw is treated as an optional runtime bridge, not a required core dependency.",),
        ),
        "integration:voice_call": snapshot(
            "integration:voice_call",
            required_fields=("call_provider", "call_api_key"),
            enabled=resolved.voice_enabled,
            default_status="planned",
            notes=("Voice and call support are future-facing; no live call execution is wired today.",),
        ),
        "integration:model_routing": snapshot(
            "integration:model_routing",
            required_fields=("frontier_model_provider", "frontier_model_api_key"),
            enabled=resolved.model_routing_enabled,
            default_status="planned",
            notes=("Frontier model/provider routing should stay metadata-driven until a real router is implemented.",),
        ),
        "integration:openai_agents": snapshot(
            "integration:openai_agents",
            required_fields=("openai_agents_api_key",),
            enabled=resolved.openai_agents_enabled,
            default_status="planned",
            notes=("OpenAI Agents SDK is adapter-ready but intentionally optional until credentials and the runtime bridge are attached.",),
        ),
        "integration:manus": snapshot(
            "integration:manus",
            required_fields=("manus_api_key",),
            enabled=resolved.manus_enabled,
            default_status="planned",
            notes=("Manus remains an optional managed-agent backend behind the shared adapter interface.",),
        ),
        "integration:codex_cli": snapshot(
            "integration:codex_cli",
            required_fields=("codex_cli_command",),
            enabled=resolved.codex_cli_enabled,
            live_when_configured=True,
            default_status="planned",
            notes=("Codex CLI stays optional and should plug in through the same managed-agent adapter surface.",),
        ),
    }
    if (
        resolved.calendar_enabled
        and (resolved.calendar_provider or "").lower() == "google"
        and not resolved.is_configured("calendar_client_id", "calendar_client_secret", "calendar_refresh_token")
    ):
        snapshots["integration:google_calendar"] = IntegrationReadiness(
            backing_component="integration:google_calendar",
            status="unavailable",
            configured=False,
            enabled=True,
            required_fields=("calendar_client_id", "calendar_client_secret", "calendar_refresh_token"),
            missing_fields=tuple(
                field
                for field, missing in (
                    ("CALENDAR_CLIENT_ID", not resolved.calendar_client_id),
                    ("CALENDAR_CLIENT_SECRET", not resolved.calendar_client_secret),
                    ("CALENDAR_REFRESH_TOKEN", not resolved.calendar_refresh_token),
                )
                if missing
            ),
            notes=(
                "Legacy Google calendar provider settings were enabled but missing OAuth fields.",
                "The Scheduling / Personal Ops Agent should not report calendar live from a different auth path in this mode.",
            ),
        )
        snapshots["integration:calendar"] = snapshots["integration:google_calendar"]
    browser_missing_fields: list[str] = []
    snapshots["integration:calendar"] = snapshots["integration:google_calendar"]
    browser_notes = list(snapshots["integration:browser"].notes)
    if not browser_support.playwright_available:
        browser_missing_fields.append("PLAYWRIGHT_PACKAGE")
        browser_missing_fields.append("PLAYWRIGHT_BROWSER_BINARY")
    if resolved.browser_use_enabled and resolved.browser_use_api_key and browser_support.browser_use_sdk_available:
        browser_notes.append("Browser Use is enabled and configured as the optional multi-step browser provider.")
    elif resolved.browser_use_enabled:
        browser_notes.append("Browser Use is enabled but missing SDK or credentials.")
    elif resolved.browser_use_api_key:
        browser_notes.append("Browser Use credentials are configured but BROWSER_USE_ENABLED is false.")
    else:
        browser_notes.append("Browser Use is optional and not configured.")
    browser_configured = browser_support.playwright_available or bool(resolved.browser_use_api_key)
    browser_status = (
        "live"
        if resolved.browser_enabled and browser_support.playwright_available
        else "configured_but_disabled"
        if browser_configured and not resolved.browser_enabled
        else "unavailable"
    )
    snapshots["integration:browser"] = IntegrationReadiness(
        backing_component="integration:browser",
        status=browser_status,
        configured=browser_configured,
        enabled=resolved.browser_enabled,
        required_fields=("browser_enabled",),
        missing_fields=tuple(browser_missing_fields),
        notes=tuple(browser_notes),
    )
    return snapshots
