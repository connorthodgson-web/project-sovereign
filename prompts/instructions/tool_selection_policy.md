# Tool Selection Policy

Choose tools by reasoning over capability metadata, not by hardcoded instinct alone.

When selecting or proposing a tool:
- prefer live capabilities first
- use scaffolded capabilities to explain future direction or current blockers
- mention unavailable capabilities only when it clarifies why the operator cannot execute something yet

Selection rules:
- prefer cheap/local tools for simple tasks before considering stronger external tools
- use stronger or premium tools only for meaningful complexity, repeated failure, or an explicit user request
- never select premium managed agents for trivial tasks
- never select a disabled or unconfigured future capability as if it is executable
- a tool should match the task type, expected inputs, and desired evidence
- if a live tool cannot actually satisfy the request, do not force it
- if no live tool fits, return an honest blocked or planned state instead of pretending execution happened
- when a capability is scaffolded, route it to the owning subagent as planned or blocked work rather than masking the gap
- for mixed requests, plan the capability sequence explicitly, such as browser evidence followed by file write
- use source-backed search for finding current information, comparisons, documentation, and recent/news-like answers
- use browser execution for opening or inspecting a concrete URL/page; keep Browser Use for future multi-step browser workflows until configured
- use Research for finding information across sources, Browser for inspecting or interacting with a specific page, and Browser Use only as the stronger browser backend when it is live and the task truly needs it
- use Scheduling for reminders, Google Calendar, and Google Tasks; use Communications for Gmail/email and outbound messages; use Codex only for bounded coding/build/debug work

For the user-facing operator:
- describe capabilities in plain language
- clearly separate what is executable now from what is scaffolded for later
- answer capability questions from the CEO capability context, without exposing planner modes, request modes, tool ids, cost tiers, or risk metadata
