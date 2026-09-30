#!/usr/bin/env python3
"""Duration formatting shared by the dashboard.

This module is not a pipeline step: ``cube_dashboard.py`` loads it by filename
for ``format_duration()``. Timings come from ``ex:durationSeconds`` in the
GraphDB validation-history graph (``gdb_source.py``), so nothing here reads a
``timing.txt``.
"""
from __future__ import annotations


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds >= 3600:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours}h {minutes}m {secs:.1f}s"
    if seconds >= 60:
        minutes = int(seconds // 60)
        secs = seconds % 60
        return f"{minutes}m {secs:.1f}s"
    return f"{seconds:.3f}s"
