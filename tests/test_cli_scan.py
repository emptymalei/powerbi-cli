"""Tests for scan CLI commands."""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from pbi_cli.cli import pbi


def test_scan_group_help():
    """Test that the scan command group shows help."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["scan", "--help"])
    assert result.exit_code == 0
    assert "initiate" in result.output
    assert "result" in result.output


def test_scan_initiate_help():
    """Test that scan initiate command shows help with expected options."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["scan", "initiate", "--help"])
    assert result.exit_code == 0
    assert "--workspace-id" in result.output or "-w" in result.output
    assert "--lineage" in result.output
    assert "--datasource-details" in result.output
    assert "--dataset-schema" in result.output
    assert "--dataset-expressions" in result.output
    assert "--get-artifact-users" in result.output


def test_scan_result_help():
    """Test that scan result command shows help with expected options."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["scan", "result", "--help"])
    assert result.exit_code == 0
    assert "scan_id" in result.output.lower() or "SCAN_ID" in result.output
    assert "--target" in result.output or "-t" in result.output


def test_scan_initiate_requires_workspace_id():
    """Test that scan initiate fails when no workspace IDs are provided."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["scan", "initiate"])
    assert result.exit_code != 0
    assert "workspace" in result.output.lower() or "missing" in result.output.lower()


def test_scan_initiate_calls_api(tmp_path):
    """Test that scan initiate sends correct workspace IDs to the API."""
    fake_response = {
        "id": "scan-123",
        "createdDateTime": "2024-01-01T00:00:00Z",
        "status": "Running",
    }

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            return_value=fake_response,
        ) as mock_initiate:
            result = runner.invoke(
                pbi,
                [
                    "scan",
                    "initiate",
                    "-w",
                    "workspace-id-1",
                    "-w",
                    "workspace-id-2",
                ],
            )

    assert result.exit_code == 0
    mock_initiate.assert_called_once_with(
        workspace_ids=["workspace-id-1", "workspace-id-2"],
        lineage=False,
        datasource_details=False,
        dataset_schema=False,
        dataset_expressions=False,
        get_artifact_users=False,
    )
    output = json.loads(result.output)
    assert output["id"] == "scan-123"


def test_scan_initiate_with_flags():
    """Test that scan initiate passes optional flags correctly."""
    fake_response = {"id": "scan-456", "status": "Running"}

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            return_value=fake_response,
        ) as mock_initiate:
            result = runner.invoke(
                pbi,
                [
                    "scan",
                    "initiate",
                    "-w",
                    "workspace-id-1",
                    "--lineage",
                    "--datasource-details",
                    "--get-artifact-users",
                ],
            )

    assert result.exit_code == 0
    mock_initiate.assert_called_once_with(
        workspace_ids=["workspace-id-1"],
        lineage=True,
        datasource_details=True,
        dataset_schema=False,
        dataset_expressions=False,
        get_artifact_users=True,
    )


def test_scan_result_prints_to_console():
    """Test that scan result prints JSON to console when no target is given."""
    fake_result = {
        "workspaces": [{"id": "workspace-id-1", "name": "My Workspace"}]
    }

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
            return_value=fake_result,
        ) as mock_result:
            result = runner.invoke(pbi, ["scan", "result", "scan-123"])

    assert result.exit_code == 0
    mock_result.assert_called_once_with(scan_id="scan-123")
    output = json.loads(result.output)
    assert output["workspaces"][0]["id"] == "workspace-id-1"


def test_scan_result_saves_to_file(tmp_path):
    """Test that scan result saves JSON to a file when target is given."""
    fake_result = {
        "workspaces": [{"id": "workspace-id-2", "name": "Other Workspace"}]
    }
    target_file = tmp_path / "scan_results.json"

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
            return_value=fake_result,
        ):
            result = runner.invoke(
                pbi, ["scan", "result", "scan-456", "-t", str(target_file)]
            )

    assert result.exit_code == 0
    assert target_file.exists()
    with open(target_file) as fp:
        saved = json.load(fp)
    assert saved["workspaces"][0]["id"] == "workspace-id-2"
