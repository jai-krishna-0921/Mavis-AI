import pytest

from mavis.config import Settings, get_settings


def test_defaults(settings) -> None:
    assert settings.agent_name == "Mavis"
    assert settings.default_timezone == "Asia/Kolkata"
    assert settings.model_fast == "deepseek-v4.1-flash"
    assert settings.model_smart == "glm-5.3"
    assert settings.llm_max_concurrency == 3
    assert settings.ollama_base_url == "https://ollama.com/v1"
    assert settings.ping_daily_budget == 6
    assert (settings.quiet_start, settings.quiet_end) == (23, 7)
    assert settings.demo_time_scale == 1.0
    assert settings.is_sqlite
    assert settings.data_dir.exists()
    assert settings.artifacts_dir.exists()


def test_allowed_chat_ids_parse_json(settings, monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_TELEGRAM_CHAT_IDS", "[111, 222]")
    get_settings.cache_clear()
    assert get_settings().allowed_telegram_chat_ids == [111, 222]


def test_db_url_defaults_to_sqlite_in_data_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    s = Settings(data_dir=tmp_path / "d")
    assert s.db_url == f"sqlite+aiosqlite:///{(tmp_path / 'd' / 'mavis.db').as_posix()}"


def test_postgres_url_is_not_sqlite(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    s = Settings(database_url="postgresql+psycopg://u:p@localhost/mavis")
    assert not s.is_sqlite


@pytest.mark.parametrize(
    ("raw", "expected"), [("", None), ("  ", None), ("-1000000000000001", -1000000000000001)]
)
def test_optional_test_chat_id_treats_blank_as_unset(settings, monkeypatch, raw, expected) -> None:
    monkeypatch.setenv("TEST_TELEGRAM_CHAT_ID", raw)
    get_settings.cache_clear()
    assert get_settings().test_telegram_chat_id == expected
