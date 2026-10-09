def test_machine_defaults_follow_owner_decisions(settings):
    assert settings.machine_enabled is False and settings.machine_browser_enabled is False
    assert settings.machine_user_monthly_usd == 5.0 and settings.machine_user_daily_minutes == 60
    assert settings.machine_max_concurrent == 3 and settings.machine_max_concurrent_per_user == 1
    assert settings.sandbox_backend == "auto" and "docker" not in _backend_choices()


def _backend_choices():
    from typing import get_args

    from mavis.config import Settings

    return get_args(Settings.model_fields["sandbox_backend"].annotation)
