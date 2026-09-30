"""Snapshot helpers for the ``pbi`` CLI surface.

Walks the command tree and records everything a user can see or rely on:
command and option names, types, choices, defaults, help text, and the output of
``--help``. ``tests/test_cli_surface.py`` compares the live CLI against fixtures
recorded from a known-good version, so a change of CLI framework (or any other
refactor) cannot silently change flags that existing scripts depend on.

The helpers only read attributes of the command objects (``.commands``,
``.params``, ...), so they work on whatever object the CLI framework produces.

After an *intentional* CLI change, regenerate the fixtures and review the diff::

    uv run python tests/cli_surface.py --write
"""

import argparse
import enum
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

FIXTURES = Path(__file__).parent / "fixtures"
SURFACE_FILE = FIXTURES / "cli_surface.json"
HELP_FILE = FIXTURES / "cli_help.json"

ROOT_NAME = "pbi"

# Deterministic environment for capturing help and greeting output.
CLI_ENV = {"USER": "tester", "COLUMNS": "80", "LINES": "50", "NO_COLOR": "1"}


def load_cli() -> Tuple[Any, Any, Any]:
    """Return ``(runner, target, command)`` for the installed CLI.

    ``target`` is what the runner invokes; ``command`` is the root command object
    whose tree is walked.
    """
    import pbi_cli.cli as cli

    # Note: the click-based CLI has an unrelated function called ``app`` (the
    # ``pbi apps app`` command), so detect a Typer app by its attributes.
    app = getattr(cli, "app", None)
    if hasattr(app, "registered_commands"):
        import typer.main
        from typer.testing import CliRunner

        return CliRunner(), app, typer.main.get_command(app)

    from click.testing import CliRunner

    return CliRunner(), cli.pbi, cli.pbi


def walk(
    command: Any, path: Tuple[str, ...] = (ROOT_NAME,)
) -> Iterator[Tuple[Tuple[str, ...], Any]]:
    """Yield ``(path, command)`` for ``command`` and all of its sub-commands."""
    yield path, command
    if hasattr(command, "commands"):
        for name in command.list_commands(None):
            yield from walk(command.get_command(None, name), path + (name,))


def _plain(value: Any) -> Any:
    """Convert a value to something JSON serializable and framework independent."""
    if value is None or type(value).__name__ == "Sentinel":
        return None
    if isinstance(value, enum.Enum):
        return _plain(value.value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _type_info(param_type: Any) -> Dict[str, Any]:
    info: Dict[str, Any] = {"name": param_type.name}
    for attr in (
        "choices",
        "case_sensitive",
        "min",
        "max",
        "min_open",
        "max_open",
        "clamp",
        "exists",
        "file_okay",
        "dir_okay",
        "readable",
        "writable",
        "resolve_path",
    ):
        if hasattr(param_type, attr):
            info[attr] = _plain(getattr(param_type, attr))
    return info


def _param_info(param: Any) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "name": param.name,
        "kind": param.param_type_name,
        "opts": list(param.opts),
        "secondary_opts": list(param.secondary_opts),
        "type": _type_info(param.type),
        "required": bool(param.required),
        "multiple": bool(param.multiple),
        "nargs": param.nargs,
        "default": _plain(param.default),
    }
    if param.param_type_name == "option":
        info.update(
            {
                "is_flag": bool(param.is_flag),
                "count": bool(getattr(param, "count", False)),
                "hidden": bool(param.hidden),
                "prompt": _plain(param.prompt),
                "envvar": _plain(param.envvar),
                "help": param.help,
            }
        )
    else:
        info["metavar"] = param.human_readable_name
    return info


def _command_info(command: Any) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "kind": "group" if hasattr(command, "commands") else "command",
        "help": command.help,
        "hidden": bool(command.hidden),
        "deprecated": _plain(command.deprecated),
        "params": [_param_info(p) for p in command.params],
    }
    if hasattr(command, "commands"):
        info["invoke_without_command"] = bool(command.invoke_without_command)
        info["no_args_is_help"] = bool(command.no_args_is_help)
        info["command_order"] = list(command.list_commands(None))
    return info


def dump_surface(command: Any) -> Dict[str, Any]:
    """Return the full CLI surface keyed by command path (``"pbi workspaces list"``)."""
    return {" ".join(path): _command_info(cmd) for path, cmd in walk(command)}


def _normalize_output(text: str) -> str:
    return (
        "\n".join(
            line.rstrip() for line in text.replace("\r\n", "\n").split("\n")
        ).rstrip()
        + "\n"
    )


def capture_help(runner: Any, target: Any, command: Any) -> Dict[str, Dict[str, Any]]:
    """Capture ``--help`` (and, for groups, the no-subcommand output) of every command."""
    captured: Dict[str, Dict[str, Any]] = {}
    for path, cmd in walk(command):
        key = " ".join(path)
        args: List[str] = list(path[1:])
        result = runner.invoke(target, args + ["--help"], env=CLI_ENV)
        captured[f"{key} --help"] = {
            "exit_code": result.exit_code,
            "output": _normalize_output(result.output),
        }
        if hasattr(cmd, "commands"):
            result = runner.invoke(target, args, env=CLI_ENV)
            captured[f"{key} (no subcommand)"] = {
                "exit_code": result.exit_code,
                "output": _normalize_output(result.output),
            }
    return captured


def _write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--write", action="store_true", help="overwrite the fixtures")
    args = parser.parse_args()

    runner, target, command = load_cli()
    surface = dump_surface(command)
    helps = capture_help(runner, target, command)

    if args.write:
        _write(SURFACE_FILE, surface)
        _write(HELP_FILE, helps)
        print(f"wrote {SURFACE_FILE} ({len(surface)} commands)")
        print(f"wrote {HELP_FILE} ({len(helps)} outputs)")
    else:
        print(json.dumps(surface, indent=2, sort_keys=True)[:2000])


if __name__ == "__main__":
    main()
