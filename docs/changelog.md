# Changelog

## Unreleased

### Changed

- The CLI is now built with [Typer](https://typer.tiangolo.com) instead of click.
  Commands, flags (including `-ft`, `-tf`, `-wi` and `-wn`), defaults, prompts and exit
  codes are unchanged. `click` is no longer a direct dependency.
- `--help` follows Typer's layout: option values are shown as `<int>` or `<json|excel>`,
  options show their `[default: ...]`, and arguments are listed in an `Arguments:`
  section. The sub-commands of a group are still listed alphabetically.
- `pbi workspaces list --top` is no longer marked `[required]` in `--help`; it always
  had a default of 1000.
- `--interval` and `--timeout` of `pbi workspaces scan get` still reject values of 0 or
  less. The error now reads `must be greater than 0`.
- The docs are built with [Zensical](https://zensical.org) instead of MkDocs Material.
  `mkdocs.yml` is replaced by `zensical.toml` and the look is unchanged (the `classic`
  theme variant). The `mkdocs-jupyter` plugin (no notebooks in the docs) and the unused
  math scripts are gone; code blocks gain a copy button.
- The CLI reference page is generated from the commands by `scripts/gen_cli_docs.py`
  instead of the `mkdocs-click` plugin, which cannot read Typer commands. The docs
  workflows check that the page is up to date before they build.

### Added

- Shell completion: `pbi --install-completion` and `pbi --show-completion`.
- `pbi_cli.core`: an API client with a local [data lake](lake.md). The commands do not use
  it yet, so their behavior is unchanged. The client reads lists page by page, waits when
  a documented quota is used up or the API answers `429 Retry-After`, and keeps what it
  fetched as versioned snapshots and per-day event logs on disk or in S3. A request is
  identified by its endpoint, its canonical parameters and the tenant, so a different
  `$expand` or another tenant never gets somebody else's cached answer.
- A registry of the read-only operations the client may call, with the quota of each taken
  from its documentation page.
- pbi_cli reads the tenant and the expiry from a token locally (nothing is sent or
  stored), and says when a token has expired. See [Authentication](auth.md).
- The guides [Authentication](auth.md) and [Data lake](lake.md), and the API reference of
  `pbi_cli.core` and `pbi_cli.errors`.
- Descriptions for the arguments of `pbi workspaces scan`, `pbi profile` and
  `pbi config` in `--help`.

### Fixed

- `pbi apps augment` and `pbi reports users` with `--file-type excel` and a target that
  has a file extension now report a usage error. They used to crash with a `TypeError`.

### Development

- `tests/test_cli_surface.py` compares every command, flag, default and `--help` output
  with recorded fixtures, so a refactor cannot silently change the CLI.
- `tests/test_cli_docs.py` fails when `docs/references/cli.md` is out of date.
