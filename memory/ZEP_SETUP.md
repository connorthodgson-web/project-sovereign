# Zep Setup

Project Sovereign now supports a staged Zep-backed memory path for durable facts and conversation turns.

## Default assumption

- Cloud Zep is assumed by default.
- If you are self-hosting Zep, set `ZEP_BASE_URL` to your deployment URL.

## Required environment variables

- `MEMORY_BACKEND`
  - `local`: keep the legacy JSON-only path
  - `hybrid`: Zep-first durable memory with local dual-write fallback
  - `zep`: Zep-first durable memory without local dual-write for facts/turns
- `ZEP_API_KEY`
  - Required for `hybrid` and `zep`
- `ZEP_BASE_URL`
  - Optional for Zep Cloud
  - Required for self-hosted Zep
- `ZEP_USER_ID`
  - Stable user-level memory identity in Zep
- `ZEP_THREAD_ID`
  - Stable conversation thread identity used for turn persistence

## Recommended first rollout

1. Create a Zep account at [app.getzep.com](https://app.getzep.com) and generate an API key.
2. Set:
   - `MEMORY_BACKEND=hybrid`
   - `ZEP_API_KEY=...`
   - `ZEP_USER_ID=sovereign-default-user` or a real app/user identifier
   - `ZEP_THREAD_ID=sovereign-default-thread` or a real conversation/thread identifier
3. Leave the local snapshot file in place during verification.
4. Run the test suite and verify memory recall behavior before switching to `zep`.

## Notes on identifiers

- `ZEP_USER_ID` should represent the durable user identity across chats.
- `ZEP_THREAD_ID` should represent the current conversation thread.
- This pass keeps the current single-thread assistant behavior stable by defaulting to one configured thread ID until richer thread identity routing is added later.
