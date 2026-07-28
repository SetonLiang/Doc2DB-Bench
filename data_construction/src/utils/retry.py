from __future__ import annotations


def should_retry(attempt: int, max_retries: int) -> bool:
    """Return whether another retry is allowed."""
    return attempt < max_retries
