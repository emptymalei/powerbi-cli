import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Union

import click
import pandas as pd
from loguru import logger
from slugify import slugify

import pbi_cli.powerbi.admin as powerbi_admin
import pbi_cli.powerbi.admin.report as powerbi_admin_report
import pbi_cli.powerbi.app as powerbi_app
import pbi_cli.powerbi.report as powerbi_report
import pbi_cli.powerbi.workspace as powerbi_workspace
from pbi_cli.auth import PBIAuth
from pbi_cli.cache import CacheManager
from pbi_cli.config import (
    VALID_GROUPS,
    PBIConfig,
    migrate_legacy_config,
    resolve_output_path,
)
from pbi_cli.powerbi.admin import User, Workspaces
from pbi_cli.powerbi.io import multi_group_dict_to_excel
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


def _handle_cache_load(
    cache_key: str, use_cache: bool, cache_only: bool, pbi_config: PBIConfig
) -> Optional[Dict[str, Any]]:
    """Handle loading data from cache.

    Returns the cached data if available, or None if not cached.
    Raises click.Abort if cache_only is True but cache is not available.
    """
    if not (use_cache or cache_only):
        return None

    if pbi_config.cache_folder and pbi_config.cache_enabled:
        cache_manager = CacheManager(cache_folder=pbi_config.cache_folder)
        cached_data = cache_manager.load(cache_key, version="latest")

        if cached_data:
            cache_version = cached_data.get("version", "unknown")
            cache_time = cached_data.get("cached_at", "unknown")
            click.secho(
                f"Using cached data from {cache_time} (version: {cache_version})",
                fg="cyan",
            )
            return cached_data.get("data")
        elif cache_only:
            click.secho(
                "Error: Cache not available and --cache-only was specified", fg="red"
            )
            raise click.Abort()
        else:
            click.secho("Cache not available, fetching from API...", fg="yellow")
            return None
    elif cache_only:
        click.secho(
            "Error: Cache not configured and --cache-only was specified", fg="red"
        )
        click.echo("Use 'pbi config set-cache-folder' to configure caching.")
        raise click.Abort()

    return None


def _handle_cache_save(
    cache_key: str, data: Any, metadata: Dict[str, Any], pbi_config: PBIConfig
):
    """Save data to cache if configured and enabled."""
    if pbi_config.cache_folder and pbi_config.cache_enabled:
        cache_manager = CacheManager(cache_folder=pbi_config.cache_folder)
        version = cache_manager.save(cache_key, data, metadata=metadata)
        if version:
            click.secho(f"Cached data (version: {version})", fg="green")


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
        click.echo(f"No {title.lower()} found.")
        return

    df = pd.json_normalize(data["value"])

    # Filter to display columns if specified
    if display_cols:
        available_cols = [col for col in display_cols if col in df.columns]
        if available_cols:
            df = df[available_cols]

    click.echo("\n" + "=" * 80)
    click.echo(f"{title}: {len(df)} record(s)")
    click.echo("=" * 80)
    click.echo(df.to_string(index=False))
    click.echo("=" * 80)


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


def _set_credential(profile: str, token: str):
    """Set credential for a profile using keyring or fallback to file storage"""
    if _check_keyring_availability():
        try:
            keyring.set_password(KEYRING_SERVICE, profile, token)
            return
        except NoKeyringError:
            pass
        except (OSError, Exception) as e:
            # Handle Windows Credential Manager errors (e.g., token too long)
            # and other keyring-specific errors
            logger.debug(f"Keyring error: {e}")
            pass

    # Fallback to file-based storage
    logger.warning(
        "Keyring not available, storing credentials in file. "
        "For better security, install a keyring backend (e.g., pip install keyrings.alt)"
    )

    config_dir = _get_config_dir()
    credentials_file = _get_credentials_file()
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
    pbi_config = PBIConfig()

    # Try to resolve from the requested group first.
    resolved_profile = profile
    if resolved_profile is None:
        resolved_profile = pbi_config.get_group_active_profile(group)

    if resolved_profile is not None and pbi_config.has_profile_in_group(
        group, resolved_profile
    ):
        # Use group-based profile.
        profile = resolved_profile
    else:
        # Fall back to legacy flat profiles for backward compatibility.
        profiles_data = _load_profiles()
        if profile is None:
            profile = profiles_data.get("active_profile")
        if profile is None:
            raise click.ClickException(
                f"No active profile set for group '{group}'. "
                f"Use 'pbi auth -g {group}' to create a profile or "
                f"'pbi profile switch -g {group}' to switch profiles."
            )
        if profile not in profiles_data.get("profiles", {}):
            raise click.ClickException(
                f"Profile '{profile}' not found in group '{group}' or flat profiles. "
                "Use 'pbi profile list' to see available profiles."
            )

    # Get token from keyring or file.
    token = _get_credential(profile)
    if token is None:
        raise click.ClickException(
            f"No credentials found for profile '{profile}'. Please re-authenticate."
        )

    return {"Authorization": f"Bearer {token}"}


@click.group(invoke_without_command=True)
@click.pass_context
def pbi(ctx):
    if ctx.invoked_subcommand is None:
        click.echo("Hello {}".format(os.environ.get("USER", "")))
        click.echo("Welcome to pbi cli. Use pbi --help for help.")
    else:
        pass


@pbi.command()
def version():
    """Show the current version of the pbi CLI tool."""
    from importlib.metadata import version as _version

    click.echo(_version("pbi_cli"))


