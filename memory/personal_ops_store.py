"""Personal Ops structured storage for user-created lists, notes, and routines."""

from __future__ import annotations

import json
import re
from pathlib import Path
from threading import Lock
from uuid import uuid4

from pydantic import BaseModel, Field

from app.config import settings
from core.models import utcnow
from memory.contacts import clean_contact_alias, is_email_address, normalize_contact_key


class PersonalListItem(BaseModel):
    """One structured item inside a Personal Ops list."""

    item_id: str = Field(default_factory=lambda: f"item-{uuid4()}")
    text: str
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: utcnow().isoformat())


class PersonalListRecord(BaseModel):
    """A durable user-created list or lightweight note bucket."""

    list_id: str = Field(default_factory=lambda: f"list-{uuid4()}")
    name: str
    normalized_name: str
    items: list[PersonalListItem] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: utcnow().isoformat())


class ProactiveRoutineRecord(BaseModel):
    """Future routine manifest entry. This does not imply live scheduling."""

    routine_id: str = Field(default_factory=lambda: f"routine-{uuid4()}")
    title: str
    cadence: str | None = None
    goal: str
    status: str = "planned"
    execution_live: bool = False
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: utcnow().isoformat())


class ContactRecord(BaseModel):
    """A user-provided contact alias stored outside semantic memory."""

    contact_id: str = Field(default_factory=lambda: f"contact-{uuid4()}")
    alias: str
    normalized_alias: str
    email: str
    name: str | None = None
    source: str = "user_explicit"
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: utcnow().isoformat())


class PersonalOpsSnapshot(BaseModel):
    """Serialized Personal Ops document persisted separately from chat memory."""

    lists: list[PersonalListRecord] = Field(default_factory=list)
    proactive_routines: list[ProactiveRoutineRecord] = Field(default_factory=list)
    contacts: list[ContactRecord] = Field(default_factory=list)


