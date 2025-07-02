from pbi_cli.powerbi.base import Base


class ReportUsers(Base):
    """Users of a report

    :param auth: dict containing the auth `{"Authorization": "Bearer xxx"}`
    :param report_id: id of the report
    :param verify: whether to verify SSL
    """
    def __init__(
        self,
        auth: dict,
        report_id: str,
        verify: bool = True,
    ):
        super().__init__(auth=auth, verify=verify)
        self.report_id = report_id

    @property
    def _base_uri(self) -> str:
        """
        Returns the base URI for Power BI report users.
        """
        return f"https://api.powerbi.com/v1.0/myorg/admin/reports/{self.report_id}/users"

    @property
    def users(self) -> list:
        req_result = self._data_retriever.get(self._base_uri)

        if req_result.ok:
            return req_result.content
        else:
            req_result.raise_for_status()
