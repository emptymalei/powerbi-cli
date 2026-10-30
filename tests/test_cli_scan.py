"""Tests for scan CLI commands (under pbi workspaces scan group)."""

import json
from unittest.mock import patch

import pytest
from fake_powerbi import FakePowerBI
from typer.testing import CliRunner

from pbi_cli.cli import app
from pbi_cli.core.scan import run_scan
from pbi_cli.core.store import LakeStore
from pbi_cli.errors import PBIError
from pbi_cli.session import quota_file


def test_scan_group_in_workspaces_help():
    """Test that the scan subgroup appears in workspaces group help."""
    runner = CliRunner()
    result = runner.invoke(app, ["workspaces", "--help"])
    assert result.exit_code == 0
    assert "scan" in result.output


def test_scan_group_help():
    """Test that the scan group lists initiate, status, result, get, and batch commands."""
    runner = CliRunner()
    result = runner.invoke(app, ["workspaces", "scan", "--help"])
    assert result.exit_code == 0
    assert "initiate" in result.output
    assert "status" in result.output
    assert "result" in result.output
    assert "get" in result.output
    assert "batch" in result.output


def test_scan_initiate_help():
    """Test that scan initiate command shows help with expected options."""
    runner = CliRunner()
    result = runner.invoke(app, ["workspaces", "scan", "initiate", "--help"])
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
    result = runner.invoke(app, ["workspaces", "scan", "result", "--help"])
    assert result.exit_code == 0
    assert "SCAN_ID" in result.output
    assert "--target" in result.output or "-t" in result.output
    assert "Admin" in result.output


def test_scan_status_help():
    """Test that scan status command shows help with expected options."""
    runner = CliRunner()
    result = runner.invoke(app, ["workspaces", "scan", "status", "--help"])
    assert result.exit_code == 0
    assert "SCAN_ID" in result.output
    assert "Admin" in result.output


def test_scan_get_help():
    """Test that scan get command shows help with expected options."""
    runner = CliRunner()
    result = runner.invoke(app, ["workspaces", "scan", "get", "--help"])
    assert result.exit_code == 0
    assert "WORKSPACE_IDS" in result.output
    assert "--interval" in result.output
    assert "--timeout" in result.output
    assert "Admin" in result.output


def test_scan_initiate_requires_workspace_id():
    """Test that scan initiate fails when no workspace IDs are provided."""
    runner = CliRunner()
    result = runner.invoke(app, ["workspaces", "scan", "initiate"])
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
                app,
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
                app,
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
            result = runner.invoke(app, ["workspaces", "scan", "status", "scan-123"])

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
            result = runner.invoke(app, ["workspaces", "scan", "status", "scan-123"])

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
            result = runner.invoke(app, ["workspaces", "scan", "result", "scan-123"])

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
                app,
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
                        app,
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
                            app,
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
                    app,
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
                            app,
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
                        app,
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
                        app,
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
            app,
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
            app,
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
    result = runner.invoke(app, ["workspaces", "scan", "batch", "--help"])
    assert result.exit_code == 0
    assert "--config" in result.output or "-c" in result.output
    assert "batches of up to 100 per request" in result.output
    assert "one by one" in result.output
    assert "Admin" in result.output


# ---------------------------------------------------------------------------
# scan batch: what the API answers is scripted with the fake service
# ---------------------------------------------------------------------------


@pytest.fixture
def fake(monkeypatch):
    service = FakePowerBI(workspaces=8, reports=4, datasets=3)
    monkeypatch.setattr("pbi_cli.core.client.make_session", service.session)
    return service


def batch_config(tmp_path, entries, **extra):
    """Write a config file for scan batch; returns (config file, target folder)."""
    target_folder = tmp_path / "scan_results"
    settings = {
        "workspace_ids": entries,
        "target_folder": str(target_folder),
        "interval": 0.2,
        "timeout": 10,
        **extra,
    }
    config_file = tmp_path / "scan_config.yaml"
    config_file.write_text(json.dumps(settings))  # JSON is YAML
    return config_file, target_folder


def scan_batch(config_file):
    return CliRunner().invoke(
        app, ["workspaces", "scan", "batch", "-c", str(config_file)]
    )


def saved(folder, name):
    return json.loads((folder / name).read_text())


