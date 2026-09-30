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
| `pbi workspaces scan batch --config <file>` | all three combined, in batches of 100 | Same as `get` for every workspace listed in a config file, saved to one file per workspace |

`initiate` accepts optional flags that map directly to the PostWorkspaceInfo
query parameters: `--lineage`, `--datasource-details`, `--dataset-schema`,
`--dataset-expressions`, `--get-artifact-users`. `get` and `batch` accept the
same flags.

A scan takes up to 100 workspaces and the API allows 500 scan requests an hour. To scan
*every* workspace of the tenant, keep the results, and continue from the last scan next
time, use [`pbi sync run scan`](sync.md#scans).

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

# Save one workspace result to a folder as <workspace-id>.json -- handy inside a loop
pbi workspaces scan get <workspace-id> -tf scan_results
```

## Scanning several workspaces: config file template

For more than a couple of workspaces, use `pbi workspaces scan batch` with a YAML
config file instead of a shell loop. The workspaces are scanned in batches of up to 100
per request, and each workspace gets its own file with the result for that workspace
alone: the workspace, and the data sources it uses. If a batch fails, its workspaces are
scanned one by one, so one failing workspace doesn't block the rest.

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
| `target_folder` | Yes | — | Folder to save results in (absolute, or relative to the [default output folder](../)). |
| `lineage` | No | `false` | Include lineage information |
| `datasource_details` | No | `false` | Include datasource details |
| `dataset_schema` | No | `false` | Include dataset schema |
| `dataset_expressions` | No | `false` | Include dataset expressions |
| `get_artifact_users` | No | `false` | Include artifact users |
| `interval` | No | `5` | Seconds between status checks |
| `timeout` | No | `300` | Max seconds to wait for one scan (a batch, or a single workspace) before giving up |

### Output file naming

- If a `workspace_ids` entry has a `name`, the result is saved as
  `<target_folder>/<slugified-name>-<workspace-id>.json` (e.g. `Finance Team`
  + `ws-a` → `finance-team-ws-a.json`).
- Otherwise it falls back to `<target_folder>/<workspace-id>.json`.

### Handling failures

If the scan of a batch fails or times out, `scan batch` says so and scans the workspaces
of that batch one by one. A workspace whose own scan fails is logged, the next one is
scanned, and the results of every workspace that succeeded are saved. At the end the
command prints the failed workspaces and exits with a non-zero status code, so you can wire
it into a script and check `$?`.

Some problems are the same for every workspace: an expired or missing token, or Power BI
asking to wait. Then the command stops at once and says so, instead of listing every
workspace as failed. Files that were already saved stay.

A workspace that Power BI does not know gets a file with an empty result, and a note.

### The data lake

When a [cache folder](lake.md) is configured, the scan of every batch is also kept in the
data lake (`pbi lake ls -e admin.scan.result`), together with the workspaces, the options
and the scan id. The requests count against the quota that `pbi sync` keeps track of.
