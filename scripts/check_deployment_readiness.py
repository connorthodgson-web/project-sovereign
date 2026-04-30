"""Lightweight deployment-readiness checks for Project Sovereign.

The script reports presence/absence and paths only. It never prints secret
values from environment files, token files, or credential files.
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
REQUIREMENTS = ROOT / "requirements.txt"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    critical: bool = False


def _read_env_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    if not path.exists():
        return keys
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            keys.add(key.upper())
    return keys


def _env_present(name: str, dotenv_keys: set[str]) -> bool:
    return bool(os.getenv(name)) or name.upper() in dotenv_keys


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None


def _git_ignores(path: str) -> bool:
    result = _run_git(["check-ignore", "--no-index", "-q", path])
    return bool(result and result.returncode == 0)


def _tracked(paths: list[str]) -> list[str]:
    result = _run_git(["ls-files", "--", *paths])
    if result is None or result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _requirement_names() -> list[str]:
    if not REQUIREMENTS.exists():
        return []
    names: list[str] = []
    pattern = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
    for line in REQUIREMENTS.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        match = pattern.match(stripped.split(";", 1)[0])
        if match:
            names.append(match.group(1))
    return names


def _package_checks() -> list[Check]:
    checks: list[Check] = []
    missing: list[str] = []
    for name in _requirement_names():
        try:
            importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
    checks.append(
        Check(
            "required Python packages",
            not missing,
            "all requirements distributions are installed in this interpreter"
            if not missing
            else "missing in this interpreter: " + ", ".join(missing),
        )
    )
    return checks


def _env_checks(dotenv_keys: set[str]) -> list[Check]:
    groups = {
        "OpenRouter reasoning": ["OPENROUTER_API_KEY"],
        "Slack Socket Mode": ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"],
        "Slack request signing": ["SLACK_SIGNING_SECRET"],
        "Browser Use": ["BROWSER_USE_API_KEY"],
        "Gmail OAuth files": ["GMAIL_CREDENTIALS_PATH", "GMAIL_TOKEN_PATH"],
        "Google Calendar OAuth files": [
            "GOOGLE_CALENDAR_CREDENTIALS_PATH",
            "GOOGLE_CALENDAR_TOKEN_PATH",
        ],
        "Google Tasks OAuth files": [
            "GOOGLE_TASKS_CREDENTIALS_PATH",
            "GOOGLE_TASKS_TOKEN_PATH",
        ],
        "Reminder scheduler": ["SCHEDULER_BACKEND", "SCHEDULER_TIMEZONE"],
        "Workspace root": ["WORKSPACE_ROOT"],
    }
    checks: list[Check] = []
    for label, fields in groups.items():
        missing = [field for field in fields if not _env_present(field, dotenv_keys)]
        checks.append(
            Check(
                f"env: {label}",
                not missing,
                "present" if not missing else "missing: " + ", ".join(missing),
            )
        )
    return checks


def _workspace_checks(dotenv_keys: set[str]) -> list[Check]:
    workspace_root = os.getenv("WORKSPACE_ROOT")
    if not workspace_root and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().upper().startswith("WORKSPACE_ROOT="):
                workspace_root = line.split("=", 1)[1].strip().strip("'\"")
                break
    workspace = Path(workspace_root) if workspace_root else ROOT / "workspace"
    if not workspace.is_absolute():
        workspace = ROOT / workspace

    paths = {
        "workspace root": workspace,
        "workspace memory dir": workspace / ".sovereign",
        "secrets dir": ROOT / "secrets",
    }
    return [
        Check(name, path.exists(), f"{path} exists" if path.exists() else f"{path} missing")
        for name, path in paths.items()
    ]


def _secret_ignore_checks() -> list[Check]:
    tracked_sensitive = _tracked([".env", "secrets", ".secrets"])
    tracked_env_like = _tracked([".envz"])
    checks = [
        Check(".env ignored", _git_ignores(".env"), ".env is ignored", critical=True),
        Check(
            ".envz ignored",
            _git_ignores(".envz"),
            ".envz is ignored" if _git_ignores(".envz") else ".envz is not ignored",
        ),
        Check(
            "secrets/*.json ignored",
            _git_ignores("secrets/token.json") and _git_ignores("secrets/gmail_token.json"),
            "token and credential JSON paths are ignored",
            critical=True,
        ),
        Check(
            ".env.example tracked",
            ENV_EXAMPLE.exists() and not _git_ignores(".env.example"),
            ".env.example exists and is not ignored",
            critical=True,
        ),
        Check(
            "no tracked local secrets",
            not tracked_sensitive,
            "no tracked .env/secrets paths"
            if not tracked_sensitive
            else "tracked sensitive paths: " + ", ".join(tracked_sensitive),
            critical=True,
        ),
        Check(
            "tracked env-like files",
            not tracked_env_like,
            "none"
            if not tracked_env_like
            else "review tracked env-like paths: " + ", ".join(tracked_env_like),
        ),
    ]
    return checks


def build_checks() -> list[Check]:
    dotenv_keys = _read_env_keys(ENV_FILE)
    checks = [
        Check(
            "Python version",
            sys.version_info >= (3, 11),
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            critical=True,
        ),
        Check(
            ".env file",
            ENV_FILE.exists(),
            ".env present" if ENV_FILE.exists() else ".env missing",
        ),
        Check(
            ".env.example file",
            ENV_EXAMPLE.exists(),
            ".env.example present" if ENV_EXAMPLE.exists() else ".env.example missing",
            critical=True,
        ),
    ]
    checks.extend(_secret_ignore_checks())
    checks.extend(_workspace_checks(dotenv_keys))
    checks.extend(_package_checks())
    checks.extend(_env_checks(dotenv_keys))
    return checks


def main() -> int:
    checks = build_checks()
    print("Project Sovereign deployment-readiness check")
    print(f"Repository: {ROOT}")
    print()
    for check in checks:
        status = "OK" if check.ok else ("BLOCKER" if check.critical else "WARN")
        print(f"[{status}] {check.name}: {check.detail}")
    print()
    blockers = [check for check in checks if check.critical and not check.ok]
    if blockers:
        print(f"Readiness result: blocked ({len(blockers)} critical issue(s)).")
        return 1
    print("Readiness result: no critical blockers found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
