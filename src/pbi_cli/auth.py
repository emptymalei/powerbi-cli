class PBIAuth:
    """Class to handle OSI PI authentication using AWS Secrets Manager.

    :param secret_id: the secret id in secret manager
    :param region: region of the aws account
    """

    def __init__(
        self, authorization: str
    ):

        self.authorization = authorization

    @property
    def headers(self) -> dict:
        return {
            "Authorization": self.authorization
        }

    @property
    def base_uri(self) -> str:
        return "https://api.powerbi.com/v1.0"
