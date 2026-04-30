"""Read-only operator console endpoints.

These routes expose safe dashboard summaries only. They intentionally avoid
returning credentials, raw provider payloads, stack traces, or unrestricted
memory contents.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from agents.catalog import build_agent_catalog
from app.config import settings
from core.models import AgentExecutionStatus, AgentResult, Task, TaskStatus
from core.state import task_state_store
from integrations.calendar.service import CalendarService
from integrations.readiness import IntegrationReadiness, build_integration_readiness
from integrations.reminders.service import reminder_scheduler_service
from integrations.tasks.service import GoogleTasksService
from memory.memory_store import memory_store


router = APIRouter(tags=["operator-console"])

SENSITIVE_FIELD_MARKERS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "credential",
    "password",
    "refresh",
    "private",
)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
LONG_SECRET_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


@router.get("/agents/status")
def get_agents_status() -> dict[str, Any]:
    """Return user-safe status cards for the standing operator lanes."""

    tasks = task_state_store.list_tasks()
    readiness = build_integration_readiness()
    return {
        "source": "live_backend_summary",
        "mock": False,
        "updated_at": _now_iso(),
        "agents": _agent_cards(tasks, readiness),
    }


@router.get("/runs/status")
def get_runs_status() -> dict[str, Any]:
    """Return recent run summaries and the same agent cards for convenience."""

    tasks = task_state_store.list_tasks()
    readiness = build_integration_readiness()
    return {
        "source": "live_backend_summary",
        "mock": False,
        "updated_at": _now_iso(),
        "agents": _agent_cards(tasks, readiness),
        "runs": [_safe_task_summary(task) for task in tasks[:8]],
    }


@router.get("/integrations/status")
def get_integrations_status() -> dict[str, Any]:
    """Return readiness metadata without secrets or credential paths."""

    readiness = build_integration_readiness()
    integrations = [
        _safe_readiness_summary(name, snapshot)
        for name, snapshot in sorted(readiness.items())
        if name.startswith("integration:")
    ]
    return {
        "source": "live_backend_readiness",
        "mock": False,
        "updated_at": _now_iso(),
        "model_provider": {
            "primary": "OpenRouter" if settings.openrouter_api_key else "deterministic fallback",
            "routing_enabled": settings.model_routing_enabled,
            "configured": bool(settings.openrouter_api_key),
            "placeholder_only": False,
        },
        "search": {
            "provider": settings.search_provider or "gemini",
            "configured": bool(settings.openrouter_api_key),
            "enabled": bool(settings.openrouter_api_key) or settings.search_enabled,
            "status": readiness.get("integration:search").status if readiness.get("integration:search") else "unknown",
        },
        "browser": {
            "mode": settings.browser_backend_mode,
            "headless": settings.browser_headless and not (settings.browser_visible or settings.browser_show_window),
            "visible": settings.browser_visible or settings.browser_show_window or not settings.browser_headless,
            "browser_use_enabled": settings.browser_use_enabled,
            "streaming_live": False,
        },
        "integrations": integrations,
    }


@router.get("/memory/summary")
def get_memory_summary() -> dict[str, Any]:
    """Return a safe memory summary for dashboard display."""

    snapshot = memory_store.snapshot()
    safe_facts = []
    for layer, facts in (
        ("project", snapshot.project_facts),
        ("user", snapshot.user_facts),
        ("operational", snapshot.operational_facts),
    ):
        for fact in facts:
            if _safe_memory_fact(fact.key, fact.category, fact.value):
                safe_facts.append(
                    {
                        "layer": layer,
                        "category": fact.category,
                        "key": fact.key,
                        "value": _truncate(fact.value, 220),
                        "confidence": fact.confidence,
                        "updated_at": fact.updated_at,
                    }
                )
    return {
        "source": "live_memory_summary",
        "mock": False,
        "updated_at": _now_iso(),
        "provider": memory_store.provider_name,
        "counts": {
            "session_turns": len(snapshot.session_turns),
            "recent_actions": len(snapshot.recent_actions),
            "active_tasks": len(snapshot.active_tasks),
            "open_loops": len(snapshot.open_loops),
            "reminders": len(snapshot.reminders),
            "project_facts": len(snapshot.project_facts),
            "user_facts": len(snapshot.user_facts),
            "operational_facts": len(snapshot.operational_facts),
        },
        "facts": safe_facts[:10],
        "recent_actions": [
            {
                "summary": _truncate(action.summary, 180),
                "status": action.status,
                "kind": action.kind,
                "created_at": action.created_at,
            }
            for action in snapshot.recent_actions[:8]
            if _safe_text(action.summary)
        ],
        "open_loops": [
            {
                "summary": _truncate(loop.summary, 180),
                "status": loop.status,
                "updated_at": loop.updated_at,
            }
            for loop in snapshot.open_loops[:8]
            if _safe_text(loop.summary)
        ],
        "secrets_exposed": False,
    }


@router.get("/life/reminders")
def get_life_reminders() -> dict[str, Any]:
    """Return safe reminder records and scheduler health."""

    health = reminder_scheduler_service.health()
    reminders = memory_store.list_reminders()
    return {
        "source": "live_memory_store",
        "mock": False,
        "updated_at": _now_iso(),
        "scheduler": {
            "live": health.live,
            "started": health.scheduler_started,
            "status": "live" if health.live else "not_live",
            "blockers": _safe_blockers(health.blockers),
        },
        "reminders": [
            {
                "id": reminder.reminder_id,
                "summary": _truncate(reminder.summary, 180),
                "deliver_at": reminder.deliver_at,
                "status": reminder.status,
                "schedule_kind": reminder.schedule_kind,
                "recurrence": reminder.recurrence_description,
                "timezone": reminder.timezone_name,
                "delivery_channel": reminder.delivery_channel,
            }
            for reminder in reminders[:20]
            if _safe_text(reminder.summary)
        ],
    }


@router.get("/life/calendar")
def get_life_calendar() -> dict[str, Any]:
    """Return today's calendar events when live, otherwise readiness only."""

    readiness = build_integration_readiness()["integration:google_calendar"]
    events: list[dict[str, Any]] = []
    status = readiness.status
    summary = "Calendar readiness loaded."
    blockers: list[str] = []
    if readiness.status == "live":
        try:
            service = CalendarService()
            result = service.events_for_day(target_day=datetime.now(_settings_timezone()))
            summary = result.summary
            status = "live" if result.success else "blocked"
            blockers = _safe_blockers(result.blockers)
            events = [
                {
                    "id": event.event_id,
                    "title": _truncate(event.title, 160),
                    "start": event.start.isoformat(),
                    "end": event.end.isoformat(),
                    "status": event.status,
                    "location": _truncate(event.location or "", 120) or None,
                }
                for event in result.events[:12]
                if _safe_text(event.title)
            ]
        except Exception:
            status = "blocked"
            summary = "Calendar status is live, but reading events failed safely."
            blockers = ["Calendar read failed without exposing provider details."]
    return {
        "source": "calendar_service_readonly" if readiness.status == "live" else "readiness_only",
        "mock": False,
        "updated_at": _now_iso(),
        "status": status,
        "summary": summary,
        "readiness": _safe_readiness_summary("integration:google_calendar", readiness),
        "events": events,
        "blockers": blockers,
    }


