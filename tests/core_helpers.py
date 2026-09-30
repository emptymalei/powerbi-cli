"""Helpers shared by the tests of ``pbi_cli.core``: synthetic tokens and a fake API."""

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Pattern, Tuple, Union

import requests
from requests.adapters import BaseAdapter

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)


def _b64(data: Dict[str, Any]) -> str:
    raw = json.dumps(data).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_token(
    tenant: Optional[str] = "tenant-1",
    expires_in: Optional[timedelta] = timedelta(hours=1),
    now: datetime = NOW,
    **claims: Any,
) -> str:
    """Build an unsigned, synthetic JWT (only the claims matter to pbi-cli)."""
    payload: Dict[str, Any] = dict(claims)
    if tenant is not None:
        payload["tid"] = tenant
    if expires_in is not None:
        payload["exp"] = int((now + expires_in).timestamp())
    return ".".join([_b64({"alg": "none", "typ": "JWT"}), _b64(payload), "signature"])


Responder = Union[requests.Response, Exception]


def make_response(
    status: int = 200,
    body: Any = None,
    headers: Optional[Dict[str, str]] = None,
    text: Optional[str] = None,
) -> requests.Response:
    """Build a ``requests.Response`` without a network."""
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    if text is not None:
        response._content = text.encode("utf-8")
    elif body is not None:
        response._content = json.dumps(body).encode("utf-8")
        response.headers.setdefault("Content-Type", "application/json")
    else:
        response._content = b""
    response.encoding = "utf-8"
    return response


class FakeAdapter(BaseAdapter):
    """A ``requests`` transport that answers from a script instead of the network.

    Routes are matched on the HTTP method and a substring or regex of the full URL, in
    the order they were added; a route answers with its responses in turn and repeats
    the last one.
    """

    def __init__(self) -> None:
        super().__init__()
        self.requests: List[requests.PreparedRequest] = []
        self._routes: List[
            Tuple[str, Union[str, Pattern[str]], List[Responder], List[int]]
        ] = []

    def add(
        self, method: str, url: Union[str, Pattern[str]], *responses: Responder
    ) -> "FakeAdapter":
        self._routes.append((method.upper(), url, list(responses), [0]))
        return self

    def session(self) -> requests.Session:
        session = requests.Session()
        session.mount("https://", self)
        session.mount("http://", self)
        return session

    def calls(self, method: Optional[str] = None) -> List[requests.PreparedRequest]:
        return [r for r in self.requests if method is None or r.method == method]

    def urls(self) -> List[str]:
        return [str(r.url) for r in self.requests]

    def send(self, request, **kwargs):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        url = str(request.url)
        for method, pattern, responses, counter in self._routes:
            if method != request.method:
                continue
            matched = (
                pattern.search(url)
                if isinstance(pattern, re.Pattern)
                else pattern in url
            )
            if not matched:
                continue
            index = min(counter[0], len(responses) - 1)
            counter[0] += 1
            outcome = responses[index]
            if isinstance(outcome, Exception):
                raise outcome
            outcome.url = url
            outcome.request = request
            return outcome
        raise AssertionError(f"FakeAdapter: no route for {request.method} {url}")

    def close(self) -> None:
        pass


class FakeClock:
    """A clock that only moves when the code under test sleeps (or the test moves it)."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start
        self.slept: List[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
