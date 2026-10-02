"""Filesystem helpers for branded assets."""

from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = PACKAGE_ROOT / "assets"
LOGO_PATH = ASSETS_DIR / "logo.jpg"
