"""Outage banners on GET /status, from the VM.

    make banner                                        list active and recent banners
    make banner ARGS='post --severity major --title "Tiles are slow" --body "Working on it."'
    make banner ARGS='resolve 12'

Writes straight to the database in a one-off api container, so it needs no
sign-in token or password: nothing secret goes on the command line, into
shell history or into a log. Banners posted here have no author
(created_by_id is null). The app shows the newest unresolved banner; a
critical one makes /status say "outage", a major one "degraded".
docs/runbook.md §15.
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.db import job_session
from app.models.status import StatusBanner
from app.services import status_banners

logger = logging.getLogger("whumpf.status")


def _line(b: StatusBanner) -> str:
    state = f"resolved {b.resolved_at:%Y-%m-%d %H:%M}Z" if b.resolved_at else "ACTIVE"
    return f"{b.id:>4}  {b.severity:8}  posted {b.created_at:%Y-%m-%d %H:%M}Z  {state:24}  {b.title}"


def _list() -> int:
    with job_session() as session:
        active = status_banners.active(session)
        recent = [b for b in status_banners.recent(session) if b.resolved_at is not None]
        if active:
            print(f"Active ({len(active)}; the app shows the newest):")
            for b in active:
                print(_line(b))
                if b.body:
                    print(f"        {b.body}")
        else:
            print("No active banner.")
        if recent:
            print("Recently resolved:")
            for b in recent:
                print(_line(b))
    return 0


def _post(title: str, body: str, severity: str) -> int:
    with job_session() as session:
        try:
            banner = status_banners.post(
                session, title=title, body=body, severity=severity, created_by_id=None,
            )
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        logger.info("status banner posted via CLI: id=%s severity=%s", banner.id, banner.severity)
        print(f"Posted banner {banner.id}. Resolve it with: make banner ARGS='resolve {banner.id}'")
        print(_line(banner))
    return 0


def _resolve(banner_id: int) -> int:
    with job_session() as session:
        banner, changed = status_banners.resolve(session, banner_id)
        if banner is None:
            print(f"No banner {banner_id}. make banner lists them.", file=sys.stderr)
            return 1
        if changed:
            logger.info("status banner resolved via CLI: id=%s", banner.id)
            print(f"Resolved banner {banner.id}.")
        else:
            print(f"Banner {banner.id} was already resolved.")
        print(_line(banner))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="active and recent banners (the default)")
    p = sub.add_parser("post", help="post a banner")
    p.add_argument("--title", required=True, help=f"at most {status_banners.TITLE_MAX} characters")
    p.add_argument("--body", default="", help="more detail, optional")
    p.add_argument("--severity", default="minor", choices=status_banners.SEVERITIES)
    r = sub.add_parser("resolve", help="resolve a banner by id")
    r.add_argument("id", type=int)
    args = parser.parse_args(argv)

    if args.command == "post":
        return _post(args.title, args.body, args.severity)
    if args.command == "resolve":
        return _resolve(args.id)
    return _list()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")
    sys.exit(main())
