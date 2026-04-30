"""Local test/runtime hygiene for the Project Sovereign workspace.

This file is imported automatically by Python when commands are run from the
repo root. Keep it tiny: it only prevents unrelated global pytest plugins from
breaking collection and prefers the checked-in virtualenv packages when a
system Python command is used from this workspace.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

_ROOT = Path(__file__).resolve().parent
_VENV_SITE = _ROOT / "venv" / "Lib" / "site-packages"
if _VENV_SITE.exists():
    _venv_site = str(_VENV_SITE)
    if _venv_site not in sys.path:
        sys.path.insert(0, _venv_site)