def test_scan_batch_saves_named_and_unnamed_workspaces(tmp_path, fake, signed_in):
    """Files are named by the workspace name plus its ID, or by the ID alone."""
    config_file, folder = batch_config(
        tmp_path, [{"id": "ws-0001", "name": "Finance Team"}, "ws-0002"]
    )

    result = scan_batch(config_file)

    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in folder.iterdir()) == [
        "finance-team-ws-0001.json",
        "ws-0002.json",
    ]
    # each file holds the result of its own workspace
    assert [
        w["id"] for w in saved(folder, "finance-team-ws-0001.json")["workspaces"]
    ] == ["ws-0001"]
    assert [w["id"] for w in saved(folder, "ws-0002.json")["workspaces"]] == ["ws-0002"]


def test_scan_batch_scans_workspaces_together_not_one_request_each(
    tmp_path, fake, signed_in
):
    config_file, _ = batch_config(tmp_path, ["ws-0001", "ws-0002", "ws-0003"])

    scan_batch(config_file)

    (request,) = fake.calls_to(r"getInfo", "POST")
    assert request.body == {"workspaces": ["ws-0001", "ws-0002", "ws-0003"]}


def test_scan_batch_sends_at_most_100_workspaces_per_request(
    tmp_path, monkeypatch, signed_in
):
    service = FakePowerBI(workspaces=250)
    monkeypatch.setattr("pbi_cli.core.client.make_session", service.session)
    config_file, folder = batch_config(tmp_path, [f"ws-{n:04d}" for n in range(1, 251)])

    result = scan_batch(config_file)

    assert result.exit_code == 0, result.output
    sizes = [len(c.body["workspaces"]) for c in service.calls_to(r"getInfo", "POST")]
    assert sizes == [100, 100, 50]
    assert len(list(folder.iterdir())) == 250
    assert "Scanned 250 workspace(s)" in result.output


def test_scan_batch_passes_the_flags_and_splits_the_data_sources(
    tmp_path, fake, signed_in
):
    config_file, folder = batch_config(
        tmp_path,
        ["ws-0001", "ws-0002"],
        lineage=True,
        datasource_details=True,
    )

    scan_batch(config_file)

    query = fake.calls_to(r"getInfo", "POST")[0].query
    assert query["lineage"] == "true" and query["datasourceDetails"] == "true"
    assert query["datasetSchema"] == "false"
    first, second = saved(folder, "ws-0001.json"), saved(folder, "ws-0002.json")
    # each file lists the data sources its own workspace uses, not its neighbour's
    assert [i["datasourceId"] for i in first["datasourceInstances"]] == ["dsi-ds-0001"]
    assert [i["datasourceId"] for i in second["datasourceInstances"]] == ["dsi-ds-0002"]


def test_scan_batch_suffixes_workspace_id_to_avoid_overwrites(
    tmp_path, fake, signed_in
):
    """Test scan batch appends workspace IDs for named workspaces."""
    config_file, folder = batch_config(
        tmp_path,
        [
            {"id": "ws-0001", "name": "Shared Team"},
            {"id": "ws-0002", "name": "Shared Team"},
        ],
    )

    result = scan_batch(config_file)

    assert result.exit_code == 0, result.output
    assert (folder / "shared-team-ws-0001.json").exists()
    assert (folder / "shared-team-ws-0002.json").exists()


def test_scan_batch_falls_back_to_workspace_id_when_name_slug_is_empty(
    tmp_path, fake, signed_in
):
    """Test scan batch uses workspace ID if a name slugifies to empty."""
    config_file, folder = batch_config(tmp_path, [{"id": "ws-0001", "name": "!!!"}])

    result = scan_batch(config_file)

    assert result.exit_code == 0, result.output
    assert (folder / "ws-0001.json").exists()
    assert not (folder / "-ws-0001.json").exists()


def test_scan_batch_reports_progress_like_scan_get(tmp_path, fake, signed_in):
    fake.scan_polls = 1
    config_file, _ = batch_config(tmp_path, ["ws-0001", "ws-0002"])

    result = scan_batch(config_file)

    assert "Initiating scan for ['ws-0001', 'ws-0002']…" in result.output
    assert "Scan started (id=scan-0001). Waiting for status…" in result.output
    assert "Attempt 1: scan status is 'Running', retrying in 0s…" in result.output
    assert "=== 2 workspace(s): ws-0001, ws-0002 ===" in result.output


