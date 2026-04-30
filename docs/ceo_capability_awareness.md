# CEO Capability Awareness

Project Sovereign now treats capability awareness as a runtime view, not a static marketing answer.

## Current Model

The editable capability inventory lives in `prompts/capabilities/tools.json`. Runtime truth comes from `integrations/readiness.py`, which checks whether each integration is live, configured but disabled, unavailable, scaffolded, or planned.

`tools/capability_manifest.py` merges those two sources into:

- `CapabilitySnapshot`: detailed capability metadata for prompts, planning, and dashboard-adjacent logic.
- `CEOCapabilityContext`: plain-language capability and agent context for CEO/operator self-knowledge.

The CEO uses this context to answer capability questions naturally while preserving the same safety and evidence rules used by execution paths.

## Readiness Flow

1. The manifest says what a capability is, who owns it, what evidence it needs, and what setup it expects.
2. `build_integration_readiness()` checks the current runtime configuration.
3. `CapabilityCatalog` resolves each manifest entry against readiness.
4. `CEOCapabilityContext` converts the resolved state into plain-language status for prompts and answers.
5. `core/conversation.py` uses the same catalog for answers like:
   - what can you do?
   - can you use the browser?
   - can you send emails?
   - can you use Codex?
   - can you see my calendar/tasks?
   - what agents do you have?
   - what is currently connected?
   - what should we build next?

The dashboard readiness endpoints still read from `integrations/readiness.py`, so assistant self-knowledge and Operator Console readiness share the same source of truth.

## Agent Delegation

- CEO/Supervisor: goal intake, planning decisions, delegation, final response.
- Planning Agent: plans, dependencies, evidence expectations, blockers.
- Research Agent: source-backed current-info search and synthesis.
- Browser Agent: page/browser workflows and browser evidence; Browser Use is only used when enabled for safe multi-step workflows.
- Coding/Codex: local file/runtime work for small tasks; Codex CLI for serious bounded implementation when enabled.
- Scheduling Agent: reminders, Google Calendar, Google Tasks.
- Communications Agent: Gmail/email and outbound notifications.
- Memory Agent: useful context capture and recall, never raw secrets.
- Reviewer/Verifier: evidence checks and anti-fake-completion gates.

## Adding Future Tools Or Agents

1. Add or update the capability entry in `prompts/capabilities/tools.json`.
2. Add a readiness snapshot in `integrations/readiness.py` if runtime setup affects the status.
3. Assign the capability to an agent in `agents/catalog.py`.
4. Add execution code only after the capability has clear inputs, outputs, blockers, safety limits, and evidence expectations.
5. Add tests that verify the CEO does not overclaim the capability before it is live.

Do not expose API keys, token paths, credential contents, or raw provider errors in capability answers.

## Remaining Gaps

- Some setup blockers are still environment-shaped internally and are translated only in the conversational layer.
- Browser Use is readiness-aware but still optional and not the default for simple browser tasks.
- Codex CLI readiness can say the lane is enabled/configured, but the adapter still performs the stronger command/workspace check at execution time.
- Dashboard controls are read-only; changing readiness still happens through environment/secrets setup.
