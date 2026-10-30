"""Tests for the certificate verification setting."""

from unittest.mock import patch

from typer.testing import CliRunner

from pbi_cli.cli import app
from pbi_cli.config import PBIConfig


def _isolated_runner(tmp_path, monkeypatch) -> CliRunner:
    """Point the config folder at a temporary home."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    return CliRunner()


def test_certificates_are_verified_by_default(tmp_path, monkeypatch):
    runner = _isolated_runner(tmp_path, monkeypatch)
    result = runner.invoke(app, ["config", "get-verify-certificates"])
    assert result.exit_code == 0
    assert "Verify certificates: True" in result.output
    assert PBIConfig().verify_certificates is True


def test_disable_verification_warns(tmp_path, monkeypatch):
    runner = _isolated_runner(tmp_path, monkeypatch)
    result = runner.invoke(app, ["config", "set-verify-certificates", "false"])
    assert result.exit_code == 0
    assert "disabled" in result.output
    assert PBIConfig().verify_certificates is False


def test_true_restores_verification(tmp_path, monkeypatch):
    runner = _isolated_runner(tmp_path, monkeypatch)
    runner.invoke(app, ["config", "set-verify-certificates", "false"])
    result = runner.invoke(app, ["config", "set-verify-certificates", "TRUE"])
    assert result.exit_code == 0
    assert PBIConfig().verify_certificates is True


def test_ca_bundle_path_is_stored_absolute(tmp_path, monkeypatch):
    runner = _isolated_runner(tmp_path, monkeypatch)
    bundle = tmp_path / "corporate-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["config", "set-verify-certificates", "corporate-ca.pem"]
    )
    assert result.exit_code == 0
    assert PBIConfig().verify_certificates == str(bundle)


def test_missing_ca_bundle_is_refused(tmp_path, monkeypatch):
    runner = _isolated_runner(tmp_path, monkeypatch)
    result = runner.invoke(
        app, ["config", "set-verify-certificates", str(tmp_path / "missing.pem")]
    )
    assert result.exit_code == 1
    assert PBIConfig().verify_certificates is True


def test_api_calls_use_the_setting(tmp_path, monkeypatch):
    runner = _isolated_runner(tmp_path, monkeypatch)
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    runner.invoke(app, ["config", "set-verify-certificates", str(bundle)])
    runner.invoke(app, ["auth", "-t", "token"])

    with patch("pbi_cli.powerbi.app.App") as fake_app:
        fake_app.return_value.return_value = {}
        result = runner.invoke(app, ["apps", "app", "-a", "some-app"])
    assert result.exit_code == 0, result.output
    assert fake_app.call_args.kwargs["verify"] == str(bundle)
