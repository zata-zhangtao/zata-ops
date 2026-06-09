"""Pytest configuration for zata-ops.

Adds the ``src/`` directory to ``sys.path`` so ``import zata_ops`` resolves
without an editable install when running from a fresh worktree.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_src_on_path() -> None:
    """Insert ``src/`` into ``sys.path`` for local imports."""
    project_root_path = Path(__file__).resolve().parents[1]
    src_path = project_root_path / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))


_ensure_src_on_path()
