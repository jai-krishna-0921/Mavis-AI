# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Teams side of the unanswered-DM nudge; the logic lives with Slack's in `slack_replies` (shared entries)."""

from __future__ import annotations

from mavis.attention.slack_replies import catch_up_teams as catch_up

__all__ = ["catch_up"]
