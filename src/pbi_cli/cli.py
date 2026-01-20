import json
import os
import sys
from pathlib import Path
from typing import Iterable, Optional, Union

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
from pbi_cli.config import (
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
CONFIG_DIR = Path.home() / ".pbi_cli"
# Legacy files for migration
AUTH_CONFIG_FILE = CONFIG_DIR / "auth.json"
LEGACY_PROFILES_FILE = CONFIG_DIR / "profiles.json"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
KEYRING_SERVICE = "pbi-cli"


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

    if not CONFIG_DIR.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    credentials = {}
    if CREDENTIALS_FILE.exists():
        with open(CREDENTIALS_FILE, "r") as fp:
            credentials = json.load(fp)

    credentials[profile] = token

    with open(CREDENTIALS_FILE, "w") as fp:
        json.dump(credentials, fp, indent=2)

    # Set restrictive permissions on the credentials file
    CREDENTIALS_FILE.chmod(0o600)


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
    if CREDENTIALS_FILE.exists():
        with open(CREDENTIALS_FILE, "r") as fp:
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
    if CREDENTIALS_FILE.exists():
        with open(CREDENTIALS_FILE, "r") as fp:
            credentials = json.load(fp)

        if profile in credentials:
            del credentials[profile]

            with open(CREDENTIALS_FILE, "w") as fp:
                json.dump(credentials, fp, indent=2)

            CREDENTIALS_FILE.chmod(0o600)


def _migrate_legacy_auth():
    """Migrate legacy auth.json to the new profile-based system"""
    pbi_config = PBIConfig()
    
    # First migrate from profiles.json to config.yaml if needed
    migrate_legacy_config()
    
    # Then migrate from auth.json if needed
    if AUTH_CONFIG_FILE.exists() and not pbi_config.profiles:
        try:
            with open(AUTH_CONFIG_FILE, "r", encoding="utf-8") as fp:
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
        "profiles": pbi_config.profiles
    }


def _save_profiles(profiles_data: dict):
    """Save profiles configuration to YAML config"""
    pbi_config = PBIConfig()
    pbi_config.active_profile = profiles_data.get("active_profile")
    pbi_config.profiles = profiles_data.get("profiles", {})


def load_auth(profile: Optional[str] = None) -> dict:
    """Load authentication for the specified profile or active profile

    :param profile: Profile name. If None, uses the active profile.
    :return: dict containing {"Authorization": "Bearer <token>"}
    """
    profiles_data = _load_profiles()

    if profile is None:
        profile = profiles_data.get("active_profile")

    if profile is None:
        raise click.ClickException(
            "No active profile set. Use 'pbi auth' to create a profile or 'pbi switch-profile' to switch profiles."
        )

    if profile not in profiles_data.get("profiles", {}):
        raise click.ClickException(
            f"Profile '{profile}' not found. Use 'pbi profiles list' to see available profiles."
        )

    # Get token from keyring or file
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
@click.option("--bearer-token", "-t", help="Bearer token", required=True)
@click.option(
    "--profile",
    "-p",
    help="Profile name for this credential set",
    default="default",
)
def auth(bearer_token: str, profile: str):
    """Store authentication bearer token securely

    ```
    pbi auth --bearer-token <your_bearer_token>
    ```

    or with a custom profile name:

    ```
    pbi auth -t <your_bearer_token> -p production
    ```

    :param bearer_token: Bearer token to authenticate with Power BI API
    :param profile: Profile name to associate with this credential
    """

    if bearer_token.startswith("Bearer"):
        logger.warning("Do not include the Bearer string in the beginning")
        bearer_token = bearer_token.replace("Bearer ", "")

    if not CONFIG_DIR.exists():
        logger.info(f"Creating config folder: {CONFIG_DIR}")
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Store token securely
    _set_credential(profile, bearer_token)

    # Update profiles configuration
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


@pbi.command(name="switch-profile")
@click.argument("profile_name", required=False)
def switch_profile_cmd(profile_name: Optional[str] = None):
    """Switch the active authentication profile

    ```
    pbi switch-profile
    ```

    or specify a profile:

    ```
    pbi switch-profile production
    ```

    :param profile_name: Profile name to switch to (optional, will show interactive selection if not provided)
    """
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
            f"Profile '{profile_name}' not found. Use 'pbi profiles list' to see available profiles.",
            fg="red",
        )
        return

    profiles_data["active_profile"] = profile_name
    _save_profiles(profiles_data)

    click.secho(f"✓ Switched to profile '{profile_name}'", fg="green")


@pbi.group(name="profiles", invoke_without_command=True)
@click.pass_context
def profiles_group(ctx):
    """Manage authentication profiles"""
    if ctx.invoked_subcommand is None:
        click.echo("Use pbi profiles --help for help.")


