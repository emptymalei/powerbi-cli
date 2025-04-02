import random
from functools import cached_property
from typing import Optional

import requests
from loguru import logger
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry


class DataRetriever:
    def __init__(
        self,
        session: Optional[requests.Session] = None,
        session_query_configs: Optional[dict] = None,
    ):
        if session is None:
            session = requests.Session()
        self._session = session

        if session_query_configs is None:
            session_query_configs = self.get_session_query_configs()

        self.session_query_configs = session_query_configs

    def get_random_user_agent(self) -> dict:
        """
        get_random_user_agent returns a random user agent.

        We provide two predefined browers, chrome and firefox.

        :return: dictionary for requests module to consude as {'User-Agent': "blabla"}
        """

        browsers = ["chrome", "firefox"]

        chrome_user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.113 Safari/537.36",
            "Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.90 Safari/537.36",
            "Mozilla/5.0 (Windows NT 5.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.90 Safari/537.36",
            "Mozilla/5.0 (Windows NT 6.2; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.90 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/44.0.2403.157 Safari/537.36",
            "Mozilla/5.0 (Windows NT 6.3; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.113 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/57.0.2987.133 Safari/537.36",
            "Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/57.0.2987.133 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/55.0.2883.87 Safari/537.36",
            "Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/55.0.2883.87 Safari/537.36",
        ]
        firefox_user_agents = [
            "Mozilla/4.0 (compatible; MSIE 9.0; Windows NT 6.1)",
            "Mozilla/5.0 (Windows NT 6.1; WOW64; Trident/7.0; rv:11.0) like Gecko",
            "Mozilla/5.0 (compatible; MSIE 9.0; Windows NT 6.1; WOW64; Trident/5.0)",
            "Mozilla/5.0 (Windows NT 6.1; Trident/7.0; rv:11.0) like Gecko",
            "Mozilla/5.0 (Windows NT 6.2; WOW64; Trident/7.0; rv:11.0) like Gecko",
            "Mozilla/5.0 (Windows NT 10.0; WOW64; Trident/7.0; rv:11.0) like Gecko",
            "Mozilla/5.0 (compatible; MSIE 9.0; Windows NT 6.0; Trident/5.0)",
            "Mozilla/5.0 (Windows NT 6.3; WOW64; Trident/7.0; rv:11.0) like Gecko",
            "Mozilla/5.0 (compatible; MSIE 9.0; Windows NT 6.1; Trident/5.0)",
            "Mozilla/5.0 (Windows NT 6.1; Win64; x64; Trident/7.0; rv:11.0) like Gecko",
            "Mozilla/5.0 (compatible; MSIE 10.0; Windows NT 6.1; WOW64; Trident/6.0)",
            "Mozilla/5.0 (compatible; MSIE 10.0; Windows NT 6.1; Trident/6.0)",
            "Mozilla/4.0 (compatible; MSIE 8.0; Windows NT 5.1; Trident/4.0; .NET CLR 2.0.50727; .NET CLR 3.0.4506.2152; .NET CLR 3.5.30729)",
        ]

        user_agents_dict = {
            "chrome": chrome_user_agents,
            "firefox": firefox_user_agents,
        }

        # error if specified browser is not in the list
        if set(browsers) - set(user_agents_dict.keys()):
            logger.error(
                f"Unknown browser: {set(browsers) - set(user_agents_dict.keys())}"
            )

        user_agent_list = sum([user_agents_dict[browser] for browser in browsers], [])

        return {"User-Agent": random.choice(user_agent_list)}

    @cached_property
    def session(self) -> requests.Session:
        """
        get_session prepares a session object.
        """
        retry_params = {
            "retries": 5,
            "backoff_factor": 0.3,
            "status_forcelist": (500, 502, 504),
        }

        retry = Retry(
            total=retry_params.get("retries", 5),  # type: ignore
            read=retry_params.get("retries", 5),  # type: ignore
            connect=retry_params.get("retries", 5),  # type: ignore
            backoff_factor=retry_params.get("backoff_factor", 0.3),  # type: ignore
            status_forcelist=retry_params.get("status_forcelist"),  # type: ignore
        )

        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

        return self._session

    def get_session_query_configs(self, headers: dict) -> dict:
        """
        get_session_query_configs creates a session config dictionary for session to use. These are the keyword arguments of the session get or post methods.

        Proxies can be set by providing a dictionary of the form

        ```python
        {
            'http': some super_proxy_url,
            'https': some super_proxy_url,
        }
        ```

        :param headers: header of the method such as use agent, defaults to random user agent from get_random_user_agent
        :param timeout: timeout strategy, defaults to (5, 14)
        :param proxies: proxy configs, defaults to {}
        :param cookies: cookie configs, defaults to {"language": "en"}
        :return: dictionary of session configs for session methods, e.g., get, to use.
        """
        cookies = {"language": "en"}

        headers = {
            **self.get_random_user_agent(),
            **headers
        }

        timeout = (5, 14)

        proxies: dict[str, str] = {}

        return dict(headers=headers, proxies=proxies, cookies=cookies, timeout=timeout)

    def get(
        self,
        link: str,
        params: Optional[dict] = None,
        auth: Optional[HTTPBasicAuth] = None,
    ) -> requests.Response:
        """Download page and save content

        :param headers: header information such as useragent, 
            defaults to random user agent from get_random_user_agent
        """
        content = self.session.get(
            link, auth=auth, params=params, **self.session_query_configs
        )

        return content

    def post(
        self,
        link: str,
        auth: Optional[HTTPBasicAuth] = None,
        data: Optional[dict] = None,
    ) -> requests.Response:
        """Download page and save content

        :param headers: header information such as useragent, defaults to random user agent from get_random_user_agent
        :type headers: dict, optional
        """
        if data is None:
            data = {}
        content = self.session.post(
            link, auth=auth, data=data, **self.session_query_configs
        )

        return content