@router.get("/life/tasks")
def get_life_tasks() -> dict[str, Any]:
    """Return operator tasks plus Google Tasks when live."""

    readiness = build_integration_readiness()["integration:google_tasks"]
    external_tasks: list[dict[str, Any]] = []
    status = readiness.status
    summary = "Task readiness loaded."
    blockers: list[str] = []
    if readiness.status == "live":
        try:
            result = GoogleTasksService().list_tasks(include_completed=False)
            summary = result.summary
            status = "live" if result.success else "blocked"
            blockers = _safe_blockers(result.blockers)
            external_tasks = [
                {
                    "id": task.task_id,
                    "title": _truncate(task.title, 180),
                    "status": task.status,
                    "due": task.due.isoformat() if task.due else None,
                }
                for task in result.tasks[:20]
                if _safe_text(task.title)
            ]
        except Exception:
            status = "blocked"
            summary = "Google Tasks is live, but reading tasks failed safely."
            blockers = ["Google Tasks read failed without exposing provider details."]
    operator_tasks = [_safe_task_summary(task) for task in task_state_store.list_tasks()[:12]]
    return {
        "source": "operator_state_and_tasks_readiness",
        "mock": False,
        "updated_at": _now_iso(),
        "status": status,
        "summary": summary,
        "readiness": _safe_readiness_summary("integration:google_tasks", readiness),
        "operator_tasks": operator_tasks,
        "external_tasks": external_tasks,
        "blockers": blockers,
    }


