"""Tests for scan CLI commands (under pbi workspaces scan group)."""

import json
import time
from unittest.mock import call, patch

from click.testing import CliRunner

from pbi_cli.cli import pbi


def test_scan_group_in_workspaces_help():
    """Test that the scan subgroup appears in workspaces group help."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["workspaces", "--help"])
    assert result.exit_code == 0
    assert "scan" in result.output


def test_scan_group_help():
    """Test that the scan group lists initiate, result, and get commands."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["workspaces", "scan", "--help"])
    assert result.exit_code == 0
    assert "initiate" in result.output
    assert "result" in result.output
    assert "get" in result.output


def test_scan_initiate_help():
    """Test that scan initiate command shows help with expected options."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["workspaces", "scan", "initiate", "--help"])
    assert result.exit_code == 0
    assert "WORKSPACE_IDS" in result.output
    assert "--lineage" in result.output
    assert "--datasource-details" in result.output
    assert "--dataset-schema" in result.output
    assert "--dataset-expressions" in result.output
    assert "--get-artifact-users" in result.output
    assert "Admin" in result.output


def test_scan_result_help():
    """Test that scan result command shows help with expected options."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["workspaces", "scan", "result", "--help"])
    assert result.exit_code == 0
    assert "SCAN_ID" in result.output
    assert "--target" in result.output or "-t" in result.output
    assert "Admin" in result.output


def test_scan_get_help():
    """Test that scan get command shows help with expected options."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["workspaces", "scan", "get", "--help"])
    assert result.exit_code == 0
    assert "WORKSPACE_IDS" in result.output
    assert "--interval" in result.output
    assert "--timeout" in result.output
    assert "Admin" in result.output


def test_scan_initiate_requires_workspace_id():
    """Test that scan initiate fails when no workspace IDs are provided."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["workspaces", "scan", "initiate"])
    assert result.exit_code != 0


def test_scan_initiate_calls_api():
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
                    "workspaces",
                    "scan",
                    "initiate",
                    "workspace-id-1",
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
                    "workspaces",
                    "scan",
                    "initiate",
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
    fake_result = {"workspaces": [{"id": "workspace-id-1", "name": "My Workspace"}]}

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
            return_value=fake_result,
        ) as mock_result:
            result = runner.invoke(pbi, ["workspaces", "scan", "result", "scan-123"])

    assert result.exit_code == 0
    mock_result.assert_called_once_with(scan_id="scan-123")
    output = json.loads(result.output)
    assert output["workspaces"][0]["id"] == "workspace-id-1"


def test_scan_result_saves_to_file(tmp_path):
    """Test that scan result saves JSON to a file when target is given."""
    fake_result = {"workspaces": [{"id": "workspace-id-2", "name": "Other Workspace"}]}
    target_file = tmp_path / "scan_results.json"

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
            return_value=fake_result,
        ):
            result = runner.invoke(
                pbi,
                [
                    "workspaces",
                    "scan",
                    "result",
                    "scan-456",
                    "-t",
                    str(target_file),
                ],
            )

    assert result.exit_code == 0
    assert target_file.exists()
    with open(target_file) as fp:
        saved = json.load(fp)
    assert saved["workspaces"][0]["id"] == "workspace-id-2"


def test_scan_get_succeeds_immediately():
    """Test scan get when the scan result is available on the first attempt."""
    fake_init = {"id": "scan-789", "status": "Running"}
    fake_result = {"workspaces": [{"id": "ws-1"}]}

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            return_value=fake_init,
        ):
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
                return_value=fake_result,
            ) as mock_result:
                result = runner.invoke(
                    pbi,
                    ["workspaces", "scan", "get", "ws-1"],
                )

    assert result.exit_code == 0
    mock_result.assert_called_once_with(scan_id="scan-789")
    output_lines = [l for l in result.output.splitlines() if l.startswith("{") or l.startswith("}") or '"workspaces"' in l]
    assert any("ws-1" in l for l in result.output.splitlines())


def test_scan_get_retries_then_succeeds():
    """Test scan get retries when scan is not ready, then succeeds."""
    fake_init = {"id": "scan-abc", "status": "Running"}
    fake_result = {"workspaces": [{"id": "ws-2"}]}

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            return_value=fake_init,
        ):
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
                side_effect=[ValueError("not ready"), ValueError("not ready"), fake_result],
            ) as mock_result:
                with patch("time.sleep"):
                    result = runner.invoke(
                        pbi,
                        ["workspaces", "scan", "get", "ws-2", "--interval", "1"],
                    )

    assert result.exit_code == 0
    assert mock_result.call_count == 3
    assert "ws-2" in result.output


def test_scan_get_times_out():
    """Test scan get raises an error when timeout is exceeded."""
    fake_init = {"id": "scan-timeout", "status": "Running"}

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            return_value=fake_init,
        ):
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
                side_effect=ValueError("not ready"),
            ):
                with patch("time.sleep"):
                    # Use a very short timeout so it expires after first failure
                    with patch("time.monotonic", side_effect=[0, 0, 999, 999]):
                        result = runner.invoke(
                            pbi,
                            ["workspaces", "scan", "get", "ws-x", "--timeout", "1"],
                        )

    assert result.exit_code != 0
    assert "did not complete" in result.output


def test_scan_get_saves_to_file(tmp_path):
    """Test scan get saves results to a file."""
    fake_init = {"id": "scan-file", "status": "Running"}
    fake_result = {"workspaces": [{"id": "ws-3"}]}
    target_file = tmp_path / "results.json"

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            return_value=fake_init,
        ):
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
                return_value=fake_result,
            ):
                result = runner.invoke(
                    pbi,
                    ["workspaces", "scan", "get", "ws-3", "-t", str(target_file)],
                )

    assert result.exit_code == 0
    assert target_file.exists()
    with open(target_file) as fp:
        saved = json.load(fp)
    assert saved["workspaces"][0]["id"] == "ws-3"
