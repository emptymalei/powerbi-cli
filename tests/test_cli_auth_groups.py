"""Tests for group-based authentication profiles."""

import pytest
from click.testing import CliRunner

from pbi_cli.cli import pbi
from pbi_cli.config import PBIConfig, VALID_GROUPS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(tmp_path) -> PBIConfig:
    """Return a PBIConfig instance pointing to the tmp_path config file."""
    config_file = tmp_path / ".pbi_cli" / "config.yaml"
    return PBIConfig(config_file=config_file)


def _isolated_runner(tmp_path, monkeypatch) -> CliRunner:
    """Return a CliRunner whose env has HOME pointed at *tmp_path*.

    Because ``PBIConfig()`` (without an explicit config_file) calls
    ``Path.home()`` at construction time, monkeypatching HOME here causes
    every CLI invocation inside the runner to use the tmp config dir.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    return CliRunner()


# ---------------------------------------------------------------------------
# PBIConfig group-management unit tests
# ---------------------------------------------------------------------------


class TestPBIConfigGroups:
    """Unit tests for the group management methods added to PBIConfig."""

    def test_default_config_has_groups(self, tmp_path):
        """Default config includes 'user' and 'admin' groups with no profiles."""
        cfg = _cfg(tmp_path)
        for group in VALID_GROUPS:
            assert cfg.get_group_profiles(group) == {}
            assert cfg.get_group_active_profile(group) is None

    def test_add_profile_to_group(self, tmp_path):
        """add_profile_to_group stores the profile under the correct group."""
        cfg = _cfg(tmp_path)
        cfg.add_profile_to_group("admin", "admin-nlm", {"name": "admin-nlm"})
        assert cfg.has_profile_in_group("admin", "admin-nlm")
        assert not cfg.has_profile_in_group("user", "admin-nlm")

    def test_set_and_get_group_active_profile(self, tmp_path):
        """set_group_active_profile updates the active profile for that group only."""
        cfg = _cfg(tmp_path)
        cfg.add_profile_to_group("admin", "admin-nlm")
        cfg.set_group_active_profile("admin", "admin-nlm")
        assert cfg.get_group_active_profile("admin") == "admin-nlm"
        assert cfg.get_group_active_profile("user") is None

    def test_groups_do_not_interfere(self, tmp_path):
        """Profiles in different groups are independent of each other."""
        cfg = _cfg(tmp_path)
        cfg.add_profile_to_group("admin", "admin-nlm")
        cfg.set_group_active_profile("admin", "admin-nlm")
        cfg.add_profile_to_group("user", "user-nlm")
        cfg.set_group_active_profile("user", "user-nlm")

        assert cfg.get_group_active_profile("admin") == "admin-nlm"
        assert cfg.get_group_active_profile("user") == "user-nlm"
        assert cfg.has_profile_in_group("admin", "admin-nlm")
        assert cfg.has_profile_in_group("user", "user-nlm")
        assert not cfg.has_profile_in_group("admin", "user-nlm")
        assert not cfg.has_profile_in_group("user", "admin-nlm")

    def test_remove_profile_from_group(self, tmp_path):
        """remove_profile_from_group deletes the profile and clears active if needed."""
        cfg = _cfg(tmp_path)
        cfg.add_profile_to_group("admin", "admin-nlm")
        cfg.set_group_active_profile("admin", "admin-nlm")
        cfg.remove_profile_from_group("admin", "admin-nlm")

        assert not cfg.has_profile_in_group("admin", "admin-nlm")
        assert cfg.get_group_active_profile("admin") is None

    def test_remove_nonexistent_profile_is_noop(self, tmp_path):
        """remove_profile_from_group does nothing if the profile does not exist."""
        cfg = _cfg(tmp_path)
        # Should not raise
        cfg.remove_profile_from_group("admin", "does-not-exist")

    def test_remove_active_profile_promotes_next(self, tmp_path):
        """Removing the active profile promotes another profile to active."""
        cfg = _cfg(tmp_path)
        cfg.add_profile_to_group("admin", "profile-a")
        cfg.add_profile_to_group("admin", "profile-b")
        cfg.set_group_active_profile("admin", "profile-a")
        cfg.remove_profile_from_group("admin", "profile-a")

        assert cfg.get_group_active_profile("admin") == "profile-b"


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestAuthCommandWithGroup:
    """Tests for `pbi auth -g <group>` behaviour."""

    def test_auth_with_admin_group(self, tmp_path, monkeypatch):
        """pbi auth -t <token> -p admin-nlm -g admin stores profile in admin group."""
        runner = _isolated_runner(tmp_path, monkeypatch)
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                pbi, ["auth", "-t", "my-token", "-p", "admin-nlm", "-g", "admin"]
            )
        assert result.exit_code == 0
        assert "admin-nlm" in result.output
        assert "admin" in result.output

    def test_auth_with_user_group(self, tmp_path, monkeypatch):
        """pbi auth -t <token> -p user-nlm -g user stores profile in user group."""
        runner = _isolated_runner(tmp_path, monkeypatch)
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                pbi, ["auth", "-t", "my-token", "-p", "user-nlm", "-g", "user"]
            )
        assert result.exit_code == 0
        assert "user-nlm" in result.output

    def test_auth_group_profiles_are_independent(self, tmp_path, monkeypatch):
        """Profiles in admin and user groups do not interfere with each other."""
        runner = _isolated_runner(tmp_path, monkeypatch)
        with runner.isolated_filesystem(temp_dir=tmp_path):
            runner.invoke(
                pbi, ["auth", "-t", "admin-token", "-p", "admin-nlm", "-g", "admin"]
            )
            runner.invoke(
                pbi, ["auth", "-t", "user-token", "-p", "user-nlm", "-g", "user"]
            )

        # Check config state through PBIConfig pointing at the same tmp dir
        cfg = _cfg(tmp_path)
        assert cfg.has_profile_in_group("admin", "admin-nlm")
        assert cfg.has_profile_in_group("user", "user-nlm")
        assert not cfg.has_profile_in_group("admin", "user-nlm")
        assert not cfg.has_profile_in_group("user", "admin-nlm")
        assert cfg.get_group_active_profile("admin") == "admin-nlm"
        assert cfg.get_group_active_profile("user") == "user-nlm"

    def test_auth_invalid_group_fails(self, tmp_path, monkeypatch):
        """pbi auth with an invalid group value is rejected."""
        runner = _isolated_runner(tmp_path, monkeypatch)
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                pbi,
                ["auth", "-t", "my-token", "-p", "some-profile", "-g", "superuser"],
            )
        assert result.exit_code != 0

    def test_auth_without_group_uses_flat_profiles(self, tmp_path, monkeypatch):
        """pbi auth without -g still writes to flat profile storage (backward compat)."""
        runner = _isolated_runner(tmp_path, monkeypatch)
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                pbi, ["auth", "-t", "my-token", "-p", "my-profile"]
            )
        assert result.exit_code == 0
        cfg = _cfg(tmp_path)
        assert cfg.has_profile("my-profile")


class TestProfileSwitchWithGroup:
    """Tests for `pbi profile switch <name> -g <group>` behaviour."""

    def test_switch_within_admin_group(self, tmp_path, monkeypatch):
        """Switching profile within admin group updates admin active profile."""
        runner = _isolated_runner(tmp_path, monkeypatch)
        cfg = _cfg(tmp_path)
        cfg.add_profile_to_group("admin", "admin-a")
        cfg.add_profile_to_group("admin", "admin-b")
        cfg.set_group_active_profile("admin", "admin-a")

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                pbi, ["profile", "switch", "admin-b", "-g", "admin"]
            )
        assert result.exit_code == 0
        assert "admin-b" in result.output
        cfg.reload()
        assert cfg.get_group_active_profile("admin") == "admin-b"

    def test_switch_within_user_group(self, tmp_path, monkeypatch):
        """Switching profile within user group updates user active profile."""
        runner = _isolated_runner(tmp_path, monkeypatch)
        cfg = _cfg(tmp_path)
        cfg.add_profile_to_group("user", "user-a")
        cfg.add_profile_to_group("user", "user-b")
        cfg.set_group_active_profile("user", "user-a")

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                pbi, ["profile", "switch", "user-b", "-g", "user"]
            )
        assert result.exit_code == 0
        assert "user-b" in result.output
        cfg.reload()
        assert cfg.get_group_active_profile("user") == "user-b"

    def test_switch_groups_do_not_interfere(self, tmp_path, monkeypatch):
        """Switching profile in one group does not affect the other group."""
        runner = _isolated_runner(tmp_path, monkeypatch)
        cfg = _cfg(tmp_path)
        cfg.add_profile_to_group("admin", "admin-a")
        cfg.add_profile_to_group("admin", "admin-b")
        cfg.set_group_active_profile("admin", "admin-a")
        cfg.add_profile_to_group("user", "user-a")
        cfg.set_group_active_profile("user", "user-a")

        with runner.isolated_filesystem(temp_dir=tmp_path):
            runner.invoke(pbi, ["profile", "switch", "admin-b", "-g", "admin"])

        cfg.reload()
        assert cfg.get_group_active_profile("admin") == "admin-b"
        # user group must be untouched
        assert cfg.get_group_active_profile("user") == "user-a"

    def test_switch_nonexistent_profile_in_group(self, tmp_path, monkeypatch):
        """Switching to a profile that doesn't exist in the group shows an error."""
        runner = _isolated_runner(tmp_path, monkeypatch)
        cfg = _cfg(tmp_path)
        cfg.add_profile_to_group("admin", "admin-a")

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                pbi, ["profile", "switch", "ghost-profile", "-g", "admin"]
            )
        assert "not found" in result.output

    def test_switch_no_profiles_in_group(self, tmp_path, monkeypatch):
        """Switching within a group that has no profiles shows an appropriate message."""
        runner = _isolated_runner(tmp_path, monkeypatch)
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                pbi, ["profile", "switch", "some-profile", "-g", "admin"]
            )
        assert "No profiles found" in result.output or "not found" in result.output


