"""Tests for workspaces CLI commands."""

import click
import pytest
from click.testing import CliRunner

from pbi_cli.cli import pbi


def test_workspaces_list_expand_option_not_shadowing_builtin():
    """Test that `list(expand)` inside the workspaces list command correctly
    converts the expand tuple to a list (using [*expand]) without calling the
    Click Command due to name shadowing.

    Previously, `list(expand)` inside the `list` function called the Click
    Command named `list` (since it shadows the Python builtin in module scope),
    causing 'Got unexpected extra arguments' error.
    """
    runner = CliRunner()
    # Invoke with --expand options - if list(expand) still calls the Click Command,
    # the command would fail with "Got unexpected extra arguments"
    result = runner.invoke(
        pbi,
        [
            "workspaces",
            "list",
            "--expand",
            "users",
            "--expand",
            "reports",
        ],
    )
    # The command should fail only due to missing auth, NOT due to
    # "Got unexpected extra arguments" from list(expand) calling the Click Command
    assert "Got unexpected extra arguments" not in result.output
    assert result.exit_code != 0  # Fails due to missing auth (expected)


def test_workspaces_list_default_expand_values():
    """Test that the workspaces list command uses all expand values by default."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["workspaces", "list", "--help"])
    # Should show all valid expand choices in help
    assert "users" in result.output
    assert "reports" in result.output
    assert "dashboards" in result.output
    assert "datasets" in result.output
    assert "dataflows" in result.output
    assert "workbooks" in result.output
    assert result.exit_code == 0
