import json
from functools import cached_property
from pathlib import Path
from typing import List, Literal, Optional

from loguru import logger

from pbi_cli.powerbi.base import Base


class Apps(Base):
    """
    A class to interact with Power BI Apps

    :param auth: dict containing the auth `{"Authorization": "Bearer xxx"}`
    :param verify: whether to verify SSL
    """

    def __init__(
        self, auth: dict, verify: bool = True, cache_file: Optional[Path] = None
    ):
        super().__init__(auth=auth, verify=verify)
        self.cache_file = cache_file
        self.cache = self._load_cache(self.cache_file)

    @staticmethod
    def _load_cache(cache_file: Optional[Path]) -> dict:
        if cache_file is None:
            return {}
        
        with open(cache_file, "r") as fp:
            data = json.load(fp)

        return data

    @property
    def apps(self):
        return [
            App(auth=self.auth, app_id=i.get("id"), verify=self.verify, app_info=i)
            for i in self.cache.get("value", [])
        ]

    @property
    def _base_uri(self) -> str:
        """
        Returns the base URI for Power BI Apps API.
        """
        return "https://api.powerbi.com/v1.0/myorg/apps"

    def _update_cache(self, new_data: dict) -> dict:
        cache_new = new_data
        new_ids = [i.get("id") for i in new_data.get("value", [])]

        cache_new["value"] = [i for i in new_data.get("value", [])] + [
            i for i in self.cache.get("value", []) if i.get("id") not in new_ids
        ]

        self.cache = cache_new

    def __call__(
        self, format: Literal["raw", "flatten"] = "raw", cache_update: bool = True
    ):
        uri = self._base_uri
        result = self._data_retriever.get(uri).json()

        if result.get("error"):
            raise ValueError(f"Error: {result}")

        logger.info(f"Listing {len(result.get('value', []))} results")

        if cache_update:
            self._update_cache(result)

        if format == "raw":
            return result
        elif format == "flatten":
            return result["value"]


class App(Base):
    """
    A class to interact with Power BI App

    :param auth: dict containing the auth `{"Authorization": "Bearer xxx"}`
    :param verify: whether to verify SSL
    """

    def __init__(
        self,
        auth: dict,
        app_id: str,
        verify: bool = True,
        app_info: Optional[dict] = None,
    ):
        super().__init__(auth=auth, verify=verify)
        self.app_id = app_id
        self.app_info = app_info

    @staticmethod
    def flatten_app(data_app: dict) -> dict:

        app_level_keys = [
            k for k, v in data_app.items() if not isinstance(data_app.get(k), list)
        ]
        keys = ["users", "reports", "dashboards"]

        flattened = {}

        for key in keys:
            flattened[key] = [
                {
                    **{
                        app_k: v
                        for app_k, v in data_app.items()
                        if app_k in app_level_keys
                    },
                    **{f"{key}_{d_k}": d[d_k] for d_k in d},
                }
                for d in data_app.get(key, [])
            ]

        flattened["app"] = [{k: v for k, v in data_app.items() if k in app_level_keys}]

        return flattened

    @property
    def _base_uri(self) -> str:
        """base uri"""
        return f"https://api.powerbi.com/v1.0/myorg/apps/{self.app_id}"

    @cached_property
    def meta(self) -> dict:
        uri = f"{self._base_uri}"
        result = self._data_retriever.get(uri).json()

        if result.get("error"):
            raise ValueError(f"Error: {result}")

        return result

    @cached_property
    def reports(self) -> dict:

        uri = f"{self._base_uri}/reports"
        result = self._data_retriever.get(uri).json()

        if result.get("error"):
            raise ValueError(f"Error: {result}")

        logger.info(f"Listing {len(result.get('value', []))} results")

        return result

    @cached_property
    def dashboards(self) -> dict:

        uri = f"{self._base_uri}/dashboards"
        result = self._data_retriever.get(uri).json()

        if result.get("error"):
            raise ValueError(f"Error: {result}")

        logger.info(f"Listing {len(result.get('value', []))} results")

        return result

    def __call__(self):
        result = self.meta
        result["reports"] = self.reports.get("value", [])
        result["dashboards"] = self.dashboards.get("value", [])

        return result
