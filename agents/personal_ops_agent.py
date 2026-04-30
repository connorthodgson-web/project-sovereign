"""Unified Personal Ops parent agent for life/admin work."""

from __future__ import annotations

import re
from dataclasses import dataclass

from agents.base_agent import BaseAgent
from agents.communications_agent import CommunicationsAgent
from agents.scheduling_agent import SchedulingPersonalOpsAgent
from core.models import AgentExecutionStatus, AgentResult, SubTask, Task, ToolEvidence
from core.operator_context import OperatorContextService, operator_context
from core.personal_ops_intent import looks_like_personal_list_request, looks_like_proactive_routine_request
from memory.personal_ops_store import (
    JsonPersonalOpsStore,
    PersonalListRecord,
    clean_item_text,
    clean_list_display_name,
)


@dataclass(frozen=True)
class _ListIntent:
    action: str
    list_name: str | None = None
    items: tuple[str, ...] = ()
    old_item: str | None = None
    new_item: str | None = None
    new_list_name: str | None = None
    summarize: bool = False


class PersonalOpsAgent(BaseAgent):
    """Parent domain for reminders, calendar, Gmail, lists/notes, and future routines."""

    name = "personal_ops_agent"

    def __init__(
        self,
        *,
        scheduling_agent: SchedulingPersonalOpsAgent | None = None,
        communications_agent: CommunicationsAgent | None = None,
        personal_store: JsonPersonalOpsStore | None = None,
        operator_context_service: OperatorContextService | None = None,
    ) -> None:
        self.scheduling_agent = scheduling_agent or SchedulingPersonalOpsAgent()
        self.communications_agent = communications_agent or CommunicationsAgent()
        self.personal_store = personal_store or JsonPersonalOpsStore()
        self.operator_context = operator_context_service or operator_context

    def can_handle_message(self, user_message: str) -> bool:
        if looks_like_personal_list_request(user_message) or looks_like_proactive_routine_request(user_message):
            return True
        if self._referent_list_intent(user_message) is not None:
            return True
        return False

    def handle_message(self, user_message: str, *, subtask_id: str = "personal-ops") -> AgentResult:
        if looks_like_proactive_routine_request(user_message):
            return self._handle_proactive_routine(user_message, subtask_id=subtask_id)
        intent = self._parse_list_intent(user_message) or self._referent_list_intent(user_message)
        if intent is None:
            return AgentResult(
                subtask_id=subtask_id,
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary="I can handle that through Personal Ops when it is a reminder, calendar, Gmail, or personal list/note action.",
                tool_name="personal_ops",
                blockers=["No supported Personal Ops action was detected."],
            )
        return self._execute_list_intent(intent, subtask_id=subtask_id)

    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        message = f"{task.goal} {subtask.objective}".strip()
        if self.can_handle_message(message):
            return self.handle_message(message, subtask_id=subtask.id)
        lowered = message.lower()
        if any(token in lowered for token in ("calendar", "event", "appointment", "meeting", "remind me", "reminder")):
            delegated = self.scheduling_agent.run(task, subtask)
            return delegated.model_copy(update={"agent": self.name})
        if any(token in lowered for token in ("gmail", "email", "mailbox", "inbox", "slack")):
            delegated = self.communications_agent.run(task, subtask)
            return delegated.model_copy(update={"agent": self.name})
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=AgentExecutionStatus.BLOCKED,
            summary="Personal Ops owns life/admin work, but this request needs another Sovereign lane.",
            tool_name="personal_ops",
            blockers=["Request did not match reminders, calendar, Gmail, personal lists/notes, or routine setup."],
        )

    def _execute_list_intent(self, intent: _ListIntent, *, subtask_id: str) -> AgentResult:
        try:
            if intent.action == "create":
                record = self.personal_store.create_list(intent.list_name or "personal", items=list(intent.items))
                return self._list_result(subtask_id, record, f"I created your {record.name} list.")
            if intent.action == "add":
                if not intent.list_name:
                    return self._needs_list_name(subtask_id)
                if not intent.items:
                    return self._blocked(subtask_id, "What should I add to that list?")
                record = self.personal_store.add_items(intent.list_name, list(intent.items))
                added = self._join_items(intent.items)
                return self._list_result(subtask_id, record, f"I added {added} to your {record.name} list.")
            if intent.action == "remove":
                if not intent.list_name:
                    return self._needs_list_name(subtask_id)
                target = intent.items[0] if intent.items else "last one"
                record, removed = self.personal_store.remove_item(intent.list_name, target)
                if removed is None:
                    return self._list_result(subtask_id, record, f"I couldn't find {target} in your {record.name} list.", blocked=True)
                return self._list_result(subtask_id, record, f"I removed {removed.text} from your {record.name} list.")
            if intent.action == "rename":
                if not intent.list_name or not intent.new_list_name:
                    return self._blocked(subtask_id, "Which list should I rename, and what should I call it?")
                record = self.personal_store.rename_list(intent.list_name, intent.new_list_name)
                return self._list_result(subtask_id, record, f"I renamed that list to {record.name}.")
            if intent.action == "update":
                if not intent.list_name or not intent.old_item or not intent.new_item:
                    return self._blocked(subtask_id, "Which item should I update, and what should it become?")
                record, updated = self.personal_store.update_item(intent.list_name, intent.old_item, intent.new_item)
                if not updated:
                    return self._list_result(subtask_id, record, f"I couldn't find {intent.old_item} in your {record.name} list.", blocked=True)
                return self._list_result(subtask_id, record, f"I updated that item in your {record.name} list.")
            if intent.action in {"read", "summarize"}:
                record = self._resolve_list(intent.list_name)
                if record is None:
                    name = intent.list_name or "that"
                    return self._blocked(subtask_id, f"I couldn't find your {name} list yet.")
                count = len(record.items)
                if intent.action == "summarize" or intent.summarize:
                    message = (
                        f"Your {record.name} list has {count} item(s): {self._join_items([item.text for item in record.items])}."
                        if count
                        else f"Your {record.name} list is empty."
                    )
                else:
                    message = (
                        f"I found {count} item(s) in your {record.name} list: {self._join_items([item.text for item in record.items])}."
                        if count
                        else f"Your {record.name} list is empty."
                    )
                return self._list_result(subtask_id, record, message)
        except KeyError:
            return self._blocked(subtask_id, f"I couldn't find your {intent.list_name or 'requested'} list yet.")
        except ValueError as exc:
            return self._blocked(subtask_id, str(exc))
        return self._blocked(subtask_id, "That personal list action is not supported yet.")

    def _handle_proactive_routine(self, user_message: str, *, subtask_id: str) -> AgentResult:
        cadence = self._extract_cadence(user_message)
        title = "Personal Ops routine"
        if cadence:
            title = f"{cadence.title()} Personal Ops routine"
        routine = self.personal_store.upsert_proactive_routine(
            title=title,
            goal=user_message,
            cadence=cadence,
            status="planned",
            execution_live=False,
        )
        return AgentResult(
            subtask_id=subtask_id,
            agent=self.name,
            status=AgentExecutionStatus.BLOCKED,
            summary=(
                "I saved that as a future Personal Ops routine placeholder, but proactive routine execution is not fully live yet."
            ),
            tool_name="proactive_routines",
            evidence=[
                ToolEvidence(
                    tool_name="proactive_routines",
                    summary="Stored a planned proactive routine manifest entry without claiming live execution.",
                    payload=routine.model_dump(),
                    verification_notes=["execution_live is false; no scheduler was started."],
                )
            ],
            blockers=["Full proactive routine execution is not implemented in this pass."],
            next_actions=["Use reminders for currently live recurring notifications when the scheduler and outbound delivery are configured."],
        )

    def _parse_list_intent(self, message: str) -> _ListIntent | None:
        lowered = " ".join(message.lower().strip().split())
        if not lowered:
            return None
        rename = re.match(r"rename\s+(?:my\s+)?(?P<name>.+?)\s+list\s+to\s+(?P<new>.+)$", message, flags=re.IGNORECASE)
        if rename:
            return _ListIntent(
                action="rename",
                list_name=clean_list_display_name(rename.group("name")),
                new_list_name=clean_list_display_name(rename.group("new")),
            )
        update = re.match(
            r"update\s+(?P<old>.+?)\s+to\s+(?P<new>.+?)\s+(?:in|on)\s+(?:my\s+)?(?P<name>.+?)\s+list$",
            message,
            flags=re.IGNORECASE,
        )
        if update:
            return _ListIntent(
                action="update",
                list_name=clean_list_display_name(update.group("name")),
                old_item=clean_item_text(update.group("old")),
                new_item=clean_item_text(update.group("new")),
            )
        add = re.match(r"add\s+(?P<items>.+?)\s+to\s+(?:my\s+)?(?P<name>.+?)\s+list$", message, flags=re.IGNORECASE)
        if add:
            return _ListIntent(
                action="add",
                list_name=clean_list_display_name(add.group("name")),
                items=tuple(self._split_items(add.group("items"))),
            )
        remove = re.match(r"(?:remove|delete)\s+(?P<items>.+?)\s+from\s+(?:my\s+)?(?P<name>.+?)\s+list$", message, flags=re.IGNORECASE)
        if remove:
            return _ListIntent(
                action="remove",
                list_name=clean_list_display_name(remove.group("name")),
                items=(clean_item_text(remove.group("items")),),
            )
        create = re.match(
            r"(?:make|create)\s+(?:a\s+)?(?:new\s+)?list\s+(?:for|called|named)\s+(?:my\s+)?(?P<name>.+?)(?:\s+with\s+(?P<items>.+))?$",
            message,
            flags=re.IGNORECASE,
        )
        if create:
            return _ListIntent(
                action="create",
                list_name=clean_list_display_name(create.group("name")),
                items=tuple(self._split_items(create.group("items") or "")),
            )
        remember = re.match(r"remember\s+this\s+list\s+for\s+(?P<name>.+?)(?::\s*(?P<items>.+))?$", message, flags=re.IGNORECASE)
        if remember:
            return _ListIntent(
                action="create",
                list_name=clean_list_display_name(remember.group("name")),
                items=tuple(self._split_items(remember.group("items") or "")),
            )
        if any(lowered.startswith(prefix) for prefix in ("what's on ", "whats on ", "what is on ")):
            name = self._extract_list_name(message) or self._recent_list_name(message)
            return _ListIntent(action="read", list_name=name)
        if lowered.startswith("summarize "):
            name = self._extract_list_name(message) or self._recent_list_name(message)
            return _ListIntent(action="summarize", list_name=name, summarize=True)
        if "what classes did i tell you" in lowered or "what class list" in lowered:
            return _ListIntent(action="read", list_name="classes")
        if lowered.startswith(("read ", "list ")) and " list" in lowered:
            return _ListIntent(action="read", list_name=self._extract_list_name(message))
        return None

    def _referent_list_intent(self, message: str) -> _ListIntent | None:
        lowered = " ".join(message.lower().strip().split())
        recent_name = self._recent_list_name(message)
        if recent_name is None:
            return None
        if lowered.startswith("add "):
            item_text = re.sub(r"^add\s+", "", message.strip(), flags=re.IGNORECASE)
            return _ListIntent(action="add", list_name=recent_name, items=tuple(self._split_items(item_text)))
        if lowered.startswith(("remove ", "delete ")):
            item_text = re.sub(r"^(?:remove|delete)\s+", "", message.strip(), flags=re.IGNORECASE)
            if item_text in {"it", "that", "this"}:
                item_text = "last one"
            return _ListIntent(action="remove", list_name=recent_name, items=(clean_item_text(item_text),))
        if "that list" in lowered or lowered in {"what's on it", "whats on it", "what is on it"}:
            return _ListIntent(action="read", list_name=recent_name)
        return None

    def _extract_list_name(self, message: str) -> str | None:
        if "that list" in message.lower() or "this list" in message.lower():
            return self._recent_list_name(message)
        patterns = (
            r"(?:my|the)\s+(?P<name>.+?)\s+list\b",
            r"\b(?P<name>[A-Za-z][A-Za-z0-9 &'-]{1,60})\s+list\b",
        )
        for pattern in patterns:
            match = re.search(pattern, message, flags=re.IGNORECASE)
            if not match:
                continue
            if "name" not in match.groupdict():
                return self._recent_list_name(message)
            return clean_list_display_name(match.group("name"))
        return None

    def _recent_list_name(self, message: str) -> str | None:
        referents = self.operator_context.resolve_recent_referents(
            object_type="personal_list",
            pronoun_text=message,
        )
        if len(referents) == 1:
            return referents[0].object_id or referents[0].summary
        return None

    def _resolve_list(self, list_name: str | None) -> PersonalListRecord | None:
        if list_name:
            return self.personal_store.get_list(list_name)
        recent = self._recent_list_name("that list")
        return self.personal_store.get_list(recent) if recent else None

    def _list_result(self, subtask_id: str, record: PersonalListRecord, summary: str, *, blocked: bool = False) -> AgentResult:
        self.operator_context.register_actionable_object(
            object_type="personal_list",
            object_id=record.list_id,
            summary=record.name,
            source="personal_ops_lists",
            confidence=0.95,
        )
        return AgentResult(
            subtask_id=subtask_id,
            agent=self.name,
            status=AgentExecutionStatus.BLOCKED if blocked else AgentExecutionStatus.COMPLETED,
            summary=summary,
            tool_name="personal_ops_lists",
            evidence=[
                ToolEvidence(
                    tool_name="personal_ops_lists",
                    summary=f"Structured Personal Ops list store updated/read for {record.name}.",
                    payload={
                        "list_id": record.list_id,
                        "name": record.name,
                        "normalized_name": record.normalized_name,
                        "items": [item.model_dump() for item in record.items],
                        "count": len(record.items),
                        "source": "personal_ops_store",
                    },
                    verification_notes=["Stored outside ordinary chat transcript memory."],
                )
            ],
            blockers=[] if not blocked else [summary],
        )

    def _blocked(self, subtask_id: str, summary: str) -> AgentResult:
        return AgentResult(
            subtask_id=subtask_id,
            agent=self.name,
            status=AgentExecutionStatus.BLOCKED,
            summary=summary,
            tool_name="personal_ops_lists",
            blockers=[summary],
        )

    def _needs_list_name(self, subtask_id: str) -> AgentResult:
        return self._blocked(subtask_id, "Which list should I use?")

    def _split_items(self, raw: str) -> list[str]:
        cleaned = re.sub(r"\s+\b(?:and)\b\s+", ",", raw.strip(), flags=re.IGNORECASE)
        return [clean_item_text(item) for item in cleaned.split(",") if clean_item_text(item)]

    def _join_items(self, items) -> str:
        values = [str(item).strip() for item in items if str(item).strip()]
        if not values:
            return "nothing"
        if len(values) == 1:
            return values[0]
        return f"{', '.join(values[:-1])}, and {values[-1]}"

    def _extract_cadence(self, message: str) -> str | None:
        lowered = " ".join(message.lower().split())
        for cadence in ("every morning", "every sunday", "every week", "weekly", "daily", "each morning"):
            if cadence in lowered:
                return cadence
        return None
