"""Helpers shared by the Typer command modules of pbi-cli."""

import functools
import re
from datetime import timedelta
from typing import Annotated, Any, Callable, List, NoReturn, Optional, Sequence

import typer
from typer.core import TyperGroup

from pbi_cli.errors import PBIError


class SortedGroup(TyperGroup):
    """A command group that lists its sub-commands alphabetically in ``--help``."""

    def list_commands(self, ctx: Any) -> List[str]:
        return sorted(self.commands)


def new_app(name: str, **kwargs: Any) -> typer.Typer:
    """Create a Typer app (or sub-command group) with the settings used by ``pbi``.

    The help text is plain (no Rich markup, so the usage examples in the docstrings
    are shown as written), tracebacks are plain Python tracebacks, and groups do not
    print their help when invoked without a sub-command: each group callback prints
    its own hint, as it always did.

    :param name: name of the app or group
    :param kwargs: any other :class:`typer.Typer` setting, overriding the defaults
    """
    settings: dict = {
        "name": name,
        "cls": SortedGroup,
        "rich_markup_mode": None,
        "pretty_exceptions_enable": False,
        "no_args_is_help": False,
        "add_completion": False,
    }
    settings.update(kwargs)
    return typer.Typer(**settings)


def fail(message: str, code: int = 1) -> NoReturn:
    """Print ``Error: <message>`` to stderr and exit with status ``code``."""
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code)


def command(app: typer.Typer, name: Optional[str] = None, **kwargs: Any) -> Callable:
    """Register a function as a command of ``app``; use instead of ``@app.command()``.

    A :class:`~pbi_cli.errors.PBIError` raised by the command is reported as
    ``Error: <message>`` with exit status 1. The decorated function itself is returned
    unchanged, so it can still be called directly.

    :param app: the Typer app or group to add the command to
    :param name: command name (defaults to the function name with ``-`` for ``_``)
    :param kwargs: any other setting accepted by :meth:`typer.Typer.command`
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kw: Any) -> Any:
            try:
                return func(*args, **kw)
            except PBIError as error:
                fail(str(error))

        app.command(name, **kwargs)(wrapper)
        return func

    return decorator


# -- options shared by the scan commands and by `pbi sync` -----------------------------

ScanLineage = Annotated[
    bool, typer.Option("--lineage", help="Include lineage information")
]
ScanDatasourceDetails = Annotated[
    bool, typer.Option("--datasource-details", help="Include datasource details")
]
ScanDatasetSchema = Annotated[
    bool, typer.Option("--dataset-schema", help="Include dataset schema")
]
ScanDatasetExpressions = Annotated[
    bool, typer.Option("--dataset-expressions", help="Include dataset expressions")
]
ScanArtifactUsers = Annotated[
    bool, typer.Option("--get-artifact-users", help="Include artifact users")
]


# -- small helpers for printing and parsing --------------------------------------------


def print_table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    """Print rows under a header; every column but the last is padded to its widest cell."""
    cells = [[str(cell) for cell in row] for row in rows]
    widths = [
        max(len(header[i]), *(len(row[i]) for row in cells)) for i in range(len(header))
    ]
    for line in [list(header), *cells]:
        typer.echo(
            "  ".join(
                cell if i == len(line) - 1 else cell.ljust(widths[i])
                for i, cell in enumerate(line)
            ).rstrip()
        )


def format_age(delta: timedelta) -> str:
    """How old something is, in the largest unit that keeps it short: 40 s, 5 min, 3 h, 2 d."""
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 90:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = round(seconds / 3600)
    if hours < 48:
        return f"{hours} h"
    return f"{round(seconds / 86400)} d"


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$", re.IGNORECASE)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str, option: str) -> timedelta:
    """Read a duration such as ``30m``, ``6h`` or ``2d`` (seconds, minutes, hours, days).

    :param text: what the user typed
    :param option: the option it was typed for, for the error message
    :raises typer.BadParameter: if it is not a number and a unit
    """
    match = _DURATION.match(text)
    if not match:
        raise typer.BadParameter(
            f"'{text}' is not a duration: use a number and a unit, such as 30m, 6h or 2d",
            param_hint=option,
        )
    return timedelta(seconds=float(match.group(1)) * _UNITS[match.group(2).lower()])
