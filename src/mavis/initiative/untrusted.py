"""Marking third-party content as data before it reaches a prompt (spec §8.3).

Single implementation lives in the memory extractor; re-exported here as the Phase 3 contract.
"""

from __future__ import annotations

from mavis.memory.extractor import wrap_untrusted

__all__ = ["wrap_untrusted"]
