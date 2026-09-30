"""Helpers shared by the Typer command modules of pbi-cli."""

import functools
from typing import Any, Callable, List, NoReturn, Optional

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
