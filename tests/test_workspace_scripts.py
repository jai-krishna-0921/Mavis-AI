"""Offline checks for the verify and smoke scripts and the production compose flag."""

from __future__ import annotations

from pathlib import Path

from scripts.smoke_workspace import READ_ONLY
from scripts.verify_composio import sample_args, slugs_to_check

from mavis.domain.policy import RiskClass
from mavis.tools.integrations.actions import ACTIONS
from mavis.tools.integrations.composio_map import COMPOSIO_ACTIONS

ROOT = Path(__file__).resolve().parents[1]


def test_every_mapped_action_has_a_valid_sample():
    for action, mapping in COMPOSIO_ACTIONS.items():
        assert isinstance(mapping.translate(sample_args(action)), dict), action


def test_gmail_and_calendar_are_checked_on_both_toolkits():
    assert slugs_to_check("mail.search") == ["GMAIL_FETCH_EMAILS", "GOOGLESUPER_FETCH_EMAILS"]
    assert slugs_to_check("calendar.update_event") == [
        "GOOGLECALENDAR_PATCH_EVENT", "GOOGLESUPER_PATCH_EVENT",
    ]
    assert slugs_to_check("drive.search") == ["GOOGLESUPER_FIND_FILE"]
    assert slugs_to_check("slack.send") == ["SLACK_SEND_MESSAGE"]


def test_smoke_script_only_reads():
    assert READ_ONLY and all(ACTIONS[a].risk is RiskClass.READ for a in READ_ONLY)
    assert all(ACTIONS[a].risk_fn is None for a in READ_ONLY)


def test_workspace_flag_and_poll_interval_are_in_prod_compose():
    compose = (ROOT / "docker-compose.prod.yml").read_text()
    assert "GOOGLE_WORKSPACE_ENABLED: ${GOOGLE_WORKSPACE_ENABLED:-true}" in compose
    assert "WORKSPACE_POLL_MINUTES: ${WORKSPACE_POLL_MINUTES:-30}" in compose
