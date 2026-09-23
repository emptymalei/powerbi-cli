"""Tests for scan CLI commands (under pbi workspaces scan group)."""

import json
from unittest.mock import patch

import click
import requests
from click.testing import CliRunner

from pbi_cli.cli import pbi


def test_scan_group_in_workspaces_help():
    """Test that the scan subgroup appears in workspaces group help."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["workspaces", "--help"])
    assert result.exit_code == 0
    assert "scan" in result.output


def test_scan_group_help():
    """Test that the scan group lists initiate, status, result, get, and batch commands."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["workspaces", "scan", "--help"])
    assert result.exit_code == 0
    assert "initiate" in result.output
    assert "status" in result.output
    assert "result" in result.output
    assert "get" in result.output
    assert "batch" in result.output


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


def test_scan_status_help():
    """Test that scan status command shows help with expected options."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["workspaces", "scan", "status", "--help"])
    assert result.exit_code == 0
    assert "SCAN_ID" in result.output
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


def test_scan_status_calls_api():
    """Test that scan status prints the status JSON to console."""
    fake_status = {
        "id": "scan-123",
        "createdDateTime": "2024-01-01T00:00:00Z",
        "status": "Succeeded",
    }

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_status",
            return_value=fake_status,
        ) as mock_status:
            result = runner.invoke(pbi, ["workspaces", "scan", "status", "scan-123"])

    assert result.exit_code == 0
    mock_status.assert_called_once_with(scan_id="scan-123")
    output = json.loads(result.output)
    assert output["status"] == "Succeeded"


def test_scan_status_handles_api_error():
    """Test that scan status surfaces expected API failures cleanly."""
    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "******"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_status",
            side_effect=ValueError("Error: {'message': 'boom'}"),
        ):
            result = runner.invoke(pbi, ["workspaces", "scan", "status", "scan-123"])

    assert result.exit_code != 0
    assert "boom" in result.output


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
    """Test scan get when the scan status is Succeeded on the first attempt."""
    fake_init = {"id": "scan-789", "status": "Running"}
    fake_status = {"id": "scan-789", "status": "Succeeded"}
    fake_result = {"workspaces": [{"id": "ws-1"}]}

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            return_value=fake_init,
        ):
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_status",
                return_value=fake_status,
            ) as mock_status:
                with patch(
                    "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
                    return_value=fake_result,
                ) as mock_result:
                    result = runner.invoke(
                        pbi,
                        ["workspaces", "scan", "get", "ws-1"],
                    )

    assert result.exit_code == 0
    mock_status.assert_called_once_with(scan_id="scan-789")
    mock_result.assert_called_once_with(scan_id="scan-789")
    assert any("ws-1" in l for l in result.output.splitlines())


def test_scan_get_retries_then_succeeds():
    """Test scan get retries while status is not terminal, then succeeds."""
    fake_init = {"id": "scan-abc", "status": "Running"}
    fake_result = {"workspaces": [{"id": "ws-2"}]}

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            return_value=fake_init,
        ):
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_status",
                side_effect=[
                    {"id": "scan-abc", "status": "NotStarted"},
                    {"id": "scan-abc", "status": "Running"},
                    {"id": "scan-abc", "status": "Succeeded"},
                ],
            ) as mock_status:
                with patch(
                    "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
                    return_value=fake_result,
                ):
                    with patch("time.sleep"):
                        result = runner.invoke(
                            pbi,
                            ["workspaces", "scan", "get", "ws-2", "--interval", "1"],
                        )

    assert result.exit_code == 0
    assert mock_status.call_count == 3
    assert "ws-2" in result.output


def test_scan_get_fails():
    """Test scan get raises an error when the scan status is Failed."""
    fake_init = {"id": "scan-failed", "status": "Running"}
    fake_status = {
        "id": "scan-failed",
        "status": "Failed",
        "error": {"code": "InternalError", "message": "boom"},
    }

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            return_value=fake_init,
        ):
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_status",
                return_value=fake_status,
            ):
                result = runner.invoke(
                    pbi,
                    ["workspaces", "scan", "get", "ws-y"],
                )

    assert result.exit_code != 0
    assert "failed" in result.output.lower()


def test_scan_get_times_out():
    """Test scan get raises an error when timeout is exceeded."""
    fake_init = {"id": "scan-timeout", "status": "Running"}
    fake_status = {"id": "scan-timeout", "status": "Running"}

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            return_value=fake_init,
        ):
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_status",
                return_value=fake_status,
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
    fake_status = {"id": "scan-file", "status": "Succeeded"}
    fake_result = {"workspaces": [{"id": "ws-3"}]}
    target_file = tmp_path / "results.json"

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            return_value=fake_init,
        ):
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_status",
                return_value=fake_status,
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


def test_scan_get_saves_to_target_folder_named_by_workspace_id(tmp_path):
    """Test scan get with --target-folder saves as <workspace_id>.json."""
    fake_init = {"id": "scan-folder", "status": "Running"}
    fake_status = {"id": "scan-folder", "status": "Succeeded"}
    fake_result = {"workspaces": [{"id": "ws-4"}]}
    target_folder = tmp_path / "scan_results"

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            return_value=fake_init,
        ):
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_status",
                return_value=fake_status,
            ):
                with patch(
                    "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
                    return_value=fake_result,
                ):
                    result = runner.invoke(
                        pbi,
                        [
                            "workspaces",
                            "scan",
                            "get",
                            "ws-4",
                            "-tf",
                            str(target_folder),
                        ],
                    )

    assert result.exit_code == 0
    output_file = target_folder / "ws-4.json"
    assert output_file.exists()
    with open(output_file) as fp:
        saved = json.load(fp)
    assert saved["workspaces"][0]["id"] == "ws-4"


def test_scan_get_target_and_target_folder_mutually_exclusive(tmp_path):
    """Test scan get rejects passing both --target and --target-folder."""
    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        result = runner.invoke(
            pbi,
            [
                "workspaces",
                "scan",
                "get",
                "ws-5",
                "-t",
                str(tmp_path / "out.json"),
                "-tf",
                str(tmp_path / "out_dir"),
            ],
        )

    assert result.exit_code != 0
    assert "not both" in result.output


def test_scan_get_target_folder_rejects_multiple_workspace_ids(tmp_path):
    """Test scan get rejects --target-folder with multiple workspace IDs."""
    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "******"}):
        result = runner.invoke(
            pbi,
            [
                "workspaces",
                "scan",
                "get",
                "ws-5",
                "ws-6",
                "-tf",
                str(tmp_path / "out_dir"),
            ],
        )

    assert result.exit_code != 0
    assert "single workspace ID" in result.output


def test_scan_batch_help():
    """Test that scan batch command shows help with expected options."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["workspaces", "scan", "batch", "--help"])
    assert result.exit_code == 0
    assert "--config" in result.output or "-c" in result.output
    assert (
        "Each workspace runs through its own initiate/status/result cycle"
        in result.output
    )
    assert "Admin" in result.output


