import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Union,
)

import pandas as pd
import requests
import typer
import yaml
from loguru import logger
from slugify import slugify

import pbi_cli.powerbi.admin as powerbi_admin
import pbi_cli.powerbi.admin.report as powerbi_admin_report
import pbi_cli.powerbi.app as powerbi_app
import pbi_cli.powerbi.report as powerbi_report
import pbi_cli.powerbi.workspace as powerbi_workspace
from pbi_cli.auth import PBIAuth
from pbi_cli.cache import LAKE_FOLDER, CacheManager
from pbi_cli.cli_lake import lake_app
from pbi_cli.cli_support import (
    ScanArtifactUsers,
    ScanDatasetExpressions,
    ScanDatasetSchema,
    ScanDatasourceDetails,
    ScanLineage,
    command,
    fail,
    new_app,
)
from pbi_cli.cli_sync import sync_app
from pbi_cli.cli_tui import launch, should_launch, tui
from pbi_cli.config import (
    VALID_GROUPS,
    PBIConfig,
    migrate_legacy_config,
    resolve_output_path,
)
from pbi_cli.core.auth import Credentials, credentials_from_headers
from pbi_cli.core.client import FOREVER, PowerBIClient, Result, rows_of
from pbi_cli.core.registry import get_endpoint
from pbi_cli.core.scan import (
    MAX_WORKSPACES,
    ScanFlags,
    chunked,
    run_scan,
    split_scan_result,
    store_scan,
)
from pbi_cli.errors import ApiError, AuthError, PBIError, RateLimitError
from pbi_cli.powerbi.admin import Workspaces
from pbi_cli.powerbi.io import multi_group_dict_to_excel
from pbi_cli.session import lake_hint, lake_path, open_client
from pbi_cli.web import DataRetriever

try:
    import keyring
    from keyring.errors import NoKeyringError, PasswordDeleteError

    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False
    NoKeyringError = Exception
    PasswordDeleteError = Exception


logger.remove()
logger.add(sys.stderr, level="INFO", enqueue=True)


__CWD__ = os.getcwd()
# These are computed at import time for backward compatibility.
# Functions that need runtime-resolved paths use Path.home() directly.
CONFIG_DIR = Path.home() / ".pbi_cli"
# Legacy files for migration
AUTH_CONFIG_FILE = CONFIG_DIR / "auth.json"
LEGACY_PROFILES_FILE = CONFIG_DIR / "profiles.json"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
KEYRING_SERVICE = "pbi-cli"


def _get_config_dir() -> Path:
    """Return the pbi-cli config directory, resolved at call time."""
    return Path.home() / ".pbi_cli"


def _get_credentials_file() -> Path:
    """Return the credentials file path, resolved at call time."""
    return _get_config_dir() / "credentials.json"


def _get_auth_config_file() -> Path:
    """Return the legacy auth config file path, resolved at call time."""
    return _get_config_dir() / "auth.json"


def _display_table(
    data: Dict[str, Any], title: str, display_cols: Optional[list] = None
):
    """Display data as a formatted table.

    Args:
        data: Dictionary with 'value' key containing list of records
        title: Title to display above the table
        display_cols: Optional list of column names to display
    """
    if not data or "value" not in data or len(data["value"]) == 0:
        typer.echo(f"No {title.lower()} found.")
        return

    df = pd.json_normalize(data["value"])

    # Filter to display columns if specified
    if display_cols:
        available_cols = [col for col in display_cols if col in df.columns]
        if available_cols:
            df = df[available_cols]

    typer.echo("\n" + "=" * 80)
    typer.echo(f"{title}: {len(df)} record(s)")
    typer.echo("=" * 80)
    typer.echo(df.to_string(index=False))
    typer.echo("=" * 80)


def _check_keyring_availability():
    """Check if keyring is available and working"""
    if not KEYRING_AVAILABLE:
        return False
    try:
        # Test if we can use keyring
        keyring.get_password("test-service", "test-user")
        return True
    except NoKeyringError:
        return False
    except Exception:
        # Any other exception means keyring might work
        return True


#: What `_set_credential` answers when the system keyring holds the token.
IN_KEYRING = "keyring"


def _forget_keyring_entry(profile: str) -> None:
    """Remove the token of a profile from the keyring, if it has one.

    The keyring is read before the file, so an older token in it would hide a newer one that
    went to the file (a token that is too long for the Windows Credential Manager does).
    """
    try:
        keyring.delete_password(KEYRING_SERVICE, profile)
    except Exception:
        # nothing there, or no keyring at all: there is nothing to forget
        pass


def _set_credential(profile: str, token: str) -> str:
    """Set credential for a profile using keyring or fallback to file storage

    :return: where the token went: ``keyring``, or the path of the credentials file
    """
    refused = False
    if _check_keyring_availability():
        try:
            keyring.set_password(KEYRING_SERVICE, profile, token)
            return IN_KEYRING
        except NoKeyringError:
            pass
        except (OSError, Exception) as e:
            # Handle Windows Credential Manager errors (e.g., token too long)
            # and other keyring-specific errors
            logger.debug(f"Keyring error: {e}")
            refused = True

    # Fallback to file-based storage
    config_dir = _get_config_dir()
    credentials_file = _get_credentials_file()
    if refused:
        logger.warning(
            "The system keyring did not take the token (on Windows usually because it is "
            f"longer than the Credential Manager holds): storing it in {credentials_file}"
        )
        _forget_keyring_entry(profile)
    else:
        logger.warning(
            "Keyring not available, storing credentials in file. "
            "For better security, install a keyring backend (e.g., pip install keyrings.alt)"
        )
    if not config_dir.exists():
        config_dir.mkdir(parents=True, exist_ok=True)

    credentials = {}
    if credentials_file.exists():
        with open(credentials_file, "r") as fp:
            credentials = json.load(fp)

    credentials[profile] = token

    with open(credentials_file, "w") as fp:
        json.dump(credentials, fp, indent=2)

    # Set restrictive permissions on the credentials file
    credentials_file.chmod(0o600)
    return str(credentials_file)


def _get_credential(profile: str) -> Optional[str]:
    """Get credential for a profile from keyring or file storage"""
    if _check_keyring_availability():
        try:
            token = keyring.get_password(KEYRING_SERVICE, profile)
            if token is not None:
                return token
        except NoKeyringError:
            pass
        except (OSError, Exception) as e:
            # Handle Windows Credential Manager errors and other keyring errors
            logger.debug(f"Keyring error: {e}")
            pass

    # Fallback to file-based storage
    credentials_file = _get_credentials_file()
    if credentials_file.exists():
        with open(credentials_file, "r") as fp:
            credentials = json.load(fp)
            return credentials.get(profile)

    return None


def _delete_credential(profile: str):
    """Delete credential for a profile from keyring or file storage"""
    if _check_keyring_availability():
        try:
            keyring.delete_password(KEYRING_SERVICE, profile)
            return
        except (NoKeyringError, PasswordDeleteError):
            pass
        except (OSError, Exception) as e:
            # Handle Windows Credential Manager errors and other keyring errors
            logger.debug(f"Keyring error: {e}")
            pass

    # Fallback to file-based storage
    credentials_file = _get_credentials_file()
    if credentials_file.exists():
        with open(credentials_file, "r") as fp:
            credentials = json.load(fp)

        if profile in credentials:
            del credentials[profile]

            with open(credentials_file, "w") as fp:
                json.dump(credentials, fp, indent=2)

            credentials_file.chmod(0o600)


def _migrate_legacy_auth():
    """Migrate legacy auth.json to the new profile-based system"""
    pbi_config = PBIConfig()

    # First migrate from profiles.json to config.yaml if needed
    migrate_legacy_config()

    # Then migrate from auth.json if needed
    auth_config_file = _get_auth_config_file()
    if auth_config_file.exists() and not pbi_config.profiles:
        try:
            with open(auth_config_file, "r", encoding="utf-8") as fp:
                legacy_auth = json.load(fp)

            if "Authorization" in legacy_auth:
                # Extract token from "Bearer <token>"
                token = legacy_auth["Authorization"].replace("Bearer ", "")
                # Save to keyring or file
                _set_credential("default", token)
                # Update config using class properties
                pbi_config.active_profile = "default"
                pbi_config.add_profile("default", {"name": "default"})
                logger.info(
                    "Migrated legacy auth to secure storage with profile 'default'"
                )
        except Exception as e:
            logger.warning(f"Could not migrate legacy auth: {e}")


def _load_profiles() -> dict:
    """Load profiles configuration from YAML config"""
    _migrate_legacy_auth()
    pbi_config = PBIConfig()

    return {
        "active_profile": pbi_config.active_profile,
        "profiles": pbi_config.profiles,
    }


def _save_profiles(profiles_data: dict):
    """Save profiles configuration to YAML config"""
    pbi_config = PBIConfig()
    pbi_config.active_profile = profiles_data.get("active_profile")
    pbi_config.profiles = profiles_data.get("profiles", {})


def _load_group_profiles(group: str) -> dict:
    """Load profiles for a specific group from config.

    :param group: Group name (e.g. 'user' or 'admin')
    :return: dict with 'active_profile' and 'profiles' keys for the group
    """
    pbi_config = PBIConfig()
    return {
        "active_profile": pbi_config.get_group_active_profile(group),
        "profiles": pbi_config.get_group_profiles(group),
    }


def _save_group_profiles(group: str, group_data: dict):
    """Save profiles for a specific group to config.

    :param group: Group name (e.g. 'user' or 'admin')
    :param group_data: dict with 'active_profile' and 'profiles' keys
    """
    pbi_config = PBIConfig()
    for profile_name, profile_info in group_data.get("profiles", {}).items():
        pbi_config.add_profile_to_group(group, profile_name, profile_info)
    pbi_config.set_group_active_profile(group, group_data.get("active_profile"))


def _resolve_profile(profile: Optional[str] = None, group: str = "user") -> str:
    """Name of the profile to use: the given one, else the active one of *group*.

    Resolves the profile from the given *group* first.  If no profile is found in
    that group the function falls back to the legacy flat profile storage so that
    existing configurations continue to work.

    :raises AuthError: if there is no such profile
    """
    pbi_config = PBIConfig()

    # Try to resolve from the requested group first.
    resolved_profile = profile
    if resolved_profile is None:
        resolved_profile = pbi_config.get_group_active_profile(group)

    if resolved_profile is not None and pbi_config.has_profile_in_group(
        group, resolved_profile
    ):
        # Use group-based profile.
        return resolved_profile

    # Fall back to legacy flat profiles for backward compatibility.
    profiles_data = _load_profiles()
    if profile is None:
        profile = profiles_data.get("active_profile")
    if profile is None:
        raise AuthError(
            f"No active profile set for group '{group}'. "
            f"Use 'pbi auth -g {group}' to create a profile or "
            f"'pbi profile switch -g {group}' to switch profiles.",
            group=group,
        )
    if profile not in profiles_data.get("profiles", {}):
        raise AuthError(
            f"Profile '{profile}' not found in group '{group}' or flat profiles. "
            "Use 'pbi profile list' to see available profiles.",
            group=group,
        )
    return profile


