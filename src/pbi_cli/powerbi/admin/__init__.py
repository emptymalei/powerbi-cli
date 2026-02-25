from functools import cached_property
from typing import List, Literal, Optional

import pandas as pd
from loguru import logger

from pbi_cli.powerbi.base import Base
from pbi_cli.web import DataRetriever


class Workspaces:
    """Accessing all workspaces

    :param auth: dict containing the auth `{"Authorization": "Bearer xxx"}`
    :param verify: whether to verify ssl
    """

    def __init__(self, auth: dict, verify: bool = True):
        self.auth = auth
        self.verify = verify

    @property
    def _data_retriever(self):
        return DataRetriever(
            session_query_configs={"headers": self.auth, "verify": self.verify}
        )

    @property
    def _base_uri(self) -> str:
        return "https://api.powerbi.com/v1.0/myorg/admin/groups"

    @staticmethod
    def _flatten_workspace(data_workspace: dict) -> dict:

        workspace_level_keys = [
            k
            for k, v in data_workspace.items()
            if not isinstance(data_workspace.get(k), list)
        ]
        keys = ["users", "reports", "dashboards", "datasets", "dataflows", "workbooks"]

        flattened = {}

        for key in keys:
            flattened[key] = [
                {
                    **{
                        k: v
                        for k, v in data_workspace.items()
                        if k in workspace_level_keys
                    },
                    **{f"{key}_{d_k}": d[d_k] for d_k in d},
                }
                for d in data_workspace.get(key, [])
            ]

        flattened["workspace"] = [
            {k: v for k, v in data_workspace.items() if k in workspace_level_keys}
        ]

        return flattened

    def flatten_workspaces(self, data_all_workspaces: list[dict]):
        all_workspaces = []
        for w in data_all_workspaces:
            all_workspaces.append(self._flatten_workspace(w))

        all_workspaces = {
            k: sum([w.get(k, []) for w in all_workspaces], [])
            for k in all_workspaces[0]
        }

        return all_workspaces

    def __call__(
        self,
        top: int = 1000,
        expand: Optional[
            List[
                Literal[
                    "users",
                    "reports",
                    "dashboards",
                    "datasets",
                    "dataflows",
                    "workbooks",
                ]
            ]
        ] = None,
        filter: Optional[str] = None,
        format: Literal["raw", "flatten"] = "raw",
    ):
        """

        See https://learn.microsoft.com/en-us/rest/api/power-bi/admin/groups-get-groups-as-admin

        :param top: top n results
        :param expand: see official docs
        :param filter: odata filter, see official docs
        """
        query_params = {"top": top}

        if (
            (expand is not None)
            and isinstance(expand, (list, tuple))
            and len(expand) >= 1
        ):
            query_params["expand"] = "%2C".join(expand)

        if filter is not None:
            query_params["filter"] = filter

        query_params_encoded = "&".join(
            [f"%24{k}={v}" for k, v in query_params.items()]
        )
        uri = f"{self._base_uri}?{query_params_encoded}"
        logger.info(f"Using API Endpoint: {uri}")

        result = self._data_retriever.get(uri).json()

        if format == "raw":
            return result
        elif format == "flatten":
            return self.flatten_workspaces(result["value"])


class User:
    """Accessing user info

    :param auth: dict containing the auth `{"Authorization": "Bearer xxx"}`
    :param verify: whether to verify ssl
    """

    def __init__(self, auth: dict, user_id: str, verify: bool = True):
        self.auth = auth
        self.verify = verify
        self.user_id = user_id

    @property
    def _data_retriever(self):
        return DataRetriever(
            session_query_configs={"headers": self.auth, "verify": self.verify}
        )

    @property
    def _base_uri(self) -> str:
        return "https://api.powerbi.com/v1.0/myorg/admin/users/{userId}/artifactAccess"

    def _get_user_artifacts(
        self,
        user_id: str,
        continueation_uri: Optional[str] = None,
        existing_data: Optional[list] = [],
    ) -> list[dict]:
        """downloading user artifacts access

        :param user_id: user id
        :param continueation_uri: continuation uri from the API return
        :param existing_data: existing data to append to
        """
        uri = self._base_uri.format(userId=user_id)

        if continueation_uri is None:
            current_page = self._data_retriever.get(uri).json()
        else:
            logger.info(f"Using continuation uri: {continueation_uri}")
            current_page = self._data_retriever.get(continueation_uri).json()

        if current_page.get("error"):
            raise ValueError(f"Error: {current_page}")

        current_data = current_page.get("ArtifactAccessEntities", [])
        logger.info(f"Downloaded {len(current_data)} results")

        existing_data.extend(current_data)

        return existing_data

    @cached_property
    def user_artifacts(self) -> list[dict]:
        return self._get_user_artifacts(user_id=self.user_id)

    def __call__(self) -> dict:

        return {
            "artifacts": self.user_artifacts,
        }