class JsonPersonalOpsStore:
    """Small JSON provider for Personal Ops data owned outside transcript memory."""

    name = "personal_ops_json"

    def __init__(self, file_path: str | Path | None = None) -> None:
        default_path = Path(settings.workspace_root) / ".sovereign" / "personal_ops.json"
        self.file_path = Path(file_path) if file_path else default_path
        self._lock = Lock()
        self._snapshot = self._load()

    def snapshot(self) -> PersonalOpsSnapshot:
        with self._lock:
            return self._snapshot.model_copy(deep=True)

    def list_lists(self) -> list[PersonalListRecord]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._snapshot.lists]

    def get_list(self, name_or_id: str) -> PersonalListRecord | None:
        normalized = normalize_personal_list_name(name_or_id)
        with self._lock:
            found = self._find_list_unlocked(normalized, name_or_id)
            return found.model_copy(deep=True) if found is not None else None

    def create_list(self, name: str, *, items: list[str] | None = None) -> PersonalListRecord:
        cleaned_name = clean_list_display_name(name)
        normalized = normalize_personal_list_name(cleaned_name)
        if not normalized:
            raise ValueError("Personal lists require a name.")
        with self._lock:
            record = self._find_list_unlocked(normalized, cleaned_name)
            if record is None:
                record = PersonalListRecord(name=cleaned_name, normalized_name=normalized)
                self._snapshot.lists.append(record)
            if items:
                self._append_items_unlocked(record, items)
            record.updated_at = utcnow().isoformat()
            self._save_unlocked()
            return record.model_copy(deep=True)

    def add_items(self, name_or_id: str, items: list[str]) -> PersonalListRecord:
        cleaned_items = [clean_item_text(item) for item in items]
        cleaned_items = [item for item in cleaned_items if item]
        if not cleaned_items:
            raise ValueError("At least one list item is required.")
        with self._lock:
            normalized = normalize_personal_list_name(name_or_id)
            record = self._find_list_unlocked(normalized, name_or_id)
            if record is None:
                record = PersonalListRecord(
                    name=clean_list_display_name(name_or_id),
                    normalized_name=normalized,
                )
                self._snapshot.lists.append(record)
            self._append_items_unlocked(record, cleaned_items)
            record.updated_at = utcnow().isoformat()
            self._save_unlocked()
            return record.model_copy(deep=True)

    def remove_item(self, name_or_id: str, item_text_or_id: str) -> tuple[PersonalListRecord, PersonalListItem | None]:
        with self._lock:
            record = self._find_list_unlocked(normalize_personal_list_name(name_or_id), name_or_id)
            if record is None:
                raise KeyError(name_or_id)
            removed: PersonalListItem | None = None
            target = " ".join(item_text_or_id.lower().split())
            if target in {"last", "last one", "the last one"} and record.items:
                removed = record.items.pop()
            else:
                for index, item in enumerate(record.items):
                    haystack = " ".join(item.text.lower().split())
                    if item.item_id == item_text_or_id or haystack == target or target in haystack:
                        removed = record.items.pop(index)
                        break
            record.updated_at = utcnow().isoformat()
            self._save_unlocked()
            return record.model_copy(deep=True), removed.model_copy(deep=True) if removed else None

    def update_item(self, name_or_id: str, old_text: str, new_text: str) -> tuple[PersonalListRecord, bool]:
        cleaned_new = clean_item_text(new_text)
        if not cleaned_new:
            raise ValueError("Updated list item cannot be empty.")
        with self._lock:
            record = self._find_list_unlocked(normalize_personal_list_name(name_or_id), name_or_id)
            if record is None:
                raise KeyError(name_or_id)
            target = " ".join(old_text.lower().split())
            for item in record.items:
                haystack = " ".join(item.text.lower().split())
                if haystack == target or target in haystack:
                    item.text = cleaned_new
                    item.updated_at = utcnow().isoformat()
                    record.updated_at = utcnow().isoformat()
                    self._save_unlocked()
                    return record.model_copy(deep=True), True
            return record.model_copy(deep=True), False

    def rename_list(self, name_or_id: str, new_name: str) -> PersonalListRecord:
        cleaned_name = clean_list_display_name(new_name)
        normalized = normalize_personal_list_name(cleaned_name)
        if not normalized:
            raise ValueError("Personal lists require a name.")
        with self._lock:
            record = self._find_list_unlocked(normalize_personal_list_name(name_or_id), name_or_id)
            if record is None:
                raise KeyError(name_or_id)
            record.name = cleaned_name
            record.normalized_name = normalized
            record.updated_at = utcnow().isoformat()
            self._save_unlocked()
            return record.model_copy(deep=True)

    def upsert_proactive_routine(
        self,
        *,
        title: str,
        goal: str,
        cadence: str | None = None,
        status: str = "planned",
        execution_live: bool = False,
    ) -> ProactiveRoutineRecord:
        cleaned_title = " ".join(title.split())
        cleaned_goal = " ".join(goal.split())
        if not cleaned_title or not cleaned_goal:
            raise ValueError("Proactive routines require a title and goal.")
        with self._lock:
            record = ProactiveRoutineRecord(
                title=cleaned_title,
                cadence=" ".join(cadence.split()) if cadence else None,
                goal=cleaned_goal,
                status=status,
                execution_live=execution_live,
            )
            self._snapshot.proactive_routines.append(record)
            self._save_unlocked()
            return record.model_copy(deep=True)

    def list_proactive_routines(self) -> list[ProactiveRoutineRecord]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._snapshot.proactive_routines]

    def upsert_contact(
        self,
        *,
        alias: str,
        email: str,
        name: str | None = None,
        source: str = "user_explicit",
    ) -> ContactRecord:
        cleaned_alias = clean_contact_alias(alias)
        normalized_alias = normalize_contact_key(cleaned_alias)
        cleaned_email = email.strip().strip(" .'\"").lower()
        if not cleaned_alias or not normalized_alias:
            raise ValueError("Contacts require a safe alias or name.")
        if not is_email_address(cleaned_email):
            raise ValueError("Contacts require a valid email address.")
        with self._lock:
            record = self._find_contact_unlocked(normalized_alias, cleaned_email)
            if record is None:
                record = ContactRecord(
                    alias=cleaned_alias,
                    normalized_alias=normalized_alias,
                    email=cleaned_email,
                    name=" ".join(name.split()) if name else None,
                    source=source,
                )
                self._snapshot.contacts.append(record)
            else:
                record.alias = cleaned_alias
                record.normalized_alias = normalized_alias
                record.email = cleaned_email
                if name:
                    record.name = " ".join(name.split())
                record.source = source
                record.updated_at = utcnow().isoformat()
            self._save_unlocked()
            return record.model_copy(deep=True)

    def list_contacts(self) -> list[ContactRecord]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._snapshot.contacts]

    def find_contacts(self, alias_or_email: str) -> list[ContactRecord]:
        query = " ".join(alias_or_email.strip().strip(" .'\"").split())
        if not query:
            return []
        normalized_query = normalize_contact_key(query)
        lowered = query.lower()
        with self._lock:
            exact = [
                item
                for item in self._snapshot.contacts
                if item.email.lower() == lowered
                or item.normalized_alias == normalized_query
                or (item.name and normalize_contact_key(item.name) == normalized_query)
            ]
            if exact:
                return [item.model_copy(deep=True) for item in exact]
            if not normalized_query:
                return []
            partial = [
                item
                for item in self._snapshot.contacts
                if normalized_query in item.normalized_alias
                or (item.name and normalized_query in normalize_contact_key(item.name))
            ]
            return [item.model_copy(deep=True) for item in partial]

    def reset(self) -> None:
        with self._lock:
            self._snapshot = PersonalOpsSnapshot()
            self._save_unlocked()

    def _append_items_unlocked(self, record: PersonalListRecord, items: list[str]) -> None:
        existing = {" ".join(item.text.lower().split()) for item in record.items}
        for item_text in items:
            cleaned = clean_item_text(item_text)
            key = " ".join(cleaned.lower().split())
            if not cleaned or key in existing:
                continue
            record.items.append(PersonalListItem(text=cleaned))
            existing.add(key)

    def _find_list_unlocked(self, normalized: str, name_or_id: str) -> PersonalListRecord | None:
        for record in self._snapshot.lists:
            if record.list_id == name_or_id or record.normalized_name == normalized:
                return record
        return None

    def _find_contact_unlocked(self, normalized_alias: str, email: str) -> ContactRecord | None:
        for record in self._snapshot.contacts:
            if record.normalized_alias == normalized_alias or record.email.lower() == email.lower():
                return record
        return None

    def _load(self) -> PersonalOpsSnapshot:
        try:
            payload = json.loads(self.file_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return PersonalOpsSnapshot()
        except (OSError, json.JSONDecodeError, ValueError):
            return PersonalOpsSnapshot()
        try:
            return PersonalOpsSnapshot.model_validate(payload)
        except Exception:
            return PersonalOpsSnapshot()

    def _save_unlocked(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_path.write_text(
            json.dumps(self._snapshot.model_dump(), indent=2, ensure_ascii=True),
            encoding="utf-8",
        )


def clean_list_display_name(value: str) -> str:
    cleaned = " ".join(value.strip().strip(" .'\"").split())
    cleaned = re.sub(r"^(?:my|the|a|an)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+(?:list|note|notes)$", "", cleaned, flags=re.IGNORECASE)
    if cleaned.lower() == "class":
        cleaned = "classes"
    return cleaned.strip(" .'\"")


def normalize_personal_list_name(value: str) -> str:
    cleaned = clean_list_display_name(value).lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    if cleaned == "class":
        return "classes"
    return cleaned


def clean_item_text(value: str) -> str:
    cleaned = " ".join(value.strip().strip(" .'\"").split())
    cleaned = re.sub(r"^(?:and\s+)?(?:also\s+)?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+(?:too|also)$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" .'\"")
