"""File operations that behave the same on every platform."""

import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Union

from cloudpathlib import CloudPath

#: Windows refuses to replace a file that something else has open (a reader of the lake,
#: say), and usually only for a few milliseconds. On Windows the replace is tried again.
RETRY_REPLACE = sys.platform == "win32"

#: How often a replace is tried, and the pause before the next try (it grows with each one).
ATTEMPTS = 10
PAUSE = 0.05

PathLike = Union[str, "os.PathLike[str]"]


def replace_file(
    source: PathLike,
    target: PathLike,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Move ``source`` over ``target``, like `os.replace`.

    A file that is only written to a temporary name first and then replaced is never seen
    half written. On Windows a reader that has the target open makes `os.replace` raise
    `PermissionError` for a moment, so there it is tried again a few times before the error
    is let through. Any other error is raised at once.

    :param source: the file to move
    :param target: where it goes; an existing file is replaced
    :param sleep: waits between two tries (for tests)
    """
    attempt = 0
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            attempt += 1
            if not RETRY_REPLACE or attempt >= ATTEMPTS:
                raise
            sleep(PAUSE * attempt)


def location_of(path: Any) -> str:
    """A path or a cloud URL as text that is the same for the same place.

    A local path is made absolute and has its ``~`` and its ``..`` resolved (and on Windows
    its capitals ignored); a cloud URL loses a trailing ``/``.
    """
    if isinstance(path, CloudPath):
        return str(path).rstrip("/")
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def same_place(first: Any, second: Any) -> bool:
    """Whether two paths or cloud URLs are the same place."""
    return location_of(first) == location_of(second)


def inside_place(inner: Any, outer: Any) -> bool:
    """Whether ``inner`` is ``outer`` itself or somewhere below it."""
    below, above = location_of(inner), location_of(outer)
    separator = "/" if isinstance(outer, CloudPath) else os.sep
    return below == above or below.startswith(above.rstrip(separator) + separator)