def load_auth(profile: Optional[str] = None, group: str = "user") -> dict:
    """Load authentication for the specified profile or active profile.

    Resolves the auth profile from the given *group* first.  If no profile is
    found in that group the function falls back to the legacy flat profile
    storage so that existing configurations continue to work.

    :param profile: Profile name. If None, the active profile for *group* is
        used, with further fallback to the flat active-profile.
    :param group: Auth group to resolve the profile from ('user' or 'admin').
        Defaults to ``'user'``.  Pass ``'admin'`` for commands that require
        admin-level access.
    :return: dict containing ``{"Authorization": "Bearer <token>"}``
    """
    profile = _resolve_profile(profile, group)

    # Get token from keyring or file.
    token = _get_credential(profile)
    if token is None:
        raise AuthError(
            f"No credentials found for profile '{profile}'. Please re-authenticate.",
            group=group,
        )

    return {"Authorization": f"Bearer {token}"}


def _credentials_provider(
    group: str, profile: Optional[str] = None
) -> Callable[[], Credentials]:
    """What the API client asks before each request for the token of *group*.

    The client asks several times per request, and a command runs for seconds, so the
    token is looked up once (not in the settings and the keyring for every request). A
    token stored with ``pbi auth`` while the command runs is used by the next command.

    :param group: ``admin`` or ``user``
    :param profile: the profile to use instead of the active one of the group
    """
    found: List[Credentials] = []

    def provide() -> Credentials:
        if not found:
            headers = load_auth(profile=profile, group=group)
            try:
                name: Optional[str] = _resolve_profile(profile, group)
            except PBIError:
                name = None  # only used to name the profile in messages
            found.append(credentials_from_headers(headers, profile=name, group=group))
        return found[0]

    return provide


@contextmanager
def _client(group: str, profile: Optional[str] = None) -> Iterator[PowerBIClient]:
    """An API client that signs in as *group* and keeps what it fetches in the data lake.

    TLS certificates are not verified, as before (see the ``tls_verify`` follow-up).

    :param group: ``admin`` or ``user``
    :param profile: the profile to use instead of the active one of the group
    """
    with open_client(_credentials_provider(group, profile), verify=False) as client:
        yield client


def _get(
    client: PowerBIClient,
    endpoint_id: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    use_cache: bool = False,
    cache_only: bool = False,
    quiet: bool = False,
) -> Result:
    """Read one endpoint; the answer goes to the data lake (when there is one).

    Without flags the API is called. ``use_cache`` accepts any stored answer to the very
    same request (and calls the API when there is none); ``cache_only`` never calls it.

    :param client: from `_client`
    :param endpoint_id: id of the operation (see ``pbi_cli.core.registry``)
    :param params: path and query parameters of the request
    :param quiet: do not say where the data came from (for commands that print JSON)
    :raises PBIError: for a failed request, or ``cache_only`` without a stored answer
    """
    if cache_only and client.store is None:
        raise PBIError(f"--cache-only needs the data lake. {lake_hint()}")

    result = client.fetch(
        endpoint_id,
        params,
        max_age=FOREVER if use_cache else None,
        refresh=not use_cache,
        offline=cache_only,
    )

    if not quiet and result.snapshot is not None:
        version = result.snapshot.version
        if result.from_cache:
            typer.secho(
                f"Using cached data from {result.fetched_at:%Y-%m-%d %H:%M} UTC "
                f"(version: {version})",
                fg="cyan",
            )
        else:
            if use_cache:
                typer.secho(
                    "Nothing stored for this request yet, fetched from the API.",
                    fg="yellow",
                )
            typer.secho(f"Saved to the data lake (version: {version})", fg="green")
    return result


def _fetch(
    endpoint_id: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    group: str,
    use_cache: bool = False,
    cache_only: bool = False,
    quiet: bool = False,
) -> Result:
    """Read one endpoint as *group* (``user`` or ``admin``); see `_get`."""
    with _client(group) as client:
        return _get(
            client,
            endpoint_id,
            params,
            use_cache=use_cache,
            cache_only=cache_only,
            quiet=quiet,
        )


class AuthGroup(str, Enum):
    """Auth groups a profile can live in (mirrors ``pbi_cli.config.VALID_GROUPS``)."""

    user = "user"
    admin = "admin"


class FileType(str, Enum):
    """Output formats of the commands that save results to files."""

    json = "json"
    excel = "excel"


class ConvertFormat(str, Enum):
    """Target formats of ``pbi workspaces format-convert``."""

    excel = "excel"


class AppRole(str, Enum):
    """Whose view of the apps ``pbi apps list`` shows."""

    admin = "admin"
    user = "user"


class Expand(str, Enum):
    """Items that can be expanded inline when listing workspaces."""

    users = "users"
    reports = "reports"
    dashboards = "dashboards"
    datasets = "datasets"
    dataflows = "dataflows"
    workbooks = "workbooks"


def _values(choices: Iterable[Enum]) -> List[Any]:
    """Return the plain values of enum choices received from Typer."""
    return [choice.value for choice in choices]


def _positive_float(value: float) -> float:
    """Typer callback: only accept numbers greater than zero."""
    if value <= 0:
        raise typer.BadParameter("must be greater than 0")
    return value


app = new_app("pbi", add_completion=True)
profile_app = new_app("profile")
config_app = new_app("config")
cache_app = new_app("cache")
workspaces_app = new_app("workspaces")
scan_app = new_app("scan")
users_app = new_app("users")
apps_app = new_app("apps")
reports_app = new_app("reports")

app.add_typer(profile_app, name="profile")
app.add_typer(config_app, name="config")
app.add_typer(cache_app, name="cache")
app.add_typer(lake_app, name="lake")
app.add_typer(sync_app, name="sync")
app.add_typer(workspaces_app, name="workspaces")
workspaces_app.add_typer(scan_app, name="scan")
app.add_typer(users_app, name="users")
app.add_typer(apps_app, name="apps")
app.add_typer(reports_app, name="reports")


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        if should_launch():  # in a terminal, with Textual and a data lake
            try:
                launch()
            except PBIError as error:
                fail(str(error))
            return
        typer.echo("Hello {}".format(os.environ.get("USER", "")))
        typer.echo("Welcome to pbi cli. Use pbi --help for help.")


command(app, "tui")(tui)


@command(app, "version")
def version():
    """Show the current version of the pbi CLI tool."""
    from importlib.metadata import version as _version

    typer.echo(_version("pbi_cli"))


@dataclass
class StoredToken:
    """What `store_token` did.

    :param profile: the profile the token was stored for
    :param group: the group the profile is in (``None``: the flat, legacy profiles)
    :param active: whether the profile is now the active one
    :param where: where the token went: ``keyring``, or the path of the credentials file
    """

    profile: str
    group: Optional[str]
    active: bool
    where: str = IN_KEYRING


def store_token(
    bearer_token: str, profile: str = "default", group: Optional[str] = None
) -> StoredToken:
    """Store a bearer token for a profile, as ``pbi auth`` does.

    The token goes to the keyring (or to a file readable by the owner only when there is
    no keyring); the profile is added to its group, and becomes the active one of the
    group when the group has none.

    :param bearer_token: the token, with or without the ``Bearer`` prefix
    :param profile: the name of the profile
    :param group: ``user`` or ``admin``; ``None`` keeps the profile in the flat, legacy list
    """
    if bearer_token.startswith("Bearer"):
        logger.warning("Do not include the Bearer string in the beginning")
        bearer_token = bearer_token.replace("Bearer ", "")

    config_dir = _get_config_dir()
    if not config_dir.exists():
        logger.info(f"Creating config folder: {config_dir}")
        config_dir.mkdir(parents=True, exist_ok=True)

    # Store token securely (keyed by profile name)
    where = _set_credential(profile, bearer_token)

    if group is not None:
        # Store in group-based config
        pbi_config = PBIConfig()
        pbi_config.add_profile_to_group(group, profile, {"name": profile})
        # Activate this profile in the group if none is set yet
        if not pbi_config.get_group_active_profile(group):
            pbi_config.set_group_active_profile(group, profile)
        return StoredToken(
            profile,
            group,
            pbi_config.get_group_active_profile(group) == profile,
            where,
        )

    # Legacy: store in flat profiles
    profiles_data = _load_profiles()
    if "profiles" not in profiles_data:
        profiles_data["profiles"] = {}

    profiles_data["profiles"][profile] = {"name": profile}

    # Set as active profile if it's the first one or if it's 'default'
    if not profiles_data.get("active_profile") or profile == "default":
        profiles_data["active_profile"] = profile

    _save_profiles(profiles_data)
    return StoredToken(profile, None, profiles_data["active_profile"] == profile, where)


@command(app, "auth")
def auth(
    bearer_token: Annotated[
        str, typer.Option("--bearer-token", "-t", help="Bearer token")
    ],
    profile: Annotated[
        str,
        typer.Option("--profile", "-p", help="Profile name for this credential set"),
    ] = "default",
    group: Annotated[
        Optional[AuthGroup],
        typer.Option(
            "--group",
            "-g",
            help="Group to store this profile in ('user' or 'admin')",
        ),
    ] = None,
):
    """Store authentication bearer token securely

    ```
    pbi auth --bearer-token <your_bearer_token>
    ```

    or with a custom profile name:

    ```
    pbi auth -t <your_bearer_token> -p production
    ```

    or scoped to a group:

    ```
    pbi auth -t <your_bearer_token> -p admin-nlm -g admin
    pbi auth -t <your_bearer_token> -p user-nlm -g user
    ```

    :param bearer_token: Bearer token to authenticate with Power BI API
    :param profile: Profile name to associate with this credential
    :param group: Optional group ('user' or 'admin') to store the profile in
    """

    group_name = group.value if group is not None else None
    stored = store_token(bearer_token, profile, group_name)
    where = (
        "securely"
        if stored.where == IN_KEYRING
        else f"in {stored.where} (the system keyring did not hold the token)"
    )
    if stored.group is not None:
        typer.secho(
            f"✓ Credentials saved {where} for profile '{stored.profile}' in group '{stored.group}'",
            fg="green",
        )
        if stored.active:
            typer.secho(
                f"✓ Profile '{stored.profile}' is now active in group '{stored.group}'",
                fg="green",
            )
    else:
        typer.secho(
            f"✓ Credentials saved {where} for profile '{stored.profile}'", fg="green"
        )
        if stored.active:
            typer.secho(f"✓ Profile '{stored.profile}' is now active", fg="green")


