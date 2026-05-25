"""Pytest configuration for tests/: adds the repo root to sys.path.

Lets tests under tests/ import modules placed at the repository root
(``mcp_server``, etc.) the same way ``test_backend.py`` already does
from the root itself.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