class TestProfileListWithGroups:
    """Tests for `pbi profile list` displaying group information."""

    def test_profile_list_shows_groups(self, tmp_path, monkeypatch):
        """profile list shows admin and user group sections."""
        runner = _isolated_runner(tmp_path, monkeypatch)
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(pbi, ["profile", "list"])
        assert "user" in result.output
        assert "admin" in result.output

    def test_profile_list_shows_group_profiles(self, tmp_path, monkeypatch):
        """profile list displays profiles stored in groups."""
        runner = _isolated_runner(tmp_path, monkeypatch)
        cfg = _cfg(tmp_path)
        cfg.add_profile_to_group("admin", "admin-nlm")
        cfg.set_group_active_profile("admin", "admin-nlm")
        cfg.add_profile_to_group("user", "user-nlm")
        cfg.set_group_active_profile("user", "user-nlm")

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(pbi, ["profile", "list"])
        assert "admin-nlm" in result.output
        assert "user-nlm" in result.output


class TestProfileDeleteWithGroup:
    """Tests for `pbi profile delete <name> -g <group>` behaviour."""

    def test_delete_profile_from_group(self, tmp_path, monkeypatch):
        """Deleting a profile from a group removes it from that group only."""
        runner = _isolated_runner(tmp_path, monkeypatch)
        cfg = _cfg(tmp_path)
        cfg.add_profile_to_group("admin", "admin-nlm")

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                pbi,
                ["profile", "delete", "admin-nlm", "-g", "admin"],
                input="y\n",
            )
        assert result.exit_code == 0
        cfg.reload()
        assert not cfg.has_profile_in_group("admin", "admin-nlm")

    def test_delete_nonexistent_profile_from_group(self, tmp_path, monkeypatch):
        """Deleting a non-existent profile from a group shows an error."""
        runner = _isolated_runner(tmp_path, monkeypatch)
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                pbi,
                ["profile", "delete", "ghost", "-g", "admin"],
                input="y\n",
            )
        assert "not found" in result.output