@profile_app.callback(invoke_without_command=True)
def profile_group(ctx: typer.Context):
    """Manage authentication profiles"""
    if ctx.invoked_subcommand is None:
        typer.echo("Use pbi profile --help for help.")


@command(profile_app, "switch")
def switch_profile_cmd(
    profile_name: Annotated[
        Optional[str],
        typer.Argument(
            metavar="PROFILE_NAME",
            help="Profile to switch to (you are asked to choose when omitted)",
        ),
    ] = None,
    group: Annotated[
        Optional[AuthGroup],
        typer.Option(
            "--group",
            "-g",
            help="Group to switch profile in ('user' or 'admin')",
        ),
    ] = None,
):
    """Switch the active authentication profile

    ```
    pbi profile switch
    ```

    or specify a profile:

    ```
    pbi profile switch production
    ```

    or within a group:

    ```
    pbi profile switch admin-nlm -g admin
    pbi profile switch user-nlm -g user
    ```

    :param profile_name: Profile name to switch to (optional, will show interactive selection if not provided)
    :param group: Optional group ('user' or 'admin') to switch within
    """
    group_name = group.value if group is not None else None
    if group_name is not None:
        pbi_config = PBIConfig()
        group_profiles = pbi_config.get_group_profiles(group_name)
        available_profiles = tuple(group_profiles.keys())

        if not available_profiles:
            typer.secho(
                f"No profiles found in group '{group_name}'. "
                f"Use 'pbi auth -g {group_name}' to create a profile.",
                fg="yellow",
            )
            return

        if profile_name is None:
            typer.echo(f"Available profiles in group '{group_name}':")
            for idx, prof in enumerate(available_profiles, 1):
                active_marker = (
                    " (active)"
                    if prof == pbi_config.get_group_active_profile(group_name)
                    else ""
                )
                typer.echo(f"  {idx}. {prof}{active_marker}")

            choice = typer.prompt(
                "Select profile number", type=int, default=1, show_default=True
            )
            if 1 <= choice <= len(available_profiles):
                profile_name = available_profiles[choice - 1]
            else:
                typer.secho("Invalid selection", fg="red")
                return

        if profile_name not in available_profiles:
            typer.secho(
                f"Profile '{profile_name}' not found in group '{group_name}'. "
                "Use 'pbi profile list' to see available profiles.",
                fg="red",
            )
            return

        pbi_config.set_group_active_profile(group_name, profile_name)
        typer.secho(
            f"✓ Switched to profile '{profile_name}' in group '{group_name}'",
            fg="green",
        )
        return

    # Legacy: switch flat active profile
    profiles_data = _load_profiles()
    # Use tuple() instead of list() to avoid shadowing by the workspaces list command
    available_profiles = tuple(profiles_data.get("profiles", {}).keys())

    if not available_profiles:
        typer.secho(
            "No profiles found. Use 'pbi auth' to create a profile.", fg="yellow"
        )
        return

    # If no profile specified, show interactive selection
    if profile_name is None:
        typer.echo("Available profiles:")
        for idx, prof in enumerate(available_profiles, 1):
            active_marker = (
                " (active)" if prof == profiles_data.get("active_profile") else ""
            )
            typer.echo(f"  {idx}. {prof}{active_marker}")

        choice = typer.prompt(
            "Select profile number", type=int, default=1, show_default=True
        )
        if 1 <= choice <= len(available_profiles):
            profile_name = available_profiles[choice - 1]
        else:
            typer.secho("Invalid selection", fg="red")
            return

    if profile_name not in available_profiles:
        typer.secho(
            f"Profile '{profile_name}' not found. Use 'pbi profile list' to see available profiles.",
            fg="red",
        )
        return

    profiles_data["active_profile"] = profile_name
    _save_profiles(profiles_data)

    typer.secho(f"✓ Switched to profile '{profile_name}'", fg="green")


@command(profile_app, "list")
def list_auth():
    """List all stored authentication profiles

    ```
    pbi profile list
    ```
    """
    profiles_data = _load_profiles()
    profiles = profiles_data.get("profiles", {})
    active_profile = profiles_data.get("active_profile")

    pbi_config = PBIConfig()

    # Show flat (legacy) profiles
    if profiles:
        typer.echo("Stored authentication profiles (ungrouped):")
        for profile_name in profiles.keys():
            active_marker = " (active)" if profile_name == active_profile else ""
            token_exists = _get_credential(profile_name) is not None
            status = "✓" if token_exists else "✗"
            typer.echo(f"  {status} {profile_name}{active_marker}")
        typer.echo()
        typer.echo(f"Active profile: {active_profile or 'None'}")
    else:
        typer.secho(
            "No ungrouped profiles found. Use 'pbi auth' to create a profile.",
            fg="yellow",
        )

    # Show group profiles
    typer.echo()
    for group in VALID_GROUPS:
        group_profiles = pbi_config.get_group_profiles(group)
        group_active = pbi_config.get_group_active_profile(group)
        if group_profiles:
            typer.echo(f"Group '{group}':")
            for profile_name in group_profiles.keys():
                active_marker = " (active)" if profile_name == group_active else ""
                token_exists = _get_credential(profile_name) is not None
                status = "✓" if token_exists else "✗"
                typer.echo(f"  {status} {profile_name}{active_marker}")
            typer.echo(f"  Active: {group_active or 'None'}")
        else:
            typer.secho(
                f"No profiles found in group '{group}'. "
                f"Use 'pbi auth -g {group}' to create one.",
                fg="yellow",
            )

    if not profiles and not any(pbi_config.get_group_profiles(g) for g in VALID_GROUPS):
        typer.secho(
            "No profiles found. Use 'pbi auth' to create a profile.", fg="yellow"
        )


@command(profile_app, "delete")
def delete_auth(
    profile: Annotated[
        str, typer.Argument(metavar="PROFILE", help="Name of the profile to delete")
    ],
    group: Annotated[
        Optional[AuthGroup],
        typer.Option(
            "--group",
            "-g",
            help="Group to delete the profile from ('user' or 'admin')",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            prompt="Are you sure you want to delete this profile?",
            help="Confirm the action without prompting.",
        ),
    ] = False,
):
    """Delete an authentication profile

    ```
    pbi profile delete production
    ```

    or from a specific group:

    ```
    pbi profile delete admin-nlm -g admin
    ```

    :param profile: Profile name to delete
    :param group: Optional group ('user' or 'admin') to delete from
    """
    if not yes:
        raise typer.Abort()

    group_name = group.value if group is not None else None
    if group_name is not None:
        pbi_config = PBIConfig()
        if not pbi_config.has_profile_in_group(group_name, profile):
            typer.secho(
                f"Profile '{profile}' not found in group '{group_name}'. "
                "Use 'pbi profile list' to see available profiles.",
                fg="red",
            )
            return

        _delete_credential(profile)
        pbi_config.remove_profile_from_group(group_name, profile)
        typer.secho(
            f"✓ Profile '{profile}' deleted from group '{group_name}'", fg="green"
        )
        new_active = pbi_config.get_group_active_profile(group_name)
        if new_active:
            typer.secho(
                f"Active profile in group '{group_name}' is now '{new_active}'",
                fg="yellow",
            )
        return

    profiles_data = _load_profiles()

    if profile not in profiles_data.get("profiles", {}):
        typer.secho(
            f"Profile '{profile}' not found. Use 'pbi profile list' to see available profiles.",
            fg="red",
        )
        return

    # Delete credential
    _delete_credential(profile)

    # Remove from profiles
    del profiles_data["profiles"][profile]

    # If this was the active profile, clear it or switch to another
    if profiles_data.get("active_profile") == profile:
        # Use tuple() instead of list() to avoid shadowing by the workspaces list command
        remaining_profiles = tuple(profiles_data["profiles"].keys())
        profiles_data["active_profile"] = (
            remaining_profiles[0] if remaining_profiles else None
        )

    _save_profiles(profiles_data)

    typer.secho(f"✓ Profile '{profile}' deleted successfully", fg="green")
    if profiles_data.get("active_profile"):
        typer.secho(
            f"Active profile is now '{profiles_data['active_profile']}'", fg="yellow"
        )


@config_app.callback(invoke_without_command=True)
def config_group(ctx: typer.Context):
    """Manage pbi-cli configuration settings"""
    if ctx.invoked_subcommand is None:
        typer.echo("Use pbi config --help for help.")


@command(config_app, "set-output-folder")
def set_output_folder(
    folder_path: Annotated[
        str,
        typer.Argument(
            metavar="FOLDER_PATH",
            help="Folder that holds the output of commands",
        ),
    ],
):
    """Set the default parent folder for all command outputs

    ```
    pbi config set-output-folder ~/PowerBI/backups
    ```

    or on Windows:

    ```
    pbi config set-output-folder "C:\\Users\\YourName\\PowerBI\\backups"
    ```

    :param folder_path: Path to the default output folder
    """
    pbi_config = PBIConfig()
    # Strip any quotes that might have been included due to shell escaping
    folder_path_clean = folder_path.strip('"').strip("'")
    pbi_config.default_output_folder = folder_path_clean
    resolved_path = Path(folder_path_clean).expanduser().absolute()
    typer.secho(f"✓ Default output folder set to: {resolved_path}", fg="green")
    typer.secho(
        "  Commands will now use subfolders within this folder by default.", fg="blue"
    )


@command(config_app, "get-output-folder")
def get_output_folder():
    """Get the currently configured default output folder

    ```
    pbi config get-output-folder
    ```
    """
    pbi_config = PBIConfig()
    folder = pbi_config.default_output_folder
    if folder:
        typer.echo(f"Default output folder: {folder}")
    else:
        typer.secho("No default output folder configured.", fg="yellow")
        typer.echo("Use 'pbi config set-output-folder' to set one.")


@command(config_app, "show")
def show_config():
    """Show all configuration settings

    ```
    pbi config show
    ```
    """
    pbi_config = PBIConfig()

    typer.echo("Current configuration:")
    typer.echo(f"  Active profile: {pbi_config.active_profile or 'None'}")
    typer.echo(
        f"  Default output folder: {pbi_config.default_output_folder or 'Not set'}"
    )
    typer.echo(f"  Cache folder: {pbi_config.cache_folder or 'Not set'}")
    typer.echo(f"  Cache enabled: {pbi_config.cache_enabled}")
    typer.echo(f"  Profiles: {len(pbi_config.profiles)}")

    if pbi_config.profiles:
        typer.echo("\n  Available profiles (ungrouped):")
        for profile_name in pbi_config.profiles.keys():
            active = " (active)" if profile_name == pbi_config.active_profile else ""
            typer.echo(f"    - {profile_name}{active}")

    typer.echo()
    typer.echo("  Groups:")
    for group in VALID_GROUPS:
        group_profiles = pbi_config.get_group_profiles(group)
        group_active = pbi_config.get_group_active_profile(group)
        typer.echo(
            f"    {group}: {len(group_profiles)} profile(s), active='{group_active or 'None'}'"
        )


