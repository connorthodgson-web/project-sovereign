"""Local runtime diagnostic for Project Sovereign development."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO_ROOT / "venv" / "Scripts" / "python.exe"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def status_line(name: str, ok: bool, detail: str) -> bool:
    marker = "OK" if ok else "WARN"
    print(f"[{marker}] {name}: {detail}")
    return ok


def has_env(name: str) -> bool:
    value = os.getenv(name)
    if value is None:
        try:
            from app.config import settings

            value = getattr(settings, name.lower(), None)
        except Exception:
            value = None
    return bool(value)


def check_import(module_name: str, package_name: str | None = None) -> bool:
    package_name = package_name or module_name
    spec = importlib.util.find_spec(module_name)
    return status_line(f"package:{package_name}", spec is not None, "importable" if spec else "not importable")


def run_probe(command: list[str], *, cwd: Path = REPO_ROOT, timeout: int = 15) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    detail = output[0] if output else f"exit code {completed.returncode}"
    return completed.returncode == 0, detail


def check_python() -> bool:
    ok = True
    ok &= status_line("python.executable", True, sys.executable)
    ok &= status_line("venv.exists", VENV_PYTHON.exists(), str(VENV_PYTHON))
    ok &= status_line(
        "using.venv",
        Path(sys.executable).resolve() == VENV_PYTHON.resolve() if VENV_PYTHON.exists() else False,
        "current process is venv Python" if Path(sys.executable).resolve() == VENV_PYTHON.resolve() else "run with .\\venv\\Scripts\\python.exe",
    )
    return ok


def check_packages() -> bool:
    ok = True
    packages = [
        ("fastapi", None),
        ("uvicorn", None),
        ("pydantic", None),
        ("pydantic_settings", "pydantic-settings"),
        ("httpx", None),
        ("playwright", None),
        ("browser_use_sdk", "browser-use-sdk"),
        ("langgraph", None),
        ("slack_bolt", "slack-bolt"),
        ("apscheduler", "APScheduler"),
        ("zep_cloud", "zep-cloud"),
        ("pytest", None),
    ]
    for module_name, package_name in packages:
        ok &= check_import(module_name, package_name)
    return ok


def check_pytest() -> bool:
    ok, detail = run_probe([str(VENV_PYTHON), "-m", "pytest", "--version"])
    return status_line("pytest.version", ok, detail)


def check_task_cli() -> bool:
    resolved = shutil.which("task")
    ok = bool(resolved)
    status_line("task.cli", ok, resolved or "not installed; install go-task to use Taskfile.yml")
    return ok


def check_playwright() -> bool:
    ok = check_import("playwright")
    if not ok:
        return False

    try:
        from integrations.browser.runtime import collect_browser_runtime_diagnostic

        diagnostic = collect_browser_runtime_diagnostic()
    except Exception as exc:
        return status_line("browser.runtime", False, f"{type(exc).__name__}: {exc}")

    browser_ok = bool(diagnostic.chromium_binary_exists and diagnostic.chromium_launch_ok)
    detail = (
        f"chromium_exists={diagnostic.chromium_binary_exists}, "
        f"launch_ok={diagnostic.chromium_launch_ok}, "
        f"navigation_ok={diagnostic.example_navigation_ok}"
    )
    status_line("browser.runtime", browser_ok, detail)
    if not browser_ok:
        if diagnostic.likely_cause:
            print(f"      likely_cause: {diagnostic.likely_cause}")
        for command in diagnostic.recommended_commands:
            print(f"      recommended: {command}")
    return browser_ok


def check_codex() -> bool:
    try:
        from app.config import settings

        configured_command = (settings.codex_cli_command or "").strip()
        workspace_root = settings.codex_cli_workspace_root or ""
        enabled = bool(settings.codex_cli_enabled)
    except Exception as exc:
        configured_command = os.getenv("CODEX_CLI_COMMAND", "codex")
        workspace_root = os.getenv("CODEX_CLI_WORKSPACE_ROOT", "")
        enabled = os.getenv("CODEX_CLI_ENABLED", "").lower() in {"1", "true", "yes"}
        print(f"[WARN] config.load: {type(exc).__name__}: {exc}")

    ok = True
    ok &= status_line("codex.enabled", enabled, f"CODEX_CLI_ENABLED={enabled}")
    ok &= status_line("codex.command", bool(configured_command), configured_command or "(empty)")
    ok &= status_line(
        "codex.workspace",
        bool(workspace_root) and Path(workspace_root).exists(),
        workspace_root or "(empty)",
    )

    command_name = configured_command.split()[0] if configured_command else "codex"
    resolved = shutil.which(command_name)
    ok &= status_line("codex.which.configured", bool(resolved), resolved or "(unresolved)")

    for candidate in ["codex", "codex.cmd", resolved or ""]:
        if not candidate:
            continue
        probe_ok, detail = run_probe([candidate, "--version"])
        status_line(f"codex.exec.{candidate}", probe_ok, detail)

    if configured_command == "codex":
        print("      diagnosis: On this Windows host, bare 'codex' may resolve to a shim that subprocess cannot execute directly.")
        print("      fix: set CODEX_CLI_COMMAND=codex.cmd, or set it to the full npm codex.cmd path.")

    return ok


def check_env() -> bool:
    ok = True
    env_names = [
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_SIGNING_SECRET",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
    ]
    for name in env_names:
        present = has_env(name)
        ok &= status_line(f"env.{name}", present, "present" if present else "missing")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Project Sovereign runtime doctor.")
    parser.add_argument("--codex-only", action="store_true", help="Only run Codex CLI diagnostics.")
    args = parser.parse_args()

    print(f"Project Sovereign doctor: {REPO_ROOT}")
    if args.codex_only:
        return 0 if check_codex() else 1

    ok = True
    ok &= check_python()
    ok &= check_packages()
    ok &= check_pytest()
    check_task_cli()
    ok &= check_playwright()
    ok &= check_codex()
    ok &= check_env()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
