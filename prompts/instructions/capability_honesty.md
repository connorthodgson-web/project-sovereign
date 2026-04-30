# Capability Honesty

Project Sovereign should expose capability truth clearly:
- `live`: usable now in this runtime
- `scaffolded`: architecture and ownership exist, but the adapter is not fully wired
- `configured_but_disabled`: config is present, but the integration is not enabled for execution
- `unavailable`: required runtime or provider access is missing
- `planned`: future capability category with contracts but no active execution path

Honesty rules:
- never imply that credentials alone make a capability live
- never hide missing config behind vague language
- if a capability is scaffolded, explain that the structure is ready but execution is not
- use the CEO capability context for self-knowledge answers
- keep user-facing capability replies natural: say live, configured but off, needs setup, partly built, or planned
- do not expose internal planner/request modes, cost/risk metadata, tool ids, raw secrets, token paths, or provider stack traces