@command(config_app, "set-cache-folder")
def set_cache_folder(
    folder_path: Annotated[
        str,
        typer.Argument(
            metavar="FOLDER_PATH",
            help="Local path or cloud URL (s3://, gs://, az://) of the cache folder",
        ),
    ],
):
    """Set the cache folder for storing API call results

    The cache folder can be:
    - A local path: "/path/to/cache" or "C:\\Users\\Name\\cache"
    - A cloud path: "s3://bucket-name/cache-folder"

    ```
    pbi config set-cache-folder ~/PowerBI/cache
    ```

    or with cloud storage:

    ```
    pbi config set-cache-folder s3://my-bucket/powerbi-cache
    ```

    :param folder_path: Path to the cache folder (local or remote)
    """
    pbi_config = PBIConfig()
    # Strip any quotes that might have been included due to shell escaping
    folder_path_clean = folder_path.strip('"').strip("'")
    pbi_config.cache_folder = folder_path_clean

    # Handle cloud paths differently for display
    if folder_path_clean.startswith(("s3://", "gs://", "az://")):
        typer.secho(f"✓ Cache folder set to: {folder_path_clean}", fg="green")
    else:
        from pathlib import Path

        resolved_path = Path(folder_path_clean).expanduser().absolute()
        typer.secho(f"✓ Cache folder set to: {resolved_path}", fg="green")

    typer.secho("  API call results will be cached in this folder.", fg="blue")


@command(config_app, "get-cache-folder")
def get_cache_folder():
    """Get the currently configured cache folder

    ```
    pbi config get-cache-folder
    ```
    """
    pbi_config = PBIConfig()
    folder = pbi_config.cache_folder
    if folder:
        typer.echo(f"Cache folder: {folder}")
        typer.echo(f"Cache enabled: {pbi_config.cache_enabled}")
    else:
        typer.secho("No cache folder configured.", fg="yellow")
        typer.echo("Use 'pbi config set-cache-folder' to set one.")


@command(config_app, "enable-cache")
def enable_cache():
    """Enable caching of API call results

    ```
    pbi config enable-cache
    ```
    """
    pbi_config = PBIConfig()
    pbi_config.cache_enabled = True
    typer.secho("✓ Cache enabled", fg="green")


@command(config_app, "disable-cache")
def disable_cache():
    """Disable caching of API call results

    ```
    pbi config disable-cache
    ```
    """
    pbi_config = PBIConfig()
    pbi_config.cache_enabled = False
    typer.secho("✓ Cache disabled", fg="yellow")


@cache_app.callback(invoke_without_command=True)
def cache_group(ctx: typer.Context):
    """Manage the legacy cache (the data lake is under pbi lake)

    Commands now keep what they fetch in the data lake: browse it with `pbi lake`.
    This group only works with the older cache, one folder per key, and never touches
    the data lake.
    """
    if ctx.invoked_subcommand is None:
        typer.echo("Use pbi cache --help for help.")


@command(cache_app, "list")
def list_cache(
    cache_key: Annotated[
        Optional[str],
        typer.Option(
            "--cache-key", "-k", help="Show versions for a specific cache key"
        ),
    ] = None,
):
    """List cached data

    Lists the legacy cache; `pbi lake ls` lists the data lake.

    ```
    # List all cache keys
    pbi cache list

    # List versions for a specific key
    pbi cache list -k workspaces
    ```

    :param cache_key: Optional cache key to show versions for
    """
    pbi_config = PBIConfig()
    cache_folder = pbi_config.cache_folder

    if not cache_folder:
        typer.secho("Cache folder not configured.", fg="yellow")
        typer.echo("Use 'pbi config set-cache-folder' to set one.")
        return

    cache_manager = CacheManager(cache_folder=cache_folder)

    if cache_key:
        # List versions for specific key
        versions = cache_manager.list_versions(cache_key)
        if versions:
            typer.echo(f"Cached versions for '{cache_key}':")
            for version in versions:
                typer.echo(f"  - {version}")
        else:
            typer.secho(f"No cached versions found for '{cache_key}'", fg="yellow")
    else:
        # List all cache keys
        keys = cache_manager.list_keys()
        if keys:
            typer.echo("Cached data:")
            for key in keys:
                versions = cache_manager.list_versions(key)
                typer.echo(f"  - {key} ({len(versions)} version(s))")
        else:
            typer.secho("No cached data found.", fg="yellow")


@command(cache_app, "clear")
def clear_cache(
    cache_key: Annotated[
        Optional[str],
        typer.Option(
            "--cache-key",
            "-k",
            help="Clear specific cache key (clears all if omitted)",
        ),
    ] = None,
    version: Annotated[
        Optional[str],
        typer.Option(
            "--version",
            "-v",
            help="Clear specific version (requires --cache-key)",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            prompt="Are you sure you want to clear the cache?",
            help="Confirm the action without prompting.",
        ),
    ] = False,
):
    """Clear cached data

    Clears the legacy cache only: the data lake is always kept. To delete old versions
    from the lake use `pbi lake prune`.

    ```
    # Clear all cache
    pbi cache clear

    # Clear specific cache key
    pbi cache clear -k workspaces

    # Clear specific version
    pbi cache clear -k workspaces -v 20240101_120000
    ```

    :param cache_key: Optional cache key to clear
    :param version: Optional version to clear (requires cache_key)
    """
    if not yes:
        raise typer.Abort()

    if cache_key == LAKE_FOLDER:
        raise PBIError(
            f"'{LAKE_FOLDER}' is the data lake, not a cache key. "
            "Use `pbi lake prune` to delete old versions from it."
        )

    pbi_config = PBIConfig()
    cache_folder = pbi_config.cache_folder

    if not cache_folder:
        typer.secho("Cache folder not configured.", fg="yellow")
        typer.echo("Use 'pbi config set-cache-folder' to set one.")
        return

    if version and not cache_key:
        typer.secho("Error: --version requires --cache-key", fg="red")
        return

    cache_manager = CacheManager(cache_folder=cache_folder)
    cache_manager.clear(cache_key=cache_key, version=version)

    if cache_key and version:
        typer.secho(f"✓ Cleared {cache_key} version {version}", fg="green")
    elif cache_key:
        typer.secho(f"✓ Cleared all versions of {cache_key}", fg="green")
    else:
        lake = lake_path(pbi_config)
        kept = " (the data lake was kept)" if lake is not None and lake.exists() else ""
        typer.secho(f"✓ Cleared entire cache{kept}", fg="green")


@command(app, "export")
def export_report(
    group_id: Annotated[str, typer.Option("--group-id", "-g", help="Group ID")],
    report_id: Annotated[str, typer.Option("--report-id", "-r", help="Report ID")],
    target: Annotated[
        Optional[Path],
        typer.Option(
            "--target",
            "-t",
            help="target file (if omitted, prints info to console)",
        ),
    ] = None,
):
    """export report based on id"""
    dr = DataRetriever(session_query_configs={"headers": load_auth(), "verify": False})

    uri = f"https://api.powerbi.com/v1.0/myorg/groups/{group_id}/reports/{report_id}/Export"

    result = dr.get(uri)

    if target is None:
        # For binary export data, we can't print it directly to console
        # Instead, show information about the export
        typer.echo("\n" + "=" * 80)
        typer.echo(f"Report Export (Group: {group_id}, Report: {report_id})")
        typer.echo("=" * 80)
        typer.echo(f"Content size: {len(result.content)} bytes")
        typer.echo(f"Content type: {result.headers.get('content-type', 'unknown')}")
        typer.echo("\nUse --target option to save the export to a file.")
        typer.echo("=" * 80)
    else:
        with open(target, "wb") as fp:
            fp.write(result.content)
        typer.secho(f"✓ Export saved to {target}", fg="green")


@workspaces_app.callback(invoke_without_command=True)
def workspaces_group(ctx: typer.Context):
    """Command group for Power BI workspaces"""
    if ctx.invoked_subcommand is None:
        typer.echo("Use pbi workspaces --help for help.")


@command(workspaces_app, "list")
def workspaces_list(
    top: Annotated[int, typer.Option("--top", help="top n results")] = 1000,
    expand: Annotated[
        List[Expand], typer.Option("--expand", "-e", show_default=True)
    ] = list(Expand),
    file_type: Annotated[List[FileType], typer.Option("--file-type", "-ft")] = [
        FileType.json
    ],
    odata_filter: Annotated[
        Optional[str], typer.Option("--odata-filter", "-f", help="odata filter")
    ] = None,
    target_folder: Annotated[
        Optional[str],
        typer.Option(
            "--target-folder",
            "-tf",
            help="target folder (absolute path or subfolder within default output folder). If omitted, prints results to console as a table.",
        ),
    ] = None,
    file_name: Annotated[
        str, typer.Option("--file-name", "-n", help="file name")
    ] = "workspaces",
    use_cache: Annotated[
        bool,
        typer.Option(
            "--use-cache",
            help="Use cached data if available instead of making API call",
        ),
    ] = False,
    cache_only: Annotated[
        bool,
        typer.Option(
            "--cache-only", help="Only use cache, fail if cache not available"
        ),
    ] = False,
):
    r"""List Power BI workspaces and save them to files or print to console

    The --target-folder can be:
    - An absolute path: "C:\Users\Name\PowerBI\backups\2024-01-01"
    - A relative subfolder: "2024-01-01" (uses default output folder + this subfolder)
    - Omitted: prints results as a table to the console (no files created)

    The answer is stored in the data lake (see `pbi lake ls`) when a cache folder is
    configured. --use-cache reuses the stored answer to the same request (same --top,
    --expand and --odata-filter), whatever its age, and --cache-only never calls the API.

    ```sh
    # Print to console as a table
    pbi workspaces list

    # Using absolute path with caching
    pbi workspaces list -ft json -ft excel -tf "C:\Users\$Env:UserName\PowerBI\backups\$(Get-Date -format 'yyyy-MM-dd')" -e users -e reports -e dashboards -e datasets -e dataflows -e workbooks

    # Use cached data if available (falls back to API if not cached)
    pbi workspaces list --use-cache

    # Only use cache (fails if not cached)
    pbi workspaces list --cache-only

    # Using relative subfolder (requires default output folder to be configured)
    pbi config set-output-folder "C:\Users\$Env:UserName\PowerBI\backups"
    pbi workspaces list -ft json -ft excel -tf "$(Get-Date -format 'yyyy-MM-dd')" -e users
    ```

    !!! warning "Requires Admin"

        This command requires an admin account.

    """
    expand = tuple(_values(expand))
    file_type = tuple(_values(file_type))
    if top < 1:
        raise typer.BadParameter("must be at least 1", param_hint="--top")

    if not cache_only:  # it never calls the API
        typer.echo(f"Retrieving workspaces for: {top=}, {expand=}, {odata_filter=}")
    result = _fetch(
        "admin.groups",
        {"$top": top, "$expand": expand, "$filter": odata_filter},
        group="admin",
        use_cache=use_cache,
        cache_only=cache_only,
    ).data

    # Display or save results
    if target_folder is None:
        display_cols = [
            "id",
            "name",
            "type",
            "state",
            "isReadOnly",
            "isOnDedicatedCapacity",
        ]
        _display_table(result, "Workspaces", display_cols)
        return

    # Resolve the target folder path (handles absolute/relative paths)
    target_path = resolve_output_path(target_folder)

    # Check if path resolution failed
    if target_path is None:
        typer.secho("Error: Unable to determine output folder.", fg="red")
        typer.echo("Use 'pbi config set-output-folder' to set a default output folder,")
        typer.echo("or provide an absolute path with --target-folder.")
        raise typer.Abort()

    if not target_path.exists():
        typer.secho(f"creating folder {target_path}", fg="blue")
        target_path.mkdir(parents=True, exist_ok=True)

    if "json" in file_type:
        json_file_path = target_path / f"{file_name}.json"
        logger.info(f"Writing to {json_file_path}")
        with open(json_file_path, "w") as fp:
            json.dump(result, fp)

    if "excel" in file_type:
        excel_file_path = target_path / f"{file_name}.xlsx"
        logger.info(f"Writing to {excel_file_path}")
        flattened = Workspaces(auth={}).flatten_workspaces(result["value"])
        multi_group_dict_to_excel(flattened, excel_file_path)


