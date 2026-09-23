# Workspace Scans

The `pbi workspaces scan` command group wraps the Power BI Admin
[WorkspaceInfo API](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/workspace-info-post-workspace-info)
to trigger a metadata scan of one or more workspaces and retrieve the
results (reports, dashboards, datasets, datasource lineage, etc).

!!! warning "Requires Admin"

    Every command in this group needs an admin-scoped auth profile:

    ```bash
    pbi auth -t <your_bearer_token> -g admin
    ```

## Commands

| Command | Maps to | Purpose |
| --- | --- | --- |
| `pbi workspaces scan initiate <workspace-id>...` | [PostWorkspaceInfo](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/workspace-info-post-workspace-info) | Start a scan, returns a scan ID |
| `pbi workspaces scan status <scan-id>` | [GetScanStatus](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/workspace-info-get-scan-status) | Check whether a scan has finished |
| `pbi workspaces scan result <scan-id>` | [GetScanResult](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/workspace-info-get-scan-result) | Fetch the scan metadata once it succeeded |
| `pbi workspaces scan get <workspace-id>...` | all three combined | Initiate, poll status, then fetch the result in one step |
| `pbi workspaces scan batch --config <file>` | all three combined, looped | Same as `get`, run once per workspace listed in a config file, saved to files |

`initiate` accepts optional flags that map directly to the PostWorkspaceInfo
query parameters: `--lineage`, `--datasource-details`, `--dataset-schema`,
`--dataset-expressions`, `--get-artifact-users`. `get` and `batch` accept the
same flags.

## Quick scan of one or two workspaces

```bash
# Step by step
pbi workspaces scan initiate <workspace-id>
pbi workspaces scan status <scan-id>
pbi workspaces scan result <scan-id>

# Or combined: initiate, poll status, fetch result
pbi workspaces scan get <workspace-id> --lineage --datasource-details

# Save to a single file instead of printing to console
pbi workspaces scan get <workspace-id> -t results.json

# Save to a folder, named after the workspace ID(s) -- handy inside a loop
pbi workspaces scan get <workspace-id> -tf scan_results
```

## Scanning several workspaces: config file template

For looping through more than a couple of workspaces, use
`pbi workspaces scan batch` with a YAML config file instead of a shell loop.
Each workspace is scanned individually (its own initiate/status/result
cycle), so one failing workspace doesn't block the rest, and each result is
saved as its own file.

Copy the template below (also available at
[`examples/scan_config.example.yaml`](https://github.com/emptymalei/powerbi-cli/blob/main/examples/scan_config.example.yaml)
in the repo) and fill in your workspace IDs:

```yaml
--8<-- "examples/scan_config.example.yaml"
```

Then run:

```bash
pbi workspaces scan batch --config scan_config.yaml
```

### Config fields

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `workspace_ids` | Yes | — | List of workspaces to scan. Each entry is a plain ID string, or a mapping with `id` and an optional `name`. |
| `target_folder` | Yes | — | Folder to save results in (absolute, or relative to the [default output folder](index.md)). |
| `lineage` | No | `false` | Include lineage information |
| `datasource_details` | No | `false` | Include datasource details |
| `dataset_schema` | No | `false` | Include dataset schema |
| `dataset_expressions` | No | `false` | Include dataset expressions |
| `get_artifact_users` | No | `false` | Include artifact users |
| `interval` | No | `5` | Seconds between status checks |
| `timeout` | No | `300` | Max seconds to wait per workspace before giving up |

### Output file naming

- If a `workspace_ids` entry has a `name`, the result is saved as
  `<target_folder>/<slugified-name>-<workspace-id>.json` (e.g. `Finance Team`
  + `ws-a` → `finance-team-ws-a.json`).
- Otherwise it falls back to `<target_folder>/<workspace-id>.json`.

### Handling failures

If a workspace's scan fails or times out, `scan batch` logs the error,
moves on to the next workspace, and saves results for every workspace that
succeeded. At the end it prints the list of failed workspace IDs and exits
with a non-zero status code, so you can wire it into a script and check
`$?`.
