"""The Power BI API client.

It sends the requests of the registry (`pbi_cli.core.registry`), reads lists page by
page, waits when the quota is used up or the API throttles (`429 Retry-After`), turns
error answers into messages a person can act on, and writes what it fetched to the lake
so the next call can be answered from disk:

```python
client = PowerBIClient(lambda: Credentials(token), store=LakeStore("~/lake"))
result = client.fetch("admin.groups", {"$expand": ["users"]}, max_age=timedelta(hours=1))
workspaces = result.data["value"]        # result.from_cache tells where it came from
```

Only read-only operations can be fetched (see the registry). Requests are identified by
the endpoint and its *canonical* parameters, so a different ``$expand`` or another tenant
never gets somebody else's cached answer. Tokens are never stored or logged.
"""

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import requests
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from pbi_cli.core.auth import Credentials, CredentialsProvider, ensure_not_expired
from pbi_cli.core.jwt import TokenInfo
from pbi_cli.core.ratelimit import (
    DEFAULT_MAX_WAIT,
    Limiter,
    MaxWait,
    QuotaTracker,
    format_wait,
)
from pbi_cli.core.registry import (
    BASE_URL,
    IDENTITY_PARAM,
    Endpoint,
    Kind,
    Paging,
    get_endpoint,
)
from pbi_cli.core.store import LakeStore, Snapshot, safe_name
from pbi_cli.errors import (
    ApiError,
    OfflineCacheMiss,
    PBIError,
    RateLimitError,
    TokenExpiredError,
)

#: ``max_age`` meaning "any age is fine".
FOREVER = timedelta.max

#: Safety net: a list that needs more pages than this is not read.
MAX_PAGES = 10_000

#: Connections the session keeps for reuse. A sync runs up to 16 requests at once; with the
#: default of 10, the others would open a connection (and a TLS handshake) for each request.
POOL_SIZE = 32

#: Keys of a response that only steer the paging.
_PAGING_KEYS = (
    "continuationUri",
    "continuationToken",
    "lastResultSet",
    "@odata.nextLink",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _user_agent() -> str:
    try:
        return f"pbi-cli/{_package_version('pbi_cli')}"
    except PackageNotFoundError:
        return "pbi-cli"


def make_session() -> requests.Session:
    """A session that retries connection problems and ``5xx`` answers of reads.

    ``429`` is *not* left to the retry adapter: it would sleep for any ``Retry-After``
    without a word, however long. The client handles throttling itself.
    """
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        respect_retry_after_header=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry, pool_connections=POOL_SIZE, pool_maxsize=POOL_SIZE
    )
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


@dataclass
class ApiResponse:
    """One answer of the API.

    :param status: HTTP status code
    :param data: the parsed JSON body, ``None`` when the body was empty
    :param headers: the response headers
    """

    status: int
    data: Any
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass
class Result:
    """What `PowerBIClient.fetch` returns.

    :param data: the response, all pages merged
    :param from_cache: whether it was read from the lake instead of the API
    :param fetched_at: when the response was received from the API (aware, UTC)
    :param manifest: the manifest of the stored snapshot (or what it would contain)
    :param snapshot: the stored snapshot, ``None`` if there is no lake or the write failed
    """

    data: Any
    from_cache: bool
    fetched_at: datetime
    manifest: Dict[str, Any]
    snapshot: Optional[Snapshot] = None


@dataclass(frozen=True)
class Throttled:
    """Reported to ``on_throttle`` when the API asks to wait (``429``).

    :param endpoint_id: the endpoint that was throttled
    :param wait: seconds the client is going to wait
    :param attempt: which retry this is (1 for the first)
    """

    endpoint_id: str
    wait: float
    attempt: int


def rows_of(endpoint: Endpoint, data: Any) -> List[Any]:
    """The list of rows in a response of ``endpoint`` (empty when there is none)."""
    if not endpoint.items_keys:
        return data if isinstance(data, list) else []
    if isinstance(data, dict):
        for key in endpoint.items_keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _as_int(value: Any) -> Optional[int]:
    return None if value is None else int(value)


def _parse_retry_after(response: requests.Response, now: datetime) -> Optional[float]:
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - now).total_seconds())


