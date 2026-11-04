"""A copy of a whole lake, taken before something writes into it.

A file of a local lake is written to a temporary name and then moved over the old one, so
it is never seen half written. S3 has no such move: a sync, a prune or a publish that stops
half way (a lost connection, a laptop that goes to sleep) leaves the lake with some objects
new and some old. A backup taken just before is the way back.

```python
from pbi_cli.core.backup import backup_lake

done = backup_lake(store.root)  # to <lake>-backup-<UTC time>, next to the lake
```

The backup has the layout of a lake, so ``pbi tui --lake BACKUP`` opens it as it is (read
only, as every lake that is not the work lake). ``backup.json`` is written last: a backup
without it stopped before it was complete. Between two places of the same cloud the
objects are copied by the cloud itself, without passing through this machine. Nothing here
imports a CLI framework.
"""

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Tuple

from cloudpathlib import CloudPath

from pbi_cli.core.fsutil import inside_place
from pbi_cli.core.store import cli_version
from pbi_cli.errors import PBIError

#: Written into a backup when it is complete.
BACKUP_FILE = "backup.json"

#: What follows the name of the lake in the name of a backup made next to it.
SUFFIX = "-backup-"


@dataclass(frozen=True)
class BackupResult:
    """What a backup copied.

    :param source: the lake that was copied
    :param destination: where the copy is
    :param files: how many files were copied
    :param size: their bytes
    :param made_at: when the backup was started (aware, UTC)
    """

    source: Any
    destination: Any
    files: int
    size: int
    made_at: datetime


def default_place(root: Any, now: datetime) -> Any:
    """Where a backup goes when no place is given: next to the lake, named after it and the
    time (``s3://bucket/pbi-lake`` gives ``s3://bucket/pbi-lake-backup-20261030T224000Z``).

    :raises PBIError: for a lake at the very top of a bucket, which has nothing next to it
    """
    if not root.name or str(root.parent).endswith("://"):
        raise PBIError(
            f"The lake {root} is the whole bucket, so there is no place next to it for a "
            "backup. Say where it goes with --backup-to."
        )
    return root.parent / f"{root.name}{SUFFIX}{now:%Y%m%dT%H%M%SZ}"


def _files(root: Any) -> Iterator[Tuple[Any, Tuple[str, ...]]]:
    """Every file of a lake with its folders below the root, the ones being written left
    out (a ``.tmp`` file is a write that did not finish)."""
    for path in root.rglob("*"):
        if path.is_file() and not path.name.endswith(".tmp"):
            yield path, path.relative_to(root).parts


def _copy(source: Any, target: Any) -> None:
    if isinstance(source, CloudPath):
        # the cloud copies within itself; to a folder, the file is downloaded
        source.copy(target, force_overwrite_to_cloud=True)
    elif isinstance(target, CloudPath):
        target.upload_from(source, force_overwrite_to_cloud=True)
    else:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _is_empty(path: Any) -> bool:
    try:
        return not path.exists() or not any(True for _ in path.iterdir())
    except NotADirectoryError:
        return False


def backup_lake(
    root: Any,
    destination: Any = None,
    *,
    now: Optional[datetime] = None,
    on_file: Optional[Callable[[int], None]] = None,
) -> Optional[BackupResult]:
    """Copy every file of a lake to another place.

    :param root: the folder or cloud URL of the lake, as a path
    :param destination: where the copy goes (default: `default_place`); it must be empty
        or not exist yet
    :param now: when the backup is made (default: now)
    :param on_file: called with the number of files copied so far, for a progress line
    :return: what was copied, or ``None`` when the lake holds nothing yet
    :raises PBIError: for a destination that is the lake, inside it or around it, or that
        holds something already; or when a file cannot be copied
    """
    when = now or datetime.now(timezone.utc)
    if _is_empty(root):
        return None
    target = default_place(root, when) if destination is None else destination
    if inside_place(target, root) or inside_place(root, target):
        raise PBIError(
            f"A backup of {root} cannot go to {target}: it must be a place of its own, "
            "not the lake, inside it or around it."
        )
    if not _is_empty(target):
        raise PBIError(
            f"{target} holds something already. A backup goes to an empty place, so "
            "that nothing there is overwritten."
        )
    files = size = 0
    try:
        for path, parts in _files(root):
            _copy(path, target.joinpath(*parts))
            files += 1
            size += path.stat().st_size
            if on_file is not None:
                on_file(files)
        info = {
            "version": 1,
            "source": str(root),
            "made_at": when.isoformat(),
            "files": files,
            "bytes": size,
            "cli_version": cli_version(),
        }
        (target / BACKUP_FILE).write_text(
            json.dumps(info, indent=2) + "\n", encoding="utf-8"
        )
    except PBIError:
        raise
    except Exception as error:  # an OSError, or what the cloud client raises
        raise PBIError(
            f"The backup of {root} to {target} stopped after {files} file(s): {error}. "
            "Nothing was written into the lake; the incomplete backup has no "
            f"{BACKUP_FILE}."
        ) from error
    return BackupResult(root, target, files, size, when)