@profiles_group.command(name="list")
def list_auth():
    """List all stored authentication profiles

    ```
    pbi list auth
    ```
    """
    profiles_data = _load_profiles()
    profiles = profiles_data.get("profiles", {})
    active_profile = profiles_data.get("active_profile")

    if not profiles:
        click.secho(
            "No profiles found. Use 'pbi auth' to create a profile.", fg="yellow"
        )
        return

    click.echo("Stored authentication profiles:")
    for profile_name in profiles.keys():
        active_marker = " (active)" if profile_name == active_profile else ""
        # Check if token exists
        token_exists = _get_credential(profile_name) is not None
        status = "✓" if token_exists else "✗"
        click.echo(f"  {status} {profile_name}{active_marker}")

    click.echo()
    click.echo(f"Active profile: {active_profile or 'None'}")


@profiles_group.command(name="delete")
@click.argument("profile")
@click.confirmation_option(prompt="Are you sure you want to delete this profile?")
def delete_auth(profile: str):
    """Delete an authentication profile

    ```
    pbi profiles delete production
    ```

    :param profile: Profile name to delete
    """
    profiles_data = _load_profiles()

    if profile not in profiles_data.get("profiles", {}):
        click.secho(
            f"Profile '{profile}' not found. Use 'pbi list auth' to see available profiles.",
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
    click.echo(f"  Default output folder: {pbi_config.default_output_folder or 'Not set'}")
    click.echo(f"  Profiles: {len(pbi_config.profiles)}")
    
    if pbi_config.profiles:
        click.echo("\n  Available profiles:")
        for profile_name in pbi_config.profiles.keys():
            active = " (active)" if profile_name == pbi_config.active_profile else ""
            click.echo(f"    - {profile_name}{active}")


@pbi.command()
@click.option("--group-id", "-g", help="Group ID", required=True)
@click.option("--report-id", "-r", help="Report ID", required=True)
@click.option(
    "--target", "-t", type=click.Path(exists=False), help="target file", required=True
)
def export(group_id: str, report_id: str, target: Path):
    """export report based on id"""
    dr = DataRetriever(session_query_configs={"headers": load_auth(), "verify": False})

    uri = f"https://api.powerbi.com/v1.0/myorg/groups/{group_id}/reports/{report_id}/Export"

    result = dr.get(uri)

    with open(target, "wb") as fp:
        fp.write(result.content)


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
    "--target-folder",
    "-tf",
    type=str,
    help="target folder (absolute path or subfolder within default output folder). If omitted, prints results to console as a table.",
    default=None,
    required=False,
)
@click.option(
    "--expand",
    "-e",
    type=click.Choice(
        ["users", "reports", "dashboards", "datasets", "dataflows", "workbooks"]
    ),
    multiple=True,
)
@click.option("--filter", "-f", type=str, help="odata filter", required=False)
@click.option(
    "--file-type",
    "-ft",
    type=click.Choice(["json", "excel"]),
    default=["json"],
    multiple=True,
)
@click.option("--file-name", "-n", type=str, help="file name", default="workspaces")
def list(
    top: int,
    target_folder: Optional[str],
    expand: list,
    filter: Optional[str],
    file_type: list[str],
    file_name: str = "workspaces",
):
    r"""List Power BI workspaces and save them to files or print to console

    The --target-folder can be:
    - An absolute path: "C:\Users\Name\PowerBI\backups\2024-01-01"
    - A relative subfolder: "2024-01-01" (uses default output folder + this subfolder)
    - Omitted: prints results as a table to the console (no files created)

    ```sh
    # Print to console as a table
    pbi workspaces list
    
    # Using absolute path
    pbi workspaces list -ft json -ft excel -tf "C:\Users\$Env:UserName\PowerBI\backups\$(Get-Date -format 'yyyy-MM-dd')" -e users -e reports -e dashboards -e datasets -e dataflows -e workbooks
    
    # Using relative subfolder (requires default output folder to be configured)
    pbi config set-output-folder "C:\Users\$Env:UserName\PowerBI\backups"
    pbi workspaces list -ft json -ft excel -tf "$(Get-Date -format 'yyyy-MM-dd')" -e users
    ```

    !!! warning "Requires Admin"

        This command requires an admin account.

    """
    
    workspaces = Workspaces(auth=load_auth(), verify=False)

    click.echo(f"Retrieving workspaces for: {top=}, {expand=}, {filter=}")

    result = workspaces(top=top, expand=expand, filter=filter)
    
    # If no target folder provided, print to console as a table
    if target_folder is None:
        if result and "value" in result and len(result["value"]) > 0:
            # Convert to DataFrame and print as table
            df = pd.json_normalize(result["value"])
            
            # Select key columns for display (avoid overwhelming output)
            display_cols = [col for col in ['id', 'name', 'type', 'state', 'isReadOnly', 'isOnDedicatedCapacity'] 
                          if col in df.columns]
            
            if display_cols:
                click.echo("\n" + "=" * 80)
                click.echo(f"Found {len(df)} workspace(s)")
                click.echo("=" * 80)
                click.echo(df[display_cols].to_string(index=False))
                click.echo("=" * 80)
            else:
                # Fallback to all columns if key columns not found
                click.echo("\n" + "=" * 80)
                click.echo(f"Found {len(df)} workspace(s)")
                click.echo("=" * 80)
                click.echo(df.to_string(index=False))
                click.echo("=" * 80)
        else:
            click.echo("No workspaces found.")
        return
    
    # Resolve the target folder path (handles absolute/relative paths)
    target_path = resolve_output_path(target_folder)
    
    # Check if path resolution failed
    if target_path is None:
        click.secho(
            "Error: Unable to determine output folder.",
            fg="red"
        )
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
        auth=load_auth(), verify=False, cache_file=source
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
                    workspace_name = workspace_data.get('name', 'Unknown')
                    for report in workspace_data.get('reports', []):
                        report_info = {
                            'workspace': workspace_name,
                            'report_name': report.get('name', ''),
                            'report_id': report.get('id', ''),
                            **{k: v for k, v in report.items() if k not in ['name', 'id']}
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
        click.secho(
            "Error: Unable to determine output folder.",
            fg="red"
        )
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
def user_access(
    user_id: str,
    target_folder: Optional[str],
    file_types: list,
    file_name: Optional[str] = None,
):
    """Get user access information from Power BI API"""
    if file_name is None:
        file_name = slugify(user_id)

    user = User(auth=load_auth(), user_id=user_id, verify=False)

    result = user()

    if target_folder is None:
        logger.info(f"No target folder provided, printing to console...")
        # Try to format as a table if possible
        if isinstance(result, dict):
            # Flatten and display as table
            try:
                df = pd.json_normalize(result)
                click.echo("\n" + "=" * 80)
                click.echo(f"User Access Information for: {user_id}")
                click.echo("=" * 80)
                click.echo(df.to_string(index=False))
                click.echo("=" * 80)
            except Exception:
                # Fallback to JSON if table formatting fails
                click.echo(json.dumps(result, indent=4))
        else:
            click.echo(json.dumps(result, indent=4))
    else:
        # Resolve the target folder path
        target_path = resolve_output_path(target_folder)
        
        if target_path is None:
            click.secho(
                "Error: Unable to determine output folder.",
                fg="red"
            )
            click.echo("Use 'pbi config set-output-folder' to set a default output folder,")
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
def list(
    target_folder: Optional[str],
    role: str,
    file_type: tuple = ("json", "excel"),
    file_name: str = "apps",
):
    """List Power BI Apps and save them to files or print to console"""
    
    if role == "user":
        user = powerbi_app.Apps(auth=load_auth(), verify=False)
    elif role == "admin":
        user = powerbi_admin.Apps(auth=load_auth(), verify=False)

    click.echo(f"Listing Apps as {role}")

    result = user()
    
    # If no target folder provided, print to console as a table
    if target_folder is None:
        if result and "value" in result and len(result["value"]) > 0:
            df = pd.json_normalize(result["value"])
            
            # Select key columns for display
            display_cols = [col for col in ['id', 'name', 'description', 'publishedBy', 'lastUpdate'] 
                          if col in df.columns]
            
            if display_cols:
                click.echo("\n" + "=" * 80)
                click.echo(f"Found {len(df)} app(s)")
                click.echo("=" * 80)
                click.echo(df[display_cols].to_string(index=False))
                click.echo("=" * 80)
            else:
                click.echo("\n" + "=" * 80)
                click.echo(f"Found {len(df)} app(s)")
                click.echo("=" * 80)
                click.echo(df.to_string(index=False))
                click.echo("=" * 80)
        else:
            click.echo("No apps found.")
        return
    
    # Resolve the target folder path
    target_path = resolve_output_path(target_folder)
    
    # Check if path resolution failed
    if target_path is None:
        click.secho(
            "Error: Unable to determine output folder.",
            fg="red"
        )
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
    "--target", "-t", type=click.Path(exists=False), help="target file", required=True
)
@click.option(
    "--file-type", "-ft", type=click.Choice(["json", "excel"]), default="json"
)
def app(app_id: str, target: Path, file_type: str = "json"):
    """Retrieve information about a specific Power BI App"""
    click.echo(f"Investigating {app_id}")

    a_app = powerbi_app.App(auth=load_auth(), verify=False, app_id=app_id)
    app_data = a_app()

    if file_type == "json":
        with open(target, "w") as fp:
            json.dump(app_data, fp)
    elif file_type == "excel":
        app_data_flattened = a_app.flatten_app(app_data)
        multi_group_dict_to_excel(app_data_flattened, target)


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
    help="target file",
    required=True,
)
def export(group_id: str, report_id: str, target: Path):
    """Export report as file"""

    pbi_report = powerbi_report.Report(
        auth=load_auth(), verify=False, report_id=report_id, group_id=group_id
    )

    result = pbi_report.export()
    # click.echo(result)

    with open(target, "wb") as fp:
        fp.write(result)


if __name__ == "__main__":
    pbi()
