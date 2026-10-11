import structlog

from mavis.logging import REDACTED, configure_logging, redact_secrets


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
    try:
        structlog.get_logger().info("hello", telegram_token="should-not-appear")
        err = capsys.readouterr().err
    finally:
        structlog.reset_defaults()  # don't leave a logger bound to a captured (later closed) stderr
    assert "hello" in err
    assert "should-not-appear" not in err


def test_access_log_lines_drop_the_query_string():
    """An OAuth callback's code and state, or a one-time link token, never reach the access log."""
    import logging

    from mavis.logging import QuerylessAccess

    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', (
        "10.0.0.1:5", "GET", "/oauth/slack/callback?code=abc.123&state=xyz", "1.1", 200), None)
    assert QuerylessAccess().filter(record)
    assert record.getMessage() == '10.0.0.1:5 - "GET /oauth/slack/callback HTTP/1.1" 200'
