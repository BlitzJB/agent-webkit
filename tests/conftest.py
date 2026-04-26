"""Test config — wires the server-reference into sys.path and provides a fixture-based factory."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server-reference"))
sys.path.insert(0, str(ROOT))
