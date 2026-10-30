"""Small helpers shared by the CLI tests."""

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Iterator


@contextlib.contextmanager
def isolated_filesystem(base: Path) -> Iterator[str]:
    """Run the body in a fresh, empty working directory created under ``base``.

    Typer's ``CliRunner`` has no ``isolated_filesystem``; this gives the tests the same
    behavior: a new temporary directory becomes the current directory and the previous
    one is restored afterwards.
    """
    previous = os.getcwd()
    path = tempfile.mkdtemp(dir=base)
    os.chdir(path)
    try:
        yield path
    finally:
        os.chdir(previous)
