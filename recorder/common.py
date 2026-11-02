"""Shared helpers for the recorder store and its study-flow mixins."""

import json
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Authenticated catalog/upload requests must never forward their bearer
        # header or payload to a redirect destination, including same-origin ones.
        return None


def open_no_redirect(request, *, timeout, context=None):
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=context)
    )
    return opener.open(request, timeout=timeout)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_capture_instant(value: Optional[str]) -> datetime:
    """Parse a collector timestamp at the millisecond precision of stored session bounds."""
    # started_at/ended_at come from utc_now() (milliseconds), and event timestamps are
    # normalised the same way. Comparing a raw microsecond value against a truncated
    # bound would push an instant from the stop's own millisecond outside the interval.
    parsed = parse_timestamp(value)
    return parsed.replace(microsecond=parsed.microsecond - parsed.microsecond % 1000)


def json_load(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message
