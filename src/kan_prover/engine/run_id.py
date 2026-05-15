"""Stable run-id generation."""

from datetime import UTC, datetime
from secrets import token_hex


def new_run_id(now: datetime | None = None) -> str:
    """Return a sortable run id with a short random suffix."""
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    return f"{instant.strftime('%Y%m%dT%H%M%S%fZ')}-{token_hex(4)}"