@pbi.command()
@click.option("--bearer-token", "-t", help="Bearer token", required=True)
@click.option(
    "--profile",
    "-p",
    help="Profile name for this credential set",
    default="default",
)
@click.option(
    "--group",
    "-g",
    help="Group to store this profile in ('user' or 'admin')",
    type=click.Choice(list(VALID_GROUPS)),
    default=None,
    required=False,
)
def auth(bearer_token: str, profile: str, group: Optional[str]):
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

    if bearer_token.startswith("Bearer"):
        logger.warning("Do not include the Bearer string in the beginning")
        bearer_token = bearer_token.replace("Bearer ", "")

    config_dir = _get_config_dir()
    if not config_dir.exists():
        logger.info(f"Creating config folder: {config_dir}")
        config_dir.mkdir(parents=True, exist_ok=True)

    # Store token securely (keyed by profile name)
    _set_credential(profile, bearer_token)

    if group is not None:
        # Store in group-based config
        pbi_config = PBIConfig()
        pbi_config.add_profile_to_group(group, profile, {"name": profile})
        # Activate this profile in the group if none is set yet
        if not pbi_config.get_group_active_profile(group):
            pbi_config.set_group_active_profile(group, profile)
        active_in_group = pbi_config.get_group_active_profile(group)
        click.secho(
            f"✓ Credentials saved securely for profile '{profile}' in group '{group}'",
            fg="green",
        )
        if active_in_group == profile:
            click.secho(
                f"✓ Profile '{profile}' is now active in group '{group}'", fg="green"
            )
    else:
        # Legacy: store in flat profiles
        profiles_data = _load_profiles()
        if "profiles" not in profiles_data:
            profiles_data["profiles"] = {}

        profiles_data["profiles"][profile] = {"name": profile}

        # Set as active profile if it's the first one or if it's 'default'
        if not profiles_data.get("active_profile") or profile == "default":
            profiles_data["active_profile"] = profile

        _save_profiles(profiles_data)

        click.secho(f"✓ Credentials saved securely for profile '{profile}'", fg="green")
        if profiles_data["active_profile"] == profile:
            click.secho(f"✓ Profile '{profile}' is now active", fg="green")


@pbi.group(name="profile", invoke_without_command=True)
@click.pass_context
def profile_group(ctx):
    """Manage authentication profiles"""
    if ctx.invoked_subcommand is None:
        click.echo("Use pbi profile --help for help.")


@profile_group.command(name="switch")
@click.argument("profile_name", required=False)
@click.option(
    "--group",
    "-g",
    help="Group to switch profile in ('user' or 'admin')",
    type=click.Choice(list(VALID_GROUPS)),
    default=None,
    required=False,
)
def switch_profile_cmd(profile_name: Optional[str] = None, group: Optional[str] = None):
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
    if group is not None:
        pbi_config = PBIConfig()
        group_profiles = pbi_config.get_group_profiles(group)
        available_profiles = tuple(group_profiles.keys())

        if not available_profiles:
            click.secho(
                f"No profiles found in group '{group}'. "
                f"Use 'pbi auth -g {group}' to create a profile.",
                fg="yellow",
            )
            return

        if profile_name is None:
            click.echo(f"Available profiles in group '{group}':")
            for idx, prof in enumerate(available_profiles, 1):
                active_marker = (
                    " (active)"
                    if prof == pbi_config.get_group_active_profile(group)
                    else ""
                )
                click.echo(f"  {idx}. {prof}{active_marker}")

            choice = click.prompt(
                "Select profile number", type=int, default=1, show_default=True
            )
            if 1 <= choice <= len(available_profiles):
                profile_name = available_profiles[choice - 1]
            else:
                click.secho("Invalid selection", fg="red")
                return

        if profile_name not in available_profiles:
            click.secho(
                f"Profile '{profile_name}' not found in group '{group}'. "
                "Use 'pbi profile list' to see available profiles.",
                fg="red",
            )
            return

        pbi_config.set_group_active_profile(group, profile_name)
        click.secho(
            f"✓ Switched to profile '{profile_name}' in group '{group}'", fg="green"
        )
        return

    # Legacy: switch flat active profile
    profiles_data = _load_profiles()
    # Use tuple() instead of list() to avoid shadowing by the workspaces list command
    available_profiles = tuple(profiles_data.get("profiles", {}).keys())

    if not available_profiles:
        click.secho(
            "No profiles found. Use 'pbi auth' to create a profile.", fg="yellow"
        )
        return

    # If no profile specified, show interactive selection
    if profile_name is None:
        click.echo("Available profiles:")
        for idx, prof in enumerate(available_profiles, 1):
            active_marker = (
                " (active)" if prof == profiles_data.get("active_profile") else ""
            )
            click.echo(f"  {idx}. {prof}{active_marker}")

        choice = click.prompt(
            "Select profile number", type=int, default=1, show_default=True
        )
        if 1 <= choice <= len(available_profiles):
            profile_name = available_profiles[choice - 1]
        else:
            click.secho("Invalid selection", fg="red")
            return

    if profile_name not in available_profiles:
        click.secho(
            f"Profile '{profile_name}' not found. Use 'pbi profile list' to see available profiles.",
            fg="red",
        )
        return

    profiles_data["active_profile"] = profile_name
    _save_profiles(profiles_data)

    click.secho(f"✓ Switched to profile '{profile_name}'", fg="green")


