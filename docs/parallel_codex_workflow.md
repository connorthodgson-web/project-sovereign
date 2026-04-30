# Parallel Codex Workflow

Project Sovereign is now large enough that separate Codex runs should work in separate branches and worktrees. Treat the main checkout as the stable coordination point, and give each agent a narrow branch with a clear ownership boundary.

## Recommended Setup

Start from a clean coordination branch:

```powershell
git status --short
git fetch
git switch main
git pull --ff-only
```

Create a worktree per concurrent Codex task:

```powershell
git worktree add ..\project-sovereign-<task> -b codex/<area>-<short-goal> main
```

Use branch names that make ownership obvious:

- `codex/docs-vps-prep`
- `codex/browser-agent-evidence`
- `codex/slack-reminder-runtime`
- `codex/frontend-operator-console`
- `codex/tests-memory-recall`

Keep each Codex session inside its own worktree. Do not run two agents in the same checkout unless one is read-only.

## Safe Parallel Tasks

These usually compose well:

- One agent updating docs while another edits code.
- One agent writing tests for a single module while another implements a different module.
- One frontend-only task and one backend-only task.
- One integration adapter task and one prompt/documentation task.
- One read-only audit agent while another agent makes a scoped patch.

The safest work has a disjoint write set. Give each agent explicit ownership such as `docs/*`, `integrations/browser/*`, or `tests/test_memory_recall.py`.

## Unsafe Overlap

Avoid parallel edits to shared orchestration surfaces unless one agent is explicitly waiting to integrate:

- `core/supervisor.py`, `core/planner.py`, `core/orchestration_graph.py`, and `core/objective_loop.py`.
- `app/config.py` and `.env.example`.
- `agents/catalog.py` and shared agent contracts.
- `tools/registry.py`, `tools/capability_manifest.py`, and capability manifests.
- Test files that cover the same behavior from different angles.
- Generated/runtime files under `workspace/`, `.pytest_cache/`, `__pycache__/`, `venv/`, `frontend/dist/`, and browser artifact folders.

When overlap is unavoidable, sequence the work: land the smaller mechanical change first, then rebase the larger feature branch.

## Merge And Review Process

Before opening or merging a Codex branch:

```powershell
git status --short
python -m pytest -q
python scripts/check_deployment_readiness.py
```

Review the diff with attention to generated files:

```powershell
git diff --stat main...HEAD
git diff --name-only main...HEAD
```

Expected branch contents are source, tests, docs, and small config changes. Generated caches, logs, local workspaces, browser screenshots, `.env`, token files, and virtualenv files should stay out of the branch.

Prefer squash merges for self-contained Codex branches. For multi-branch work, merge in dependency order, rerun tests after each merge, and let the next branch rebase onto the updated target before review.

## Cursor's Role

Cursor is best used as the interactive editor and reviewer on top of Codex branches:

- Open one worktree per Cursor window when comparing branches.
- Use Cursor for human-led inspection, naming cleanup, and quick navigation.
- Let Codex own autonomous implementation in its worktree.
- Do not let Cursor auto-format unrelated files in another agent's branch.

The working rhythm should be: Codex produces a focused branch, Cursor reviews and tightens it, tests run in that same worktree, then the branch merges back through the normal review path.

## Hygiene Rules For Agents

- Read `AGENTS.md` before changing code.
- Keep changes inside the assigned ownership boundary.
- Do not touch secrets, token contents, or `.env` values.
- Do not commit `venv/`, `node_modules/`, caches, logs, screenshots, or browser run artifacts.
- Include evidence for meaningful changes: test output, readiness output, screenshots for browser/UI work, or a clear blocked state.
- If the worktree starts dirty, identify which files are preexisting and do not revert them unless the user asks.
