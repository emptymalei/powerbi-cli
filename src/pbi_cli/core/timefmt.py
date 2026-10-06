"""Small helpers that put times in words."""

from datetime import timedelta


def format_age(delta: timedelta) -> str:
    """How old something is, in the largest unit that keeps it short: 40 s, 5 min, 3 h, 2 d."""
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 90:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = round(seconds / 3600)
    if hours < 48:
        return f"{hours} h"
    return f"{round(seconds / 86400)} d"
