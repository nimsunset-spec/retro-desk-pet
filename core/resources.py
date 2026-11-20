"""Resolve bundled read-only resources in source and frozen app builds."""

from __future__ import annotations

import sys
from pathlib import Path


def resource_path(*parts: str) -> Path:
    """Return a path relative to the source root or PyInstaller bundle root."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    root = Path(bundle_root) if bundle_root else Path(__file__).resolve().parent.parent
    return root.joinpath(*parts)
