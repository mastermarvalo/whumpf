"""Logging filters that keep API keys and tokens out of logs.

COtrip takes its key only as a URL parameter (`?apiKey=`), and httpx puts the
full URL in its INFO request lines and in its exception messages. The road
jobs never log URLs or httpx messages (app/jobs/roads_refresh.py), so this
is the backstop: every record that reaches a handler with this filter has
`apiKey=...` and the configured key itself replaced, in the message, the
arguments, the traceback and the stack.

Strava's webhook check sends our verify token as `?hub.verify_token=`
(GET /strava/webhook), and uvicorn's access log prints every query string,
so `install_access()` redacts it there (and in every other line here).

    install()          # on every root handler, after logging.basicConfig
    install_access()   # on uvicorn's access logger (the API, app/main.py)
"""

from __future__ import annotations

import logging
import re

from app.config import get_settings

REDACTED = "<redacted>"
_PARAM = re.compile(r"(?i)(apikey=|hub\.verify_token=)[^&\s'\"<>]+")


def _secrets() -> list[str]:
    s = get_settings()
    # A short value could match ordinary text; real keys are ~30 characters.
    return [v for v in (s.cotrip_api_key, s.strava_webhook_verify_token) if len(v) >= 8]


def redact(text: str) -> str:
    text = _PARAM.sub(rf"\g<1>{REDACTED}", text)
    for secret in _secrets():
        text = text.replace(secret, REDACTED)
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not drop the record
            message = str(record.msg)
        record.msg, record.args = redact(message), None
        if record.exc_info:
            text = record.exc_text or logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def install(handlers: list[logging.Handler] | None = None) -> None:
    """Add the filter to `handlers` (default: every root handler)."""
    for handler in handlers if handlers is not None else logging.getLogger().handlers:
        if not any(isinstance(f, RedactFilter) for f in handler.filters):
            handler.addFilter(RedactFilter())


class AccessRedactFilter(logging.Filter):
    """For uvicorn's access logger, whose formatter needs the record's args
    tuple (client, method, path, version, status): redact each string in it
    instead of flattening the message."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def install_access(logger_name: str = "uvicorn.access") -> None:
    """Add AccessRedactFilter to the logger (applies to all its records)."""
    logger = logging.getLogger(logger_name)
    if not any(isinstance(f, AccessRedactFilter) for f in logger.filters):
        logger.addFilter(AccessRedactFilter())
