"""Errors that pbi-cli reports to the user instead of crashing with a traceback."""

from typing import Optional


class PBIError(Exception):
    """A problem the user can act on: bad input, missing credentials, an API failure.

    Commands registered with `pbi_cli.cli_support.command` print the message as
    ``Error: <message>`` on stderr and exit with status 1, without a traceback.
    """


class AuthError(PBIError):
    """The credentials are missing or were refused.

    :param message: what is wrong, in words the user can act on
    :param group: the kind of account it is about (``admin`` or ``user``), when known, so
        that something that asks for a new token can ask for the right kind
    """

    def __init__(self, message: str = "", group: Optional[str] = None):
        super().__init__(message)
        self.group = group


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
    :param endpoint: id of the operation that has no quota left
    """

    def __init__(
        self,
        message: str,
        status: Optional[int] = 429,
        retry_after: Optional[float] = None,
        endpoint: Optional[str] = None,
    ):
        super().__init__(message, status=status)
        self.retry_after = retry_after
        self.endpoint = endpoint


class ScanError(ApiError):
    """A metadata scan failed, was rejected, or did not finish in time.

    :param message: what went wrong
    :param scan_id: the id of the scan, to look it up again (when it got that far)
    """

    def __init__(self, message: str, scan_id: Optional[str] = None):
        super().__init__(message)
        self.scan_id = scan_id


class ScanTimeout(ScanError):
    """A scan did not finish in the time that was given to it.

    The scan may still succeed: its id is kept in `ScanError.scan_id` so that the result
    can be collected later (the API keeps it for 24 hours).
    """


class Stopped(PBIError):
    """Work was stopped on request (for example by the Stop button of the TUI).

    It is not a failure: a sync that is stopped keeps what is done and can be run again.
    """


class ReadOnlyLake(PBIError):
    """Something tried to write to a lake that can only be read.

    Only the work lake (the configured cache folder) is written. A lake opened with
    ``--lake`` is read-only, and so is a published lake, whichever way it was opened.
    """


class OfflineCacheMiss(PBIError):
    """Offline mode was requested but the lake holds no matching snapshot."""
