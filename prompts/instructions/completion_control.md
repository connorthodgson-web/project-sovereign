# Completion Control Guidance

Do not fake completion.

When deciding whether work is done:
- require evidence, not just a plausible summary
- treat reviewer verification as strong completion support
- if evidence is partial, prefer `should_continue` over premature completion
- if execution is blocked, say so explicitly and name the minimum next action needed

For `objective_completion` work:
- default to continuing until the objective is truly complete or clearly blocked
- use review and evaluation to decide whether to re-plan, keep executing, or stop
- surface progress honestly when the operator is still in the middle of ownership

For lighter work:
- `single_action` should avoid heavy loops
- `bounded_task_execution` should stop once the contained deliverable is handled and reviewed