def test_scan_batch_requires_config():
    """Test that scan batch fails when --config is missing."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["workspaces", "scan", "batch"])
    assert result.exit_code != 0


def test_scan_batch_saves_named_and_unnamed_workspaces(tmp_path):
    """Test scan batch scans each workspace and names files by workspace name
    plus workspace ID suffix when given, falling back to the workspace ID
    otherwise."""
    config_file = tmp_path / "scan_config.yaml"
    target_folder = tmp_path / "scan_results"
    config_file.write_text(
        f"""
workspace_ids:
  - id: ws-a
    name: Finance Team
  - ws-b
target_folder: {target_folder}
interval: 1
timeout: 10
"""
    )

    fake_init = {"id": "scan-x", "status": "Running"}
    fake_status = {"id": "scan-x", "status": "Succeeded"}

    def fake_get_scan_result(self, scan_id):
        return {"workspaces": [{"id": "some-id"}]}

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            return_value=fake_init,
        ) as mock_initiate:
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_status",
                return_value=fake_status,
            ):
                with patch(
                    "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
                    fake_get_scan_result,
                ):
                    result = runner.invoke(
                        pbi,
                        ["workspaces", "scan", "batch", "-c", str(config_file)],
                    )

    assert result.exit_code == 0, result.output
    assert mock_initiate.call_count == 2
    mock_initiate.assert_any_call(
        workspace_ids=["ws-a"],
        lineage=False,
        datasource_details=False,
        dataset_schema=False,
        dataset_expressions=False,
        get_artifact_users=False,
    )
    mock_initiate.assert_any_call(
        workspace_ids=["ws-b"],
        lineage=False,
        datasource_details=False,
        dataset_schema=False,
        dataset_expressions=False,
        get_artifact_users=False,
    )
    assert (target_folder / "finance-team-ws-a.json").exists()
    assert (target_folder / "ws-b.json").exists()


def test_scan_batch_suffixes_workspace_id_to_avoid_overwrites(tmp_path):
    """Test scan batch appends workspace IDs for named workspaces."""
    config_file = tmp_path / "scan_config.yaml"
    target_folder = tmp_path / "scan_results"
    config_file.write_text(
        f"""
