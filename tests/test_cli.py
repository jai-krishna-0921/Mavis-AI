from typer.testing import CliRunner

from zento.cli import app


def test_cli_lists_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("dev", "api", "worker", "chat", "migrate"):
        assert command in result.output


def test_migrate_runs_against_temp_db(settings) -> None:
    result = CliRunner().invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert (settings.data_dir.parent / "test.db").exists()