@router.get("/browser/status")
def get_browser_status() -> dict[str, Any]:
    """Return the latest safe browser evidence summary."""

    readiness = build_integration_readiness()
    latest = _latest_browser_result(task_state_store.list_tasks())
    filesystem_artifacts = _browser_filesystem_artifacts()
    if latest is None:
        status = "idle"
        evidence = None
        blocker = None
    else:
        task, result, payload = latest
        status = "completed" if result.status == AgentExecutionStatus.COMPLETED else "blocked"
        blocker = (result.blockers[0] if result.blockers else payload.get("blocker")) or None
        evidence = _safe_browser_payload(task, result, payload)
    return {
        "source": "live_browser_evidence_summary",
        "mock": False,
        "updated_at": _now_iso(),
        "status": status,
        "readiness": {
            "browser": _safe_readiness_summary("integration:browser", readiness["integration:browser"]),
            "browser_use": _safe_readiness_summary("integration:browser_use", readiness["integration:browser_use"]),
        },
        "blocker": _truncate(str(blocker), 180) if blocker else None,
        "human_action_required": _human_action_required(blocker),
        "evidence": evidence,
        "recent_artifacts": filesystem_artifacts[:10],
        "live_stream": {
            "available": False,
            "label": "Live browser streaming is not implemented yet.",
            "future_ready": True,
        },
    }


@router.get("/browser/artifacts")
def get_browser_artifacts() -> dict[str, Any]:
    """Return recent safe browser artifacts from task evidence and workspace files."""

    tasks = task_state_store.list_tasks()
    task_artifacts: list[dict[str, Any]] = []
    for task in tasks:
        for result in task.results:
            if result.tool_name != "browser_tool":
                continue
            for evidence in result.evidence:
                payload = getattr(evidence, "payload", {}) or {}
                artifact = _safe_browser_artifact(payload.get("screenshot_path"))
                if artifact is not None:
                    task_artifacts.append({**artifact, "task_id": task.id, "result_status": result.status.value})
    return {
        "source": "live_browser_artifact_summary",
        "mock": False,
        "updated_at": _now_iso(),
        "artifacts": (task_artifacts + _browser_filesystem_artifacts())[:20],
    }


