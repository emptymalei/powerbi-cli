"""Tests for the certificate verification setting."""

from unittest.mock import patch

import pytest
from core_helpers import make_response
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


@pytest.mark.parametrize(
    "setting, expected",
    [(None, True), ("true", True), ("false", False), ("<bundle>", "<bundle>")],
    ids=["default", "true", "false", "bundle"],
)
def test_the_commands_that_use_the_lake_verify_as_the_setting_says(
    fake_api, signed_in, tmp_path, monkeypatch, setting, expected
):
    runner = _isolated_runner(tmp_path, monkeypatch)
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    if setting is not None:
        runner.invoke(
            app,
            [
                "config",
                "set-verify-certificates",
                setting.replace("<bundle>", str(bundle)),
            ],
        )
    fake_api.add("GET", "/admin/groups", make_response(200, {"value": []}))

    result = runner.invoke(app, ["workspaces", "list"])

    assert result.exit_code == 0, result.output
    assert fake_api.verifies == [str(bundle) if expected == "<bundle>" else expected]


# ---------------------------------------------------------------------------
# system: the certificate store of the operating system
# ---------------------------------------------------------------------------


@pytest.fixture
def injected(monkeypatch):
    """Record the calls that would make ``ssl`` trust the operating system: the real one
    would change ``ssl`` for the whole test run."""
    import truststore

    calls = []
    monkeypatch.setattr(truststore, "inject_into_ssl", lambda: calls.append(True))
    return calls


def test_system_is_kept_as_a_word_not_as_a_path(tmp_path, monkeypatch):
    runner = _isolated_runner(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["config", "set-verify-certificates", "System"])

    assert result.exit_code == 0, result.output
    assert PBIConfig().verify_certificates == "system"
    shown = runner.invoke(app, ["config", "get-verify-certificates"])
    assert "Verify certificates: system" in shown.output


def test_system_makes_ssl_trust_the_operating_system_and_verifies(injected):
    from pbi_cli.session import requests_verify

    assert requests_verify("system") is True
    assert injected == [True]


@pytest.mark.parametrize("setting", [True, False, "/certs/ca.pem"])
def test_the_other_settings_leave_ssl_alone(injected, setting):
    from pbi_cli.session import requests_verify

    assert requests_verify(setting) == setting
    assert injected == []


def test_the_commands_that_use_the_lake_trust_the_system_when_told(
    fake_api, signed_in, tmp_path, monkeypatch, injected
):
    runner = _isolated_runner(tmp_path, monkeypatch)
    runner.invoke(app, ["config", "set-verify-certificates", "system"])
    fake_api.add("GET", "/admin/groups", make_response(200, {"value": []}))

    result = runner.invoke(app, ["workspaces", "list"])

    assert result.exit_code == 0, result.output
    assert injected and fake_api.verifies == [True]


def test_an_unverified_certificate_says_what_to_set():
    import requests

    from pbi_cli.core.client import _tls_hint

    failed = requests.exceptions.SSLError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "self-signed certificate in certificate chain"
    )
    assert "pbi config set-verify-certificates system" in _tls_hint(failed)
    assert _tls_hint(requests.exceptions.SSLError("handshake timed out")) == ""
    assert _tls_hint(requests.exceptions.ConnectionError("refused")) == ""
