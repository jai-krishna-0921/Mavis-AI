# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Cheap token estimate (≈4 chars/token). Good enough for prompt budgeting."""


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4
