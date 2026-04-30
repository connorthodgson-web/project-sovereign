"""Repo-local shim for `python -m pytest`.

The system Python on this machine sees user-site pytest plugins and packages
before the project's virtualenv. This shim sets the same deterministic test
environment as sitecustomize, then delegates to the real pytest package.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV_SITE = ROOT / "venv" / "Lib" / "site-packages"
IS_MAIN = __name__ == "__main__"

os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

removed_entries: list[str] = []
for entry in ("", str(ROOT)):
    while entry in sys.path:
        sys.path.remove(entry)
        removed_entries.append(entry)

if VENV_SITE.exists():
    venv_site = str(VENV_SITE)
    if venv_site not in sys.path:
        sys.path.insert(0, venv_site)

sys.modules.pop("pytest", None)
_real_pytest = importlib.import_module("pytest")

for entry in reversed(removed_entries):
    if entry not in sys.path:
        sys.path.insert(0, entry)

globals().update(_real_pytest.__dict__)

if IS_MAIN:
    raise SystemExit(_real_pytest.console_main())
