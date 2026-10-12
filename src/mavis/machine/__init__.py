# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""The per-user machine: sandbox, browser and workspace ports and the runtime (Phase 12)."""

from __future__ import annotations

_runtime = None


def get_runtime():
    return _runtime


def set_runtime(rt) -> None:
    global _runtime
    _runtime = rt
