"""The service layer of pbi-cli: one API client, one local store, no CLI framework.

The command line (and later the sync engine and the TUI) sit on top of these modules:

``registry``
    The catalog of Power BI REST operations pbi-cli may call, with their documented
    quotas.
``auth`` and ``jwt``
    Credentials, and reading the expiry and tenant out of a bearer token locally.
``ratelimit``
    Local counters of the requests made, to stay within the quotas.
``store``
    The data lake: versioned snapshots and event logs on a local folder or in S3.
``client``
    Sends the requests (paging, throttling, errors) and writes what it fetched to the
    store.
"""