workspace_ids:
  - id: ws-a
    name: Shared Team
  - id: ws-b
    name: Shared Team
target_folder: {target_folder}
interval: 1
timeout: 10
"""
    )

    fake_init = {"id": "scan-x", "status": "Running"}
    fake_status = {"id": "scan-x", "status": "Succeeded"}

    def fake_get_scan_result(self, scan_id):
        return {"workspaces": [{"id": "some-id"}]}

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "******"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            return_value=fake_init,
        ):
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_status",
                return_value=fake_status,
            ):
                with patch(
                    "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
                    fake_get_scan_result,
                ):
                    result = runner.invoke(
                        pbi,
                        ["workspaces", "scan", "batch", "-c", str(config_file)],
                    )

    assert result.exit_code == 0, result.output
    assert (target_folder / "shared-team-ws-a.json").exists()
    assert (target_folder / "shared-team-ws-b.json").exists()


def test_scan_batch_falls_back_to_workspace_id_when_name_slug_is_empty(tmp_path):
    """Test scan batch uses workspace ID if a name slugifies to empty."""
    config_file = tmp_path / "scan_config.yaml"
    target_folder = tmp_path / "scan_results"
    config_file.write_text(
        f"""
workspace_ids:
  - id: ws-empty
    name: "!!!"
target_folder: {target_folder}
interval: 1
timeout: 10
"""
    )

    fake_init = {"id": "scan-z", "status": "Running"}
    fake_status = {"id": "scan-z", "status": "Succeeded"}

    def fake_get_scan_result(self, scan_id):
        return {"workspaces": [{"id": "ws-empty"}]}

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "******"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            return_value=fake_init,
        ):
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_status",
                return_value=fake_status,
            ):
                with patch(
                    "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
                    fake_get_scan_result,
                ):
                    result = runner.invoke(
                        pbi,
                        ["workspaces", "scan", "batch", "-c", str(config_file)],
                    )

    assert result.exit_code == 0, result.output
    assert (target_folder / "ws-empty.json").exists()
    assert not (target_folder / "-ws-empty.json").exists()


def test_scan_batch_continues_after_one_workspace_fails(tmp_path):
    """Test scan batch reports a non-zero exit but still saves the workspaces
    that succeeded when one workspace's scan fails."""
    config_file = tmp_path / "scan_config.yaml"
    target_folder = tmp_path / "scan_results"
    config_file.write_text(
        f"""
workspace_ids:
  - ws-good
  - ws-bad
target_folder: {target_folder}
interval: 1
timeout: 10
"""
    )

    fake_init = {"id": "scan-y", "status": "Running"}

    def fake_get_scan_status(self, scan_id):
        # Simulate the "bad" workspace's scan failing, the "good" one succeeding.
        if scan_id == "scan-y-bad":
            return {"status": "Failed", "error": {"message": "boom"}}
        return {"status": "Succeeded"}

    def fake_initiate_scan(self, workspace_ids, **kwargs):
        scan_id = "scan-y-bad" if workspace_ids == ["ws-bad"] else "scan-y-good"
        return {"id": scan_id, "status": "Running"}

    def fake_get_scan_result(self, scan_id):
        return {"workspaces": [{"id": "ws-good"}]}

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            fake_initiate_scan,
        ):
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_status",
                fake_get_scan_status,
            ):
                with patch(
                    "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
                    fake_get_scan_result,
                ):
                    result = runner.invoke(
                        pbi,
                        ["workspaces", "scan", "batch", "-c", str(config_file)],
                    )

    assert result.exit_code != 0
    assert (target_folder / "ws-good.json").exists()
    assert not (target_folder / "ws-bad.json").exists()
    assert "ws-bad" in result.output


