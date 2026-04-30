"""Inspect non-secret local memory facts and storage details."""

from __future__ import annotations

import json
from pathlib import Path

from app.config import settings
from memory.memory_store import MemoryStore


def _default_local_memory_path() -> Path:
    return Path(settings.workspace_root) / ".sovereign" / "operator_memory.json"


def main() -> None:
    store = MemoryStore()
    provider = getattr(store, "_provider", None)
    local_provider = getattr(provider, "local", provider)
    file_path = Path(getattr(local_provider, "file_path", _default_local_memory_path()))
    snapshot = store.snapshot()

    payload = {
        "memory_backend": settings.memory_backend,
        "provider_name": store.provider_name,
        "storage_path": str(file_path),
        "storage_exists": file_path.exists(),
        "session_turn_count": len(snapshot.session_turns),
        "active_task_count": len(snapshot.active_tasks),
        "open_loop_count": len(snapshot.open_loops),
        "reminder_count": len(snapshot.reminders),
        "facts": {
            "user": [fact.model_dump() for fact in store.list_facts("user")],
            "project": [fact.model_dump() for fact in store.list_facts("project")],
            "operational": [fact.model_dump() for fact in store.list_facts("operational")],
        },
    }
    print(json.dumps(payload, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
