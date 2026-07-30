#!/usr/bin/env python3
"""Read-only exact-SHA perceptual-reuse opportunity analysis."""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comic_automation.archive.perceptual_reuse_cli import main


if __name__ == "__main__":
    raise SystemExit(main())

