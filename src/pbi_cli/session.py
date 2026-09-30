"""Builds the data lake and the API client from the user's settings.

The commands build their client here, so they all write to the same lake and count
against the same quota:

- the lake is the ``lake`` folder inside the cache folder (``pbi config set-cache-folder``),
  and is used unless caching is switched off (``pbi config disable-cache``);
- the quota counters are kept in ``~/.pbi_cli/quota.json``, so a second run of the command
  knows what the first one used.
"""

import warnings
from pathlib import Path
from typing import Any, Optional

from cloudpathlib import AnyPath
from urllib3.exceptions import InsecureRequestWarning

from pbi_cli.cache import LAKE_FOLDER
from pbi_cli.config import PBIConfig
from pbi_cli.core.auth import CredentialsProvider
from pbi_cli.core.client import PowerBIClient
from pbi_cli.core.ratelimit import Limiter, QuotaTracker
from pbi_cli.core.store import LakeStore


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