@command(workspaces_app, "format-convert")
def format_convert(
    source: Annotated[
        Path, typer.Option("--source", "-s", exists=True, help="source file")
    ],
    target: Annotated[Path, typer.Option("--target", "-t", help="target file")],
    format: Annotated[
        ConvertFormat, typer.Option("--format", help="format of the file")
    ] = ConvertFormat.excel,
):
    """Convert output format of workspaces list
    (`pbi workspaces list`)
    from json to excel.

    ```sh
    pbi workspaces format-convert -s "workspaces.json" -t "workspaces.xlsx"
    ```
    """
    format = format.value
    workspaces = Workspaces(auth={}, verify=False)

    typer.echo(f"Converting to {format=}: {source=} -> {target}")

    with open(source, "r") as fp:
        workspaces_data = json.load(fp)

    flattened = workspaces.flatten_workspaces(workspaces_data["value"])

    multi_group_dict_to_excel(flattened, target)


@command(workspaces_app, "report-users")
def report_users(
    source: Annotated[
        Path,
        typer.Option(
            "--source",
            "-s",
            exists=True,
            help="source json/excel file that contains all workspace information",
        ),
    ],
    target_folder: Annotated[
        Optional[str],
        typer.Option(
            "--target-folder",
            "-t",
            help=(
                "target folder (absolute path or subfolder within default output folder). "
                "If omitted, prints results to console as a table. "
                "Do not include the trailing (back)slash"
            ),
        ),
    ] = None,
    file_type: Annotated[
        List[FileType],
        typer.Option("--file-type", "-ft", help="file type to save the results as"),
    ] = [FileType.json, FileType.excel],
    wait_interval: Annotated[
        int,
        typer.Option(
            "--wait-interval",
            "-wi",
            help="number of seconds to wait between requests",
        ),
    ] = 3,
    file_name: Annotated[
        str,
        typer.Option("--file-name", "-n", help="file name without extension"),
    ] = "workspaces_reports_users",
    workspace_name: Annotated[
        Optional[List[str]],
        typer.Option("--workspace-name", "-wn", help="workspace names to download"),
    ] = None,
):
    r"""
    Augment Power BI Workspace data from a source file
    and save to target file together with report users

    The source `-s` should be the excel file exported from the command
    `pbi workspaces list`

    ```sh
    # Print to console
    pbi workspaces report-users -s "workspaces.xlsx" -wn $w -wi 5

    # Save to folder
    pbi workspaces report-users -s "workspaces.xlsx" -t "$(Get-Date -format 'yyyy-MM-dd')" -wn $w -n $w -wi 5
    ```

    Combined with PowerShell or bash, we can automatatically
    use different workspace names for the reports.

    Here is an example using PowerShell.

    ```powershell
    $workspaces = "AA", "BB"
    foreach ($w in $workspaces) {
        Write-Host "Backing up for: $w"
        pbi workspaces report-users -s "C:\Users\$Env:UserName\PowerBI\backups\workspaces.xlsx" --target-folder "C:\Users\$Env:UserName\PowerBI\backups\$(Get-Date -format 'yyyy-MM-dd')" -wn $w -n $w -wi 5
    }
    # Wait for 300 seconds (5 minutes) before the next loop
    Start-Sleep -Seconds 300
    ```
    """
    file_type = tuple(_values(file_type))
    # Like click, an omitted -wn means "no workspace names", not "all workspaces".
    workspace_name = tuple(workspace_name or ())

    typer.secho("getting report user details requires admin token")

    pbi_workspaces = powerbi_workspace.Workspaces(
        auth=load_auth(group="admin"), verify=False, cache_file=source
    )

    report_users = pbi_workspaces.report_users(
        workspace_types=["Workspace"],
        workspace_name=workspace_name,
        wait_interval=wait_interval,
    )

    # If no target folder provided, print to console as a table
    if target_folder is None:
        if report_users and len(report_users) > 0:
            try:
                # Flatten the structure for better table display
                all_reports = []
                for workspace_data in report_users:
                    workspace_name = workspace_data.get("name", "Unknown")
                    for report in workspace_data.get("reports", []):
                        report_info = {
                            "workspace": workspace_name,
                            "report_name": report.get("name", ""),
                            "report_id": report.get("id", ""),
                            **{
                                k: v
                                for k, v in report.items()
                                if k not in ["name", "id"]
                            },
                        }
                        all_reports.append(report_info)

                if all_reports:
                    df = pd.DataFrame(all_reports)
                    typer.echo("\n" + "=" * 80)
                    typer.echo(f"Found {len(all_reports)} report(s) across workspaces")
                    typer.echo("=" * 80)
                    typer.echo(df.to_string(index=False))
                    typer.echo("=" * 80)
                else:
                    typer.echo("No reports found.")
            except Exception as e:
                # Fallback to JSON if table formatting fails
                typer.echo(json.dumps(report_users, indent=4))
        else:
            typer.echo("No report users data found.")
        return

    # Resolve the target folder path (handles absolute/relative paths)
    target_path = resolve_output_path(target_folder)

    # Check if path resolution failed
    if target_path is None:
        typer.secho("Error: Unable to determine output folder.", fg="red")
        typer.echo("Use 'pbi config set-output-folder' to set a default output folder,")
        typer.echo("or provide an absolute path with --target-folder.")
        raise typer.Abort()

    if not target_path.exists():
        typer.secho(f"creating folder {target_path}", fg="blue")
        target_path.mkdir(parents=True, exist_ok=True)

    typer.secho(f"Writing results to the folder {target_path}")
    if "json" in file_type:
        json_file_path = target_path / f"{file_name}.json"
        logger.info(f"Writing json file to {json_file_path}...")
        with open(json_file_path, "w") as fp:
            json.dump(report_users, fp)
    if "excel" in file_type:
        excel_file_path = target_path / f"{file_name}.xlsx"
        logger.info(f"Writing excel file to {excel_file_path}...")

        multi_group_dict_to_excel(
            pbi_workspaces.flatten_workspaces_reports_users(report_users),
            excel_file_path,
        )


#######################
# Users Command Group
#######################


@users_app.callback(invoke_without_command=True)
def users_group(ctx: typer.Context):
    """Command group for Power BI users"""
    if ctx.invoked_subcommand is None:
        typer.echo("Use pbi users --help.")


@command(users_app, "user-access")
def user_access(
    user_id: Annotated[str, typer.Option("--user-id", "-u", help="user id")],
    target_folder: Annotated[
        Optional[str],
        typer.Option(
            "--target-folder",
            "-tf",
            help="target folder (absolute path or subfolder within default output folder). If omitted, prints results to console as a table.",
        ),
    ] = None,
    file_types: Annotated[List[FileType], typer.Option("--file-types", "-ft")] = [
        FileType.json
    ],
    file_name: Annotated[
        Optional[str],
        typer.Option("--file-name", "-n", help="file name without extension"),
    ] = None,
    use_cache: Annotated[
        bool,
        typer.Option(
            "--use-cache",
            help="Use cached data if available instead of making API call",
        ),
    ] = False,
    cache_only: Annotated[
        bool,
        typer.Option(
            "--cache-only", help="Only use cache, fail if cache not available"
        ),
    ] = False,
):
    """Get user access information from Power BI API

    Reads every page of the items the user has access to. The answer is stored in the
    data lake (see `pbi lake ls`) when a cache folder is configured; --use-cache reuses
    the stored answer and --cache-only never calls the API.

    !!! warning "Requires Admin"

        This command requires an admin account.

    """
    file_types = tuple(_values(file_types))
    if file_name is None:
        file_name = slugify(user_id)

    answer = _fetch(
        "admin.users.artifact_access",
        {"userId": user_id},
        group="admin",
        use_cache=use_cache,
        cache_only=cache_only,
    )
    result = {
        "artifacts": rows_of(get_endpoint("admin.users.artifact_access"), answer.data)
    }

    # Display or save results
    if target_folder is None:
        logger.info(f"No target folder provided, printing to console...")
        if isinstance(result, dict):
            try:
                df = pd.json_normalize(result)
                typer.echo("\n" + "=" * 80)
                typer.echo(f"User Access Information for: {user_id}")
                typer.echo("=" * 80)
                typer.echo(df.to_string(index=False))
                typer.echo("=" * 80)
            except Exception:
                typer.echo(json.dumps(result, indent=4))
        else:
            typer.echo(json.dumps(result, indent=4))
        return

    # Resolve the target folder path
    target_path = resolve_output_path(target_folder)

    if target_path is None:
        typer.secho("Error: Unable to determine output folder.", fg="red")
        typer.echo("Use 'pbi config set-output-folder' to set a default output folder,")
        typer.echo("or provide an absolute path with --target-folder.")
        raise typer.Abort()

    if not target_path.exists():
        typer.secho(f"creating folder {target_path}", fg="blue")
        target_path.mkdir(parents=True, exist_ok=True)

    if "json" in file_types:
        json_file_path = target_path / f"{file_name}.json"
        logger.info(f"Writing json file to {json_file_path}...")
        with open(json_file_path, "w") as fp:
            json.dump(result, fp)
    if "excel" in file_types:
        excel_file_path = target_path / f"{file_name}.xlsx"
        logger.info(f"Writing excel file to {excel_file_path}...")
        df = pd.json_normalize(result)
        df.to_excel(excel_file_path)


