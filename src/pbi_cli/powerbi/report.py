from typing import List, Literal, Optional

from pbi_cli.powerbi.base import Base


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
