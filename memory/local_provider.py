"""JSON-backed memory provider used as the compatibility fallback."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from threading import Lock

from app.config import settings
from core.models import ReminderScheduleKind, ReminderStatus, TaskStatus, utcnow
from memory.types import (
    ActionRecord,
    ActiveTaskRecord,
    ConversationTurn,
    MemoryFact,
    MemorySnapshot,
    OpenLoopRecord,
    ReminderRecord,
)


class LocalMemoryProvider:
    """Compatibility provider backed by the existing JSON snapshot file."""

    name = "local_json"
    max_turns = 24
    max_recent_actions = 24
    max_facts_per_layer = 64
    max_reminders = 128
    fact_category_weights = {
        "preference": 1.55,
        "current_priority": 1.5,
        "priority": 1.45,
        "decision": 1.4,
        "identity": 1.38,
        "current_goal": 1.35,
        "active_task": 1.35,
        "practical_detail": 1.3,
        "personal_fact": 1.25,
        "goal": 1.25,
        "follow_up": 1.2,
        "constraint": 1.2,
        "recent_result": 1.0,
        "task_context": 0.55,
        "context": 0.95,
    }
    fact_layer_weights = {
        "user": 1.2,
        "project": 1.1,
        "operational": 1.0,
    }
    retrieval_stopwords = {
        "about",
        "again",
        "before",
        "did",
        "does",
        "earlier",
        "have",
        "know",
        "remember",
        "tell",
        "that",
        "the",
        "this",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "you",
        "your",
    }

    def __init__(self, file_path: str | Path | None = None) -> None:
        default_path = Path(settings.workspace_root) / ".sovereign" / "operator_memory.json"
        self.file_path = Path(file_path) if file_path else default_path
        self._lock = Lock()
        self._snapshot = self._load()

    def snapshot(self) -> MemorySnapshot:
        with self._lock:
            return self._snapshot.model_copy(deep=True)

    def record_turn(self, role: str, content: str) -> None:
        cleaned = " ".join(content.split())
        if not cleaned:
            return
        with self._lock:
            self._snapshot.session_turns.append(ConversationTurn(role=role, content=cleaned))
            self._snapshot.session_turns = self._snapshot.session_turns[-self.max_turns :]
            self._save_unlocked()

    def list_turns(self, *, limit: int | None = None) -> list[ConversationTurn]:
        with self._lock:
            turns = [item.model_copy(deep=True) for item in self._snapshot.session_turns]
        if limit is None:
            return turns
        return turns[-limit:]

    def record_action(
        self,
        summary: str,
        *,
        status: str,
        kind: str = "action",
        task_id: str | None = None,
    ) -> None:
        cleaned = " ".join(summary.split())
        if not cleaned:
            return
        with self._lock:
            self._snapshot.recent_actions.append(
                ActionRecord(summary=cleaned, status=status, kind=kind, task_id=task_id)
            )
            self._snapshot.recent_actions = self._snapshot.recent_actions[-self.max_recent_actions :]
            self._save_unlocked()

    def upsert_fact(
        self,
        *,
        layer: str,
        category: str,
        key: str,
        value: str,
        confidence: float = 0.5,
        source: str = "system",
    ) -> None:
        cleaned_value = " ".join(value.split())
        cleaned_key = " ".join(key.lower().split())
        if not cleaned_key or not cleaned_value:
            return

        with self._lock:
            facts = self._facts_for_layer_unlocked(layer)
            canonical_value = self._canonical_fact_value(cleaned_value)
            for fact in facts:
                if fact.key == cleaned_key and fact.category == category:
                    if fact.value == cleaned_value and abs(fact.confidence - confidence) < 0.01:
                        fact.updated_at = utcnow().isoformat()
                    else:
                        fact.value = cleaned_value
                        fact.confidence = max(fact.confidence, confidence)
                        fact.source = source
                        fact.updated_at = utcnow().isoformat()
                    self._save_unlocked()
                    return
                if fact.category == category and self._canonical_fact_value(fact.value) == canonical_value:
                    fact.key = cleaned_key
                    fact.value = cleaned_value
                    fact.confidence = max(fact.confidence, confidence)
                    fact.source = source
                    fact.updated_at = utcnow().isoformat()
                    self._save_unlocked()
                    return

            facts.append(
                MemoryFact(
                    layer=layer,
                    category=category,
                    key=cleaned_key,
                    value=cleaned_value,
                    confidence=max(0.0, min(confidence, 1.0)),
                    source=source,
                )
            )
            self._trim_facts_unlocked(facts)
            self._save_unlocked()

    def list_facts(self, layer: str, *, category: str | None = None) -> list[MemoryFact]:
        with self._lock:
            facts = list(self._facts_for_layer_unlocked(layer))
        selected = facts if category is None else [fact for fact in facts if fact.category == category]
        return sorted(selected, key=self._fact_sort_key, reverse=True)

    def search_facts(self, query: str, *, layers: tuple[str, ...] | None = None) -> list[MemoryFact]:
        query_text = " ".join(query.lower().split())
        query_terms = self._tokenize(query_text)
        if not query_terms:
            return []

        candidate_layers = layers or ("user", "project", "operational")
        matches: list[tuple[float, MemoryFact]] = []
        with self._lock:
            for layer in candidate_layers:
                for fact in self._facts_for_layer_unlocked(layer):
                    score = self._fact_relevance_score(query_text, query_terms, fact)
                    if score > 0:
                        matches.append((score, fact))
        matches.sort(key=lambda item: (item[0], self._fact_sort_key(item[1])), reverse=True)
        seen: set[tuple[str, str]] = set()
        ranked: list[MemoryFact] = []
        for _, fact in matches:
            dedupe_key = (fact.category, " ".join(fact.value.lower().split()))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            ranked.append(fact)
            if len(ranked) >= 8:
                break
        return ranked

    def set_active_task(
        self,
        *,
        task_id: str,
        goal: str,
        status: TaskStatus | str,
        summary: str | None = None,
    ) -> None:
        with self._lock:
            existing = next(
                (task for task in self._snapshot.active_tasks if task.task_id == task_id),
                None,
            )
            status_value = status.value if isinstance(status, TaskStatus) else str(status)
            if existing is None:
                self._snapshot.active_tasks.append(
                    ActiveTaskRecord(task_id=task_id, goal=goal, status=status_value, summary=summary)
                )
            else:
                existing.goal = goal
                existing.status = status_value
                existing.summary = summary
                existing.updated_at = utcnow().isoformat()
            self._snapshot.active_tasks.sort(key=lambda item: item.updated_at, reverse=True)
            self._snapshot.active_tasks = self._snapshot.active_tasks[:10]
            self._save_unlocked()

    def remove_active_task(self, task_id: str) -> None:
        with self._lock:
            self._snapshot.active_tasks = [
                task for task in self._snapshot.active_tasks if task.task_id != task_id
            ]
            self._save_unlocked()

    def upsert_open_loop(
        self,
        *,
        key: str,
        summary: str,
        status: str = "open",
        source: str = "system",
    ) -> None:
        cleaned_key = " ".join(key.lower().split())
        cleaned_summary = " ".join(summary.split())
        if not cleaned_key or not cleaned_summary:
            return

        with self._lock:
            existing = next((loop for loop in self._snapshot.open_loops if loop.key == cleaned_key), None)
            if existing is None:
                self._snapshot.open_loops.append(
                    OpenLoopRecord(key=cleaned_key, summary=cleaned_summary, status=status, source=source)
                )
            else:
                existing.summary = cleaned_summary
                existing.status = status
                existing.source = source
                existing.updated_at = utcnow().isoformat()
            self._snapshot.open_loops.sort(key=lambda item: item.updated_at, reverse=True)
            self._snapshot.open_loops = self._snapshot.open_loops[:16]
            self._save_unlocked()

    def close_open_loop(self, key: str) -> None:
        cleaned_key = " ".join(key.lower().split())
        with self._lock:
            for loop in self._snapshot.open_loops:
                if loop.key == cleaned_key:
                    loop.status = "closed"
                    loop.updated_at = utcnow().isoformat()
            self._save_unlocked()

    def upsert_reminder(
        self,
        *,
        reminder_id: str,
        summary: str,
        deliver_at: str,
        channel: str,
        recipient: str | None = None,
        delivery_channel: str = "slack",
        status: ReminderStatus | str = ReminderStatus.PENDING,
        schedule_kind: ReminderScheduleKind | str = ReminderScheduleKind.ONE_TIME,
        recurrence_rule: str | None = None,
        recurrence_description: str | None = None,
        timezone_name: str | None = None,
        source: str = "system",
        metadata: dict[str, str] | None = None,
    ) -> ReminderRecord:
        cleaned_summary = " ".join(summary.split())
        cleaned_channel = " ".join(channel.split())
        if not reminder_id or not cleaned_summary or not cleaned_channel:
            raise ValueError("Reminder records require id, summary, and channel.")

        status_value = status.value if isinstance(status, ReminderStatus) else str(status)
        schedule_kind_value = (
            schedule_kind.value
            if isinstance(schedule_kind, ReminderScheduleKind)
            else str(schedule_kind)
        )
        with self._lock:
            existing = next(
                (item for item in self._snapshot.reminders if item.reminder_id == reminder_id),
                None,
            )
            if existing is None:
                record = ReminderRecord(
                    reminder_id=reminder_id,
                    summary=cleaned_summary,
                    deliver_at=deliver_at,
                    channel=cleaned_channel,
                    recipient=recipient,
                    delivery_channel=delivery_channel,
                    status=status_value,
                    schedule_kind=schedule_kind_value,
                    recurrence_rule=recurrence_rule,
                    recurrence_description=recurrence_description,
                    timezone_name=timezone_name,
                    source=source,
                    metadata=metadata or {},
                )
                self._snapshot.reminders.append(record)
            else:
                existing.summary = cleaned_summary
                existing.deliver_at = deliver_at
                existing.channel = cleaned_channel
                existing.recipient = recipient
                existing.delivery_channel = delivery_channel
                existing.status = status_value
                existing.schedule_kind = schedule_kind_value
                existing.recurrence_rule = recurrence_rule
                existing.recurrence_description = recurrence_description
                existing.timezone_name = timezone_name
                existing.source = source
                existing.metadata = metadata or existing.metadata
                existing.updated_at = utcnow().isoformat()
                record = existing

            self._snapshot.reminders.sort(
                key=lambda item: (item.updated_at, item.deliver_at),
                reverse=True,
            )
            self._snapshot.reminders = self._snapshot.reminders[: self.max_reminders]
            self._save_unlocked()
            return record.model_copy(deep=True)

    def list_reminders(self, *, statuses: tuple[ReminderStatus | str, ...] | None = None) -> list[ReminderRecord]:
        with self._lock:
            reminders = [item.model_copy(deep=True) for item in self._snapshot.reminders]
        if statuses is None:
            return reminders
        status_values = {
            item.value if isinstance(item, ReminderStatus) else str(item) for item in statuses
        }
        return [item for item in reminders if item.status in status_values]

    def get_reminder(self, reminder_id: str) -> ReminderRecord | None:
        with self._lock:
            existing = next(
                (item for item in self._snapshot.reminders if item.reminder_id == reminder_id),
                None,
            )
            return existing.model_copy(deep=True) if existing is not None else None

    def mark_reminder_delivered(
        self,
        reminder_id: str,
        *,
        delivery_id: str | None = None,
    ) -> ReminderRecord | None:
        with self._lock:
            existing = next(
                (item for item in self._snapshot.reminders if item.reminder_id == reminder_id),
                None,
            )
            if existing is None:
                return None
            timestamp = utcnow().isoformat()
            existing.status = ReminderStatus.DELIVERED.value
            existing.delivered_at = timestamp
            existing.last_delivered_at = timestamp
            existing.delivery_id = delivery_id
            existing.failure_reason = None
            existing.updated_at = timestamp
            self._save_unlocked()
            return existing.model_copy(deep=True)

    def mark_recurring_reminder_delivered(
        self,
        reminder_id: str,
        *,
        next_deliver_at: str,
        delivery_id: str | None = None,
    ) -> ReminderRecord | None:
        with self._lock:
            existing = next(
                (item for item in self._snapshot.reminders if item.reminder_id == reminder_id),
                None,
            )
            if existing is None:
                return None
            timestamp = utcnow().isoformat()
            existing.status = ReminderStatus.PENDING.value
            existing.deliver_at = next_deliver_at
            existing.delivered_at = timestamp
            existing.last_delivered_at = timestamp
            existing.delivery_id = delivery_id
            existing.failure_reason = None
            existing.failed_at = None
            existing.updated_at = timestamp
            self._save_unlocked()
            return existing.model_copy(deep=True)

    def mark_reminder_failed(self, reminder_id: str, *, reason: str) -> ReminderRecord | None:
        cleaned_reason = " ".join(reason.split()) or "Reminder delivery failed."
        with self._lock:
            existing = next(
                (item for item in self._snapshot.reminders if item.reminder_id == reminder_id),
                None,
            )
            if existing is None:
                return None
            timestamp = utcnow().isoformat()
            existing.status = ReminderStatus.FAILED.value
            existing.failed_at = timestamp
            existing.failure_reason = cleaned_reason
            existing.updated_at = timestamp
            self._save_unlocked()
            return existing.model_copy(deep=True)

    def cancel_reminder(self, reminder_id: str, *, reason: str | None = None) -> ReminderRecord | None:
        with self._lock:
            existing = next(
                (item for item in self._snapshot.reminders if item.reminder_id == reminder_id),
                None,
            )
            if existing is None:
                return None
            timestamp = utcnow().isoformat()
            existing.status = ReminderStatus.CANCELED.value
            existing.canceled_at = timestamp
            existing.failure_reason = " ".join((reason or "").split()) or existing.failure_reason
            existing.updated_at = timestamp
            self._save_unlocked()
            return existing.model_copy(deep=True)

    def reset(self) -> None:
        with self._lock:
            self._snapshot = MemorySnapshot()
            self._save_unlocked()

    def delete_fact(self, *, layer: str, key: str, category: str | None = None) -> None:
        cleaned_key = " ".join(key.lower().split())
        if not cleaned_key:
            return
        with self._lock:
            facts = self._facts_for_layer_unlocked(layer)
            facts[:] = [
                fact
                for fact in facts
                if not (fact.key == cleaned_key and (category is None or fact.category == category))
            ]
            self._save_unlocked()

    def prune_transient_memories(self) -> int:
        with self._lock:
            removed = 0
            active_task_ids = {item.task_id for item in self._snapshot.active_tasks}
            project_retained: list[MemoryFact] = []
            for fact in self._snapshot.project_facts:
                if fact.category == "current_goal" and self._timestamp_age_hours(fact.updated_at) > 24:
                    removed += 1
                    continue
                project_retained.append(fact)
            self._snapshot.project_facts = project_retained

            operational_retained: list[MemoryFact] = []
            for fact in self._snapshot.operational_facts:
                task_id = self._task_id_from_fact_key(fact.key)
                age_hours = self._timestamp_age_hours(fact.updated_at)
                if fact.category == "active_task" and task_id not in active_task_ids:
                    removed += 1
                    continue
                if fact.category in {"recent_result", "task_context"} and age_hours > 24 and task_id not in active_task_ids:
                    removed += 1
                    continue
                operational_retained.append(fact)
            self._snapshot.operational_facts = operational_retained

            if removed:
                self._save_unlocked()
            return removed

    def _facts_for_layer_unlocked(self, layer: str) -> list[MemoryFact]:
        if layer == "user":
            return self._snapshot.user_facts
        if layer == "project":
            return self._snapshot.project_facts
        if layer == "operational":
            return self._snapshot.operational_facts
        raise ValueError(f"Unsupported memory layer: {layer}")

    def _trim_facts_unlocked(self, facts: list[MemoryFact]) -> None:
        facts.sort(key=self._fact_sort_key, reverse=True)
        del facts[self.max_facts_per_layer :]

    def _fact_sort_key(self, fact: MemoryFact) -> tuple[float, float, str]:
        return (
            self.fact_category_weights.get(fact.category, 1.0),
            self._recency_score(fact.updated_at) + fact.confidence,
            fact.updated_at,
        )

    def _fact_relevance_score(self, query_text: str, query_terms: list[str], fact: MemoryFact) -> float:
        haystack = f"{fact.key} {fact.value} {fact.category}".lower()
        haystack_terms = set(self._tokenize(haystack))
        overlap = len(haystack_terms.intersection(query_terms))
        exact_hits = int(bool(query_text and query_text in fact.value.lower())) + int(
            bool(query_text and query_text in fact.key.lower())
        )
        substring_hits = sum(1 for term in query_terms if term in haystack)
        phrase_hits = sum(1 for phrase in self._bigrams(query_terms) if phrase and phrase in haystack)
        if not overlap and query_text not in haystack and not substring_hits and not phrase_hits:
            return 0.0
        score = (
            overlap * 2.2
            + substring_hits * 1.0
            + phrase_hits * 1.4
            + exact_hits * 2.8
            + fact.confidence * 1.5
            + self._recency_score(fact.updated_at) * 1.2
        )
        score *= self.fact_category_weights.get(fact.category, 1.0)
        score *= self.fact_layer_weights.get(fact.layer, 1.0)
        return score

    def _recency_score(self, iso_timestamp: str) -> float:
        try:
            timestamp = datetime.fromisoformat(iso_timestamp)
        except ValueError:
            return 0.0
        age_seconds = max((utcnow() - timestamp).total_seconds(), 0.0)
        if age_seconds <= 3600:
            return 1.0
        if age_seconds <= 86400:
            return 0.75
        if age_seconds <= 604800:
            return 0.45
        return 0.2

    def _timestamp_age_hours(self, iso_timestamp: str) -> float:
        try:
            timestamp = datetime.fromisoformat(iso_timestamp)
        except ValueError:
            return 10_000.0
        return max((utcnow() - timestamp).total_seconds() / 3600.0, 0.0)

    def _tokenize(self, value: str) -> list[str]:
        tokens = re.findall(r"[a-z0-9]+", value.lower())
        return [token for token in tokens if len(token) > 2 and token not in self.retrieval_stopwords]

    def _bigrams(self, tokens: list[str]) -> list[str]:
        return [" ".join(tokens[index : index + 2]) for index in range(len(tokens) - 1)]

    def _task_id_from_fact_key(self, key: str) -> str | None:
        match = re.search(r"task[:\-]([a-z0-9\-]+)", key.lower())
        return match.group(1) if match else None

    def _canonical_fact_value(self, value: str) -> str:
        lowered = " ".join(value.lower().split()).strip(" .")
        lowered = re.sub(r"^(you told me:|you prefer|your name is|the current project priority is)\s+", "", lowered)
        lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
        return " ".join(lowered.split())

    def _load(self) -> MemorySnapshot:
        try:
            payload = json.loads(self.file_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return MemorySnapshot()
        except (OSError, json.JSONDecodeError, ValueError):
            return MemorySnapshot()
        try:
            return MemorySnapshot.model_validate(payload)
        except Exception:
            return MemorySnapshot()

    def _save_unlocked(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_path.write_text(
            json.dumps(self._snapshot.model_dump(), indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
