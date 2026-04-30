"""Managed Codex CLI adapter for bounded coding execution."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from agents.adapter import AgentAdapter
from app.config import Settings, settings
from core.model_routing import ModelRequestContext, ModelRouter
from core.models import AgentExecutionStatus, AgentResult, ExecutionEscalation, SubTask, Task, ToolEvidence


class CodexCliAgentAdapter(AgentAdapter):
    """Executes bounded coding work through a locally installed Codex CLI."""

    report_outcomes = {"completed", "blocked", "incomplete", "needs_user_review"}
    failed_test_patterns = (
        r"\bfailed\b",
        r"\bfailures?\b",
        r"\berrors?\b",
        r"\bexit code [1-9]\d*\b",
        r"\btests? failed\b",
    )

    def __init__(
        self,
        *,
        descriptor,
        runtime_settings: Settings | None = None,
        subprocess_run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        which: Callable[[str], str | None] | None = None,
        time_monotonic: Callable[[], float] | None = None,
    ) -> None:
        super().__init__()
        self.descriptor = descriptor
        self.runtime_settings = runtime_settings or settings
        self._subprocess_run = subprocess_run or subprocess.run
        self._which = which or shutil.which
        self._time_monotonic = time_monotonic or time.monotonic
        self.model_router = ModelRouter()
        self.descriptor.enabled = self._environment_check()["ready"]

    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        self.logger.info(
            "CODEX_AGENT_START task=%s subtask=%s agent=%s",
            task.id,
            subtask.id,
            self.agent_id,
        )
        env_check = self._environment_check()
        self.logger.info(
            "CODEX_ENV_CHECK task=%s subtask=%s ready=%s enabled_flag=%s command=%s workspace_root=%s command_available=%s",
            task.id,
            subtask.id,
            env_check["ready"],
            env_check["enabled_flag"],
            env_check["command"],
            env_check["workspace_root"],
            env_check["command_available"],
        )
        if not env_check["ready"]:
            result = self._blocked_result(
                task,
                subtask,
                summary="Codex CLI Agent is blocked because the local Codex runtime is not fully configured.",
                blockers=env_check["blockers"],
                details=env_check["details"],
                next_actions=self._setup_next_actions(env_check),
                payload={"env_check": env_check},
            )
            self.logger.info(
                "CODEX_AGENT_END task=%s subtask=%s status=%s",
                task.id,
                subtask.id,
                result.status.value,
            )
            return result

        workspace_root = Path(str(env_check["workspace_root"])).resolve()
        request_text = " ".join([task.goal, subtask.objective, subtask.description]).strip()
        if self._is_dangerous_request(request_text) and not self._is_explicitly_authorized(request_text):
            result = self._blocked_result(
                task,
                subtask,
                summary="Codex CLI Agent blocked a destructive or high-risk request that was not explicitly authorized.",
                blockers=[
                    "Dangerous or destructive coding instructions require explicit authorization before Codex can proceed."
                ],
                details=[
                    f"Goal context: {task.goal}",
                    f"Objective: {subtask.objective}",
                    "Guardrail triggered on destructive-request screening.",
                ],
                next_actions=[
                    "Restate the request as a bounded coding task without destructive actions.",
                    "If destructive work is truly required, provide explicit authorization and the exact intended scope.",
                ],
                payload={"env_check": env_check, "guardrail": "dangerous_request"},
            )
            self.logger.info(
                "CODEX_AGENT_END task=%s subtask=%s status=%s",
                task.id,
                subtask.id,
                result.status.value,
            )
            return result

        bounded_prompt = self._build_bounded_prompt(task, subtask, workspace_root)
        git_before = self._capture_git_snapshot(workspace_root)
        command_parts = self._command_parts(str(env_check["command"]))
        mode_flag = "--auto-edit" if self.runtime_settings.codex_cli_auto_mode else "--suggest"
        command = command_parts + [mode_flag]

        stdout = ""
        stderr = ""
        exit_code: int | None = None
        timed_out = False
        start_time = self._time_monotonic()
        self.logger.info(
            "CODEX_RUN_START task=%s subtask=%s command=%s workspace_root=%s mode=%s",
            task.id,
            subtask.id,
            command,
            workspace_root,
            mode_flag,
        )
        try:
            completed = self._subprocess_run(
                command,
                cwd=workspace_root,
                input=bounded_prompt,
                capture_output=True,
                text=True,
                timeout=self.runtime_settings.codex_cli_timeout_seconds,
                check=False,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = self._coerce_process_output(exc.stdout)
            stderr = self._coerce_process_output(exc.stderr)
        except OSError as exc:
            git_after = self._capture_git_snapshot(workspace_root)
            payload = self._build_payload(
                env_check=env_check,
                workspace_root=workspace_root,
                command_invoked=command,
                bounded_prompt=bounded_prompt,
                stdout="",
                stderr=str(exc),
                exit_code=None,
                timed_out=False,
                duration_ms=int((self._time_monotonic() - start_time) * 1000),
                git_before=git_before,
                git_after=git_after,
                parsed_report={},
            )
            self.logger.info(
                "CODEX_DIFF_CAPTURED task=%s subtask=%s git_available=%s repo_detected=%s changed_files=%s",
                task.id,
                subtask.id,
                payload["git_after"].get("git_available"),
                payload["git_after"].get("repo_detected"),
                len(payload["changed_files"]),
            )
            result = self._blocked_result(
                task,
                subtask,
                summary="Codex CLI could not be started in the current runtime.",
                blockers=[str(exc)],
                details=[
                    f"Goal context: {task.goal}",
                    f"Objective: {subtask.objective}",
                    f"Workspace root: {workspace_root}",
                ],
                next_actions=[
                    "Confirm the CODEX_CLI_COMMAND value points to an executable Codex CLI binary.",
                    "Retry after the local Codex installation can be executed from this runtime.",
                ],
                payload=payload,
            )
            self.logger.info(
                "CODEX_RUN_END task=%s subtask=%s exit_code=%s timed_out=%s report_outcome=%s",
                task.id,
                subtask.id,
                exit_code,
                timed_out,
                payload.get("codex_report", {}).get("outcome"),
            )
            self.logger.info(
                "CODEX_AGENT_END task=%s subtask=%s status=%s",
                task.id,
                subtask.id,
                result.status.value,
            )
            return result

        duration_ms = int((self._time_monotonic() - start_time) * 1000)
        parsed_report = self._parse_codex_report(stdout, stderr)
        git_after = self._capture_git_snapshot(workspace_root)
        payload = self._build_payload(
            env_check=env_check,
            workspace_root=workspace_root,
            command_invoked=command,
            bounded_prompt=bounded_prompt,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=duration_ms,
            git_before=git_before,
            git_after=git_after,
            parsed_report=parsed_report,
        )
        self.logger.info(
            "CODEX_DIFF_CAPTURED task=%s subtask=%s git_available=%s repo_detected=%s changed_files=%s",
            task.id,
            subtask.id,
            payload["git_after"].get("git_available"),
            payload["git_after"].get("repo_detected"),
            len(payload["changed_files"]),
        )

        execution_blockers: list[str] = []
        next_actions: list[str] = []
        status = AgentExecutionStatus.COMPLETED
        report_outcome = parsed_report.get("outcome")
        evidence_blockers = self._completion_evidence_blockers(payload)
        if timed_out:
            status = AgentExecutionStatus.BLOCKED
            execution_blockers.append(
                f"Codex CLI timed out after {self.runtime_settings.codex_cli_timeout_seconds} seconds."
            )
            next_actions.append("Retry with a narrower bounded coding prompt or a longer timeout.")
        elif exit_code not in (0, None):
            status = AgentExecutionStatus.BLOCKED
            execution_blockers.append(f"Codex CLI exited with code {exit_code}.")
            next_actions.append("Inspect Codex stdout/stderr and retry once the failure is understood.")
        elif report_outcome == "blocked":
            status = AgentExecutionStatus.BLOCKED
            execution_blockers.extend(parsed_report.get("blockers", []) or ["Codex reported that the task is blocked."])
            next_actions.extend(
                parsed_report.get("next_actions", [])
                or ["Resolve the reported blocker before retrying the delegated coding task."]
            )
        elif report_outcome in {"incomplete", "needs_user_review"}:
            status = AgentExecutionStatus.BLOCKED
            execution_blockers.extend(
                parsed_report.get("blockers", [])
                or [f"Codex reported the task outcome as {report_outcome}."]
            )
            next_actions.extend(
                parsed_report.get("next_actions", [])
                or ["Review the Codex output before deciding whether to retry or continue."]
            )
        elif evidence_blockers:
            status = AgentExecutionStatus.BLOCKED
            execution_blockers.extend(evidence_blockers)
            next_actions.append(
                "Retry the bounded coding task and require Codex to report changed files plus any verification it ran."
            )

        summary = self._build_summary(
            exit_code=exit_code,
            timed_out=timed_out,
            report_outcome=report_outcome,
            parsed_report=parsed_report,
            payload=payload,
        )
        result = AgentResult(
            subtask_id=subtask.id,
            agent=self.agent_id,
            status=status,
            summary=summary,
            tool_name="codex_cli",
            details=[
                f"Goal context: {task.goal}",
                f"Objective: {subtask.objective}",
                f"Workspace root: {workspace_root}",
                f"Codex command: {' '.join(command)}",
                f"Exit code: {exit_code}",
                f"Timed out: {timed_out}",
                f"Reported outcome: {report_outcome or 'unspecified'}",
                f"Changed files captured: {len(payload['changed_files'])}",
                f"Tests reported: {', '.join(payload['tests_run']) if payload['tests_run'] else 'none reported'}",
                f"Completion evidence state: {payload['completion_evidence_state']}",
            ],
            artifacts=[f"codex_cli:{workspace_root}"],
            evidence=[
                ToolEvidence(
                    tool_name="codex_cli",
                    summary=summary,
                    payload=payload,
                    verification_notes=[],
                )
            ],
            blockers=execution_blockers,
            next_actions=next_actions,
        )
        self.logger.info(
            "CODEX_RUN_END task=%s subtask=%s exit_code=%s timed_out=%s report_outcome=%s",
            task.id,
            subtask.id,
            exit_code,
            timed_out,
            report_outcome,
        )
        self.logger.info(
            "CODEX_AGENT_END task=%s subtask=%s status=%s",
            task.id,
            subtask.id,
            result.status.value,
        )
        return result

    def _environment_check(self) -> dict[str, Any]:
        command = (self.runtime_settings.codex_cli_command or "").strip()
        workspace_root = (self.runtime_settings.codex_cli_workspace_root or "").strip()
        enabled_flag = bool(self.runtime_settings.codex_cli_enabled)
        command_executable = self._resolve_command_executable(command)
        command_available = bool(command_executable)
        workspace_exists = bool(workspace_root) and Path(workspace_root).exists()
        blockers: list[str] = []
        if not enabled_flag:
            blockers.append("Set CODEX_CLI_ENABLED=true to enable the managed Codex coding lane.")
        if not command:
            blockers.append("Set CODEX_CLI_COMMAND=codex (or another executable command) in the environment.")
        elif not command_available:
            blockers.append(f"Configured Codex command is not available: {command}")
        if not workspace_root:
            blockers.append("Set CODEX_CLI_WORKSPACE_ROOT to an existing allowed workspace directory.")
        elif not workspace_exists:
            blockers.append(f"Configured CODEX_CLI_WORKSPACE_ROOT does not exist: {workspace_root}")
        ready = enabled_flag and command_available and workspace_exists
        details = [
            f"Enabled flag: {enabled_flag}",
            f"Configured command: {command or '(empty)'}",
            f"Resolved command executable: {command_executable or '(unresolved)'}",
            f"Configured workspace root: {workspace_root or '(empty)'}",
            f"Workspace exists: {workspace_exists}",
            f"Auto mode enabled: {self.runtime_settings.codex_cli_auto_mode}",
            f"Timeout seconds: {self.runtime_settings.codex_cli_timeout_seconds}",
        ]
        return {
            "ready": ready,
            "enabled_flag": enabled_flag,
            "command": command,
            "command_executable": command_executable,
            "command_available": command_available,
            "workspace_root": workspace_root,
            "workspace_exists": workspace_exists,
            "auto_mode": bool(self.runtime_settings.codex_cli_auto_mode),
            "timeout_seconds": int(self.runtime_settings.codex_cli_timeout_seconds),
            "blockers": blockers,
            "details": details,
        }

    def _resolve_command_executable(self, command: str) -> str | None:
        if not command:
            return None
        first_part = self._command_parts(command)[0]
        if Path(first_part).exists():
            return str(Path(first_part))
        return self._which(first_part)

    def _command_parts(self, command: str) -> list[str]:
        return shlex.split(command, posix=True)

    def _build_bounded_prompt(self, task: Task, subtask: SubTask, workspace_root: Path) -> str:
        tier, tier_reason = self.model_router.codex_tier_guidance(
            " ".join([task.goal, subtask.objective, subtask.description]).strip(),
            context=ModelRequestContext(
                intent_label="coding",
                request_mode=task.request_mode.value,
                selected_lane="execution_flow",
                selected_agent=self.agent_id,
                task_complexity=(
                    "high"
                    if task.escalation_level == ExecutionEscalation.OBJECTIVE_COMPLETION
                    else "medium"
                ),
                risk_level="high" if "auth" in task.goal.lower() or "regression" in task.goal.lower() else "medium",
                requires_tool_use=False,
                requires_review=True,
                reviewer_rejected=any(
                    result.agent == "reviewer_agent" and result.status == AgentExecutionStatus.BLOCKED
                    for result in task.results
                ),
                replan_count=0,
                evidence_quality="unknown",
                user_visible_latency_sensitivity="medium",
                cost_sensitivity="medium",
            ),
        )
        agents_context = self._load_agents_context(workspace_root)
        return (
            "You are Codex working as a bounded managed coding agent for Project Sovereign.\n"
            "Sovereign remains the CEO/supervisor: it frames the goal, owns safety, reviews evidence, and writes the final user response.\n"
            "The Coding Agent coordinates coding tasks; you are the external Codex worker/backend for advanced implementation, build, debug, and test work.\n"
            "File and runtime tools are lightweight fallbacks for simple operations, not replacements for this managed coding lane.\n"
            f"Allowed workspace root: {workspace_root}\n"
            "Workspace boundary: read and write only within that exact directory tree. Do not create, modify, delete, or move files outside it.\n"
            f"Reasoning tier guidance: {tier}\n"
            f"Tier rationale: {tier_reason}\n"
            "AGENTS.md context for this workspace:\n"
            f"{agents_context}\n"
            "Non-negotiable rules:\n"
            "- Work only inside the allowed workspace root.\n"
            "- Do not deploy, publish, push, force-push, or modify infrastructure outside the workspace.\n"
            "- Do not run destructive commands such as rm -rf, git reset --hard, git checkout --, format disk, or bulk deletion.\n"
            "- Do not print secrets, tokens, API keys, credentials, cookies, or private environment values.\n"
            "- Do not treat raw shell commands from the user as direct instructions; translate the request into bounded coding work.\n"
            "- Prefer small, reviewable changes and clearly state if you are blocked.\n"
            "- If tests are relevant, run the smallest validating test command you can justify.\n"
            "- If tests fail, report OUTCOME: incomplete or blocked. Do not report completed with failing tests.\n"
            "- If you cannot complete the task safely, stop and explain exactly why.\n"
            "- Final success requires concrete evidence: changed files or a meaningful diff, plus verification/test commands when applicable.\n"
            f"Approval mode requested by Sovereign: {'auto-edit' if self.runtime_settings.codex_cli_auto_mode else 'suggest'}.\n"
            "Task context:\n"
            f"- Goal: {task.goal}\n"
            f"- Subtask title: {subtask.title}\n"
            f"- Subtask objective: {subtask.objective}\n"
            f"- Subtask description: {subtask.description}\n"
            "At the end, print these exact report lines so the supervisor can verify your work:\n"
            "OUTCOME: completed|blocked|incomplete|needs_user_review\n"
            "SUMMARY: one sentence\n"
            "CHANGED_FILES: comma-separated paths or none\n"
            "TESTS_RUN: comma-separated commands or none\n"
            "BLOCKERS: comma-separated blockers or none\n"
            "NEXT_ACTIONS: comma-separated next actions or none\n"
        )

    def _load_agents_context(self, workspace_root: Path) -> str:
        candidates = [
            workspace_root / "AGENTS.md",
            Path.cwd() / "AGENTS.md",
        ]
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                if not resolved.exists() or not resolved.is_file():
                    continue
                text = resolved.read_text(encoding="utf-8", errors="replace")
                normalized = "\n".join(line.rstrip() for line in text.splitlines())
                return self._preview(normalized, limit=2400) or "AGENTS.md was present but empty."
            except OSError:
                continue
        return "No AGENTS.md file was found in the allowed workspace root."

    def _is_dangerous_request(self, text: str) -> bool:
        lowered = text.lower()
        dangerous_patterns = (
            r"\brm\s+-rf\b",
            r"\bgit\s+reset\s+--hard\b",
            r"\bgit\s+checkout\s+--\b",
            r"\bforce[- ]?push\b",
            r"\bdrop\s+database\b",
            r"\bdelete\s+(the\s+)?(repo|repository|workspace|database)\b",
            r"\bwipe\b",
            r"\bformat\s+disk\b",
            r"\bshutdown\b",
            r"\bself[- ]?destruct\b",
            r"\bdeploy\b",
        )
        return any(re.search(pattern, lowered) for pattern in dangerous_patterns)

    def _is_explicitly_authorized(self, text: str) -> bool:
        lowered = text.lower()
        return any(
            phrase in lowered
            for phrase in (
                "explicitly authorized",
                "i authorize",
                "you are authorized",
                "approved to delete",
                "approved to deploy",
            )
        )

    def _build_payload(
        self,
        *,
        env_check: dict[str, Any],
        workspace_root: Path,
        command_invoked: list[str],
        bounded_prompt: str,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        timed_out: bool,
        duration_ms: int,
        git_before: dict[str, Any],
        git_after: dict[str, Any],
        parsed_report: dict[str, Any],
    ) -> dict[str, Any]:
        changed_files = parsed_report.get("changed_files") or git_after.get("changed_files", [])
        tests_run = parsed_report.get("tests_run") or self._infer_tests_run(stdout, stderr)
        completion_blockers = self._raw_completion_evidence_blockers(
            exit_code=exit_code,
            timed_out=timed_out,
            parsed_report=parsed_report,
            changed_files=changed_files,
            diff_summary=git_after.get("diff_summary"),
            tests_run=tests_run,
            stdout=stdout,
            stderr=stderr,
        )
        return {
            "workspace_root": str(workspace_root),
            "command": env_check["command"],
            "command_invoked": command_invoked,
            "command_invoked_display": " ".join(command_invoked),
            "command_executable": env_check["command_executable"],
            "auto_mode": env_check["auto_mode"],
            "timeout_seconds": env_check["timeout_seconds"],
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_ms": duration_ms,
            "stdout_preview": self._preview(stdout),
            "stderr_preview": self._preview(stderr),
            "stdout_length": len(stdout),
            "stderr_length": len(stderr),
            "prompt_preview": self._preview(bounded_prompt, limit=1200),
            "git_before": git_before,
            "git_after": git_after,
            "changed_files": changed_files,
            "diff_summary": git_after.get("diff_summary"),
            "tests_run": tests_run,
            "codex_report": parsed_report,
            "final_report": parsed_report,
            "completion_evidence_state": "blocked" if completion_blockers else "reviewable",
            "completion_evidence_blockers": completion_blockers,
            "env_check": env_check,
        }

    def _completion_evidence_blockers(self, payload: dict[str, Any]) -> list[str]:
        return [str(item) for item in payload.get("completion_evidence_blockers", []) if str(item).strip()]

    def _raw_completion_evidence_blockers(
        self,
        *,
        exit_code: int | None,
        timed_out: bool,
        parsed_report: dict[str, Any],
        changed_files: list[str],
        diff_summary: str | None,
        tests_run: list[str],
        stdout: str,
        stderr: str,
    ) -> list[str]:
        blockers: list[str] = []
        report_outcome = str(parsed_report.get("outcome", "")).strip().lower()
        if report_outcome != "completed":
            return blockers
        if timed_out or exit_code not in (0, None):
            return blockers
        has_change_evidence = bool(changed_files or diff_summary)
        has_output = bool(self._preview(stdout) or self._preview(stderr))
        if not has_change_evidence:
            blockers.append("Codex reported completion without changed files or git diff evidence.")
        if not has_output:
            blockers.append("Codex reported completion without stdout/stderr evidence.")
        if tests_run and self._test_output_indicates_failure(stdout, stderr):
            blockers.append("Codex reported completion even though captured test output indicates failures.")
        return blockers

    def _test_output_indicates_failure(self, stdout: str, stderr: str) -> bool:
        combined = "\n".join([stdout, stderr]).lower()
        if re.search(r"\b0\s+failed\b", combined):
            return False
        return any(re.search(pattern, combined) for pattern in self.failed_test_patterns)

    def _parse_codex_report(self, stdout: str, stderr: str) -> dict[str, Any]:
        combined = "\n".join(part for part in [stdout, stderr] if part).splitlines()
        parsed: dict[str, Any] = {}
        for line in combined:
            if ":" not in line:
                continue
            key, raw_value = line.split(":", 1)
            normalized_key = key.strip().upper()
            value = self._redact_sensitive(raw_value.strip())
            if normalized_key == "OUTCOME":
                lowered = value.lower()
                if lowered in self.report_outcomes:
                    parsed["outcome"] = lowered
            elif normalized_key == "SUMMARY":
                parsed["summary"] = value
            elif normalized_key == "CHANGED_FILES":
                parsed["changed_files"] = self._parse_csv_field(value)
            elif normalized_key == "TESTS_RUN":
                parsed["tests_run"] = self._parse_csv_field(value)
            elif normalized_key == "BLOCKERS":
                parsed["blockers"] = self._parse_csv_field(value)
            elif normalized_key == "NEXT_ACTIONS":
                parsed["next_actions"] = self._parse_csv_field(value)
        return parsed

    def _parse_csv_field(self, value: str) -> list[str]:
        if not value or value.lower() == "none":
            return []
        return [item.strip() for item in value.split(",") if item.strip()]

    def _infer_tests_run(self, stdout: str, stderr: str) -> list[str]:
        combined = "\n".join([stdout, stderr])
        patterns = (
            r"(pytest[^\n\r]*)",
            r"(python\s+-m\s+pytest[^\n\r]*)",
            r"(npm\s+test[^\n\r]*)",
            r"(pnpm\s+test[^\n\r]*)",
            r"(yarn\s+test[^\n\r]*)",
            r"(go\s+test[^\n\r]*)",
            r"(cargo\s+test[^\n\r]*)",
        )
        tests_run: list[str] = []
        for pattern in patterns:
            for match in re.findall(pattern, combined, flags=re.IGNORECASE):
                cleaned = " ".join(str(match).split())
                if cleaned and cleaned not in tests_run:
                    tests_run.append(cleaned)
        return tests_run

    def _capture_git_snapshot(self, workspace_root: Path) -> dict[str, Any]:
        git_executable = self._which("git")
        if not git_executable:
            return {
                "git_available": False,
                "repo_detected": False,
                "status_lines": [],
                "changed_files": [],
                "diff_summary": None,
            }

        repo_probe = self._subprocess_run(
            [git_executable, "rev-parse", "--is-inside-work-tree"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if repo_probe.returncode != 0 or "true" not in (repo_probe.stdout or "").lower():
            return {
                "git_available": True,
                "repo_detected": False,
                "status_lines": [],
                "changed_files": [],
                "diff_summary": None,
            }

        status_run = self._subprocess_run(
            [git_executable, "status", "--short"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        diff_run = self._subprocess_run(
            [git_executable, "diff", "--stat", "--no-ext-diff"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        status_lines = [line.rstrip() for line in (status_run.stdout or "").splitlines() if line.strip()]
        changed_files = [line[3:].strip() for line in status_lines if len(line) >= 4]
        return {
            "git_available": True,
            "repo_detected": True,
            "status_lines": status_lines,
            "changed_files": changed_files,
            "diff_summary": self._preview(diff_run.stdout, limit=400),
        }

    def _preview(self, text: str | None, *, limit: int = 240) -> str | None:
        if text is None:
            return None
        normalized = " ".join(self._redact_sensitive(text).split())
        if not normalized:
            return None
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[: limit - 3]}..."

    def _redact_sensitive(self, text: str) -> str:
        patterns = (
            r"(?i)(api[_-]?key|token|secret|password|credential|cookie)(\s*[:=]\s*)([^\s,'\"]+)",
            r"(?i)(bearer\s+)([A-Za-z0-9._\-]+)",
            r"(?i)(sk-[A-Za-z0-9_\-]{8,})",
        )
        redacted = text
        for pattern in patterns:
            if "sk-" in pattern:
                redacted = re.sub(pattern, "[REDACTED_SECRET]", redacted)
            else:
                redacted = re.sub(pattern, r"\1\2[REDACTED]", redacted)
        return redacted

    def _coerce_process_output(self, value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    def _build_summary(
        self,
        *,
        exit_code: int | None,
        timed_out: bool,
        report_outcome: str | None,
        parsed_report: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        if timed_out:
            return "Codex CLI timed out before it could finish the delegated coding task."
        if exit_code not in (0, None):
            return f"Codex CLI failed with exit code {exit_code}."
        if parsed_report.get("summary"):
            return str(parsed_report["summary"])
        if report_outcome == "needs_user_review":
            return "Codex completed a bounded run and requested user review before the task is marked done."
        if report_outcome == "incomplete":
            return "Codex completed a bounded run but reported the task as incomplete."
        if report_outcome == "blocked":
            return "Codex completed a bounded run but reported a blocker."
        changed_files = payload.get("changed_files", [])
        if changed_files:
            return f"Codex completed a bounded coding run and reported changes in {len(changed_files)} file(s)."
        return "Codex completed a bounded coding run but did not report file changes."

    def _setup_next_actions(self, env_check: dict[str, Any]) -> list[str]:
        actions = []
        if not env_check["enabled_flag"]:
            actions.append("Set CODEX_CLI_ENABLED=true.")
        if not env_check["command"]:
            actions.append("Set CODEX_CLI_COMMAND=codex.")
        elif not env_check["command_available"]:
            actions.append(f"Install or expose the Codex CLI command on PATH: {env_check['command']}.")
        if not env_check["workspace_root"]:
            actions.append("Set CODEX_CLI_WORKSPACE_ROOT to the repo path you want Codex to edit.")
        elif not env_check["workspace_exists"]:
            actions.append(f"Create or correct the workspace root path: {env_check['workspace_root']}.")
        actions.append("Retry the bounded coding request after the environment check passes.")
        return actions

    def _blocked_result(
        self,
        task: Task,
        subtask: SubTask,
        *,
        summary: str,
        blockers: list[str],
        details: list[str],
        next_actions: list[str],
        payload: dict[str, Any],
    ) -> AgentResult:
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.agent_id,
            status=AgentExecutionStatus.BLOCKED,
            summary=summary,
            tool_name="codex_cli",
            details=details,
            artifacts=[f"codex_cli:{subtask.id}"],
            evidence=[
                ToolEvidence(
                    tool_name="codex_cli",
                    summary=summary,
                    payload=payload,
                    verification_notes=[],
                )
            ],
            blockers=blockers,
            next_actions=next_actions,
        )
