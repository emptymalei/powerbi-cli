"""A config file that cannot be read is never replaced, and it is never seen half written."""

import pytest
from typer.testing import CliRunner

from pbi_cli import config as config_module
from pbi_cli.cli import app
from pbi_cli.config import PBIConfig
from pbi_cli.errors import PBIError

BROKEN = "groups:\n  admin: {profiles: [unclosed\n"


def run(*args: str):
    return CliRunner().invoke(app, list(args))


def config_file():
    return PBIConfig()._config_file


def write_config(text: str) -> None:
    config_file().parent.mkdir(parents=True, exist_ok=True)
    config_file().write_text(text, encoding="utf-8")


def test_a_broken_config_file_is_not_replaced_by_the_defaults(tmp_path):
    write_config(BROKEN)

    result = run("config", "set-cache-folder", str(tmp_path / "cache"))

    assert result.exit_code == 1
    assert f"Error: The config file {config_file()} cannot be read" in result.output
    assert config_file().read_text(encoding="utf-8") == BROKEN


def test_pbi_auth_does_not_replace_a_broken_config_file():
    write_config(BROKEN)

    result = run("auth", "-t", "abc", "-p", "adm", "-g", "admin")

    assert result.exit_code == 1
    assert "cannot be read" in result.output
    assert config_file().read_text(encoding="utf-8") == BROKEN


@pytest.mark.parametrize("text", ["- a list\n- of things\n", "just text\n"])
def test_a_config_file_without_settings_is_not_replaced_either(text):
    write_config(text)

    with pytest.raises(PBIError):
        PBIConfig().cache_folder = "/somewhere"

    assert config_file().read_text(encoding="utf-8") == text


def test_a_broken_config_file_is_still_read_as_the_defaults():
    write_config(BROKEN)

    result = run("config", "get-verify-certificates")

    assert result.exit_code == 0
    assert "Verify certificates: True" in result.output


def test_an_empty_config_file_can_be_saved():
    write_config("")

    PBIConfig().cache_folder = "/somewhere"

    assert PBIConfig().cache_folder == "/somewhere"


def test_the_config_file_is_replaced_in_one_step(monkeypatch):
    PBIConfig().cache_folder = "/before"
    before = config_file().read_text(encoding="utf-8")

    def broken_dump(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(config_module.yaml, "dump", broken_dump)
    with pytest.raises(OSError):
        PBIConfig().cache_folder = "/after"

    assert config_file().read_text(encoding="utf-8") == before
    assert [p.name for p in config_file().parent.iterdir()] == ["config.yaml"]


def test_saving_goes_through_a_replace(monkeypatch):
    replaced = []
    real = config_module.replace_file

    def watching(source, target, **kwargs):
        replaced.append((source, target))
        real(source, target, **kwargs)

    monkeypatch.setattr(config_module, "replace_file", watching)

    PBIConfig().cache_folder = "/somewhere"

    [(source, target)] = replaced
    assert target == config_file() and source.parent == config_file().parent
    assert PBIConfig().cache_folder == "/somewhere"
