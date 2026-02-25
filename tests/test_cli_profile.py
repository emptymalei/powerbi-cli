"""Tests for profile CLI commands."""

import pytest
from click.testing import CliRunner

from pbi_cli.cli import pbi


def test_profile_group_help():
    """Test that `pbi profile --help` works."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["profile", "--help"])
    assert result.exit_code == 0
    assert "profile" in result.output


def test_profile_group_no_subcommand():
    """Test that `pbi profile` without subcommand shows help hint."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["profile"])
    assert result.exit_code == 0
    assert "pbi profile --help" in result.output


def test_profile_list_help():
    """Test that `pbi profile list --help` works."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["profile", "list", "--help"])
    assert result.exit_code == 0
    assert "List all stored authentication profiles" in result.output


def test_profile_switch_help():
    """Test that `pbi profile switch --help` works."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["profile", "switch", "--help"])
    assert result.exit_code == 0
    assert "Switch the active authentication profile" in result.output


def test_profile_delete_help():
    """Test that `pbi profile delete --help` works."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["profile", "delete", "--help"])
    assert result.exit_code == 0
    assert "Delete an authentication profile" in result.output


def test_profile_list_no_profiles(tmp_path, monkeypatch):
    """Test `pbi profile list` with no profiles shows appropriate message."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(pbi, ["profile", "list"])
    assert "No profiles found" in result.output


def test_profile_switch_no_profiles(tmp_path, monkeypatch):
    """Test `pbi profile switch` with no profiles shows appropriate message."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(pbi, ["profile", "switch", "someprofile"])
    assert "No profiles found" in result.output


def test_old_profiles_command_does_not_exist():
    """Test that the old `pbi profiles` command group no longer exists."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["profiles", "--help"])
    assert result.exit_code != 0
    assert "No such command" in result.output


def test_old_switch_profile_command_does_not_exist():
    """Test that the old `pbi switch-profile` command no longer exists."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["switch-profile"])
    assert result.exit_code != 0
    assert "No such command" in result.output
