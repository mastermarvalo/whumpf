"""Privacy policy and terms: GET /legal/privacy and /legal/terms.

Public on purpose (CLAUDE.md public exceptions): App Store Connect and
Strava's API Policy (§7.3) need a privacy policy anyone can open, and the
app links both pages. Per-IP limit (`legal` bucket); Cloudflare caches them
(docs/runbook.md §16). The paths are permanent: shipped app builds link them.

The text is app/legal/<page>.md. `{contact_email}` in it becomes a mailto
link to LEGAL_CONTACT_EMAIL. Each page is rendered once per worker.
"""

from __future__ import annotations

import logging
import re
from functools import cache
from html import escape
from pathlib import Path

import markdown
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app import tile_tokens
from app.config import get_settings
from app.rate_limit import client_ip

router = APIRouter(prefix="/legal", tags=["legal"])
logger = logging.getLogger("whumpf.legal")

PAGES_DIR = Path(__file__).resolve().parent.parent / "legal"
PLACEHOLDER = "{contact_email}"

HEADERS = {
    "Cache-Control": "public, max-age=3600",
    # Static text: no scripts, no outside requests, only the inline style.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

_STYLE = """
:root { color-scheme: light dark; --fg: #1c1f23; --bg: #fff; --muted: #5b6470; --rule: #d9dde3; --link: #0b5cad; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e6e8eb; --bg: #15181c; --muted: #9aa3ad; --rule: #2e343b; --link: #7cb7ff; }
}
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 17px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 42em; margin: 0 auto; padding: 1.5em 1.25em 4em; }
h1 { font-size: 1.7em; line-height: 1.2; margin: 0.4em 0 0.2em; }
h2 { font-size: 1.2em; margin: 2em 0 0.5em; padding-top: 1em; border-top: 1px solid var(--rule); }
a { color: var(--link); overflow-wrap: anywhere; }
ul { padding-left: 1.3em; }
li { margin: 0.3em 0; }
.table { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 0.92em; }
th, td { text-align: left; vertical-align: top; padding: 0.5em 0.6em; border-bottom: 1px solid var(--rule); }
th { color: var(--muted); font-weight: 600; }
"""

_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{title}</title>
<style>{style}</style>
</head>
<body>
<main>
{body}
</main>
</body>
</html>
"""


def _contact_html() -> str:
    email = get_settings().legal_contact_email.strip()
    if not email:
        return "(contact email not configured)"
    return f'<a href="mailto:{escape(email)}">{escape(email)}</a>'


@cache
def render(page: str) -> str:
    """The page's HTML. Cached per worker; the text only changes on deploy."""
    text = (PAGES_DIR / f"{page}.md").read_text(encoding="utf-8")
    body = markdown.markdown(text, extensions=["tables"], output_format="html")
    body = body.replace("<table>", '<div class="table"><table>').replace("</table>", "</table></div>")
    body = body.replace(PLACEHOLDER, _contact_html())
    match = re.search(r"<h1>(.*?)</h1>", body)
    title = match.group(1) if match else "Whumpf"
    return _TEMPLATE.format(title=title, style=_STYLE, body=body)


def limit_legal(request: Request) -> None:
    tile_tokens.enforce("legal", client_ip(request))


@router.get("/privacy", response_class=HTMLResponse, dependencies=[Depends(limit_legal)])
def privacy() -> HTMLResponse:
    return HTMLResponse(render("privacy"), headers=HEADERS)


@router.get("/terms", response_class=HTMLResponse, dependencies=[Depends(limit_legal)])
def terms() -> HTMLResponse:
    return HTMLResponse(render("terms"), headers=HEADERS)


# HEAD too: link checkers use it, and FastAPI answers 405 to HEAD on a GET
# route. Separate routes, so the schema has no duplicate operation ids.
for _path, _handler in (("/privacy", privacy), ("/terms", terms)):
    router.add_api_route(_path, _handler, methods=["HEAD"], include_in_schema=False,
                         response_class=HTMLResponse, dependencies=[Depends(limit_legal)])

if not get_settings().legal_contact_email.strip():
    logger.warning("LEGAL_CONTACT_EMAIL is not set; /legal pages show no contact address")