@profile_group.command(name="list")
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
        click.echo("Stored authentication profiles (ungrouped):")
        for profile_name in profiles.keys():
            active_marker = " (active)" if profile_name == active_profile else ""
            token_exists = _get_credential(profile_name) is not None
            status = "✓" if token_exists else "✗"
            click.echo(f"  {status} {profile_name}{active_marker}")
        click.echo()
        click.echo(f"Active profile: {active_profile or 'None'}")
    else:
        click.secho(
            "No ungrouped profiles found. Use 'pbi auth' to create a profile.",
            fg="yellow",
        )

    # Show group profiles
    click.echo()
    for group in VALID_GROUPS:
        group_profiles = pbi_config.get_group_profiles(group)
        group_active = pbi_config.get_group_active_profile(group)
        if group_profiles:
            click.echo(f"Group '{group}':")
            for profile_name in group_profiles.keys():
                active_marker = " (active)" if profile_name == group_active else ""
                token_exists = _get_credential(profile_name) is not None
                status = "✓" if token_exists else "✗"
                click.echo(f"  {status} {profile_name}{active_marker}")
            click.echo(f"  Active: {group_active or 'None'}")
        else:
            click.secho(
                f"No profiles found in group '{group}'. "
                f"Use 'pbi auth -g {group}' to create one.",
                fg="yellow",
            )

    if not profiles and not any(pbi_config.get_group_profiles(g) for g in VALID_GROUPS):
        click.secho(
            "No profiles found. Use 'pbi auth' to create a profile.", fg="yellow"
        )


