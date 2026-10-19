"""A logging filter that keeps API keys out of logs.

COtrip takes its key only as a URL parameter (`?apiKey=`), and httpx puts the
full URL in its INFO request lines and in its exception messages. The road
jobs never log URLs or httpx messages (app/jobs/roads_refresh.py), so this
is the backstop: every record that reaches a handler with this filter has
`apiKey=...` and the configured key itself replaced, in the message, the
arguments, the traceback and the stack.

    install()   # on every root handler, after logging.basicConfig
"""

from __future__ import annotations

import logging
import re

from app.config import get_settings

REDACTED = "<redacted>"
_PARAM = re.compile(r"(?i)(apikey=)[^&\s'\"<>]+")


def _secrets() -> list[str]:
    key = get_settings().cotrip_api_key
    # A short value could match ordinary text; real keys are ~30 characters.
    return [key] if len(key) >= 8 else []


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