def _error_details(response: requests.Response) -> Tuple[Optional[str], str]:
    """The error code and message of an error answer."""
    try:
        body = response.json()
    except ValueError:
        body = None
    code: Optional[str] = None
    message: Optional[str] = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")
            pbi = error.get("pbi.error")
            if not code and isinstance(pbi, dict):
                code = pbi.get("code")
        elif isinstance(error, str):
            message = error
        message = message or body.get("message")
    if not message:
        text = (response.text or "").strip()
        message = text[:200] if text else (response.reason or "no details")
    return code, str(message)


class PowerBIClient:
    """Talks to the Power BI REST API and keeps what it learns in the lake.

    The client can be shared by threads.

    :param credentials: returns the current credentials; called before each request
    :param store: the lake to read from and write to; ``None`` disables caching
    :param limiter: makes requests wait for quota; by default one that counts in memory
    :param tenant: the tenant the lake is keyed by; by default the one in the token
    :param session: the HTTP session (replaced in tests)
    :param base_url: where the API lives
    :param verify: TLS verification: ``True``, ``False`` or a CA bundle path
    :param timeout: ``(connect, read)`` timeouts in seconds
    :param max_throttle_retries: how often a ``429`` is retried before giving up
    :param max_throttle_wait: longest ``Retry-After`` the client waits out; a longer one
        raises `RateLimitError` (and blocks the endpoint locally)
    :param clock: returns the current time as an aware UTC datetime
    :param sleep: waits for some seconds (replaced in tests)
    :param on_throttle: called with a `Throttled` before the client waits
    """

    def __init__(
        self,
        credentials: CredentialsProvider,
        *,
        store: Optional[LakeStore] = None,
        limiter: Optional[Limiter] = None,
        tenant: Optional[str] = None,
        session: Optional[requests.Session] = None,
        base_url: str = BASE_URL,
        verify: Any = True,
        timeout: Tuple[float, float] = (10, 60),
        max_throttle_retries: int = 5,
        max_throttle_wait: float = 300.0,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], None] = time.sleep,
        on_throttle: Optional[Callable[[Throttled], None]] = None,
    ):
        self._credentials = credentials
        self._store = store
        self._tenant = tenant
        self._session = session or make_session()
        self._base_url = base_url
        self._verify = verify
        self._timeout = timeout
        self._max_throttle_retries = max_throttle_retries
        self._max_throttle_wait = max_throttle_wait
        self._clock = clock
        self._sleep = sleep
        self._on_throttle = on_throttle
        self._limiter = limiter or Limiter(QuotaTracker(), sleep=sleep)
        self._user_agent = _user_agent()

    # -- lifecycle -----------------------------------------------------------------

    def close(self) -> None:
        """Close the HTTP session."""
        self._session.close()

    def __enter__(self) -> "PowerBIClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    @property
    def store(self) -> Optional[LakeStore]:
        """The lake the client reads from and writes to."""
        return self._store

    @property
    def limiter(self) -> Limiter:
        """The limiter that counts the requests (for showing quotas)."""
        return self._limiter

    # -- identity ------------------------------------------------------------------

    def _tenant_key(self, credentials: Credentials) -> str:
        """What the lake is keyed by: the tenant, or else the profile, never a guess."""
        if self._tenant:
            return self._tenant
        if credentials.tenant_id:
            return credentials.tenant_id
        if credentials.profile:
            return f"profile-{safe_name(credentials.profile)}"
        return "unknown"

    def tenant_key(self) -> str:
        """The key the lake uses for the current credentials."""
        return self._tenant_key(self._credentials())

    def profile_name(self) -> Optional[str]:
        """The name of the profile the current credentials belong to, if they say."""
        return self._credentials().profile

    def identity_key(self) -> str:
        """Who the current credentials are for (see `Credentials.identity`): what the lake
        keeps the answers of the operations that depend on who asks apart by."""
        return self._credentials().identity

    def token_info(self) -> TokenInfo:
        """The tenant and expiry the current token states.

        They are read from the token itself: nothing is sent, and the signature is not
        checked.
        """
        return self._credentials().info

    # -- one request ---------------------------------------------------------------

    def _check_origin(self, url: str) -> None:
        """Never send the token anywhere but to the API (continuation links come from it)."""
        wanted, given = urlsplit(self._base_url), urlsplit(url)
        if (given.scheme, given.netloc.lower()) != (
            wanted.scheme,
            wanted.netloc.lower(),
        ):
            raise ApiError(
                f"Refusing to follow a link to {given.netloc or url!r}: "
                f"it is not {wanted.netloc}."
            )

    def request(
        self,
        endpoint_id: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        body: Any = None,
        url: Optional[str] = None,
        max_wait: MaxWait = DEFAULT_MAX_WAIT,
    ) -> ApiResponse:
        """Send one request and return the parsed answer (no paging, no lake).

        :param endpoint_id: registry id of the operation
        :param params: path placeholders and query parameters
        :param body: JSON body (for the scan start)
        :param url: a continuation link the API returned for this endpoint; replaces the
            URL built from ``params``
        :param max_wait: how long the request may wait for quota (see
            `Limiter.slot`)
        :raises TokenExpiredError: if the token expired or was rejected (``401``)
        :raises RateLimitError: if the quota is used up or the API throttles for too long
        :raises ApiError: for any other error answer or a connection problem
        """
        endpoint = get_endpoint(endpoint_id)
        credentials = self._credentials()
        ensure_not_expired(credentials, now=self._clock())
        tenant = self._tenant_key(credentials)

        if url is None:
            path_params, query = endpoint.split_canonical(params)
            target = endpoint.build_url(path_params, query, self._base_url)
        else:
            self._check_origin(url)
            target = url

        attempt = 0
        while True:
            with self._limiter.slot(endpoint, tenant, max_wait):
                response = self._send(endpoint, target, credentials, body)
            if response.status_code != 429:
                break
            attempt += 1
            wait = _parse_retry_after(response, self._clock())
            if wait is None:
                wait = min(2.0**attempt, 60.0)
            if attempt > self._max_throttle_retries or wait > self._max_throttle_wait:
                self._limiter.block(endpoint, tenant, wait)
                raise RateLimitError(
                    f"Power BI is throttling requests to {endpoint.id} (HTTP 429). "
                    f"Try again in {format_wait(wait)}.",
                    retry_after=wait,
                    endpoint=endpoint.id,
                )
            logger.info(f"Throttled on {endpoint.id}, waiting {format_wait(wait)}")
            if self._on_throttle is not None:
                self._on_throttle(Throttled(endpoint.id, wait, attempt))
            self._sleep(wait)

        self._raise_for_status(endpoint, credentials, response)
        return ApiResponse(
            status=response.status_code,
            data=self._parse_body(endpoint, response),
            headers=dict(response.headers),
        )

    def _send(
        self,
        endpoint: Endpoint,
        url: str,
        credentials: Credentials,
        body: Any,
    ) -> requests.Response:
        headers = {
            **credentials.headers,
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }
        logger.debug(f"{endpoint.method} {endpoint.path}")
        try:
            return self._session.request(
                endpoint.method,
                url,
                headers=headers,
                json=body,
                timeout=self._timeout,
                verify=self._verify,
            )
        except requests.RequestException as error:
            raise ApiError(
                f"{endpoint.id}: the request could not be sent "
                f"({type(error).__name__}): {error}"
            ) from error

    @staticmethod
    def _parse_body(endpoint: Endpoint, response: requests.Response) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise ApiError(
                f"{endpoint.id}: the answer is not JSON (HTTP {response.status_code}).",
                status=response.status_code,
            ) from None

    @staticmethod
    def _raise_for_status(
        endpoint: Endpoint, credentials: Credentials, response: requests.Response
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        code, message = _error_details(response)
        detail = f"{message} [{code}]" if code else message
        if status == 401:
            raise TokenExpiredError(
                f"Power BI rejected the token (401 Unauthorized) for {endpoint.id}. "
                f"Sign in again and store a fresh token with "
                f"`{credentials.sign_in_hint()}`.",
                group=credentials.group,
                profile=credentials.profile,
            )
        if status == 403:
            if endpoint.scope.value == "admin":
                hint = (
                    " This operation needs a Fabric administrator: store that token "
                    "with `pbi auth -t <token> -g admin`."
                )
            elif endpoint.needs:
                hint = f" The account needs {endpoint.needs}."
            else:
                hint = " The account may have no access to this item, or the token lacks the scope."
            raise ApiError(
                f"{endpoint.id}: forbidden (403): {detail}.{hint}",
                status=403,
                code=code,
            )
        if status == 404:
            raise ApiError(
                f"{endpoint.id}: not found (404): {detail}", status=404, code=code
            )
        if status == 400:
            raise ApiError(
                f"{endpoint.id}: invalid request (400): {detail}", status=400, code=code
            )
        raise ApiError(
            f"{endpoint.id}: Power BI answered HTTP {status}: {detail}",
            status=status,
            code=code,
        )

    # -- paging --------------------------------------------------------------------

    def iter_pages(
        self,
        endpoint_id: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        url: Optional[str] = None,
        max_wait: MaxWait = DEFAULT_MAX_WAIT,
    ) -> Iterator[ApiResponse]:
        """Yield the pages of a list that is read with continuation links.

        Use this to store each page as it arrives and to resume: pass the
        ``continuationUri`` of the last stored page as ``url``.

        :raises ApiError: if the API loops (repeats a link) or needs too many pages
        """
        endpoint = get_endpoint(endpoint_id)
        seen = set()
        page = self.request(endpoint.id, params, url=url, max_wait=max_wait)
        pages = 1
        while True:
            yield page
            following = self._next_request(endpoint, params, page.data)
            if following is None:
                return
            key = following[0] or json.dumps(following[1], sort_keys=True)
            if key in seen:
                raise ApiError(f"{endpoint.id}: the API repeated a continuation link.")
            seen.add(key)
            if pages >= MAX_PAGES:
                raise ApiError(
                    f"{endpoint.id}: more than {MAX_PAGES} pages; giving up."
                )
            next_url, next_params = following
            page = self.request(
                endpoint.id, next_params, url=next_url, max_wait=max_wait
            )
            pages += 1

    @staticmethod
    def _next_request(
        endpoint: Endpoint, params: Optional[Mapping[str, Any]], data: Any
    ) -> Optional[Tuple[Optional[str], Optional[Mapping[str, Any]]]]:
        if not isinstance(data, dict):
            return None
        if data.get("continuationUri"):
            return str(data["continuationUri"]), None
        token = data.get("continuationToken")
        if token and "continuationToken" in endpoint.query:
            return None, {**(params or {}), "continuationToken": token}
        return None

    @staticmethod
    def _merge(endpoint: Endpoint, first: Any, rows: List[Any]) -> Any:
        """One response holding all rows, in the shape of a single page."""
        if not endpoint.items_keys:
            return rows
        merged = dict(first) if isinstance(first, dict) else {}
        key = next(
            (k for k in endpoint.items_keys if k in merged), endpoint.items_keys[0]
        )
        merged[key] = rows
        for name in _PAGING_KEYS:
            merged.pop(name, None)
        return merged

    def _collect(
        self,
        endpoint: Endpoint,
        params: Optional[Mapping[str, Any]],
        max_wait: MaxWait,
    ) -> Tuple[Any, int]:
        """Read every page of a request; returns the merged response and the page count."""
        if endpoint.paging is Paging.CONTINUATION:
            first = None
            rows: List[Any] = []
            pages = 0
            for page in self.iter_pages(endpoint.id, params, max_wait=max_wait):
                first = page.data if first is None else first
                rows.extend(rows_of(endpoint, page.data))
                pages += 1
            return self._merge(endpoint, first, rows), pages

        if endpoint.paging is Paging.SKIP:
            return self._collect_skip(endpoint, params, max_wait)

        response = self.request(endpoint.id, params, max_wait=max_wait)
        return response.data, 1

    def _collect_skip(
        self,
        endpoint: Endpoint,
        params: Optional[Mapping[str, Any]],
        max_wait: MaxWait,
    ) -> Tuple[Any, int]:
        assert endpoint.page_size
        given = dict(params or {})
        wanted = _as_int(given.pop("$top", None))  # the caller's cap on the total
        start = _as_int(given.pop("$skip", None)) or 0
        if wanted is not None and wanted <= 0:
            raise ValueError("$top must be at least 1")

        rows: List[Any] = []
        first: Any = None
        pages = 0
        while True:
            want = endpoint.page_size
            if wanted is not None:
                want = min(want, wanted - len(rows))
            page_params: Dict[str, Any] = {**given, "$top": want}
            if start + len(rows):
                page_params["$skip"] = start + len(rows)
            response = self.request(endpoint.id, page_params, max_wait=max_wait)
            pages += 1
            first = response.data if first is None else first
            page_rows = rows_of(endpoint, response.data)
            rows.extend(page_rows)
            if len(page_rows) < want:
                break
            if wanted is not None and len(rows) >= wanted:
                break
            if pages >= MAX_PAGES:
                raise ApiError(
                    f"{endpoint.id}: more than {MAX_PAGES} pages; giving up."
                )
        return self._merge(endpoint, first, rows), pages

    # -- fetching with the lake ----------------------------------------------------

    def _identified(
        self, endpoint: Endpoint, params: Optional[Mapping[str, Any]]
    ) -> Optional[Mapping[str, Any]]:
        """Say who asks, for an operation whose answer depends on it (unless it says)."""
        if not endpoint.per_identity or (params and params.get(IDENTITY_PARAM)):
            return params
        return {**(params or {}), IDENTITY_PARAM: self._credentials().identity}

    def fetch(
        self,
        endpoint_id: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        max_age: Optional[timedelta] = None,
        refresh: bool = False,
        offline: bool = False,
        max_wait: MaxWait = DEFAULT_MAX_WAIT,
    ) -> Result:
        """Get a read-only list or item: from the lake if fresh enough, else from the API.

        What is fetched from the API is written to the lake.

        :param endpoint_id: registry id of a snapshot endpoint
        :param params: path placeholders and query parameters; ``$top`` caps the number
            of rows, without it every page is read
        :param max_age: a stored snapshot younger than this is used (default: the
            endpoint's ``ttl``); `FOREVER` accepts any age, ``timedelta(0)`` always fetches
        :param refresh: ignore the lake and fetch (the result is still stored)
        :param offline: never call the API: answer from the lake whatever its age
            (wins over ``refresh``)
        :param max_wait: how long requests may wait for quota
        :raises OfflineCacheMiss: if ``offline`` and nothing is stored for this request
        :raises PBIError: if ``offline`` and the lake cannot be read (without ``offline``
            an unreadable lake is skipped and the API is asked)
        :raises ValueError: if the endpoint is not a read-only snapshot endpoint
        """
        endpoint = get_endpoint(endpoint_id)
        if endpoint.kind is not Kind.SNAPSHOT or endpoint.method != "GET":
            raise ValueError(
                f"{endpoint.id} cannot be fetched into the lake as a snapshot "
                f"({endpoint.kind.value}); use request() or the sync engine."
            )
        params = self._identified(endpoint, params)
        canonical = endpoint.canonical_params(params)  # also validates the parameters

        credentials: Optional[Credentials] = None
        if self._tenant:
            tenant = self._tenant
        else:
            credentials = self._credentials()
            tenant = self._tenant_key(credentials)

        if self._store is not None and (offline or not refresh):
            try:
                snapshot = self._store.latest(tenant, endpoint.id, canonical)
                if snapshot is not None:
                    limit = max_age if max_age is not None else endpoint.ttl
                    if offline or snapshot.age(self._clock()) < limit:
                        return Result(
                            data=snapshot.load(),
                            from_cache=True,
                            fetched_at=snapshot.fetched_at,
                            manifest=snapshot.manifest,
                            snapshot=snapshot,
                        )
            except Exception as error:  # an unreadable lake is not a reason to fail
                if offline:
                    raise PBIError(
                        f"The data lake could not be read: {error}"
                    ) from error
                logger.warning(
                    f"Could not read {endpoint.id} from the lake, asking the API "
                    f"instead: {error}"
                )
        if offline:
            raise OfflineCacheMiss(
                f"Nothing is stored for {endpoint.id} with these parameters"
                + ("" if self._store else " (no lake is configured)")
                + ". Run the command without --cache-only to fetch it."
            )

        data, pages = self._collect(endpoint, params, max_wait)
        fetched_at = self._clock()
        credentials = credentials or self._credentials()
        rows = (
            len(rows_of(endpoint, data))
            if endpoint.items_keys or isinstance(data, list)
            else None
        )

        snapshot = None
        if self._store is not None:
            try:
                snapshot = self._store.write_snapshot(
                    tenant,
                    endpoint.id,
                    canonical,
                    data,
                    request={
                        "method": endpoint.method,
                        "path": endpoint.path,
                        "params": canonical,
                        "body_sha256": None,
                    },
                    profile=credentials.profile,
                    pages=pages,
                    rows=rows,
                    fetched_at=fetched_at,
                )
            except Exception as error:  # the answer is good even if it cannot be kept
                logger.warning(f"Could not save {endpoint.id} to the lake: {error}")
        manifest = (
            snapshot.manifest
            if snapshot
            else {
                "endpoint": endpoint.id,
                "tenant": tenant,
                "fetched_at": fetched_at.isoformat(),
                "pages": pages,
                "rows": rows,
            }
        )
        return Result(
            data=data,
            from_cache=False,
            fetched_at=fetched_at,
            manifest=manifest,
            snapshot=snapshot,
        )


def body_hash(body: Any) -> Optional[str]:
    """SHA-256 of a request body, for the request record of a manifest."""
    if body is None:
        return None
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