# -- when a batch fails ----------------------------------------------------------------


def test_scan_batch_scans_one_by_one_when_a_batch_fails(tmp_path, fake, signed_in):
    """Test scan batch reports a non-zero exit but still saves the workspaces
    that succeeded when one workspace's scan fails."""
    fake.failing_scan_workspaces = {"ws-0002"}
    config_file, folder = batch_config(tmp_path, ["ws-0001", "ws-0002", "ws-0003"])

    result = scan_batch(config_file)

    assert result.exit_code != 0
    assert sorted(p.name for p in folder.iterdir()) == ["ws-0001.json", "ws-0003.json"]
    assert "Scanning these 3 workspaces together failed" in result.output
    assert "Scanning them one by one…" in result.output
    assert (
        "✗ ws-0002:" in result.output and "Failed workspaces: ws-0002" in result.output
    )
    # one scan of the three, then one of each
    assert [c.body["workspaces"] for c in fake.calls_to(r"getInfo", "POST")] == [
        ["ws-0001", "ws-0002", "ws-0003"],
        ["ws-0001"],
        ["ws-0002"],
        ["ws-0003"],
    ]


def test_scan_batch_loses_nothing_when_only_the_batch_as_a_whole_fails(
    tmp_path, fake, signed_in
):
    """A server error on the result of the batch does not cost any workspace."""
    fake.fail("GET", r"scanResult/scan-0001", 500)
    config_file, folder = batch_config(tmp_path, ["ws-0001", "ws-0002"])

    result = scan_batch(config_file)

    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in folder.iterdir()) == ["ws-0001.json", "ws-0002.json"]
    assert "Scanning them one by one…" in result.output


def test_scan_batch_continues_after_one_workspace_hits_an_api_error(
    tmp_path, fake, signed_in
):
    fake.fail("GET", r"scanResult/scan-0001", 500)  # the batch
    fake.fail("GET", r"scanResult/scan-0003", 500)  # the scan of ws-0002 alone
    config_file, folder = batch_config(tmp_path, ["ws-0001", "ws-0002"])

    result = scan_batch(config_file)

    assert result.exit_code != 0
    assert [p.name for p in folder.iterdir()] == ["ws-0001.json"]
    assert "ws-0002" in result.output and "Failed workspaces: ws-0002" in result.output


def test_scan_batch_saves_an_empty_result_for_a_workspace_the_api_does_not_know(
    tmp_path, fake, signed_in
):
    config_file, folder = batch_config(tmp_path, ["ws-0001", "ws-9999"])

    result = scan_batch(config_file)

    assert result.exit_code == 0, result.output
    assert saved(folder, "ws-9999.json") == {
        "workspaces": [],
        "datasourceInstances": [],
        "misconfiguredDatasourceInstances": [],
    }
    assert "ws-9999: Power BI returned nothing for this workspace" in result.output


# -- what stops the command --------------------------------------------------------------


def test_scan_batch_stops_at_once_without_credentials(tmp_path, fake):
    """No token is a problem for every workspace: it is said once, nothing is scanned."""
    config_file, folder = batch_config(tmp_path, ["ws-0001", "ws-0002"])

    result = scan_batch(config_file)

    assert result.exit_code == 1
    assert "No active profile set for group 'admin'" in result.output
    assert "Failed workspaces" not in result.output
    assert fake.calls == [] and not folder.exists() or not list(folder.iterdir())


def test_scan_batch_reports_admin_auth_loading_errors(tmp_path, fake):
    """An error while loading the admin credentials stops the command with its message."""
    config_file, _ = batch_config(tmp_path, ["ws-0001", "ws-0002"])

    with patch(
        "pbi_cli.cli.load_auth",
        side_effect=PBIError("No credentials found for profile 'admin'."),
    ):
        result = scan_batch(config_file)

    assert result.exit_code != 0
    assert "No credentials found for profile 'admin'." in result.output
    assert "Failed workspaces" not in result.output and fake.calls == []