def test_scan_batch_continues_after_workspace_api_value_error(tmp_path):
    """Test scan batch continues when a workspace API call raises ValueError."""
    config_file = tmp_path / "scan_config.yaml"
    target_folder = tmp_path / "scan_results"
    config_file.write_text(
        f"""
workspace_ids:
  - ws-good
  - ws-bad
target_folder: {target_folder}
interval: 1
timeout: 10
"""
    )

    def fake_initiate_scan(self, workspace_ids, **kwargs):
        scan_id = "scan-y-bad" if workspace_ids == ["ws-bad"] else "scan-y-good"
        return {"id": scan_id, "status": "Running"}

    def fake_get_scan_status(self, scan_id):
        if scan_id == "scan-y-bad":
            raise ValueError("Error: {'message': 'boom'}")
        return {"status": "Succeeded"}

    def fake_get_scan_result(self, scan_id):
        return {"workspaces": [{"id": "ws-good"}]}

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "******"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            fake_initiate_scan,
        ):
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_status",
                fake_get_scan_status,
            ):
                with patch(
                    "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
                    fake_get_scan_result,
                ):
                    result = runner.invoke(
                        pbi,
                        ["workspaces", "scan", "batch", "-c", str(config_file)],
                    )

    assert result.exit_code != 0
    assert (target_folder / "ws-good.json").exists()
    assert not (target_folder / "ws-bad.json").exists()
    assert "ws-bad" in result.output


def test_scan_batch_continues_after_workspace_http_error(tmp_path):
    """Test scan batch continues when a workspace API call raises HTTPError."""
    config_file = tmp_path / "scan_config.yaml"
    target_folder = tmp_path / "scan_results"
    config_file.write_text(
        f"""
workspace_ids:
  - ws-good
  - ws-bad
target_folder: {target_folder}
interval: 1
timeout: 10
"""
    )

    def fake_initiate_scan(self, workspace_ids, **kwargs):
        scan_id = "scan-y-bad" if workspace_ids == ["ws-bad"] else "scan-y-good"
        return {"id": scan_id, "status": "Running"}

    def fake_get_scan_status(self, scan_id):
        return {"status": "Succeeded"}

    def fake_get_scan_result(self, scan_id):
        if scan_id == "scan-y-bad":
            raise requests.HTTPError("500 Server Error")
        return {"workspaces": [{"id": "ws-good"}]}

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "******"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            fake_initiate_scan,
        ):
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_status",
                fake_get_scan_status,
            ):
                with patch(
                    "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
                    fake_get_scan_result,
                ):
                    result = runner.invoke(
                        pbi,
                        ["workspaces", "scan", "batch", "-c", str(config_file)],
                    )

    assert result.exit_code != 0
    assert (target_folder / "ws-good.json").exists()
    assert not (target_folder / "ws-bad.json").exists()
    assert "ws-bad" in result.output


