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
from pbi_cli.powerbi.admin import User, Workspaces
from pbi_cli.powerbi.io import multi_group_dict_to_excel
from pbi_cli.web import DataRetriever

logger.remove()
logger.add(sys.stderr, level="INFO", enqueue=True)


__CWD__ = os.getcwd()
CONFIG_DIR = Path.home() / ".pbi_cli"
AUTH_CONFIG_FILE = CONFIG_DIR / "auth.json"


def load_auth(auth_path: Path = AUTH_CONFIG_FILE) -> dict:
    """Load the auth file from the config directory

    :param auth_path: Path to the auth file
    """

    with open(auth_path, "r") as fp:
        auth_data = json.load(fp)

    return auth_data


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
def auth(bearer_token: str):
    """get auth bearer token and cache it

    ```
    pbi auth --bearer-token <your_bearer_token>
    ```

    or

    ```
    pbi auth -t <your_bearer_token>
    ```

    for short.

    :param bearer_token: Bearer token to authenticate with Power BI API
    """

    if bearer_token.startswith("Bearer"):
        logger.warning("Do not include the Bearer string in the begining")
        bearer_token = bearer_token.replace("Bearer ", "")

    if not CONFIG_DIR.exists():
        logger.info("creating config folder: {CONFIG_DIR}")
        CONFIG_DIR.mkdir()

    auth_bearer_token = {
        "Authorization": "Bearer " + bearer_token,
    }

    with open(AUTH_CONFIG_FILE, "w") as fp:
        json.dump(auth_bearer_token, fp)

    return auth_bearer_token


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
    type=click.Path(exists=False, path_type=Path),
    help="target folder",
    required=True,
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
    target_folder: Path,
    expand: list,
    filter: Optional[str],
    file_type: list[str],
    file_name: str = "workspaces",
):
    """List Power BI workspaces and save them to files"""

    if not target_folder.exists():
        click.secho(f"creating folder {target_folder}", fg="blue")
        target_folder.mkdir(parents=True, exist_ok=True)

    workspaces = Workspaces(auth=load_auth(), verify=False)

    click.echo(f"Retrieving workspaces for: {top=}, {expand=}, {filter=}")

    result = workspaces(top=top, expand=expand, filter=filter)

    if "json" in file_type:
        json_file_path = target_folder / f"{file_name}.json"
        logger.info(f"Writing to {json_file_path}")
        with open(json_file_path, "w") as fp:
            json.dump(result, fp)

    if "excel" in file_type:
        excel_file_path = target_folder / f"{file_name}.xlsx"
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
    """Convert output format of workspaces"""
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
    type=click.Path(exists=False, path_type=Path),
    help="target folder to save the results to",
    required=True,
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
    default="3",
)
@click.option(
    "--file-name", "-n", type=str, help="file name without extension", default="workspaces_reports_users"
)
def report_users(
    source: Path, target_folder: Path, file_type: str = "json", wait_interval: int = 3,
    file_name: str="workspaces_reports_users"
):
    """
    Augment Power BI Workspace data from a source file
    and save to target file together with report users
    """
    click.secho("getting report user details requires admin token")

    if file_type == "excel":
        if target_folder.suffix:
            click.echo("Please specify a folder instead for excel output")
            raise click.BadOptionUsage(message=f"{target_folder=}")
        else:
            click.secho(f"creating folder {target_folder}", fg="blue")
            target_folder.mkdir(parents=True, exist_ok=True)

    pbi_workspaces = powerbi_workspace.Workspaces(
        auth=load_auth(), verify=False, cache_file=source
    )

    report_users = pbi_workspaces.report_users(
        workspace_types=["Workspace"],
        wait_interval=wait_interval,
    )

    if "json" in file_type:
        json_file_path = target_folder / f"{file_name}.json"
        logger.info(f"Writing json file to {json_file_path}...")
        with open(json_file_path, "w") as fp:
            json.dump(report_users, fp)
    elif file_type == "excel":
        excel_file_path = target_folder / f"{file_name}.xlsx"
        logger.info(f"Writing excel file to {excel_file_path}...")
        multi_group_dict_to_excel(
            report_users, excel_file_path
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
    type=click.Path(exists=False, path_type=Path),
    help="target file",
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
    target_folder: Optional[Path],
    file_types: list,
    file_name: Optional[str] = None,
):
    """Get user access information from Power BI API"""
    if file_name is None:
        file_name = slugify(user_id)

    user = User(auth=load_auth(), user_id=user_id, verify=False)

    result = user()

    if target_folder is None:
        logger.info(f"No target folder provided, printing to interface...")
        click.echo(json.dumps(result, indent=4))
    else:
        if not target_folder.exists():
            click.secho(f"creating folder {target_folder}", fg="blue")
            target_folder.mkdir(parents=True, exist_ok=True)

        if "json" in file_types:
            json_file_path = target_folder / f"{file_name}.json"
            logger.info(f"Writing json file to {json_file_path}...")
            with open(json_file_path, "w") as fp:
                json.dump(result, fp)
        if "excel" in file_types:
            excel_file_path = target_folder / f"{file_name}.xlsx"
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
    type=click.Path(exists=False, path_type=Path),
    help="target folder",
    required=True,
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
    target_folder: Path, role: str, file_type: str = "json", file_name: str = "apps"
):
    """List Power BI Apps and save them to files"""
    if not target_folder.exists():
        click.secho(f"creating folder {target_folder}", fg="blue")
        target_folder.mkdir(parents=True, exist_ok=True)

    if role == "user":
        user = powerbi_app.Apps(auth=load_auth(), verify=False)
    elif role == "admin":
        user = powerbi_admin.Apps(auth=load_auth(), verify=False)

    click.echo(f"Listing Apps as {role}")

    result = user()

    if "json" in file_type:
        json_file_path = target_folder / f"{file_name}.json"
        logger.info(f"Writing json file to {json_file_path}")
        with open(json_file_path, "w") as fp:
            json.dump(result, fp)

    if "excel" in file_type:
        excel_file_path = target_folder / f"{file_name}.xlsx"
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
