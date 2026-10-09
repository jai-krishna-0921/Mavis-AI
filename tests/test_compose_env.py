"""Shared contract C: every env var prod needs must be whitelisted in docker-compose.prod.yml x-app-env.

Plans add their keys to PASSTHROUGH. The compose file whitelists variables, so a key missing here never
reaches the containers (339e301)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PASSTHROUGH: set[str] = {
    # Phase 12 slice A
    "PROGRESS_CARD_ENABLED", "PROGRESS_CARD_AFTER_S", "PROGRESS_EDIT_MIN_INTERVAL_S",
    "PROGRESS_MAX_SCREENSHOTS", "TELEGRAM_GLOBAL_SEND_RATE", "TEST_MIRROR_CHAT_ID",
    "TASK_TIMEOUT_MAX_S", "MACHINE_TASK_TIMEOUT_S", "MACHINE_FILE_MAX_MB",
    # Phase 12 slice B (machine)
    "MACHINE_ENABLED", "MACHINE_BROWSER_ENABLED", "MACHINE_USERS", "MACHINE_ALLOW_SPEND",
    "MACHINE_LIVE_TOKEN_SECRET", "E2B_API_KEY", "SANDBOX_BACKEND", "BROWSER_BACKEND", "AGENTCORE_REGION",
    "AGENTCORE_CODE_INTERPRETER_ID", "AGENTCORE_BROWSER_ID", "WORKSPACE_BACKEND", "WORKSPACE_BUCKET",
    "WORKSPACE_PREFIX", "MACHINE_MAX_CONCURRENT", "MACHINE_USER_DAILY_MINUTES",
    "MACHINE_USER_MONTHLY_USD", "WORKSPACE_QUOTA_MB", "OPERATOR_MAX_STEPS", "ANALYST_MAX_STEPS",
    "MACHINE_ALLOW_EGRESS", "MACHINE_PLATFORM_TAGS", "MACHINE_PYTHON_VERSION", "BROWSER_DOMAIN_DENY",
    "MACHINE_PACKAGE_ALLOW",
    "PIP_INDEX_URL", "MAX_UPLOAD_MB",
    # Native Google and Slack connectors (spec 2026-10-09)
    "INTEGRATION_PROVIDER", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "SLACK_CLIENT_ID",
    "SLACK_CLIENT_SECRET", "SLACK_SIGNING_SECRET", "NATIVE_TOKEN_KEK", "NATIVE_TOKEN_KEK_PREVIOUS",
    "SYNC_GMAIL_DAYS", "SYNC_SLACK_DAYS", "SYNC_CALENDAR_BACK_DAYS", "SYNC_CALENDAR_AHEAD_DAYS",
    # Web dashboard (spec 2026-10-09)
    "DASHBOARD_ENABLED", "GOOGLE_SIGNIN_ENABLED", "DASHBOARD_SESSION_DAYS", "TELEGRAM_BOT_USERNAME",
    "INVITE_CAP_STANDARD", "INVITE_CAP_TRUSTED", "INVITE_WEB_USES",
}


def app_env_keys() -> set[str]:
    text = (ROOT / "docker-compose.prod.yml").read_text()
    block = text.split("x-app-env: &app-env", 1)[1].split("\n\n", 1)[0]
    return set(re.findall(r"^\s{2}([A-Z][A-Z0-9_]+):", block, flags=re.M))


def test_every_passthrough_key_is_in_app_env():
    missing = sorted(PASSTHROUGH - app_env_keys())
    assert missing == [], f"add to x-app-env in docker-compose.prod.yml: {missing}"


def test_passthrough_keys_are_settings_fields():
    from mavis.config import Settings

    fields = {name.upper() for name in Settings.model_fields}
    assert sorted(PASSTHROUGH - fields) == []


def _ignored_paths() -> list[str]:
    return [ln.strip() for ln in (ROOT / ".dockerignore").read_text().splitlines()
            if ln.strip() and not ln.startswith("#")]


def test_every_dir_the_dockerfile_copies_is_not_dockerignored():
    copied = set()
    for ln in (ROOT / "Dockerfile").read_text().splitlines():
        if ln.startswith("COPY ") and "--from" not in ln:
            args = [p for p in ln.split()[1:-1] if not p.startswith("--")]
            copied.update(p.rstrip("/").removeprefix("./") for p in args)
    ignored = set(_ignored_paths())
    assert sorted(copied & ignored) == [], "the build would fail: COPY of an ignored path"


def test_sink_and_reports_volume_is_shared_by_api_and_worker():
    import yaml

    doc = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())
    app_volumes = doc["x-app"]["volumes"]
    assert any(v.endswith(":/app/data/e2e") for v in app_volumes)
    for svc in ("api", "worker"):
        assert doc["services"][svc]["volumes"] == app_volumes or "volumes" not in doc["services"][svc]
    assert "e2edata" in doc["volumes"]


def test_sink_file_lives_in_the_shared_volume():
    from pathlib import Path as P

    from mavis.channels.test_sink import sink_path

    assert sink_path(P("/app/data")).parent == P("/app/data/e2e")


def test_verify_machine_runs_in_the_worker_that_writes_the_sink():
    text = (ROOT / "deploy/aws/deploy.sh").read_text()
    assert "exec -T worker python -m scripts.machine_demo" in text


def test_the_reverse_proxy_serves_the_oauth_callback_and_the_slack_webhook():
    caddy = (ROOT / "Caddyfile").read_text()
    public = next(ln for ln in caddy.splitlines() if ln.strip().startswith("@api path")).split()
    assert "/api/*" in public and "/oauth/*" in public and "/webhooks/*" in public
    assert "/telegram/webhook" in public and "/connect/callback" in public


def test_deploy_forces_the_native_provider_after_copying_local_values():
    text = (ROOT / "deploy" / "aws" / "deploy.sh").read_text()
    assert "set_key INTEGRATION_PROVIDER native force" in text
    forced = text.index("set_key INTEGRATION_PROVIDER native force")
    assert forced > text.index("SLACK_SIGNING_SECRET INTEGRATION_PROVIDER")
