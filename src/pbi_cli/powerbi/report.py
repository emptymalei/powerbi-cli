import logging
from typing import List, Literal, Optional

import requests

from pbi_cli.powerbi.base import Base

logger = logging.getLogger(__name__)


class Report(Base):
    """
    A class to interact with Power BI Report

    :param auth: dict containing the auth `{"Authorization": "Bearer xxx"}`
    :param verify: whether to verify SSL
    """

    def __init__(
        self,
        auth: dict,
        report_id: str,
        group_id: Optional[str] = None,
        verify: bool = True,
    ):
        super().__init__(auth=auth, verify=verify)
        self.report_id = report_id
        self.group_id = group_id

    @property
    def _base_uri(self) -> str:
        """
        Returns the base URI for Power BI Apps API.
        """
        if self.group_id is None:
            return f"https://api.powerbi.com/v1.0/myorg/reports/{self.report_id}"
        else:
            return f"https://api.powerbi.com/v1.0/myorg/groups/{self.group_id}/reports/{self.report_id}"

    def export(self):

        uri = f"{self._base_uri}/Export"
        req_result = self._data_retriever.get(uri)

        if req_result.ok:
            return req_result.content
        else:
            req_result.raise_for_status()

    @property
    def pages(self) -> dict:
        """
        Returns a list of pages within the report.

        Calls the Power BI API endpoint:
        ``GET /v1.0/myorg/groups/{groupId}/reports/{reportId}/pages``

        :return: dict with the API response containing the list of pages
        :raises requests.HTTPError: if the API request fails
        """
        uri = f"{self._base_uri}/pages"
        req_result = self._data_retriever.get(uri)

        if req_result.ok:
            return req_result.json()
        else:
            req_result.raise_for_status()


class GroupReports(Base):
    """
    A class to interact with all Power BI Reports within a workspace group.

    :param auth: dict containing the auth `{"Authorization": "Bearer xxx"}`
    :param group_id: the workspace/group ID
    :param verify: whether to verify SSL
    """

    def __init__(self, auth: dict, group_id: str, verify: bool = True):
        super().__init__(auth=auth, verify=verify)
        self.group_id = group_id

    @property
    def _base_uri(self) -> str:
        """
        Returns the base URI for the group's reports API.
        """
        return f"https://api.powerbi.com/v1.0/myorg/groups/{self.group_id}/reports"

    @property
    def reports(self) -> dict:
        """
        Returns a list of reports from the specified workspace group.

        Calls the Power BI API endpoint:
        ``GET /v1.0/myorg/groups/{groupId}/reports``

        :return: dict with the API response containing the list of reports
        :raises requests.HTTPError: if the API request fails
        """
        req_result = self._data_retriever.get(self._base_uri)

        if req_result.ok:
            return req_result.json()
        else:
            req_result.raise_for_status()

    def all_pages(self) -> List[dict]:
        """
        Returns pages for every report in the workspace group.

        Iterates over all reports retrieved via :attr:`reports` and fetches
        the pages for each one. Each entry in the returned list contains the
        pages response augmented with the parent report's ``id`` and ``name``.

        If fetching pages for a specific report fails, that report is skipped
        and an error is logged; processing continues with the remaining reports.

        :return: list of dicts, one per report, each containing the report id,
                 report name, and the pages API response
        """
        reports_data = self.reports
        report_list = reports_data.get("value", [])

        results = []
        for report in report_list:
            report_id = report.get("id")
            report_name = report.get("name")
            report_obj = Report(
                auth=self.auth,
                report_id=report_id,
                group_id=self.group_id,
                verify=self.verify,
            )
            try:
                pages_data = report_obj.pages
            except requests.RequestException as exc:
                logger.error(
                    "Failed to retrieve pages for report '%s' (id=%s): %s",
                    report_name,
                    report_id,
                    exc,
                )
                continue
            results.append(
                {
                    "report_id": report_id,
                    "report_name": report_name,
                    "pages": pages_data,
                }
            )

        return results
