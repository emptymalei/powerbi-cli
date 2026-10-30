"""Signing in with PowerShell: what is run, what is read, what is said when it fails."""

import subprocess

import pytest
from typer.testing import CliRunner

from pbi_cli import cli
from pbi_cli.core import powershell
from pbi_cli.errors import PBIError


def fake_run(output="Bearer abc.def\n", code=0, error=""):
    def run(command, **kwargs):
        run.command = command
        return subprocess.CompletedProcess(command, code, output, error)

    return run


def test_the_token_is_the_last_line_without_the_bearer_prefix():
    assert powershell.parse_token("noise\nBearer abc def\n") == "abcdef"
    assert powershell.parse_token("") == ""


def test_the_login_runs_powershell_and_gives_the_token(monkeypatch):
    run = fake_run()
    monkeypatch.setattr(powershell.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(powershell.subprocess, "run", run)

    assert powershell.powershell_token() == "abc.def"
    assert "Login-PowerBI" in run.command[-1] and "-AsString" in run.command[-1]


@pytest.mark.parametrize(
    "which, run, said",
    [
        (lambda n: None, fake_run(), "PowerShell was not found"),
        (
            lambda n: "/bin/x",
            fake_run("", 1, "no module\nModule not found"),
            "Module not found",
        ),
        (lambda n: "/bin/x", fake_run("\n"), "gave no token"),
    ],
)
def test_a_login_that_fails_says_why_and_how_to_install(monkeypatch, which, run, said):
    monkeypatch.setattr(powershell.shutil, "which", which)
    monkeypatch.setattr(powershell.subprocess, "run", run)

    with pytest.raises(PBIError, match=said) as info:
        powershell.powershell_token()
    assert "Install-Module MicrosoftPowerBIMgmt" in str(info.value)


def test_a_login_that_is_not_finished_in_time_is_said(monkeypatch):
    def slow(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(powershell.shutil, "which", lambda n: "/bin/x")
    monkeypatch.setattr(powershell.subprocess, "run", slow)

    with pytest.raises(PBIError, match="not finished in time"):
        powershell.powershell_token()


def stored(monkeypatch):
    seen = []
    real = cli.store_token
    monkeypatch.setattr(
        cli, "store_token", lambda t, p, g: seen.append((t, p, g)) or real(t, p, g)
    )
    return seen


def test_pbi_auth_login_stores_the_token_for_the_profile_and_group(monkeypatch):
    seen = stored(monkeypatch)
    monkeypatch.setattr(cli, "powershell_token", lambda: "tok123")

    result = CliRunner().invoke(
        cli.app, ["auth", "--login", "-p", "adm", "-g", "admin"]
    )

    assert result.exit_code == 0, result.output
    assert seen == [("tok123", "adm", "admin")]


def test_pbi_auth_reads_a_piped_token(monkeypatch):
    seen = stored(monkeypatch)

    result = CliRunner().invoke(
        cli.app, ["auth", "-t", "-", "-p", "svc", "-g", "user"], input="Bearer piped1\n"
    )

    assert result.exit_code == 0, result.output
    assert seen[0][1:] == ("svc", "user") and seen[0][0].endswith("piped1")


def test_pbi_auth_login_and_a_token_together_are_refused():
    result = CliRunner().invoke(cli.app, ["auth", "--login", "-t", "x"])

    assert result.exit_code == 1 and "not both" in result.output


def test_pbi_auth_without_any_token_says_what_to_do():
    result = CliRunner().invoke(cli.app, ["auth"], input="")

    assert result.exit_code == 1 and "No token was given" in result.output
