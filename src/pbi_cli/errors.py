"""Errors that pbi-cli reports to the user instead of crashing with a traceback."""


class PBIError(Exception):
    """A problem the user can act on: bad input, missing credentials, an API failure.

    Commands registered with :func:`pbi_cli.cli_support.command` print the message as
    ``Error: <message>`` on stderr and exit with status 1, without a traceback.
    """
