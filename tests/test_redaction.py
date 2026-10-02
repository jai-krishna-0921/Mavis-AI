from mavis.logging import redact_secrets


def test_redacts_secret_like_keys_nested_and_bearer_values() -> None:
    out = redact_secrets(None, "info", {
        "event": "calling api",
        "api_key": "sk-123",
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "headers": {"Authorization": "Bearer abc.def", "x-api-key": "k", "accept": "json"},
        "note": "plain text stays",
        "detail": "sent Bearer abc.def to host",
    })
    assert out["api_key"] == "***"
    assert out["TELEGRAM_BOT_TOKEN"] == "***"
    assert out["headers"] == {"Authorization": "Bearer ***", "x-api-key": "***", "accept": "json"}
    assert out["note"] == "plain text stays"
    assert out["detail"] == "sent Bearer *** to host"
    assert out["event"] == "calling api"


def test_scrubs_bot_tokens_lists_and_exceptions() -> None:
    token = "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345"
    url = f"https://api.telegram.org/bot{token}/getMe"
    out = redact_secrets(None, "info", {
        "event": f"GET {url}",
        "urls": [url, {"u": url}],
        "err": RuntimeError(f"failed {url}"),
        "bare": f"token is {token}",
    })
    assert token not in str(out)
    assert out["urls"][0] == "https://api.telegram.org/bot***/getMe"
    assert out["err"].startswith("RuntimeError: failed")