def _agent_cards(
    tasks: list[Task],
    readiness: dict[str, IntegrationReadiness],
) -> list[dict[str, Any]]:
    catalog = build_agent_catalog()
    recent_results = [result for task in tasks for result in task.results]
    card_specs = [
        ("supervisor", "CEO/Supervisor", ("supervisor",), "Owns goal intake, orchestration, and final response."),
        ("research_agent", "Research Agent", ("research_agent",), "Source-backed search and synthesis."),
        ("browser_agent", "Browser Agent", ("browser_agent",), "Browser execution, blockers, and page evidence."),
        ("browser_use", "Browser Use provider", (), "Optional provider for richer browser workflows."),
        ("scheduling_agent", "Scheduling Agent", ("scheduling_agent", "reminder_scheduler_agent"), "Reminders, calendar, and task scheduling."),
        ("communications_agent", "Communications Agent", ("communications_agent",), "Gmail, Slack outbound, and future notifications."),
        ("coding_codex_agent", "Coding/Codex Agent", ("coding_agent", "codex_cli_agent"), "Workspace edits, runtime work, and Codex CLI lane."),
        ("reviewer_verifier", "Reviewer/Verifier", ("reviewer_agent", "verifier_agent"), "Evidence review and anti-fake-completion checks."),
    ]
    cards = []
    for card_id, label, agent_names, fallback in card_specs:
        if card_id == "browser_use":
            browser_use = readiness["integration:browser_use"]
            cards.append(
                {
                    "id": card_id,
                    "name": label,
                    "status": _readiness_to_agent_status(browser_use.status),
                    "last_action": "Provider readiness checked.",
                    "evidence_count": 0,
                    "blocker": _first_missing_field_note(browser_use),
                    "summary": fallback,
                    "source": "integration_readiness",
                }
            )
            continue
        matching_results = [result for result in recent_results if result.agent in agent_names]
        latest = matching_results[-1] if matching_results else None
        definition = next((catalog.by_name(name) for name in agent_names if catalog.by_name(name)), None)
        running = any(
            task.status in {TaskStatus.PLANNING, TaskStatus.ROUTING, TaskStatus.RUNNING}
            for task in tasks
            if any(subtask.assigned_agent in agent_names for subtask in task.subtasks)
        )
        status = "running" if running else _agent_status_from_result(latest, definition.status if definition else "live")
        cards.append(
            {
                "id": card_id,
                "name": label,
                "status": status,
                "last_action": _result_last_action(latest) if latest else "Ready for delegated work.",
                "evidence_count": _evidence_count(matching_results),
                "blocker": latest.blockers[0] if latest and latest.blockers else None,
                "summary": definition.summary if definition else fallback,
                "source": "task_state" if latest else "agent_catalog",
            }
        )
    return cards


def _safe_task_summary(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": _truncate(task.title, 160),
        "goal": _truncate(task.goal, 220),
        "status": task.status.value,
        "request_mode": task.request_mode.value,
        "escalation_level": task.escalation_level.value,
        "planner_mode": task.planner_mode,
        "summary": _truncate(task.summary or "", 220) or None,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "subtask_count": len(task.subtasks),
        "result_count": len(task.results),
        "evidence_count": _evidence_count(task.results),
        "blockers": _safe_blockers(blocker for result in task.results for blocker in result.blockers),
    }


def _safe_readiness_summary(name: str, snapshot: IntegrationReadiness) -> dict[str, Any]:
    return {
        "id": name.replace("integration:", ""),
        "name": name,
        "component": snapshot.backing_component,
        "status": snapshot.status,
        "configured": snapshot.configured,
        "enabled": snapshot.enabled,
        "missing_fields": list(snapshot.missing_fields),
        "notes": [_truncate(note, 220) for note in snapshot.notes[:4] if _safe_text(note)],
    }


def _latest_browser_result(tasks: list[Task]) -> tuple[Task, AgentResult, dict[str, Any]] | None:
    for task in tasks:
        for result in reversed(task.results):
            if result.tool_name != "browser_tool":
                continue
            for evidence in reversed(result.evidence):
                payload = getattr(evidence, "payload", {}) or {}
                if isinstance(payload, dict):
                    return task, result, payload
            return task, result, {}
    return None


def _safe_browser_payload(task: Task, result: AgentResult, payload: dict[str, Any]) -> dict[str, Any]:
    artifact = _safe_browser_artifact(payload.get("screenshot_path"))
    return {
        "task_id": task.id,
        "result_status": result.status.value,
        "requested_url": _safe_url(payload.get("requested_url")),
        "final_url": _safe_url(payload.get("final_url")),
        "title": _truncate(str(payload.get("title") or ""), 160) or None,
        "headings": [_truncate(str(item), 140) for item in (payload.get("headings") or [])[:6] if _safe_text(str(item))],
        "summary": _truncate(str(payload.get("summary_text") or payload.get("extracted_result") or result.summary), 320),
        "text_preview": _truncate(str(payload.get("text_preview") or ""), 320) or None,
        "backend": payload.get("backend"),
        "headless": payload.get("headless"),
        "local_visible": payload.get("local_visible"),
        "screenshot": artifact,
        "artifacts": [_safe_browser_artifact(path) for path in payload.get("artifacts", []) if _safe_browser_artifact(path)],
        "blockers": _safe_blockers(result.blockers or payload.get("blockers", [])),
    }


