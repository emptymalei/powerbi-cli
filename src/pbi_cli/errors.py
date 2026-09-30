"""Errors that pbi-cli reports to the user instead of crashing with a traceback."""

from typing import Optional


class PBIError(Exception):
    """A problem the user can act on: bad input, missing credentials, an API failure.

    Commands registered with :func:`pbi_cli.cli_support.command` print the message as
    ``Error: <message>`` on stderr and exit with status 1, without a traceback.
    """


class AuthError(PBIError):
    """The credentials are missing or were refused."""


class TokenExpiredError(AuthError):
    """The bearer token has expired, or the API answered ``401 Unauthorized``.

    Sign in again and store a fresh token with ``pbi auth``. Long running work such as a
    sync stops when this is raised and can be resumed after signing in.
    """


class ApiError(PBIError):
    """The Power BI API answered with an error status, or could not be reached.

    :param message: what went wrong, in words the user can act on
    :param status: the HTTP status code, when there was a response
    :param code: the error code from the response body (for example ``InvalidRequest``)
    """

    def __init__(
        self,
        message: str,
        status: Optional[int] = None,
        code: Optional[str] = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code


class RateLimitError(ApiError):
    """The API throttled the request (``429``) and retrying would take too long.

    :param message: what happened and when to try again
    :param retry_after: seconds the API (or the local quota counters) asked to wait
    """

    def __init__(
        self,
        message: str,
        status: Optional[int] = 429,
        retry_after: Optional[float] = None,
    ):
        super().__init__(message, status=status)
        self.retry_after = retry_after


class OfflineCacheMiss(PBIError):
    """Offline mode was requested but the lake holds no matching snapshot."""
