# Memory Migration Map

This pass keeps Sovereign's memory policy custom while replacing durable storage and retrieval with a provider layer that can stage Zep in safely.

## Current write touchpoints

- `core/operator_context.py`
  - `record_user_message()`: conversation turns, proactive fact capture, open loops
  - `record_assistant_reply()`: assistant turn persistence
  - `task_started()`: active task records, action history, operational fact write
  - `task_progress()`: action history, blocker open loops
  - `task_finished()`: active-task cleanup, recent summaries, stale fact deletion, blocker close/update
- `integrations/reminders/service.py`
  - reminder records
  - reminder-related open loops
  - reminder delivery action history

## Current read touchpoints

- `core/operator_context.py`
  - `build_runtime_snapshot()`: snapshot reads, facts by layer, reminders, open loops, active tasks
  - `build_assistant_recall()`: search relevant memory, list durable preferences/project facts
  - `recall_facts()`: direct fact search
  - `recent_user_turns()`: conversation continuity
- `core/conversation.py`
  - user/project memory answers
  - continuity answers
  - reminder status answers

## Staged backend split in this pass

- Zep-backed now:
  - durable fact storage
  - durable fact retrieval / semantic search
  - conversation turn persistence
- Local JSON retained temporarily:
  - reminders
  - open loops
  - active tasks
  - recent action log
  - compatibility snapshot shape for assistant/runtime code

## Migration safety strategy

- Provider abstraction: assistant policy depends on a backend contract rather than direct JSON storage.
- `local` mode: preserves previous behavior exactly.
- `hybrid` mode: Zep-first for durable facts and turns, dual-write to local for rollback safety.
- `zep` mode: Zep-first reads for durable memory, local remains only for unsupported operational records.