@apps_app.callback(invoke_without_command=True)
def apps_group(ctx: typer.Context):
    """Power BI Apps Command Group"""
    if ctx.invoked_subcommand is None:
        typer.echo("Use pbi apps --help.")


@command(apps_app, "list")
def apps_list(
    target_folder: Annotated[
        Optional[str],
        typer.Option(
            "--target-folder",
            "-tf",
            help="target folder (absolute path or subfolder within default output folder). If omitted, prints results to console as a table.",
        ),
    ] = None,
    file_type: Annotated[List[FileType], typer.Option("--file-type", "-ft")] = [
        FileType.json
    ],
    role: Annotated[AppRole, typer.Option("--role", "-r")] = AppRole.user,
    file_name: Annotated[
        str, typer.Option("--file-name", "-n", help="file name")
    ] = "apps",
    use_cache: Annotated[
        bool,
        typer.Option(
            "--use-cache",
            help="Use cached data if available instead of making API call",
        ),
    ] = False,
    cache_only: Annotated[
        bool,
        typer.Option(
            "--cache-only", help="Only use cache, fail if cache not available"
        ),
    ] = False,
):
    """List Power BI Apps and save them to files or print to console

    With --role admin all apps of the tenant are listed (this requires an admin account),
    with --role user the apps the signed-in user has installed. The answer is stored in
    the data lake (see `pbi lake ls`) when a cache folder is configured; --use-cache
    reuses the stored answer and --cache-only never calls the API.
    """
    role = role.value
    file_type = tuple(_values(file_type))

    if not cache_only:  # it never calls the API
        typer.echo(f"Listing Apps as {role}")
    result = _fetch(
        "user.apps" if role == "user" else "admin.apps",
        group=role,
        use_cache=use_cache,
        cache_only=cache_only,
    ).data

    # Display or save results
    if target_folder is None:
        display_cols = ["id", "name", "description", "publishedBy", "lastUpdate"]
        _display_table(result, f"Apps ({role})", display_cols)
        return

    # Resolve the target folder path
    target_path = resolve_output_path(target_folder)

    # Check if path resolution failed
    if target_path is None:
        typer.secho("Error: Unable to determine output folder.", fg="red")
        typer.echo("Use 'pbi config set-output-folder' to set a default output folder,")
        typer.echo("or provide an absolute path with --target-folder.")
        raise typer.Abort()

    if not target_path.exists():
        typer.secho(f"creating folder {target_path}", fg="blue")
        target_path.mkdir(parents=True, exist_ok=True)

    if "json" in file_type:
        json_file_path = target_path / f"{file_name}.json"
        logger.info(f"Writing json file to {json_file_path}")
        with open(json_file_path, "w") as fp:
            json.dump(result, fp)

    if "excel" in file_type:
        excel_file_path = target_path / f"{file_name}.xlsx"
        logger.info(f"Writing excel file to {excel_file_path}")
        df = pd.json_normalize(result["value"])
        df.to_excel(excel_file_path)


@command(apps_app, "app")
def app_info(
    app_id: Annotated[str, typer.Option("--app-id", "-a", help="app id")],
    target: Annotated[
        Optional[Path],
        typer.Option(
            "--target", "-t", help="target file (if omitted, prints to console)"
        ),
    ] = None,
    file_type: Annotated[FileType, typer.Option("--file-type", "-ft")] = FileType.json,
):
    """Retrieve information about a specific Power BI App"""
    file_type = file_type.value
    typer.echo(f"Investigating {app_id}")

    a_app = powerbi_app.App(auth=load_auth(), verify=False, app_id=app_id)
    app_data = a_app()

    if target is None:
        # Print to console
        typer.echo("\n" + "=" * 80)
        typer.echo(f"App: {app_data.get('name', 'N/A')} (ID: {app_id})")
        typer.echo("=" * 80)
        typer.echo(json.dumps(app_data, indent=2))
        typer.echo("=" * 80)
    else:
        # Save to file
        if file_type == "json":
            with open(target, "w") as fp:
                json.dump(app_data, fp)
            typer.secho(f"✓ Saved to {target}", fg="green")
        elif file_type == "excel":
            app_data_flattened = a_app.flatten_app(app_data)
            multi_group_dict_to_excel(app_data_flattened, target)
            typer.secho(f"✓ Saved to {target}", fg="green")


@command(apps_app, "augment")
def augment(
    source: Annotated[
        Path, typer.Option("--source", "-s", exists=True, help="source file")
    ],
    target: Annotated[Path, typer.Option("--target", "-t", help="target file")],
    file_type: Annotated[FileType, typer.Option("--file-type", "-ft")] = FileType.json,
):
    """Augment Power BI Apps data from a source file and save to target file"""
    file_type = file_type.value
    if file_type == "excel":
        if target.suffix:
            raise typer.BadParameter(
                "Use a folder (a path without a file extension) as target for excel output",
                param_hint="--target",
            )
        else:
            typer.secho(f"creating folder {target}", fg="blue")
            target.mkdir(parents=True, exist_ok=True)

    pbi_apps = powerbi_app.Apps(auth=load_auth(), verify=False, cache_file=source)

    apps_data = []
    for a in pbi_apps.apps:
        try:
            apps_data.append(a())
        except ValueError as e:
            typer.secho(f"Cannot download {a.app_info}", fg="red")

    if file_type == "json":
        with open(target, "w") as fp:
            json.dump(apps_data, fp)
    elif file_type == "excel":
        for a_data in apps_data:
            a_id = a_data.get("id")
            a_name = a_data.get("name")
            a_data_flattened = pbi_apps.apps[0].flatten_app(a_data)
            multi_group_dict_to_excel(
                a_data_flattened, target / f"{a_name}_{a_id}.xlsx"
            )


@reports_app.callback(invoke_without_command=True)
def reports_group(ctx: typer.Context):
    """Reports Command Group"""
    if ctx.invoked_subcommand is None:
        typer.echo("Use pbi reports --help.")


@command(reports_app, "users")
def reports_users(
    source: Annotated[
        Path, typer.Option("--source", "-s", exists=True, help="source file")
    ],
    target: Annotated[Path, typer.Option("--target", "-t", help="target file")],
    file_type: Annotated[FileType, typer.Option("--file-type", "-ft")] = FileType.json,
):
    """Augment Power BI Apps data from a source file and save to target file together with report users"""
    file_type = file_type.value

    typer.secho("getting report user details requires admin token")

    if file_type == "excel":
        if target.suffix:
            raise typer.BadParameter(
                "Use a folder (a path without a file extension) as target for excel output",
                param_hint="--target",
            )
        else:
            typer.secho(f"creating folder {target}", fg="blue")
            target.mkdir(parents=True, exist_ok=True)

    pbi_apps = powerbi_app.Apps(auth=load_auth(), verify=False, cache_file=source)

    apps_data = []
    for a in pbi_apps.apps:
        try:
            apps_data.append(a())
        except ValueError as e:
            typer.secho(f"Can not download {a.app_info}", fg="red")

    updated_apps_data = []
    for a in apps_data:
        report_data = []
        failed_id = []
        for r in a.get("reports", []):
            report_id = r.get("id")
            try:
                logger.debug(f"Retrieving user info for {r['name']}, {report_id}")
                r_data = powerbi_admin_report.ReportUsers(
                    auth=load_auth(), report_id=report_id, verify=False
                ).users
                r_data = {**r_data, **r}
                report_data.append(r_data)
            except ValueError as e:
                failed_id.append(report_id)
                logger.warning(f"Failed to download {r['name']}, {report_id}\n{e}")
        a["reports"] = report_data
        updated_apps_data.append(a)

    if file_type == "json":
        with open(target, "w") as fp:
            json.dump(updated_apps_data, fp)
    elif file_type == "excel":
        for a_data in updated_apps_data:
            a_id = a_data.get("id")
            a_name = a_data.get("name")
            a_data_flattened = pbi_apps.apps[0].flatten_app(a_data)
            multi_group_dict_to_excel(
                a_data_flattened, target / f"{a_name}_{a_id}_report_users.xlsx"
            )


@command(reports_app, "export")
def reports_export(
    group_id: Annotated[str, typer.Option("--group-id", "-g", help="Group ID")],
    report_id: Annotated[str, typer.Option("--report-id", "-r", help="Report ID")],
    target: Annotated[
        Optional[Path],
        typer.Option(
            "--target",
            "-t",
            help="target file (if omitted, prints info to console)",
        ),
    ] = None,
):
    """Export report as file"""

    pbi_report = powerbi_report.Report(
        auth=load_auth(), verify=False, report_id=report_id, group_id=group_id
    )

    result = pbi_report.export()

    if target is None:
        # For binary export data, we can't print it directly to console
        # Instead, show information about the export
        typer.echo("\n" + "=" * 80)
        typer.echo(f"Report Export (Group: {group_id}, Report: {report_id})")
        typer.echo("=" * 80)
        typer.echo(f"Content size: {len(result)} bytes")
        typer.echo("\nUse --target option to save the export to a file.")
        typer.echo("=" * 80)
    else:
        with open(target, "wb") as fp:
            fp.write(result)
        typer.secho(f"✓ Export saved to {target}", fg="green")


def _all_report_pages(group_id: str) -> List[Dict[str, Any]]:
    """The pages of every report of a workspace, each with the id and name of its report.

    A report whose pages cannot be read (no access to it, deleted in the meantime) is
    skipped and an error is logged. A problem that affects every request, an expired
    token or throttling, stops the command instead.
    """
    with _client("user") as client:
        reports = _get(client, "user.group_reports", {"groupId": group_id}, quiet=True)
        results = []
        for report in rows_of(get_endpoint("user.group_reports"), reports.data):
            report_id, report_name = report.get("id"), report.get("name")
            try:
                pages = _get(
                    client,
                    "user.report_pages",
                    {"groupId": group_id, "reportId": report_id},
                    quiet=True,
                )
            except RateLimitError:
                raise
            except ApiError as error:
                logger.error(
                    f"Failed to retrieve pages for report '{report_name}' "
                    f"(id={report_id}): {error}"
                )
                continue
            results.append(
                {
                    "report_id": report_id,
                    "report_name": report_name,
                    "pages": pages.data,
                }
            )
    return results


