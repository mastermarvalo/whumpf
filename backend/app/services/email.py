"""Email sending — pluggable provider with a no-op console default.

The full account-lifecycle flow (verification, password reset) is wired
end-to-end on the backend. Email delivery is the only piece that needs an
external provider; the `console` default just logs the code/URL the user would
otherwise receive, so the flow remains usable for self-testing on a host
without SMTP/transactional-email infrastructure.

Switch providers by setting `mail_provider=resend` in .env and supplying
`resend_api_key`. Resend was picked because it has a free tier, a simple
HTTP API, and no SDK dependency.
"""

from __future__ import annotations

import logging
from html import escape
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

def _code_parts(code: str, purpose: str) -> tuple[str, str]:
    return (
        f"Your code to {purpose}: {code}\n\nIt expires in 15 minutes.",
        (f"<p>Your code to {purpose}:</p>"
        f'<p style="font-size:24px;font-weight:bold;letter-spacing:4px">{code}</p>'
        f"<p>It expires in 15 minutes.</p>"),
    )


async def send_verification_email(*, to: str, code: str, token: str | None = None) -> None:
    """``token`` adds the legacy ?verify= link for the old website."""
    s = get_settings()
    subject = "Verify your whumpf email"
    code_text, code_html = _code_parts(code, "verify your email")
    text = f"Welcome to whumpf!\n\n{code_text}\n\n"
    html = f"<p>Welcome to whumpf!</p>{code_html}"
    if token:
        url = f"{s.app_base_url.rstrip('/')}/?verify={token}"
        text += (f"On the website you can use this link instead: {url}\n"
                 f"(expires in {s.email_verification_ttl_s // 3600}h)\n\n")
        html += f'<p>On the website you can <a href="{url}">verify with this link</a> instead.</p>'
    text += "If you didn't sign up, ignore this email."
    html += "<p>If you didn't sign up, ignore this email.</p>"
    await _provider().send(to=to, subject=subject, html=html, text=text)


async def send_password_reset_email(*, to: str, code: str, token: str | None = None) -> None:
    """``token`` adds the legacy ?reset= link for the old website."""
    s = get_settings()
    subject = "Reset your whumpf password"
    text, html = _code_parts(code, "reset your whumpf password")
    text += "\n\n"
    if token:
        url = f"{s.app_base_url.rstrip('/')}/?reset={token}"
        text += (f"On the website you can use this link instead: {url}\n"
                 f"(expires in {s.password_reset_ttl_s // 60} minutes)\n\n")
        html += f'<p>On the website you can <a href="{url}">reset with this link</a> instead.</p>'
    text += "If you didn't request a reset, you can safely ignore this email."
    html += "<p>If you didn't request a reset, ignore this email.</p>"
    await _provider().send(to=to, subject=subject, html=html, text=text)


async def send_email_change_code(*, to: str, code: str) -> None:
    subject = "Confirm your new whumpf email"
    text, html = _code_parts(code, "confirm this address for your whumpf account")
    text += "\n\nIf you didn't ask to change your email, ignore this email."
    html += "<p>If you didn't ask to change your email, ignore this email.</p>"
    await _provider().send(to=to, subject=subject, html=html, text=text)


async def send_security_notice(*, to: str, what: str) -> None:
    """Tell the account's (old) address that ``what`` happened."""
    subject = "Your whumpf account was changed"
    text = (
        f"{what}\n\n"
        "If this was you, no action is needed. If not, reset your password "
        "right away and contact support."
    )
    html = (
        f"<p>{escape(what)}</p>"
        "<p>If this was you, no action is needed. If not, reset your password "
        "right away and contact support.</p>"
    )
    await _provider().send(to=to, subject=subject, html=html, text=text)


async def send_strava_data_deleted(*, to: str, why: str, routes_deleted: int) -> None:
    """Written confirmation that a user's Strava data was deleted (Strava API
    Policy §2.5). ``why`` is a sentence such as "You disconnected Strava."."""
    subject = "Your Strava data was deleted from whumpf"
    routes = (
        f"{routes_deleted} route{'s' if routes_deleted != 1 else ''} imported from Strava"
        if routes_deleted else "no routes imported from Strava"
    )
    done = (
        f"We deleted your Strava connection and sign-in tokens, and {routes} "
        "(copies included). Routes you drew yourself are not affected."
    )
    again = "You can connect Strava again at any time in the app."
    text = f"{why}\n\n{done}\n\n{again}"
    html = f"<p>{escape(why)}</p><p>{escape(done)}</p><p>{escape(again)}</p>"
    await _provider().send(to=to, subject=subject, html=html, text=text)


async def send_friend_request(*, to: str, requester_email: str) -> None:
    s = get_settings()
    url = s.app_base_url.rstrip("/")
    subject = "You have a new friend request on whumpf"
    text = (
        f"{requester_email} wants to be your friend on whumpf.\n\n"
        f"Log in to respond: {url}"
    )
    html = (
        f"<p><b>{requester_email}</b> wants to be your friend on whumpf.</p>"
        f'<p><a href="{url}">Log in to respond</a></p>'
    )
    await _provider().send(to=to, subject=subject, html=html, text=text)


async def send_trip_invite(*, to: str, trip_name: str, inviter_email: str, trip_id: int) -> None:
    s = get_settings()
    url = f"{s.app_base_url.rstrip('/')}/?trip_invite={trip_id}"
    subject = f"{inviter_email} invited you to a whumpf trip"
    text = (
        f"{inviter_email} invited you to the trip \"{trip_name}\" on whumpf.\n\n"
        f"Log in to view the plan and respond: {url}"
    )
    html = (
        f'<p><b>{inviter_email}</b> invited you to the trip "<b>{trip_name}</b>" on whumpf.</p>'
        f'<p><a href="{url}">View the plan and respond</a></p>'
    )
    await _provider().send(to=to, subject=subject, html=html, text=text)
