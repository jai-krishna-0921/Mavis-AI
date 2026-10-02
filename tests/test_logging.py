import structlog

from zento.logging import REDACTED, configure_logging, redact_secrets


def test_redacts_secret_like_keys() -> None:
    out = redact_secrets(
        None, "info",
        {
            "event": "x", "api_key": "abc", "Token": "t", "user_id": 1,
            "db_password": "p", "client_secret": "s",
        },
    )
    assert out == {
        "event": "x", "api_key": REDACTED, "Token": REDACTED, "user_id": 1,
        "db_password": REDACTED, "client_secret": REDACTED,
    }


def test_configure_logging_runs(settings, capsys) -> None:
    configure_logging()
    structlog.get_logger().info("hello", telegram_token="should-not-appear")
    err = capsys.readouterr().err
    assert "hello" in err
    assert "should-not-appear" not in err
