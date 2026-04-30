"""Focused Playwright runtime diagnostic for Project Sovereign."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from integrations.browser.runtime import collect_browser_runtime_diagnostic


def main() -> None:
    diagnostic = collect_browser_runtime_diagnostic()
    print(json.dumps(
        {
            "python_executable": diagnostic.python_executable,
            "playwright_import_ok": diagnostic.playwright_import_ok,
            "playwright_version": diagnostic.playwright_version,
            "chromium_executable_path": diagnostic.chromium_executable_path,
            "chromium_binary_exists": diagnostic.chromium_binary_exists,
            "chromium_launch_ok": diagnostic.chromium_launch_ok,
            "example_navigation_ok": diagnostic.example_navigation_ok,
            "page_title": diagnostic.page_title,
            "body_snippet": diagnostic.body_snippet,
            "browser_closed_cleanly": diagnostic.browser_closed_cleanly,
            "likely_cause": diagnostic.likely_cause,
            "recommended_commands": list(diagnostic.recommended_commands),
            "raw_error": diagnostic.raw_error,
        },
        indent=2,
    ))


if __name__ == "__main__":
    main()
