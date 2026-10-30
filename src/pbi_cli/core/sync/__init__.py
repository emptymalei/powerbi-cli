"""Keeping the Power BI tenant in the data lake: `pbi sync`.

A sync decides what is worth fetching (`targets`), works out what that costs given what the
lake already holds and the quotas of the API (`plan`), fetches it with a pool of threads,
one request or job at a time, and remembers how it went (`state`) so that the next run can
continue where this one stopped: after an expired token, after a quota ran out, after a
failure.
"""
