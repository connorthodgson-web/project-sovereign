# Memory Audit

## Current Architecture

Sovereign's memory path is centered on `OperatorContextService`.

- User turns enter through the assistant/supervisor path and call `record_user_message`.
- Trivial greetings and thanks are skipped as durable session turns.
- Secret-like messages are skipped before session-turn or fact storage and leave only a safety action record.
- Recent conversation turns are stored as short-term semantic continuity.
- Durable facts are extracted by the LLM memory extractor when available, with local heuristics as fallback for names, preferences, project priorities, parking/location details, Project Sovereign identity notes, goals, and open loops.
- Facts are stored through `MemoryStore`, which delegates to semantic and operational adapters over the configured backend.
- The default provider is local JSON. `MEMORY_PROVIDER=chroma` enables an optional local Chroma vector provider when `chromadb` is installed.
- Zep remains staged behind `HybridMemoryProvider` for the older hosted-memory path, but the lightweight open-source path is now local JSON plus optional Chroma.
- Retrieval uses keyword scoring in `LocalMemoryProvider.search_facts` by default. With Chroma enabled, user/project facts are dual-written to local JSON and Chroma, and recall uses Chroma semantic similarity before falling back to local keyword search.
- Prompt memory is assembled through `compile_prompt_context`, split into core memory, retrieved memory, Personal Ops state, operational state, and short-term state.
- Assistant prompts receive memory through `RuntimeSnapshot.to_prompt_block`.

## Local JSON vs Chroma

`MEMORY_PROVIDER=local` is the default and safest baseline. It requires no extra services, stores the existing JSON snapshot under `.sovereign/operator_memory.json`, and is best for tests, development, deterministic fallback behavior, and deployments where optional packages are not installed.

`MEMORY_PROVIDER=chroma` keeps that JSON snapshot and adds a local Chroma collection for semantic search over durable user/project facts. It is useful when paraphrased recall matters, such as retrieving "concise answers" from a later query like "keep replies short." Chroma is local, open-source, and does not require paid infrastructure.

Chroma should not be used for secrets. Secret-like messages and facts are blocked before durable storage, and the Chroma provider also checks fact keys/values before embedding them. Credentials should continue to live only in a dedicated secrets layer.

## What Improved

- Project-priority phrasing is now captured more reliably, including "my project priority is..." and "memory is the next priority in Sovereign...".
- Open-loop phrasing such as "we still need to..." and "blocked on..." is preserved as operational memory and open-loop state.
- Secret blocking now covers more credential shapes, including API keys, bearer tokens, OpenAI-style keys, Slack tokens, GitHub tokens, Google API keys, and long hex tokens.
- Fact writes now run through a usefulness/safety filter before durable storage.
- Duplicate facts are coalesced by normalized value as well as exact key/category matches.
- Keyword retrieval now ignores generic memory-question words such as "what", "you", "remember", and "about", reducing unrelated recall.
- Chroma retrieval deduplicates semantic matches by normalized fact value and applies a distance cutoff before falling back to local search, which improves paraphrase recall without flooding prompts with unrelated memories.
- User-facing memory replies avoid backend-flavored wording like durable/local memory and answer in normal assistant language.
- Greeting replies continue to bypass memory context, preventing unrelated saved facts from leaking into social messages.

## Remaining Gaps

- Local extraction is intentionally narrow. The LLM extractor should remain the main path for nuanced durable memory once model access is configured.
- Local-only keyword retrieval is still a bridge. It cannot understand paraphrases as well as embeddings or graph memory.
- Staleness is handled with simple recency/category weighting. There is no explicit supersession model beyond stable keys and duplicate coalescing.
- Memory deletion is limited. Name deletion exists, but broader user-controlled forgetting is not yet comprehensive.
- Project memory and operational memory are still both prompt-visible in task contexts, so future work should add stronger task-scoped retrieval policies.
- Secrets are blocked from ordinary memory, but a real secrets vault/access layer is still needed.

## Recommended Future Direction

- Use Chroma as the first lightweight semantic memory layer for local development and no-cost deployments.
- Keep Zep as an optional hosted graph-memory path where relationship memory and managed infrastructure are worth the tradeoff.
- Keep the local JSON provider as a fallback and test fixture, not the long-term source of truth.
- Add Supabase for operational persistence: tasks, open loops, reminders, run traces, evidence records, and dashboard history.
- Keep secrets out of semantic memory entirely. Use a dedicated secrets store with references such as "Gmail credential exists" rather than raw values.
- Add explicit memory lifecycle fields: source turn, supersedes, expires_at, privacy class, and user-visible label.
- Add a verifier pass for higher-impact memory writes so the system can reject junk, collapse duplicates, and mark stale project state.
- Move from keyword-only retrieval to hybrid retrieval: semantic search for recall, recency/project-state boosts for current work, and strict filters for Personal Ops and secrets.
- Future vector upgrades can swap in Qdrant for a stronger local/network vector service, Mem0 for a memory-focused product layer, or a hosted managed vector database if the project later needs multi-user scale. The provider boundary should stay the same: local JSON remains the compatibility fallback, semantic providers improve retrieval, and secrets remain outside embeddings.