def _safe_browser_artifact(path_value: Any) -> dict[str, Any] | None:
    if not path_value:
        return None
    try:
        path = Path(str(path_value)).expanduser().resolve()
        workspace_root = Path(settings.workspace_root).expanduser().resolve()
        path.relative_to(workspace_root)
    except Exception:
        return None
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        return None
    return {
        "path": str(path),
        "name": path.name,
        "exists": path.exists(),
        "preview_available": False,
    }


def _browser_filesystem_artifacts() -> list[dict[str, Any]]:
    root = Path(settings.workspace_root).expanduser().resolve() / ".sovereign" / "browser_artifacts"
    if not root.exists():
        return []
    artifacts: list[dict[str, Any]] = []
    for path in sorted(root.glob("*"), key=lambda item: item.stat().st_mtime, reverse=True):
        artifact = _safe_browser_artifact(path)
        if artifact is not None:
            artifacts.append(artifact)
    return artifacts


def _agent_status_from_result(result: AgentResult | None, catalog_status: str) -> str:
    if result is None:
        return "idle" if catalog_status in {"live", "scaffolded"} else "planned"
    if result.status == AgentExecutionStatus.COMPLETED:
        return "completed"
    if result.status == AgentExecutionStatus.BLOCKED:
        return "blocked"
    if result.status == AgentExecutionStatus.SIMULATED:
        return "idle"
    return "running"


def _readiness_to_agent_status(status: str) -> str:
    if status == "live":
        return "completed"
    if status in {"configured_but_disabled", "unavailable"}:
        return "blocked"
    return "idle"


def _result_last_action(result: AgentResult | None) -> str:
    if result is None:
        return "Ready for delegated work."
    return _truncate(result.summary, 220)


def _evidence_count(results: list[AgentResult]) -> int:
    return sum(len(result.evidence) + len(result.artifacts) for result in results)


def _first_missing_field_note(snapshot: IntegrationReadiness) -> str | None:
    if snapshot.status == "live":
        return None
    if snapshot.missing_fields:
        return f"Missing: {', '.join(snapshot.missing_fields[:3])}"
    if snapshot.status == "planned":
        return "Provider is planned, not live."
    if snapshot.status == "configured_but_disabled":
        return "Configured but disabled."
    return None


def _safe_memory_fact(key: str, category: str, value: str) -> bool:
    return _safe_text(key) and _safe_text(category) and _safe_text(value)


def _safe_text(value: str) -> bool:
    lowered = value.lower()
    if any(marker in lowered for marker in SENSITIVE_FIELD_MARKERS):
        return False
    if EMAIL_RE.search(value):
        return False
    if LONG_SECRET_RE.search(value):
        return False
    return True


def _safe_blockers(blockers: Any) -> list[str]:
    safe: list[str] = []
    for blocker in blockers or []:
        text = str(blocker)
        if not _safe_text(text):
            safe.append("Configuration is missing or blocked; details are hidden for safety.")
            continue
        safe.append(_truncate(text, 180))
    return list(dict.fromkeys(safe))[:5]


def _safe_url(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if text.startswith("file:"):
        return "local file URL"
    return _truncate(text, 240)


def _human_action_required(blocker: Any) -> bool:
    if not blocker:
        return False
    lowered = str(blocker).lower()
    return any(token in lowered for token in ("login", "captcha", "2fa", "verification", "human", "payment"))


def _truncate(value: str, limit: int) -> str:
    clean = " ".join(str(value).split())
    if len(clean) <= limit:
        return clean
    return f"{clean[: limit - 3]}..."


def _settings_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(settings.scheduler_timezone)
    except Exception:
        return ZoneInfo("UTC")


def _now_iso() -> str:
    return datetime.now(_settings_timezone()).isoformat()
