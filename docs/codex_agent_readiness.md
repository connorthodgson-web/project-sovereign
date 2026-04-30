# Codex Agent Readiness

## Architecture

Project Sovereign treats Codex as a first-class coding worker, not as the main operator.

The intended chain is:

1. CEO / Supervisor frames the user goal, risk, and completion criteria.
2. Coding Agent owns coding-task coordination and simple local file/runtime fallback paths.
3. Codex CLI Agent executes advanced bounded implementation, build, refactor, and debug work when configured.
4. Codex returns file, runtime, git diff, test, final-report, and blocker evidence.
5. Reviewer / Verifier decide whether the evidence is strong enough to count as complete.
6. Sovereign writes the final user-facing answer in plain language.

Codex is therefore an execution backend under Sovereign. It should never own final user communication, skip review gates, or turn an incomplete report into success.

## Windows Usage Notes

- Run Sovereign from the repository root so `AGENTS.md`, tests, and git metadata are visible.
- Set `CODEX_CLI_WORKSPACE_ROOT` to an absolute Windows path such as `C:\Users\conno\project-sovereign`.
- Ensure the configured Codex command works from PowerShell before enabling it.
- Prefer commands that do not require an interactive desktop session. Sovereign invokes the CLI as a subprocess and captures stdout, stderr, exit code, timeout, and git state.
- Paths in Codex reports should be workspace-relative when possible.

## CLI Setup

Codex is disabled by default. Enable it only when the local CLI is installed and the workspace is correct:

```powershell
$env:CODEX_CLI_ENABLED = "true"
$env:CODEX_CLI_COMMAND = "codex"
$env:CODEX_CLI_WORKSPACE_ROOT = "C:\Users\conno\project-sovereign"
$env:CODEX_CLI_TIMEOUT_SECONDS = "900"
```

`CODEX_CLI_AUTO_MODE=false` keeps the adapter in suggest mode. Set it to true only when you want Codex to edit files automatically inside the configured workspace.

## When To Use Codex

Use Codex when the goal is bounded but meaningfully coding-heavy:

- implement or refactor a feature
- debug a failing module or regression
- update tests around a code change
- make cross-file changes that are too involved for the simple file tool
- run a build/test/fix loop with evidence

Use file/runtime tools instead when the request is simple:

- create or read one file
- list a workspace directory
- run one small command
- generate tiny scripts already covered by deterministic local artifact flow

Use browser tools for web navigation and page evidence. Use search/research tools for source-backed research. Do not route browser, search, email, reminder, calendar, or life-assistant actions through Codex.

## Safety Policy

The Codex adapter:

- is disabled unless `CODEX_CLI_ENABLED=true`, a command is available, and the workspace root exists
- prompts Codex with the exact workspace boundary and AGENTS.md context
- forbids deploys, pushes, force-pushes, destructive git commands, broad deletion, and edits outside the workspace
- redacts secret-like stdout/stderr/report values before storing previews
- captures command invoked, exit code, stdout/stderr previews, changed files, diff summary, tests run, final report, blockers, and next actions
- treats `blocked`, `incomplete`, and `needs_user_review` reports as not complete
- rejects claimed success without changed-file or diff evidence
- rejects claimed success when captured test output indicates failures

Reviewer and verifier gates remain mandatory for meaningful coding work. A Codex run only becomes user-visible success after Sovereign can point to concrete evidence.

## Future Path

Future Codex Cloud, Codex app, or computer-use integrations should preserve the same contract:

- Sovereign owns goal framing, safety policy, review, verification, and final response
- Codex owns bounded code execution only
- outputs must include structured evidence, not just chat text
- cloud or app-backed execution should expose workspace boundary, changed files, commands/tests run, and final report fields equivalent to the current CLI adapter
- no provider should bypass reviewer/verifier gates or store credentials in ordinary conversational memory