def test_scan_batch_stops_when_the_token_is_rejected(tmp_path, fake, signed_in):
    fake.expire_token_after(0)
    config_file, folder = batch_config(tmp_path, ["ws-0001", "ws-0002"])

    result = scan_batch(config_file)

    assert result.exit_code == 1
    assert (
        "Power BI rejected the token" in result.output and "pbi auth" in result.output
    )
    assert len(fake.calls) == 1  # no scan of each workspace after the batch was refused
    assert "Failed workspaces" not in result.output


def test_scan_batch_stops_when_the_api_throttles(tmp_path, fake, signed_in):
    fake.fail("POST", r"getInfo", 429, headers={"Retry-After": "4000"})
    config_file, _ = batch_config(tmp_path, ["ws-0001", "ws-0002", "ws-0003"])

    result = scan_batch(config_file)

    assert result.exit_code == 1
    assert "throttling requests to admin.scan.start" in result.output
    assert fake.count(r"getInfo", "POST") == 1  # not tried again for each workspace
    assert "Scanning them one by one" not in result.output
    assert (
        "Failed workspaces" not in result.output
    )  # the command stopped, it did not fail each


def test_scan_batch_uses_defaults_for_null_interval_and_timeout(
    tmp_path, fake, signed_in
):
    """Test scan batch treats null interval/timeout values as defaults."""
    config_file, folder = batch_config(
        tmp_path, ["ws-0001"], interval=None, timeout=None
    )
    seen = {}

    def spy(client, ids, flags, **kwargs):
        seen.update(kwargs)
        return run_scan(client, ids, flags, **kwargs)

    with patch("pbi_cli.cli.run_scan", spy):
        result = scan_batch(config_file)

    assert result.exit_code == 0, result.output
    assert seen["interval"] == 5.0 and seen["timeout"] == 300.0
    assert (folder / "ws-0001.json").exists()


# -- the lake and the quota ----------------------------------------------------------------


def test_scan_batch_keeps_the_scans_in_the_lake(
    tmp_path, fake, signed_in, cache_folder
):
    config_file, _ = batch_config(tmp_path, ["ws-0003", "ws-0001"])

    scan_batch(config_file)

    (stored,) = LakeStore(cache_folder / "lake").parameter_sets(
        "tenant-1", "admin.scan.result"
    )
    assert stored.latest.manifest["workspace_ids"] == ["ws-0001", "ws-0003"]
    assert [w["id"] for w in stored.latest.load()["workspaces"]] == [
        "ws-0003",
        "ws-0001",
    ]


def test_scan_batch_works_without_a_lake(tmp_path, fake, signed_in):
    config_file, folder = batch_config(tmp_path, ["ws-0001"])

    result = scan_batch(config_file)

    assert result.exit_code == 0 and (folder / "ws-0001.json").exists()


def test_scan_batch_counts_its_requests_against_the_quota(tmp_path, fake, signed_in):
    config_file, _ = batch_config(tmp_path, ["ws-0001", "ws-0002", "ws-0003"])

    scan_batch(config_file)

    counters = json.loads(quota_file().read_text())["calls"]
    assert (
        len(counters["tenant-1/admin.scan.start"]) == 1
    )  # one request for three workspaces
    assert len(counters["tenant-1/admin.scan.result"]) == 1


def test_scan_batch_requires_config():
    """Test that scan batch fails when --config is missing."""
    runner = CliRunner()
    result = runner.invoke(app, ["workspaces", "scan", "batch"])
    assert result.exit_code != 0


def test_scan_batch_requires_workspace_ids(tmp_path):
    """Test scan batch fails with a clear error when workspace_ids is missing."""
    config_file = tmp_path / "scan_config.yaml"
    config_file.write_text("target_folder: results\n")

    runner = CliRunner()
    result = runner.invoke(app, ["workspaces", "scan", "batch", "-c", str(config_file)])

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
    result = runner.invoke(app, ["workspaces", "scan", "batch", "-c", str(config_file)])

    assert result.exit_code != 0
    assert "'lineage' must be a boolean value." in result.output


def test_scan_batch_requires_target_folder(tmp_path):
    """Test scan batch fails with a clear error when target_folder is missing."""
    config_file = tmp_path / "scan_config.yaml"
    config_file.write_text("workspace_ids:\n  - ws-1\n")

    runner = CliRunner()
    result = runner.invoke(app, ["workspaces", "scan", "batch", "-c", str(config_file)])

    assert result.exit_code != 0
    assert "target_folder" in result.output
