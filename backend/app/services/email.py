"""Email sending — pluggable provider with a no-op console default.

The full account-lifecycle flow (verification, password reset) is wired
end-to-end on the backend. Email delivery is the only piece that needs an
external provider; the `console` default just logs the URL the user would
otherwise receive, so the flow remains usable for self-testing on a host
without SMTP/transactional-email infrastructure.

Switch providers by setting `mail_provider=resend` in .env and supplying
`resend_api_key`. Resend was picked because it has a free tier, a simple
HTTP API, and no SDK dependency.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from app.config import get_settings

logger = logging.getLogger("whumpf.email")

_HTTP = httpx.AsyncClient(timeout=10.0)


class MailProvider(Protocol):
    async def send(self, *, to: str, subject: str, html: str, text: str) -> None: ...


class ConsoleProvider:
    """Logs the message instead of sending. Useful for dev/staging."""

    async def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        del html
        logger.info("[email/console] to=%s subject=%s\n%s", to, subject, text)


class ResendProvider:
    """resend.com — uses their HTTP API; no SDK needed."""

    _URL = "https://api.resend.com/emails"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise RuntimeError("RESEND_API_KEY is not set")
        self._api_key = api_key

    async def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        settings = get_settings()
        r = await _HTTP.post(
            self._URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "from": settings.mail_from,
                "to": [to],
                "subject": subject,
                "html": html,
                "text": text,
            },
        )
        r.raise_for_status()


def _provider() -> MailProvider:
    s = get_settings()
    if s.mail_provider == "resend":
        return ResendProvider(s.resend_api_key)
    return ConsoleProvider()


# ── high-level helpers ─────────────────────────────────────────────────────────

async def send_verification_email(*, to: str, token: str) -> None:
    s = get_settings()
    url = f"{s.app_base_url.rstrip('/')}/?verify={token}"
    subject = "Verify your whumpf email"
    text = (
        "Welcome to whumpf!\n\n"
        f"Click here to verify your email: {url}\n\n"
        f"This link expires in {s.email_verification_ttl_s // 3600}h. "
        "If you didn't sign up, ignore this email."
    )
    html = (
        f'<p>Welcome to whumpf!</p>'
        f'<p><a href="{url}">Verify your email</a></p>'
        f"<p>This link expires in {s.email_verification_ttl_s // 3600}h. "
        f"If you didn't sign up, ignore this email.</p>"
    )
    await _provider().send(to=to, subject=subject, html=html, text=text)


async def send_password_reset_email(*, to: str, token: str) -> None:
    s = get_settings()
    url = f"{s.app_base_url.rstrip('/')}/?reset={token}"
    subject = "Reset your whumpf password"
    text = (
        f"Click here to reset your password: {url}\n\n"
        f"This link expires in {s.password_reset_ttl_s // 60} minutes. "
        "If you didn't request a reset, you can safely ignore this email."
    )
    html = (
        f'<p><a href="{url}">Reset your whumpf password</a></p>'
        f"<p>This link expires in {s.password_reset_ttl_s // 60} minutes. "
        f"If you didn't request a reset, ignore this email.</p>"
    )
    await _provider().send(to=to, subject=subject, html=html, text=text)
