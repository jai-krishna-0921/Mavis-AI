"""Cheap token estimate (≈4 chars/token). Good enough for prompt budgeting."""


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4