class Apps(Base):
    """
    A class to interact with Power BI Apps using the Base class.

    :param auth: dict containing the auth `{"Authorization": "Bearer xxx"}`
    :param verify: whether to verify SSL
    """

    def __init__(self, auth: dict, verify: bool = True):
        super().__init__(auth=auth, verify=verify)

    @property
    def _base_uri(self) -> str:
        """
        Returns the base URI for Power BI Apps API.
        """
        return "https://api.powerbi.com/v1.0/myorg/admin/apps"

    def __call__(
        self,
        top: int = 200,
        format: Literal["raw", "flatten"] = "raw",
    ):
        query_params = {"top": top}

        query_params_encoded = self._encode_query_params(query_params)
        uri = f"{self._base_uri}?{query_params_encoded}"
        result = self._data_retriever.get(uri).json()

        if result.get("error"):
            raise ValueError(f"Error: {result}")

        logger.info(f"Listing {len(result.get('value', []))} results")

        if format == "raw":
            return result
        elif format == "flatten":
            return result["value"]


class WorkspaceInfo(Base):
    """
    A class to initiate workspace scans and retrieve scan results via the
    Power BI Admin Workspace Info API.

    :param auth: dict containing the auth ``{"Authorization": "Bearer xxx"}``
    :param verify: whether to verify SSL
    """

    def __init__(self, auth: dict, verify: bool = True):
        super().__init__(auth=auth, verify=verify)

    @property
    def _base_uri(self) -> str:
        """
        Returns the base URI for the Workspace Info API.
        """
        return "https://api.powerbi.com/v1.0/myorg/admin/workspaces"

    def initiate_scan(
        self,
        workspace_ids: List[str],
        lineage: bool = False,
        datasource_details: bool = False,
        dataset_schema: bool = False,
        dataset_expressions: bool = False,
        get_artifact_users: bool = False,
    ) -> dict:
        """
        Initiate a workspace scan via the Power BI Admin API.

        See https://learn.microsoft.com/en-us/rest/api/power-bi/admin/workspace-info-post-workspace-info

        :param workspace_ids: list of workspace IDs to scan
        :param lineage: whether to include lineage information
        :param datasource_details: whether to include datasource details
        :param dataset_schema: whether to include dataset schema
        :param dataset_expressions: whether to include dataset expressions
        :param get_artifact_users: whether to include artifact users
        :return: API response as a dict containing the scan ID
        """
        query_params = {
            "lineage": str(lineage).lower(),
            "datasourceDetails": str(datasource_details).lower(),
            "datasetSchema": str(dataset_schema).lower(),
            "datasetExpressions": str(dataset_expressions).lower(),
            "getArtifactUsers": str(get_artifact_users).lower(),
        }
        query_string = "&".join([f"{k}={v}" for k, v in query_params.items()])
        uri = f"{self._base_uri}/getInfo?{query_string}"
        logger.info(f"Using API Endpoint: {uri}")

        payload = {"workspaces": workspace_ids}
        result = self._data_retriever.post(uri, body=payload).json()

        if result.get("error"):
            raise ValueError(f"Error: {result}")

        return result

    def get_scan_result(self, scan_id: str) -> dict:
        """
        Retrieve the scan result for a given scan ID.

        See https://learn.microsoft.com/en-us/rest/api/power-bi/admin/workspace-info-get-scan-result

        :param scan_id: the scan ID returned by :meth:`initiate_scan`
        :return: API response as a dict containing the scan results
        """
        uri = f"{self._base_uri}/scanResult/{scan_id}"
        logger.info(f"Using API Endpoint: {uri}")

        result = self._data_retriever.get(uri).json()

        if result.get("error"):
            raise ValueError(f"Error: {result}")

        return result
