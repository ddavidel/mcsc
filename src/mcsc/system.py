"""Host system helpers."""

from __future__ import annotations

import subprocess
import sys

import psutil

NO_WINDOW_FLAGS: int = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
"""``creationflags`` for java processes: on Windows they never open a console
window of their own (e.g. when the host app runs under ``pythonw``)."""


def total_ram_gb() -> int:
    """Total physical memory, rounded to whole GB."""
    return round(psutil.virtual_memory().total / 1024**3)


def suggested_ram_gb(max_gb: int = 6) -> int:
    """RAM to dedicate to a new server: a quarter of the system's, capped at ``max_gb``.

    4-6 GB fits most servers; heavily modded ones may need more.
    """
    return max(1, min(round(total_ram_gb() / 4), max_gb))