@command(reports_app, "list")
def reports_list_group(
    group_id: Annotated[
        str, typer.Option("--group-id", "-g", help="Group (workspace) ID")
    ],
    target: Annotated[
        Optional[Path],
        typer.Option(
            "--target",
            "-t",
            help="target file (if omitted, prints info to console)",
        ),
    ] = None,
):
    """List all reports in a workspace group.

    Retrieves the full list of reports from the specified workspace group and
    either prints the result to the console or saves it to a JSON file. The answer
    is also stored in the data lake (see `pbi lake`) when a cache folder is configured.
    """
    result = _fetch(
        "user.group_reports", {"groupId": group_id}, group="user", quiet=True
    ).data

    if target is None:
        typer.echo(json.dumps(result, indent=2))
    else:
        with open(target, "w") as fp:
            json.dump(result, fp, indent=2)
        typer.secho(f"✓ Reports list saved to {target}", fg="green")


@command(reports_app, "pages")
def reports_pages(
    group_id: Annotated[
        str, typer.Option("--group-id", "-g", help="Group (workspace) ID")
    ],
    report_id: Annotated[
        Optional[str],
        typer.Option(
            "--report-id",
            "-r",
            help="Report ID. If omitted, pages for all reports in the group are returned.",
        ),
    ] = None,
    target: Annotated[
        Optional[Path],
        typer.Option(
            "--target",
            "-t",
            help="target file (if omitted, prints info to console)",
        ),
    ] = None,
):
    """Get pages of a report (or all reports) in a workspace group.

    When ``--report-id`` is provided, retrieves the pages for that specific
    report. When omitted, iterates over every report in the group and returns
    the combined pages for all of them. The answers are also stored in the data
    lake (see `pbi lake`) when a cache folder is configured.

    Examples::

        pbi reports pages -g GROUP_ID -r REPORT_ID

        pbi reports pages -g GROUP_ID
    """
    if report_id is not None:
        result = _fetch(
            "user.report_pages",
            {"groupId": group_id, "reportId": report_id},
            group="user",
            quiet=True,
        ).data
    else:
        result = _all_report_pages(group_id)

    if target is None:
        typer.echo(json.dumps(result, indent=2))
    else:
        with open(target, "w") as fp:
            json.dump(result, fp, indent=2)
        typer.secho(f"✓ Report pages saved to {target}", fg="green")


def _run_scan(
    workspace_info: "powerbi_admin.WorkspaceInfo",
    workspace_ids: list,
    lineage: bool,
    datasource_details: bool,
    dataset_schema: bool,
    dataset_expressions: bool,
    get_artifact_users: bool,
    interval: float,
    timeout: float,
) -> dict:
    """Initiate a scan, poll its status until it succeeds/fails/times out, and
    return the scan result.

    Shared by ``pbi workspaces scan get`` and ``pbi workspaces scan batch``.
    """
    import time

    typer.echo(f"Initiating scan for {workspace_ids}…")
    try:
        scan_response = workspace_info.initiate_scan(
            workspace_ids=workspace_ids,
            lineage=lineage,
            datasource_details=datasource_details,
            dataset_schema=dataset_schema,
            dataset_expressions=dataset_expressions,
            get_artifact_users=get_artifact_users,
        )
    except (ValueError, requests.exceptions.RequestException) as e:
        raise PBIError(str(e)) from e
    scan_id = scan_response.get("id")
    if not scan_id:
        raise PBIError(f"Unexpected initiate response: {scan_response}")
    typer.echo(f"Scan started (id={scan_id}). Waiting for status…")

    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            status_response = workspace_info.get_scan_status(scan_id=scan_id)
        except (ValueError, requests.exceptions.RequestException) as e:
            raise PBIError(str(e)) from e
        status = status_response.get("status")

        if status == "Succeeded":
            break

        if status == "Failed":
            raise PBIError(f"Scan {scan_id} failed: {status_response.get('error')}")

        if time.monotonic() >= deadline:
            raise PBIError(
                f"Scan {scan_id} did not complete within {timeout}s "
                f"(last status: {status})."
            )
        remaining = deadline - time.monotonic()
        sleep_time = min(interval, remaining)
        typer.echo(
            f"  Attempt {attempt}: scan status is '{status}', retrying in {sleep_time:.0f}s…"
        )
        time.sleep(sleep_time)

    try:
        return workspace_info.get_scan_result(scan_id=scan_id)
    except (ValueError, requests.exceptions.RequestException) as e:
        raise PBIError(str(e)) from e


def _normalize_workspace_entries(entries: Iterable) -> list:
    """Normalize a config file's ``workspace_ids`` list into ``{"id", "name"}`` dicts.

    Each entry may be a plain workspace ID string, or a mapping with an ``id``
    and an optional ``name`` (used to make output filenames readable):

    ```yaml
    workspace_ids:
      - <workspace-id-1>
      - id: <workspace-id-2>
        name: Finance
    ```
    """
    if not isinstance(entries, list):
        raise PBIError("'workspace_ids' must be a YAML list.")
    normalized = []
    for entry in entries:
        if isinstance(entry, str):
            normalized.append({"id": entry, "name": None})
        elif isinstance(entry, dict):
            workspace_id = entry.get("id")
            if not workspace_id:
                raise PBIError(f"Workspace entry missing 'id': {entry}")
            normalized.append({"id": workspace_id, "name": entry.get("name")})
        else:
            raise PBIError(f"Invalid workspace entry: {entry!r}")
    return normalized


def _parse_yaml_bool(raw_config: dict, key: str, default: bool = False) -> bool:
    """Return a boolean config value or raise for invalid YAML types."""
    value = raw_config.get(key, default)
    if isinstance(value, bool):
        return value
    raise PBIError(f"'{key}' must be a boolean value.")


@scan_app.callback(invoke_without_command=True)
def workspaces_scan(ctx: typer.Context):
    """Command group for workspace scan operations.

    !!! warning "Requires Admin"

        All commands in this group require an admin account.

    """
    if ctx.invoked_subcommand is None:
        typer.echo("Use pbi workspaces scan --help for help.")


# Arguments and options shared by the scan commands.
ScanWorkspaceIds = Annotated[
    List[str],
    typer.Argument(metavar="WORKSPACE_IDS", help="One or more workspace IDs"),
]


@command(scan_app, "initiate")
def scan_initiate(
    workspace_ids: ScanWorkspaceIds,
    lineage: ScanLineage = False,
    datasource_details: ScanDatasourceDetails = False,
    dataset_schema: ScanDatasetSchema = False,
    dataset_expressions: ScanDatasetExpressions = False,
    get_artifact_users: ScanArtifactUsers = False,
):
    """Initiate a workspace scan for one or more WORKSPACE_IDS.

    Returns the scan ID which can be used with ``pbi workspaces scan result``.

    ```sh
    pbi workspaces scan initiate <workspace-id> <workspace-id>

    pbi workspaces scan initiate <workspace-id> --lineage --datasource-details
    ```

    !!! warning "Requires Admin"

        This command requires an admin account.

    """
    workspace_info = powerbi_admin.WorkspaceInfo(
        auth=load_auth(group="admin"), verify=False
    )
    result = workspace_info.initiate_scan(
        workspace_ids=[*workspace_ids],
        lineage=lineage,
        datasource_details=datasource_details,
        dataset_schema=dataset_schema,
        dataset_expressions=dataset_expressions,
        get_artifact_users=get_artifact_users,
    )
    typer.echo(json.dumps(result, indent=2))


@command(scan_app, "result")
def scan_result(
    scan_id: Annotated[
        str,
        typer.Argument(
            metavar="SCAN_ID",
            help="Scan ID returned by `pbi workspaces scan initiate`",
        ),
    ],
    target: Annotated[
        Optional[Path],
        typer.Option(
            "--target",
            "-t",
            help="Target file to save scan results (if omitted, prints to console)",
        ),
    ] = None,
):
    """Get scan results for SCAN_ID.

    Retrieves the scan results for the given scan ID returned by
    ``pbi workspaces scan initiate``.

    ```sh
    pbi workspaces scan result <scan-id>

    pbi workspaces scan result <scan-id> -t results.json
    ```

    !!! warning "Requires Admin"

        This command requires an admin account.

    """
    workspace_info = powerbi_admin.WorkspaceInfo(
        auth=load_auth(group="admin"), verify=False
    )
    result = workspace_info.get_scan_result(scan_id=scan_id)

    if target is None:
        typer.echo(json.dumps(result, indent=2))
    else:
        with open(target, "w") as fp:
            json.dump(result, fp, indent=2)
        typer.secho(f"✓ Scan results saved to {target}", fg="green")


@command(scan_app, "status")
def scan_status(
    scan_id: Annotated[
        str,
        typer.Argument(
            metavar="SCAN_ID",
            help="Scan ID returned by `pbi workspaces scan initiate`",
        ),
    ],
):
    """Get the scan status for SCAN_ID.

    Retrieves the current status (e.g. ``NotStarted``, ``Running``,
    ``Succeeded``, ``Failed``) for the given scan ID returned by
    ``pbi workspaces scan initiate``. Check this before calling
    ``pbi workspaces scan result``.

    ```sh
    pbi workspaces scan status <scan-id>
    ```

    !!! warning "Requires Admin"

        This command requires an admin account.

    """
    workspace_info = powerbi_admin.WorkspaceInfo(
        auth=load_auth(group="admin"), verify=False
    )
    try:
        result = workspace_info.get_scan_status(scan_id=scan_id)
    except (ValueError, requests.exceptions.RequestException) as e:
        raise PBIError(str(e)) from e
    typer.echo(json.dumps(result, indent=2))


