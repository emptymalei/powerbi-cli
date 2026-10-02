"""Builds the data lake and the API client from the user's settings.

The commands build their client here, so they all write to the same lake and count
against the same quota:

- the lake is the ``lake`` folder inside the cache folder (``pbi config set-cache-folder``),
  and is used unless caching is switched off (``pbi config disable-cache``);
- the quota counters are kept in ``~/.pbi_cli/quota.json``, so a second run of the command
  knows what the first one used.

Commands that only look at a lake (the TUI, ``pbi lake``, ``pbi sync status``) can look at
another one with ``--lake``. Only the *work lake*, the lake of the cache folder, is ever
written: any other lake is opened read-only (`resolve_lake`).
"""

import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from cloudpathlib import AnyPath, CloudPath
from urllib3.exceptions import InsecureRequestWarning

from pbi_cli.cache import LAKE_FOLDER
from pbi_cli.config import PBIConfig
from pbi_cli.core.auth import CredentialsProvider
from pbi_cli.core.client import PowerBIClient
from pbi_cli.core.fsutil import same_place
from pbi_cli.core.ratelimit import Limiter, QuotaTracker
from pbi_cli.core.store import PUBLISH_FILE, LakeStore
from pbi_cli.errors import PBIError

#: The environment variable that names a lake to look at, like ``--lake``.
LAKE_ENV = "PBI_LAKE"


def quota_file() -> Path:
    """Where the quota counters are kept between runs."""
    return Path.home() / ".pbi_cli" / "quota.json"


def lake_path(config: Optional[PBIConfig] = None) -> Optional[Any]:
    """The folder of the data lake, or ``None`` when no cache folder is configured.

    This ignores ``cache_enabled``: browsing or tidying the lake stays possible while
    caching is switched off.
    """
    folder = (config or PBIConfig()).cache_folder
    return AnyPath(folder) / LAKE_FOLDER if folder else None


def open_lake(config: Optional[PBIConfig] = None) -> Optional[LakeStore]:
    """The lake the commands read from and write to, or ``None`` when caching is off.

    That is the case without a cache folder and with caching disabled.
    """
    config = config or PBIConfig()
    path = lake_path(config)
    if path is None or not config.cache_enabled:
        return None
    return LakeStore(path)


def lake_hint(config: Optional[PBIConfig] = None) -> str:
    """What to do to get a lake, in words (for error messages)."""
    config = config or PBIConfig()
    if not config.cache_folder:
        return "Set a cache folder first: pbi config set-cache-folder <folder>."
    if not config.cache_enabled:
        return "Caching is disabled: run pbi config enable-cache."
    return "The data lake is available."


def open_client(
    credentials: CredentialsProvider,
    config: Optional[PBIConfig] = None,
    *,
    verify: Any = True,
) -> PowerBIClient:
    """A client that keeps what it fetches in the data lake and counts its requests.

    :param credentials: returns the current credentials; called before each request
    :param config: the settings to read (default: the stored ones)
    :param verify: TLS verification, as for ``requests``. ``False`` also silences the
        warning ``requests`` prints about it, which is what the commands always did.
    """
    config = config or PBIConfig()
    if verify is False:
        warnings.filterwarnings("ignore", category=InsecureRequestWarning)
    return PowerBIClient(
        credentials,
        store=open_lake(config),
        limiter=Limiter(QuotaTracker(path=quota_file())),
        verify=verify,
    )


# -- choosing the lake to look at ------------------------------------------------------


@dataclass(frozen=True)
class OpenedLake:
    """A lake that a command or the TUI opened.

    :param store: the lake; it refuses writes unless it is the work lake
    :param work: whether it is the work lake (the lake of the cache folder)
    :param source: where its location came from: ``--lake``, ``PBI_LAKE`` or the cache folder
    """

    store: LakeStore
    work: bool
    source: str

    @property
    def readonly(self) -> bool:
        """Whether writes are refused (a published lake is, even when it is the work lake)."""
        return not self.store.writable


def as_path(location: str) -> Any:
    """A folder or a cloud URL as a path (``~`` is the home folder)."""
    path = AnyPath(location)
    if not isinstance(path, CloudPath):
        path = path.expanduser()  # "~/lake" means the home folder, not a folder "~"
    return path


def _holds_a_lake(path: Any) -> bool:
    """Whether a folder looks like a lake: it is published, or it has a tenant."""
    try:
        return bool(
            (path / PUBLISH_FILE).exists()
            or any(child.name.startswith("tenant=") for child in path.iterdir())
        )
    except (OSError, NotADirectoryError):
        return False


def lake_root(path: Any) -> Any:
    """The folder of the lake: the location itself, or its ``lake`` folder when the
    location is a cache folder (the lake is the ``lake`` folder inside it)."""
    if not _holds_a_lake(path) and _holds_a_lake(path / LAKE_FOLDER):
        return path / LAKE_FOLDER
    return path


def resolve_lake(
    location: Optional[str] = None,
    config: Optional[PBIConfig] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[OpenedLake]:
    """The lake to look at: the one asked for, else ``PBI_LAKE``, else the work lake.

    A location is a folder or a URL such as ``s3://bucket/folder``. It may also be a cache
    folder, whose ``lake`` folder is then the lake. Only the work lake is writable (and only
    while it is not a published lake): any other one is opened read-only.

    :param location: what ``--lake`` or the dialog of the TUI gave
    :param config: the settings (default: the stored ones)
    :param environ: the environment (default: the real one)
    :return: the lake, or ``None`` when none was asked for and no cache folder is set
    :raises PBIError: when the location cannot be read, or holds no lake
    """
    config = config or PBIConfig()
    environment = os.environ if environ is None else environ
    work = lake_path(config)
    wanted, source = None, "the cache folder"
    if location:
        wanted, source = location, "--lake"
    elif environment.get(LAKE_ENV, "").strip():
        wanted, source = environment[LAKE_ENV].strip(), LAKE_ENV
    if wanted is None:
        return None if work is None else OpenedLake(LakeStore(work), True, source)

    try:
        path = lake_root(as_path(wanted))
        found = _holds_a_lake(path)
    except Exception as error:  # missing credentials or client library, bad URL, ...
        raise PBIError(f"Cannot read the lake at {wanted}: {error}") from error
    if work is not None and same_place(path, work):
        return OpenedLake(LakeStore(path), True, source)
    if not found:
        raise PBIError(
            f"There is no data lake at {wanted}: nothing there looks like one. Check the "
            "location (and, for a bucket, your credentials)."
        )
    reason = (
        f"The lake {path} was opened with {source}, which only reads. Only the work lake "
        "(the cache folder, see `pbi config set-cache-folder`) is ever written."
    )
    return OpenedLake(LakeStore(path, readonly=True, reason=reason), False, source)