@profile_group.command(name="delete")
@click.argument("profile")
@click.option(
    "--group",
    "-g",
    help="Group to delete the profile from ('user' or 'admin')",
    type=click.Choice(list(VALID_GROUPS)),
    default=None,
    required=False,
)
@click.confirmation_option(prompt="Are you sure you want to delete this profile?")
def delete_auth(profile: str, group: Optional[str]):
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
    if group is not None:
        pbi_config = PBIConfig()
        if not pbi_config.has_profile_in_group(group, profile):
            click.secho(
                f"Profile '{profile}' not found in group '{group}'. "
                "Use 'pbi profile list' to see available profiles.",
                fg="red",
            )
            return

        _delete_credential(profile)
        pbi_config.remove_profile_from_group(group, profile)
        click.secho(f"✓ Profile '{profile}' deleted from group '{group}'", fg="green")
        new_active = pbi_config.get_group_active_profile(group)
        if new_active:
            click.secho(
                f"Active profile in group '{group}' is now '{new_active}'", fg="yellow"
            )
        return

    profiles_data = _load_profiles()

    if profile not in profiles_data.get("profiles", {}):
        click.secho(
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

    click.secho(f"✓ Profile '{profile}' deleted successfully", fg="green")
    if profiles_data.get("active_profile"):
        click.secho(
            f"Active profile is now '{profiles_data['active_profile']}'", fg="yellow"
        )


@pbi.group(name="config", invoke_without_command=True)
@click.pass_context
def config_group(ctx):
    """Manage pbi-cli configuration settings"""
    if ctx.invoked_subcommand is None:
        click.echo("Use pbi config --help for help.")


@config_group.command(name="set-output-folder")
@click.argument("folder_path", type=click.Path())
def set_output_folder(folder_path: str):
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
    click.secho(f"✓ Default output folder set to: {resolved_path}", fg="green")
    click.secho(
        "  Commands will now use subfolders within this folder by default.", fg="blue"
    )


@config_group.command(name="get-output-folder")
def get_output_folder():
    """Get the currently configured default output folder

    ```
    pbi config get-output-folder
    ```
    """
    pbi_config = PBIConfig()
    folder = pbi_config.default_output_folder
    if folder:
        click.echo(f"Default output folder: {folder}")
    else:
        click.secho("No default output folder configured.", fg="yellow")
        click.echo("Use 'pbi config set-output-folder' to set one.")


@config_group.command(name="show")
def show_config():
    """Show all configuration settings

    ```
    pbi config show
    ```
    """
    pbi_config = PBIConfig()

    click.echo("Current configuration:")
    click.echo(f"  Active profile: {pbi_config.active_profile or 'None'}")
    click.echo(
        f"  Default output folder: {pbi_config.default_output_folder or 'Not set'}"
    )
    click.echo(f"  Cache folder: {pbi_config.cache_folder or 'Not set'}")
    click.echo(f"  Cache enabled: {pbi_config.cache_enabled}")
    click.echo(f"  Profiles: {len(pbi_config.profiles)}")

    if pbi_config.profiles:
        click.echo("\n  Available profiles (ungrouped):")
        for profile_name in pbi_config.profiles.keys():
            active = " (active)" if profile_name == pbi_config.active_profile else ""
            click.echo(f"    - {profile_name}{active}")

    click.echo()
    click.echo("  Groups:")
    for group in VALID_GROUPS:
        group_profiles = pbi_config.get_group_profiles(group)
        group_active = pbi_config.get_group_active_profile(group)
        click.echo(
            f"    {group}: {len(group_profiles)} profile(s), active='{group_active or 'None'}'"
        )


@config_group.command(name="set-cache-folder")
@click.argument("folder_path", type=click.Path())
def set_cache_folder(folder_path: str):
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
        click.secho(f"✓ Cache folder set to: {folder_path_clean}", fg="green")
    else:
        from pathlib import Path

        resolved_path = Path(folder_path_clean).expanduser().absolute()
        click.secho(f"✓ Cache folder set to: {resolved_path}", fg="green")

    click.secho("  API call results will be cached in this folder.", fg="blue")


@config_group.command(name="get-cache-folder")
def get_cache_folder():
    """Get the currently configured cache folder

    ```
    pbi config get-cache-folder
    ```
    """
    pbi_config = PBIConfig()
    folder = pbi_config.cache_folder
    if folder:
        click.echo(f"Cache folder: {folder}")
        click.echo(f"Cache enabled: {pbi_config.cache_enabled}")
    else:
        click.secho("No cache folder configured.", fg="yellow")
        click.echo("Use 'pbi config set-cache-folder' to set one.")


@config_group.command(name="enable-cache")
def enable_cache():
    """Enable caching of API call results

    ```
    pbi config enable-cache
    ```
    """
    pbi_config = PBIConfig()
    pbi_config.cache_enabled = True
    click.secho("✓ Cache enabled", fg="green")


@config_group.command(name="disable-cache")
def disable_cache():
    """Disable caching of API call results

    ```
    pbi config disable-cache
    ```
    """
    pbi_config = PBIConfig()
    pbi_config.cache_enabled = False
    click.secho("✓ Cache disabled", fg="yellow")


@pbi.group(name="cache", invoke_without_command=True)
@click.pass_context
def cache_group(ctx):
    """Manage cached API call results"""
    if ctx.invoked_subcommand is None:
        click.echo("Use pbi cache --help for help.")


@cache_group.command(name="list")
@click.option(
    "--cache-key",
    "-k",
    help="Show versions for a specific cache key",
    default=None,
)
def list_cache(cache_key: Optional[str] = None):
    """List cached data

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
        click.secho("Cache folder not configured.", fg="yellow")
        click.echo("Use 'pbi config set-cache-folder' to set one.")
        return

    cache_manager = CacheManager(cache_folder=cache_folder)

    if cache_key:
        # List versions for specific key
        versions = cache_manager.list_versions(cache_key)
        if versions:
            click.echo(f"Cached versions for '{cache_key}':")
            for version in versions:
                click.echo(f"  - {version}")
        else:
            click.secho(f"No cached versions found for '{cache_key}'", fg="yellow")
    else:
        # List all cache keys
        keys = cache_manager.list_keys()
        if keys:
            click.echo("Cached data:")
            for key in keys:
                versions = cache_manager.list_versions(key)
                click.echo(f"  - {key} ({len(versions)} version(s))")
        else:
            click.secho("No cached data found.", fg="yellow")


@cache_group.command(name="clear")
@click.option(
    "--cache-key",
    "-k",
    help="Clear specific cache key (clears all if omitted)",
    default=None,
)
@click.option(
    "--version",
    "-v",
    help="Clear specific version (requires --cache-key)",
    default=None,
)
@click.confirmation_option(prompt="Are you sure you want to clear the cache?")
def clear_cache(cache_key: Optional[str] = None, version: Optional[str] = None):
    """Clear cached data

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
    pbi_config = PBIConfig()
    cache_folder = pbi_config.cache_folder

    if not cache_folder:
        click.secho("Cache folder not configured.", fg="yellow")
        click.echo("Use 'pbi config set-cache-folder' to set one.")
        return

    if version and not cache_key:
        click.secho("Error: --version requires --cache-key", fg="red")
        return

    cache_manager = CacheManager(cache_folder=cache_folder)
    cache_manager.clear(cache_key=cache_key, version=version)

    if cache_key and version:
        click.secho(f"✓ Cleared {cache_key} version {version}", fg="green")
    elif cache_key:
        click.secho(f"✓ Cleared all versions of {cache_key}", fg="green")
    else:
        click.secho("✓ Cleared entire cache", fg="green")


@pbi.command()
@click.option("--group-id", "-g", help="Group ID", required=True)
@click.option("--report-id", "-r", help="Report ID", required=True)
@click.option(
    "--target",
    "-t",
    type=click.Path(exists=False),
    help="target file (if omitted, prints info to console)",
    default=None,
    required=False,
)
def export(group_id: str, report_id: str, target: Optional[Path]):
    """export report based on id"""
    dr = DataRetriever(session_query_configs={"headers": load_auth(), "verify": False})

    uri = f"https://api.powerbi.com/v1.0/myorg/groups/{group_id}/reports/{report_id}/Export"

    result = dr.get(uri)

    if target is None:
        # For binary export data, we can't print it directly to console
        # Instead, show information about the export
        click.echo("\n" + "=" * 80)
        click.echo(f"Report Export (Group: {group_id}, Report: {report_id})")
        click.echo("=" * 80)
        click.echo(f"Content size: {len(result.content)} bytes")
        click.echo(f"Content type: {result.headers.get('content-type', 'unknown')}")
        click.echo("\nUse --target option to save the export to a file.")
        click.echo("=" * 80)
    else:
        with open(target, "wb") as fp:
            fp.write(result.content)
        click.secho(f"✓ Export saved to {target}", fg="green")


@pbi.group(invoke_without_command=True)
@click.pass_context
def workspaces(ctx):
    """Command group for Power BI workspaces"""
    if ctx.invoked_subcommand is None:
        click.echo("Use pbi workspaces --help for help.")
    else:
        pass


@workspaces.command()
@click.option("--top", help="top n results", type=int, default=1000, required=True)
@click.option(
    "--expand",
    "-e",
    type=click.Choice(
        ["users", "reports", "dashboards", "datasets", "dataflows", "workbooks"]
    ),
    default=["users", "reports", "dashboards", "datasets", "dataflows", "workbooks"],
    multiple=True,
    show_default=True,
)
@click.option(
    "--file-type",
    "-ft",
    type=click.Choice(["json", "excel"]),
    default=["json"],
    multiple=True,
)
@click.option("--odata-filter", "-f", type=str, help="odata filter", required=False)
@click.option(
    "--target-folder",
    "-tf",
    type=str,
    help="target folder (absolute path or subfolder within default output folder). If omitted, prints results to console as a table.",
    default=None,
    required=False,
)
@click.option("--file-name", "-n", type=str, help="file name", default="workspaces")
@click.option(
    "--use-cache",
    is_flag=True,
    help="Use cached data if available instead of making API call",
    default=False,
)
@click.option(
    "--cache-only",
    is_flag=True,
    help="Only use cache, fail if cache not available",
    default=False,
)
def list(
    top: int,
    expand: list,
    file_type: list[str],
    odata_filter: Optional[str],
    target_folder: Optional[str],
    file_name: str = "workspaces",
    use_cache: bool = False,
    cache_only: bool = False,
):
    r"""List Power BI workspaces and save them to files or print to console

    The --target-folder can be:
    - An absolute path: "C:\Users\Name\PowerBI\backups\2024-01-01"
    - A relative subfolder: "2024-01-01" (uses default output folder + this subfolder)
    - Omitted: prints results as a table to the console (no files created)

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
    pbi_config = PBIConfig()
    cache_key = "workspaces"

    # Try to load from cache
    result = _handle_cache_load(cache_key, use_cache, cache_only, pbi_config)

    # Fetch from API if not using cache
    if result is None:
        workspaces = Workspaces(auth=load_auth(group="admin"), verify=False)
        click.echo(f"Retrieving workspaces for: {top=}, {expand=}, {odata_filter=}")
        result = workspaces(top=top, expand=expand, filter=odata_filter)

        # Save to cache
        _handle_cache_save(
            cache_key,
            result,
            {"top": top, "expand": [*expand] if expand else [], "filter": odata_filter},
            pbi_config,
        )

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
        click.secho("Error: Unable to determine output folder.", fg="red")
        click.echo("Use 'pbi config set-output-folder' to set a default output folder,")
        click.echo("or provide an absolute path with --target-folder.")
        raise click.Abort()

    if not target_path.exists():
        click.secho(f"creating folder {target_path}", fg="blue")
        target_path.mkdir(parents=True, exist_ok=True)

    if "json" in file_type:
        json_file_path = target_path / f"{file_name}.json"
        logger.info(f"Writing to {json_file_path}")
        with open(json_file_path, "w") as fp:
            json.dump(result, fp)

    if "excel" in file_type:
        excel_file_path = target_path / f"{file_name}.xlsx"
        logger.info(f"Writing to {excel_file_path}")
        flattened = workspaces.flatten_workspaces(result["value"])
        multi_group_dict_to_excel(flattened, excel_file_path)


@workspaces.command()
@click.option(
    "--source",
    "-s",
    type=click.Path(exists=True, path_type=Path),
    help="source file",
    required=True,
)
@click.option(
    "--target",
    "-t",
    type=click.Path(exists=False, path_type=Path),
    help="target file",
    required=True,
)
@click.option(
    "--format",
    type=click.Choice(["excel"]),
    help="format of the file",
    default="excel",
    required=False,
)
def format_convert(source: Path, target: Path, format):
    """Convert output format of workspaces list
    (`pbi workspaces list`)
    from json to excel.

    ```sh
    pbi workspaces format-convert -s "workspaces.json" -t "workspaces.xlsx"
    ```
    """
    workspaces = Workspaces(auth={}, verify=False)

    click.echo(f"Converting to {format=}: {source=} -> {target}")

    with open(source, "r") as fp:
        workspaces_data = json.load(fp)

    flattened = workspaces.flatten_workspaces(workspaces_data["value"])

    multi_group_dict_to_excel(flattened, target)


@workspaces.command()
@click.option(
    "--source",
    "-s",
    type=click.Path(exists=True, path_type=Path),
    help="source json/excel file that contains all workspace information",
    required=True,
)
@click.option(
    "--target-folder",
    "-t",
    type=str,
    help=(
        "target folder (absolute path or subfolder within default output folder). "
        "If omitted, prints results to console as a table. "
        "Do not include the trailing (back)slash"
    ),
    default=None,
    required=False,
)
@click.option(
    "--file-type",
    "-ft",
    help="file type to save the results as",
    type=click.Choice(["json", "excel"]),
    default=["json", "excel"],
    multiple=True,
)
@click.option(
    "--wait-interval",
    "-wi",
    help="number of seconds to wait between requests",
    type=int,
    default=3,
)
@click.option(
    "--file-name",
    "-n",
    type=str,
    help="file name without extension",
    default="workspaces_reports_users",
)
@click.option(
    "--workspace-name",
    "-wn",
    help="workspace names to download",
    type=str,
    default=None,
    multiple=True,
    required=False,
)
def report_users(
    source: Path,
    target_folder: Optional[str],
    file_type: list = ["json", "excel"],
    wait_interval: int = 3,
    file_name: str = "workspaces_reports_users",
    workspace_name: Optional[list] = None,
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
    click.secho("getting report user details requires admin token")

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
                    click.echo("\n" + "=" * 80)
                    click.echo(f"Found {len(all_reports)} report(s) across workspaces")
                    click.echo("=" * 80)
                    click.echo(df.to_string(index=False))
                    click.echo("=" * 80)
                else:
                    click.echo("No reports found.")
            except Exception as e:
                # Fallback to JSON if table formatting fails
                click.echo(json.dumps(report_users, indent=4))
        else:
            click.echo("No report users data found.")
        return

    # Resolve the target folder path (handles absolute/relative paths)
    target_path = resolve_output_path(target_folder)

    # Check if path resolution failed
    if target_path is None:
        click.secho("Error: Unable to determine output folder.", fg="red")
        click.echo("Use 'pbi config set-output-folder' to set a default output folder,")
        click.echo("or provide an absolute path with --target-folder.")
        raise click.Abort()

    if not target_path.exists():
        click.secho(f"creating folder {target_path}", fg="blue")
        target_path.mkdir(parents=True, exist_ok=True)

    click.secho(f"Writing results to the folder {target_path}")
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


@pbi.group(invoke_without_command=True)
@click.pass_context
def users(ctx):
    """Command group for Power BI users"""
    if ctx.invoked_subcommand is None:
        click.echo("Use pbi users --help.")
    else:
        pass


@users.command()
@click.option("--user-id", "-u", help="user id", type=str, required=True)
@click.option(
    "--target-folder",
    "-tf",
    type=str,
    help="target folder (absolute path or subfolder within default output folder). If omitted, prints results to console as a table.",
    required=False,
)
@click.option(
    "--file-types",
    "-ft",
    type=click.Choice(["json", "excel"]),
    multiple=True,
    default=["json"],
    required=False,
)
@click.option(
    "--file-name", "-n", type=str, help="file name without extension", default=None
)
@click.option(
    "--use-cache",
    is_flag=True,
    help="Use cached data if available instead of making API call",
    default=False,
)
@click.option(
    "--cache-only",
    is_flag=True,
    help="Only use cache, fail if cache not available",
    default=False,
)
def user_access(
    user_id: str,
    target_folder: Optional[str],
    file_types: list,
    file_name: Optional[str] = None,
    use_cache: bool = False,
    cache_only: bool = False,
):
    """Get user access information from Power BI API"""
    if file_name is None:
        file_name = slugify(user_id)

    pbi_config = PBIConfig()
    cache_key = f"user_access_{slugify(user_id)}"

    # Try to load from cache
    result = _handle_cache_load(cache_key, use_cache, cache_only, pbi_config)

    # Fetch from API if not using cache
    if result is None:
        user = User(auth=load_auth(group="admin"), user_id=user_id, verify=False)
        result = user()

        # Save to cache
        _handle_cache_save(cache_key, result, {"user_id": user_id}, pbi_config)

    # Display or save results
    if target_folder is None:
        logger.info(f"No target folder provided, printing to console...")
        if isinstance(result, dict):
            try:
                df = pd.json_normalize(result)
                click.echo("\n" + "=" * 80)
                click.echo(f"User Access Information for: {user_id}")
                click.echo("=" * 80)
                click.echo(df.to_string(index=False))
                click.echo("=" * 80)
            except Exception:
                click.echo(json.dumps(result, indent=4))
        else:
            click.echo(json.dumps(result, indent=4))
        return
        # Resolve the target folder path
        target_path = resolve_output_path(target_folder)

        if target_path is None:
            click.secho("Error: Unable to determine output folder.", fg="red")
            click.echo(
                "Use 'pbi config set-output-folder' to set a default output folder,"
            )
            click.echo("or provide an absolute path with --target-folder.")
            raise click.Abort()

        if not target_path.exists():
            click.secho(f"creating folder {target_path}", fg="blue")
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


@pbi.group(invoke_without_command=True)
@click.pass_context
def apps(ctx):
    """Power BI Apps Command Group"""
    if ctx.invoked_subcommand is None:
        click.echo("Use pbi apps --help.")
    else:
        pass


@apps.command()
@click.option(
    "--target-folder",
    "-tf",
    type=str,
    help="target folder (absolute path or subfolder within default output folder). If omitted, prints results to console as a table.",
    default=None,
    required=False,
)
@click.option(
    "--file-type",
    "-ft",
    type=click.Choice(["json", "excel"]),
    default=["json"],
    multiple=True,
)
@click.option("--role", "-r", type=click.Choice(["admin", "user"]), default="user")
@click.option("--file-name", "-n", type=str, help="file name", default="apps")
@click.option(
    "--use-cache",
    is_flag=True,
    help="Use cached data if available instead of making API call",
    default=False,
)
@click.option(
    "--cache-only",
    is_flag=True,
    help="Only use cache, fail if cache not available",
    default=False,
)
def list(
    target_folder: Optional[str],
    role: str,
    file_type: tuple = ("json", "excel"),
    file_name: str = "apps",
    use_cache: bool = False,
    cache_only: bool = False,
):
    """List Power BI Apps and save them to files or print to console"""
    pbi_config = PBIConfig()
    cache_key = f"apps_{role}"

    # Try to load from cache
    result = _handle_cache_load(cache_key, use_cache, cache_only, pbi_config)

    # Fetch from API if not using cache
    if result is None:
        click.echo(f"Listing Apps as {role}")
        if role == "user":
            user = powerbi_app.Apps(auth=load_auth(group="user"), verify=False)
        else:  # admin
            user = powerbi_admin.Apps(auth=load_auth(group="admin"), verify=False)
        result = user()

        # Save to cache
        _handle_cache_save(cache_key, result, {"role": role}, pbi_config)

    # Display or save results
    if target_folder is None:
        display_cols = ["id", "name", "description", "publishedBy", "lastUpdate"]
        _display_table(result, f"Apps ({role})", display_cols)
        return

    # Resolve the target folder path
    target_path = resolve_output_path(target_folder)

    # Check if path resolution failed
    if target_path is None:
        click.secho("Error: Unable to determine output folder.", fg="red")
        click.echo("Use 'pbi config set-output-folder' to set a default output folder,")
        click.echo("or provide an absolute path with --target-folder.")
        raise click.Abort()

    if not target_path.exists():
        click.secho(f"creating folder {target_path}", fg="blue")
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


@apps.command()
@click.option("--app-id", "-a", help="app id", type=str, required=True)
@click.option(
    "--target",
    "-t",
    type=click.Path(exists=False),
    help="target file (if omitted, prints to console)",
    default=None,
    required=False,
)
@click.option(
    "--file-type", "-ft", type=click.Choice(["json", "excel"]), default="json"
)
def app(app_id: str, target: Optional[Path], file_type: str = "json"):
    """Retrieve information about a specific Power BI App"""
    click.echo(f"Investigating {app_id}")

    a_app = powerbi_app.App(auth=load_auth(), verify=False, app_id=app_id)
    app_data = a_app()

    if target is None:
        # Print to console
        click.echo("\n" + "=" * 80)
        click.echo(f"App: {app_data.get('name', 'N/A')} (ID: {app_id})")
        click.echo("=" * 80)
        click.echo(json.dumps(app_data, indent=2))
        click.echo("=" * 80)
    else:
        # Save to file
        if file_type == "json":
            with open(target, "w") as fp:
                json.dump(app_data, fp)
            click.secho(f"✓ Saved to {target}", fg="green")
        elif file_type == "excel":
            app_data_flattened = a_app.flatten_app(app_data)
            multi_group_dict_to_excel(app_data_flattened, target)
            click.secho(f"✓ Saved to {target}", fg="green")


@apps.command()
@click.option(
    "--source",
    "-s",
    type=click.Path(exists=True, path_type=Path),
    help="source file",
    required=True,
)
@click.option(
    "--target",
    "-t",
    type=click.Path(exists=False, path_type=Path),
    help="target file",
    required=True,
)
@click.option(
    "--file-type", "-ft", type=click.Choice(["json", "excel"]), default="json"
)
def augment(source: Path, target: Path, file_type: str = "json"):
    """Augment Power BI Apps data from a source file and save to target file"""
    if file_type == "excel":
        if target.suffix:
            click.echo("Use path as target for excel output")
            raise click.BadOptionUsage(message=f"{target=}")
        else:
            click.secho(f"creating folder {target}", fg="blue")
            target.mkdir(parents=True, exist_ok=True)

    pbi_apps = powerbi_app.Apps(auth=load_auth(), verify=False, cache_file=source)

    apps_data = []
    for a in pbi_apps.apps:
        try:
            apps_data.append(a())
        except ValueError as e:
            click.secho(f"Cannot download {a.app_info}", fg="red")

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


@pbi.group(invoke_without_command=True)
@click.pass_context
def reports(ctx):
    """Reports Command Group"""
    if ctx.invoked_subcommand is None:
        click.echo("Use pbi reports --help.")
    else:
        pass


@reports.command()
@click.option(
    "--source",
    "-s",
    type=click.Path(exists=True, path_type=Path),
    help="source file",
    required=True,
)
@click.option(
    "--target",
    "-t",
    type=click.Path(exists=False, path_type=Path),
    help="target file",
    required=True,
)
@click.option(
    "--file-type", "-ft", type=click.Choice(["json", "excel"]), default="json"
)
def users(source: Path, target: Path, file_type: str = "json"):
    """Augment Power BI Apps data from a source file and save to target file together with report users"""

    click.secho("getting report user details requires admin token")

    if file_type == "excel":
        if target.suffix:
            click.echo("Use path as target for excel output")
            raise click.BadOptionUsage(message=f"{target=}")
        else:
            click.secho(f"creating folder {target}", fg="blue")
            target.mkdir(parents=True, exist_ok=True)

    pbi_apps = powerbi_app.Apps(auth=load_auth(), verify=False, cache_file=source)

    apps_data = []
    for a in pbi_apps.apps:
        try:
            apps_data.append(a())
        except ValueError as e:
            click.secho(f"Can not download {a.app_info}", fg="red")

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


@reports.command()
@click.option("--group-id", "-g", help="Group ID", required=True)
@click.option("--report-id", "-r", help="Report ID", required=True)
@click.option(
    "--target",
    "-t",
    type=click.Path(exists=False, path_type=Path),
    help="target file (if omitted, prints info to console)",
    default=None,
    required=False,
)
def export(group_id: str, report_id: str, target: Optional[Path]):
    """Export report as file"""

    pbi_report = powerbi_report.Report(
        auth=load_auth(), verify=False, report_id=report_id, group_id=group_id
    )

    result = pbi_report.export()

    if target is None:
        # For binary export data, we can't print it directly to console
        # Instead, show information about the export
        click.echo("\n" + "=" * 80)
        click.echo(f"Report Export (Group: {group_id}, Report: {report_id})")
        click.echo("=" * 80)
        click.echo(f"Content size: {len(result)} bytes")
        click.echo("\nUse --target option to save the export to a file.")
        click.echo("=" * 80)
    else:
        with open(target, "wb") as fp:
            fp.write(result)
        click.secho(f"✓ Export saved to {target}", fg="green")


@reports.command(name="list-group")
@click.option("--group-id", "-g", help="Group (workspace) ID", required=True)
@click.option(
    "--target",
    "-t",
    type=click.Path(exists=False, path_type=Path),
    help="target file (if omitted, prints info to console)",
    default=None,
    required=False,
)
def reports_list_group(group_id: str, target: Optional[Path]):
    """List all reports in a workspace group.

    Retrieves the full list of reports from the specified workspace group and
    either prints the result to the console or saves it to a JSON file.
    """
    group_reports = powerbi_report.GroupReports(
        auth=load_auth(), verify=False, group_id=group_id
    )
    result = group_reports.reports

    if target is None:
        click.echo(json.dumps(result, indent=2))
    else:
        with open(target, "w") as fp:
            json.dump(result, fp, indent=2)
        click.secho(f"✓ Reports list saved to {target}", fg="green")


@reports.command(name="pages")
@click.option("--group-id", "-g", help="Group (workspace) ID", required=True)
@click.option("--report-id", "-r", help="Report ID", required=True)
@click.option(
    "--target",
    "-t",
    type=click.Path(exists=False, path_type=Path),
    help="target file (if omitted, prints info to console)",
    default=None,
    required=False,
)
def reports_pages(group_id: str, report_id: str, target: Optional[Path]):
    """Get all pages of a report in a workspace group.

    Retrieves the list of pages for the given report from the specified
    workspace group and either prints the result to the console or saves it
    to a JSON file.
    """
    pbi_report = powerbi_report.Report(
        auth=load_auth(), verify=False, report_id=report_id, group_id=group_id
    )
    result = pbi_report.pages

    if target is None:
        click.echo(json.dumps(result, indent=2))
    else:
        with open(target, "w") as fp:
            json.dump(result, fp, indent=2)
        click.secho(f"✓ Report pages saved to {target}", fg="green")


@reports.command(name="all-pages")
@click.option("--group-id", "-g", help="Group (workspace) ID", required=True)
@click.option(
    "--target",
    "-t",
    type=click.Path(exists=False, path_type=Path),
    help="target file (if omitted, prints info to console)",
    default=None,
    required=False,
)
def reports_all_pages(group_id: str, target: Optional[Path]):
    """Get all pages of every report in a workspace group.

    Retrieves the list of reports for the specified group, then fetches the
    pages for each report. The combined result is either printed to the console
    or saved to a JSON file.
    """
    group_reports = powerbi_report.GroupReports(
        auth=load_auth(), verify=False, group_id=group_id
    )
    result = group_reports.all_pages()

    if target is None:
        click.echo(json.dumps(result, indent=2))
    else:
        with open(target, "w") as fp:
            json.dump(result, fp, indent=2)
        click.secho(f"✓ All report pages saved to {target}", fg="green")


@workspaces.group(name="scan", invoke_without_command=True)
@click.pass_context
def workspaces_scan(ctx):
    """Command group for workspace scan operations.

    !!! warning "Requires Admin"

        All commands in this group require an admin account.

    """
    if ctx.invoked_subcommand is None:
        click.echo("Use pbi workspaces scan --help for help.")


@workspaces_scan.command(name="initiate")
@click.argument("workspace_ids", nargs=-1, required=True)
@click.option(
    "--lineage",
    is_flag=True,
    default=False,
    help="Include lineage information",
)
@click.option(
    "--datasource-details",
    is_flag=True,
    default=False,
    help="Include datasource details",
)
@click.option(
    "--dataset-schema",
    is_flag=True,
    default=False,
    help="Include dataset schema",
)
@click.option(
    "--dataset-expressions",
    is_flag=True,
    default=False,
    help="Include dataset expressions",
)
@click.option(
    "--get-artifact-users",
    is_flag=True,
    default=False,
    help="Include artifact users",
)
def scan_initiate(
    workspace_ids: tuple,
    lineage: bool,
    datasource_details: bool,
    dataset_schema: bool,
    dataset_expressions: bool,
    get_artifact_users: bool,
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
    click.echo(json.dumps(result, indent=2))


@workspaces_scan.command(name="result")
@click.argument("scan_id")
@click.option(
    "--target",
    "-t",
    type=click.Path(exists=False, path_type=Path),
    help="Target file to save scan results (if omitted, prints to console)",
    default=None,
    required=False,
)
def scan_result(scan_id: str, target: Optional[Path]):
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
        click.echo(json.dumps(result, indent=2))
    else:
        with open(target, "w") as fp:
            json.dump(result, fp, indent=2)
        click.secho(f"✓ Scan results saved to {target}", fg="green")


@workspaces_scan.command(name="get")
@click.argument("workspace_ids", nargs=-1, required=True)
@click.option(
    "--lineage",
    is_flag=True,
    default=False,
    help="Include lineage information",
)
@click.option(
    "--datasource-details",
    is_flag=True,
    default=False,
    help="Include datasource details",
)
@click.option(
    "--dataset-schema",
    is_flag=True,
    default=False,
    help="Include dataset schema",
)
@click.option(
    "--dataset-expressions",
    is_flag=True,
    default=False,
    help="Include dataset expressions",
)
@click.option(
    "--get-artifact-users",
    is_flag=True,
    default=False,
    help="Include artifact users",
)
@click.option(
    "--interval",
    type=click.FloatRange(min=0, min_open=True),
    default=5.0,
    show_default=True,
    help="Seconds to wait between status checks",
)
@click.option(
    "--timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=300.0,
    show_default=True,
    help="Maximum seconds to wait for scan completion",
)
@click.option(
    "--target",
    "-t",
    type=click.Path(exists=False, path_type=Path),
    help="Target file to save scan results (if omitted, prints to console)",
    default=None,
    required=False,
)
def scan_get(
    workspace_ids: tuple,
    lineage: bool,
    datasource_details: bool,
    dataset_schema: bool,
    dataset_expressions: bool,
    get_artifact_users: bool,
    interval: float,
    timeout: float,
    target: Optional[Path],
):
    """Initiate a scan for WORKSPACE_IDS, wait for completion, and return results.

    Combines ``pbi workspaces scan initiate`` and ``pbi workspaces scan result``
    into a single step: starts the scan, polls until it completes (or times out),
    then prints or saves the results.

    ```sh
    pbi workspaces scan get <workspace-id>

    pbi workspaces scan get <workspace-id> <workspace-id> --lineage -t results.json
    ```

    !!! warning "Requires Admin"

        This command requires an admin account.

    """
    import time

    workspace_info = powerbi_admin.WorkspaceInfo(
        auth=load_auth(group="admin"), verify=False
    )

    click.echo("Initiating scan…")
    scan_response = workspace_info.initiate_scan(
        workspace_ids=[*workspace_ids],
        lineage=lineage,
        datasource_details=datasource_details,
        dataset_schema=dataset_schema,
        dataset_expressions=dataset_expressions,
        get_artifact_users=get_artifact_users,
    )
    scan_id = scan_response.get("id")
    if not scan_id:
        raise click.ClickException(f"Unexpected initiate response: {scan_response}")
    click.echo(f"Scan started (id={scan_id}). Waiting for results…")

    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            result = workspace_info.get_scan_result(scan_id=scan_id)
            break
        except powerbi_admin.ScanNotReadyError:
            if time.monotonic() >= deadline:
                raise click.ClickException(
                    f"Scan {scan_id} did not complete within {timeout}s."
                )
            remaining = deadline - time.monotonic()
            sleep_time = min(interval, remaining)
            click.echo(
                f"  Attempt {attempt}: scan not ready, retrying in {sleep_time:.0f}s…"
            )
            time.sleep(sleep_time)

    if target is None:
        click.echo(json.dumps(result, indent=2))
    else:
        with open(target, "w") as fp:
            json.dump(result, fp, indent=2)
        click.secho(f"✓ Scan results saved to {target}", fg="green")


if __name__ == "__main__":
    pbi()
