# Power BI CLI Tool

`pbi_cli` is a CLI tool for Power BI written in Python. It calls the
[Power BI REST API](https://learn.microsoft.com/en-us/rest/api/power-bi/) to list
workspaces, apps and reports, scan workspaces, and save the results as JSON or Excel.

## Getting started

Install the project with [uv](https://docs.astral.sh/uv/) (see the repository README for
the development setup) and check that the command works:

```bash
uv sync
uv run pbi --help
```

Every group of commands has its own help, for example `pbi workspaces --help` and
`pbi workspaces scan get --help`. The full list is in the
[CLI reference](references/cli.md).

## Sign in

The CLI calls the API with a bearer token that you provide. Store it with `pbi auth`:

```bash
# Default profile
pbi auth --bearer-token <your_bearer_token>

# A named profile in the admin group (needed by the admin commands)
pbi auth -t <your_bearer_token> -p admin-nlm -g admin

# See and switch profiles
pbi profile list
pbi profile switch admin-nlm -g admin
```

Tokens are kept in your system keyring. They expire after a while (typically about an
hour); when a command reports a missing or rejected token, sign in again and run
`pbi auth` with a fresh one.

Commands marked "Requires Admin" in the reference need a profile in the `admin` group.

## Shell completion

```bash
pbi --install-completion   # install completion for your current shell
pbi --show-completion      # print the completion script instead
```

## Guides

- [Workspace Scans](scan.md): scan workspaces one at a time or from a config file.
- [Cache](cache.md): reuse the results of earlier API calls.
- [Changelog](changelog.md)
