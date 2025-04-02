from abc import ABC, abstractmethod
from pbi_cli.web import DataRetriever


class Base(ABC):
    """
    Abstract Base Class for accessing all workspaces.

    :param auth: dict containing the auth `{"Authorization": "Bearer xxx"}`
    :param verify: whether to verify SSL
    """

    def __init__(self, auth: dict, verify: bool = True):
        self.auth = auth
        self.verify = verify

    @property
    def _data_retriever(self) -> DataRetriever:
        """
        Returns an instance of DataRetriever configured with session query configs.
        """
        return DataRetriever(
            session_query_configs={"headers": self.auth, "verify": self.verify}
        )
    
    @staticmethod
    def _encode_query_params(query_params: dict, leading_char: str = "%24") -> str:
        query_params_encoded = "&".join([
            f"{leading_char}{k}={v}"
            for k, v in query_params.items()
        ])
        return query_params_encoded

    @property
    @abstractmethod
    def _base_uri(self) -> str:
        """
        Abstract property to return the base URI. Must be implemented by subclasses.
        """
        pass