def test_scan_batch_requires_workspace_ids(tmp_path):
    """Test scan batch fails with a clear error when workspace_ids is missing."""
    config_file = tmp_path / "scan_config.yaml"
    config_file.write_text("target_folder: results\n")

    runner = CliRunner()
    result = runner.invoke(pbi, ["workspaces", "scan", "batch", "-c", str(config_file)])

    assert result.exit_code != 0
    assert "workspace_ids" in result.output


def test_scan_batch_rejects_invalid_boolean_config_value(tmp_path):
    """Test scan batch rejects non-boolean YAML flag values."""
    config_file = tmp_path / "scan_config.yaml"
    config_file.write_text(
        f"""
workspace_ids:
  - ws-1
target_folder: {tmp_path / "scan_results"}
lineage: "false"
"""
    )

    runner = CliRunner()
    result = runner.invoke(pbi, ["workspaces", "scan", "batch", "-c", str(config_file)])

    assert result.exit_code != 0
    assert "'lineage' must be a boolean value." in result.output


def test_scan_batch_uses_defaults_for_null_interval_and_timeout(tmp_path):
    """Test scan batch treats null interval/timeout values as defaults."""
    config_file = tmp_path / "scan_config.yaml"
    target_folder = tmp_path / "scan_results"
    config_file.write_text(
        f"""
workspace_ids:
  - ws-1
target_folder: {target_folder}
interval:
timeout:
"""
    )

    fake_init = {"id": "scan-null", "status": "Running"}
    fake_status = {"id": "scan-null", "status": "Succeeded"}

    def fake_get_scan_result(self, scan_id):
        return {"workspaces": [{"id": "ws-1"}]}

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "******"}):
        with patch(
            "pbi_cli.powerbi.admin.WorkspaceInfo.initiate_scan",
            return_value=fake_init,
        ) as mock_initiate:
            with patch(
                "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_status",
                return_value=fake_status,
            ):
                with patch(
                    "pbi_cli.powerbi.admin.WorkspaceInfo.get_scan_result",
                    fake_get_scan_result,
                ):
                    result = runner.invoke(
                        pbi,
                        ["workspaces", "scan", "batch", "-c", str(config_file)],
                    )

    assert result.exit_code == 0, result.output
    mock_initiate.assert_called_once_with(
        workspace_ids=["ws-1"],
        lineage=False,
        datasource_details=False,
        dataset_schema=False,
        dataset_expressions=False,
        get_artifact_users=False,
    )
    assert (target_folder / "ws-1.json").exists()


def test_scan_batch_reports_admin_auth_loading_errors(tmp_path):
    """Test scan batch wraps admin auth failures with batch-specific context."""
    config_file = tmp_path / "scan_config.yaml"
    config_file.write_text(
        f"""
workspace_ids:
  - ws-1
target_folder: {tmp_path / "scan_results"}
"""
    )

    runner = CliRunner()
    with patch(
        "pbi_cli.cli.load_auth",
        side_effect=click.ClickException("No credentials found for profile 'admin'."),
    ):
        result = runner.invoke(
            pbi, ["workspaces", "scan", "batch", "-c", str(config_file)]
        )

    assert result.exit_code != 0
    assert "Unable to load admin auth for scan batch" in result.output
    assert "No credentials found for profile 'admin'." in result.output


def test_scan_batch_requires_target_folder(tmp_path):
    """Test scan batch fails with a clear error when target_folder is missing."""
    config_file = tmp_path / "scan_config.yaml"
    config_file.write_text("workspace_ids:\n  - ws-1\n")

    runner = CliRunner()
    result = runner.invoke(pbi, ["workspaces", "scan", "batch", "-c", str(config_file)])

    assert result.exit_code != 0
    assert "target_folder" in result.output
