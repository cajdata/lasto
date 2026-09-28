"""Where things live."""

from __future__ import annotations

from pathlib import Path

SITE = Path(__file__).resolve().parents[1]
ROOT = SITE.parent
CONTENT = SITE / "content"
DATA = SITE / "data"
TEMPLATES = SITE / "templates"
STATIC = SITE / "static"
FONT_SRC = STATIC / "fonts" / "src"
CACHE = SITE / ".cache"
DIST = SITE / "dist"

# The app source the build reads (never imports).
APP = ROOT / "src" / "lasto"
SAFETY = APP / "safety"
CLI = APP / "cli.py"
