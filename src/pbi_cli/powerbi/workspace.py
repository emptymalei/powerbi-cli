from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

import pbi_cli.powerbi.admin.report as powerbi_admin_report
from pbi_cli.powerbi.base import Base


class Workspaces(Base):
    """
    A class to interact with Power BI Workspaces

    :param auth: dict containing the auth `{"Authorization": "Bearer xxx"}`
    :param verify: whether to verify SSL
    """

    def __init__(
        self, auth: dict, verify: bool = True, cache_file: Optional[Path] = None
    ):
        super().__init__(auth=auth, verify=verify)
        self.cache_file = cache_file
        self.cache = self._load_cache(self.cache_file)

    def _load_cache(self, cache_path: Path) -> dict:
        """
        load cached data from a folder containing Excel files.
        """

        return self._load_excel_all_sheets(cache_excel=cache_path)

    @staticmethod
    def _load_excel_all_sheets(cache_excel: Path) -> dict:
        """
        Load cache from a folder containing JSON files.
        """
        if cache_excel.suffix not in ["xlsx", ".xls"]:
            raise ValueError(
                f"Unsupported file type: {cache_excel.suffix}. "
                "Please provide an Excel file (.xlsx or .xls)."
            )

        return pd.read_excel(cache_excel, sheet_name=None)

    @property
    def _base_uri(self) -> str:
        """
        Returns the base URI for Power BI Apps API.
        """
        return "https://api.powerbi.com/v1.0/myorg/groups"

    def report_users(self, workspace_types: Optional[list] = None) -> pd.DataFrame:
        """
        Returns a DataFrame with details of all users of reports in the workspaces.
        """
        logger.info("Getting all users of reports requires admin permissions.")
        if not self.cache:
            logger.error("No cache data available.")

        df_reports = self.cache.get("reports", pd.DataFrame())

        if workspace_types is not None:
            if not isinstance(workspace_types, list):
                raise ValueError("workspace_types must be a list.")
            df_reports = df_reports[df_reports["type"].isin(workspace_types)]

        failed_ids = []
        all_reports = {}
        for workspace_name in df_reports.unique("name"):
            if df_reports.empty:
                logger.warning(f"No reports found in workspace {workspace_name}.")
                return pd.DataFrame()

            workspace_reports_augmented = []
            for r in df_reports.loc[df_reports["name"] == workspace_name].to_dict(
                "records"
            ):
                report_id = r.get("reports_id")
                try:
                    logger.debug(
                        f"Retrieving user info for {r.get('reports_name')}, {report_id}"
                    )
                    r_users = powerbi_admin_report.ReportUsers(
                        auth=self.auth, report_id=report_id, verify=False
                    ).users
                    r_users_augmented = {
                        **{f"reports_users_{r_u_k}": v for r_u_k, v in r_users.items()},
                        **r,
                    }
                    workspace_reports_augmented.append(r_users_augmented)
                except ValueError as e:
                    failed_ids.append(report_id)
                    logger.warning(f"Failed to download {r['name']}, {report_id}\n{e}")

            all_reports[workspace_name] = workspace_reports_augmented

        return all_reports

    def save_as(self, target_path: Path):
        """
        Save the cache data to an Excel file.
        """
        if not target_path.suffix in [".xlsx", ".xls"]:
            raise ValueError(
                f"Unsupported file type: {target_path.suffix}. "
                "Please provide an Excel file (.xlsx or .xls)."
            )

        with pd.ExcelWriter(target_path) as writer:
            for sheet_name, df in self.cache.items():
                df.to_excel(writer, sheet_name=sheet_name, index=False)

        logger.info(f"Cache saved to {target_path}")
