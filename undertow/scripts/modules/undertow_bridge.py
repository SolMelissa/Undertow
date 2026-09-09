"""
Bridges the standalone scripts in this folder to the main `undertow` package.

Scripts run as `python -u undertow/scripts/<name>.py` (see scripts_runner.start), which puts
`undertow/scripts/` at sys.path[0] - not the repo root - so `from undertow import ...` fails
unless the repo root is added explicitly first. Importing this module does that, then
re-exports the undertow submodules scripts actually use so they don't each repeat the
sys.path fix.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from undertow import api_client, config, hydrus_client  # noqa: E402

__all__ = ["api_client", "config", "hydrus_client"]