@command(scan_app, "get")
def scan_get(
    ctx: typer.Context,
    workspace_ids: ScanWorkspaceIds,
    lineage: ScanLineage = False,
    datasource_details: ScanDatasourceDetails = False,
    dataset_schema: ScanDatasetSchema = False,
    dataset_expressions: ScanDatasetExpressions = False,
    get_artifact_users: ScanArtifactUsers = False,
    interval: Annotated[
        float,
        typer.Option(
            "--interval",
            callback=_positive_float,
            help="Seconds to wait between status checks",
        ),
    ] = 5.0,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            callback=_positive_float,
            help="Maximum seconds to wait for scan completion",
        ),
    ] = 300.0,
    target: Annotated[
        Optional[Path],
        typer.Option(
            "--target",
            "-t",
            help="Target file to save scan results (if omitted, prints to console)",
        ),
    ] = None,
    target_folder: Annotated[
        Optional[str],
        typer.Option(
            "--target-folder",
            "-tf",
            help=(
                "Target folder to save scan results (absolute path or subfolder within "
                "the default output folder). Only supported when scanning a single "
                "workspace ID, and saves the result as <workspace_id>.json. Handy "
                "when looping over several workspaces. Mutually exclusive with "
                "--target."
            ),
        ),
    ] = None,
):
    """Initiate a scan for WORKSPACE_IDS, wait for completion, and return results.

    Combines ``pbi workspaces scan initiate``, ``pbi workspaces scan status``, and
    ``pbi workspaces scan result`` into a single step: starts the scan, polls the
    scan status until it succeeds, fails, or times out, then prints or saves the
    results.

    ```sh
    pbi workspaces scan get <workspace-id>

    pbi workspaces scan get <workspace-id> <workspace-id> --lineage -t results.json

    # Loop over workspaces, saving each result as <workspace-id>.json
    for w in ws-1 ws-2 ws-3; do
        pbi workspaces scan get "$w" -tf scan_results
    done
    ```

    !!! warning "Requires Admin"

        This command requires an admin account.

    """
    if target is not None and target_folder is not None:
        ctx.fail("Use either --target or --target-folder, not both.")
    if target_folder is not None and len(workspace_ids) != 1:
        ctx.fail(
            "--target-folder only supports a single workspace ID. "
            "Use --target for multi-workspace scans."
        )

    workspace_info = powerbi_admin.WorkspaceInfo(
        auth=load_auth(group="admin"), verify=False
    )

    result = _run_scan(
        workspace_info,
        workspace_ids=[*workspace_ids],
        lineage=lineage,
        datasource_details=datasource_details,
        dataset_schema=dataset_schema,
        dataset_expressions=dataset_expressions,
        get_artifact_users=get_artifact_users,
        interval=interval,
        timeout=timeout,
    )

    if target_folder is not None:
        target_path = resolve_output_path(target_folder)
        if target_path is None:
            typer.secho("Error: Unable to determine output folder.", fg="red")
            typer.echo(
                "Use 'pbi config set-output-folder' to set a default output folder,"
            )
            typer.echo("or provide an absolute path with --target-folder.")
            raise typer.Abort()

        if not target_path.exists():
            typer.secho(f"creating folder {target_path}", fg="blue")
            target_path.mkdir(parents=True, exist_ok=True)

        output_file = target_path / f"{workspace_ids[0]}.json"
        with open(output_file, "w", encoding="utf-8") as fp:
            json.dump(result, fp, indent=2)
        typer.secho(f"✓ Scan results saved to {output_file}", fg="green")
    elif target is None:
        typer.echo(json.dumps(result, indent=2))
    else:
        with open(target, "w", encoding="utf-8") as fp:
            json.dump(result, fp, indent=2)
        typer.secho(f"✓ Scan results saved to {target}", fg="green")


@command(scan_app, "batch")
def scan_batch(
    config_path: Annotated[
        Path,
        typer.Option(
            "--config",
            "-c",
            exists=True,
            help="Path to a YAML config file listing workspace_ids and scan parameters.",
        ),
    ],
):
    """Scan every workspace listed in a YAML config file and save each result.

    Workspaces are scanned in batches of up to 100 per request: the API allows 500
    scan requests an hour, so scanning one by one would run out of quota. When a batch
    fails, its workspaces are scanned one by one, so one failing workspace doesn't
    block the rest. Each workspace gets its own file, holding the result for that
    workspace alone (the workspace and the data sources it uses), saved as
    ``<target_folder>/<slugified-name>-<workspace_id>.json`` when a name is provided,
    or ``<target_folder>/<workspace_id>.json`` otherwise. The scans are also kept in
    the data lake (see `pbi lake`) when a cache folder is configured.

    Example config file:

    ```yaml
    workspace_ids:
      - id: <workspace-id-1>
        name: Finance
      - id: <workspace-id-2>
        name: Marketing
      - <workspace-id-3>  # name is optional; falls back to the ID
    target_folder: scan_results
    lineage: true
    datasource_details: true
    dataset_schema: false
    dataset_expressions: false
    get_artifact_users: false
    interval: 5
    timeout: 300
    ```

    Only ``workspace_ids`` and ``target_folder`` are required; the scan flags
    default to ``false`` and ``interval``/``timeout`` default to ``5``/``300``
    seconds for each scan, same as ``pbi workspaces scan get``.

    If the token has expired or the API is throttling, every workspace would fail the
    same way, so the command stops and says so instead of listing each of them.

    ```sh
    pbi workspaces scan batch --config scan_config.yaml
    ```

    !!! warning "Requires Admin"

        This command requires an admin account.

    """
    with open(config_path, "r", encoding="utf-8") as fp:
        raw_config = yaml.safe_load(fp) or {}

    if not isinstance(raw_config, dict):
        raise PBIError(f"Config file {config_path} must contain a YAML mapping.")

    workspace_entries = _normalize_workspace_entries(
        raw_config.get("workspace_ids") or []
    )
    if not workspace_entries:
        raise PBIError(
            f"Config file {config_path} must list at least one workspace ID "
            "under 'workspace_ids'."
        )

    target_folder = raw_config.get("target_folder")
    if not target_folder:
        raise PBIError(f"Config file {config_path} must set 'target_folder'.")

    target_path = resolve_output_path(str(target_folder))
    if target_path is None:
        typer.secho("Error: Unable to determine output folder.", fg="red")
        typer.echo("Use 'pbi config set-output-folder' to set a default output folder,")
        typer.echo("or set 'target_folder' to an absolute path in the config file.")
        raise typer.Abort()

    if not target_path.exists():
        typer.secho(f"creating folder {target_path}", fg="blue")
        target_path.mkdir(parents=True, exist_ok=True)

    lineage = _parse_yaml_bool(raw_config, "lineage", default=False)
    datasource_details = _parse_yaml_bool(
        raw_config, "datasource_details", default=False
    )
    dataset_schema = _parse_yaml_bool(raw_config, "dataset_schema", default=False)
    dataset_expressions = _parse_yaml_bool(
        raw_config, "dataset_expressions", default=False
    )
    get_artifact_users = _parse_yaml_bool(
        raw_config, "get_artifact_users", default=False
    )
    raw_interval = raw_config.get("interval")
    raw_timeout = raw_config.get("timeout")
    try:
        interval = float(5.0 if raw_interval is None else raw_interval)
        timeout = float(300.0 if raw_timeout is None else raw_timeout)
    except (TypeError, ValueError) as e:
        raise PBIError("'interval' and 'timeout' must be numeric values.") from e
    if interval <= 0 or timeout <= 0:
        raise PBIError("'interval' and 'timeout' must be greater than 0.")

    flags = ScanFlags(
        lineage=lineage,
        datasource_details=datasource_details,
        dataset_schema=dataset_schema,
        dataset_expressions=dataset_expressions,
        get_artifact_users=get_artifact_users,
    )

    failed: List[str] = []
    with _client("admin") as client:
        client.profile_name()  # no token is a problem for every workspace: say so at once
        for chunk in chunked(workspace_entries, MAX_WORKSPACES):
            ids = ", ".join(_entry_label(entry) for entry in chunk[:3])
            more = f" and {len(chunk) - 3} more" if len(chunk) > 3 else ""
            typer.echo(f"\n=== {len(chunk)} workspace(s): {ids}{more} ===")
            scanned = _scan_entries(client, chunk, flags, interval, timeout, failed)
            for entry in chunk:
                if _entry_label(entry) in failed:
                    continue
                piece = scanned.get(entry["id"])
                if piece is None:
                    # The API leaves out workspaces it does not know: save the empty result
                    piece = {
                        "workspaces": [],
                        "datasourceInstances": [],
                        "misconfiguredDatasourceInstances": [],
                    }
                    typer.secho(
                        f"  {_entry_label(entry)}: Power BI returned nothing for this "
                        "workspace (an empty result is saved)",
                        fg="yellow",
                    )
                name_slug = slugify(entry["name"]) if entry.get("name") else ""
                file_stub = f"{name_slug}-{entry['id']}" if name_slug else entry["id"]
                output_file = target_path / f"{file_stub}.json"
                with open(output_file, "w", encoding="utf-8") as fp:
                    json.dump(piece, fp, indent=2)
                typer.secho(f"✓ Saved {output_file}", fg="green")

    if failed:
        typer.secho(f"\nFailed workspaces: {', '.join(failed)}", fg="red")
        raise typer.Exit(1)

    typer.secho(
        f"\n✓ Scanned {len(workspace_entries)} workspace(s) into {target_path}",
        fg="green",
    )


def _entry_label(entry: dict) -> str:
    """How a workspace of a batch config is named in messages."""
    name = entry.get("name")
    return f"{name} ({entry['id']})" if name else entry["id"]


def _scan_entries(
    client: PowerBIClient,
    entries: List[dict],
    flags: ScanFlags,
    interval: float,
    timeout: float,
    failed: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Scan workspaces together; when that fails, scan them one by one.

    What goes wrong for one workspace is recorded in ``failed`` and the others go on. A
    problem that every workspace would meet, an expired token, missing credentials or
    throttling, stops the command.

    :return: the result of each workspace that was scanned, by workspace id
    """
    ids = [entry["id"] for entry in entries]
    try:
        typer.echo(
            f"Initiating scan for {ids}…"
            if len(ids) <= 3
            else f"Initiating scan for {len(ids)} workspaces…"
        )
        run = run_scan(
            client,
            ids,
            flags,
            interval=interval,
            timeout=timeout,
            on_started=lambda job: typer.echo(
                f"Scan started (id={job.scan_id}). Waiting for status…"
            ),
            on_poll=lambda attempt, status, wait: typer.echo(
                f"  Attempt {attempt}: scan status is '{status}', "
                f"retrying in {wait:.0f}s…"
            ),
        )
    except (AuthError, RateLimitError):
        raise
    except PBIError as error:
        if len(entries) == 1:
            typer.secho(f"✗ {_entry_label(entries[0])}: {error}", fg="red")
            failed.append(_entry_label(entries[0]))
            return {}
        typer.secho(
            f"✗ Scanning these {len(entries)} workspaces together failed: {error}",
            fg="red",
        )
        typer.echo("Scanning them one by one…")
        scanned: Dict[str, Dict[str, Any]] = {}
        for entry in entries:
            scanned.update(
                _scan_entries(client, [entry], flags, interval, timeout, failed)
            )
        return scanned

    if client.store is not None:
        try:
            store_scan(
                client.store,
                client.tenant_key(),
                run,
                ids,
                flags,
                profile=client.profile_name(),
            )
        except Exception as error:  # the result is good even if it cannot be kept
            logger.warning(f"Could not save the scan to the data lake: {error}")
    return split_scan_result(run.result)


if __name__ == "__main__":
    app()
