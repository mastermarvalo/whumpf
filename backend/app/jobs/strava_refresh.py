"""Strava 7-day rule: re-fetch imported routes before they're 7 days old.

    make strava-refresh                 (every 6 h, host cron; docs/runbook.md §14)
    make strava-refresh ARGS=--dry-run  (what is due and what would expire; asks Strava nothing)
    python -m app.jobs.strava_refresh [--limit 90] [--dry-run]

Strava's API Policy §6.2: "You may not retain Strava Data in your cache for
longer than seven (7) days. If your Developer Application checks for a
resource ... and that resource is no longer available from Strava, you must
remove it from your cache immediately". A route imported from Strava is
Strava data (its track, name and the profile sampled along it), so:

- Routes fetched 6 or more days ago (routes.strava_fetched_at) are fetched
  again, one Strava call per activity, oldest first. Still there: the date is
  bumped, and the terrain is sampled again only if the track changed. Gone
  (404/403): its routes are deleted. Our tokens are dead (the athlete revoked
  whumpf and we missed the webhook): all their Strava data is deleted and
  they get the confirmation email. No connection: the routes are deleted.
- Then every imported route still older than 7 days is deleted, whatever the
  reason it couldn't be refreshed (Strava down, the run hit --limit).

Strava allows 100 read calls per 15 min; --limit (default 90) caps the
activity calls per run and a 429 stops the run. Four runs a day give an
unrefreshed route four tries before it expires. Exits 1 if Strava was rate
limited or unreachable for anything, so the cron log shows it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

from geoalchemy2.shape import to_shape
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import job_session
from app.models.route import Route
from app.models.strava import StravaConnection
from app.routers.routes import compute_profile
from app.services import strava as strava_svc
from app.services import strava_data

logger = logging.getLogger("whumpf.strava_refresh")

REFRESH_AFTER = timedelta(days=6)
MAX_AGE = timedelta(days=7)
DEFAULT_LIMIT = 90


def _imported_older_than(cutoff: datetime):
    return select(Route).where(
        Route.source_strava_id.is_not(None),
        or_(Route.strava_fetched_at.is_(None), Route.strava_fetched_at < cutoff),
    )


def _vertices(route: Route) -> list[tuple[float, float]]:
    return [(round(c[0], 5), round(c[1], 5)) for c in to_shape(route.geom).coords]


def _apply(session: Session, routes: list[Route], detail: dict, now: datetime, counts: Counter) -> None:
    """Strava still has the activity: bump the fetch time; re-sample the
    terrain if the track changed. A track that can't be sampled any more is
    left alone and expires (better than serving a stale profile)."""
    map_ = detail.get("map") or {}
    encoded = map_.get("polyline") or map_.get("summary_polyline") or ""
    coords = [(round(c[0], 5), round(c[1], 5)) for c in strava_svc._decode_polyline(encoded)] if encoded else []
    for route in routes:
        if coords and coords != _vertices(route):
            try:
                geom, summary, samples = compute_profile(coords, route.region, get_settings())
            except Exception as exc:  # noqa: BLE001 — HTTPException from validation, or a COG read
                logger.warning("route %s: Strava track changed but can't be sampled (%s); left to expire",
                               route.id, getattr(exc, "detail", type(exc).__name__))
                counts["resample_failed"] += 1
                continue
            route.geom, route.summary, route.samples = geom, summary, samples
            counts["resampled"] += 1
        route.strava_fetched_at = now
        counts["refreshed"] += 1


async def refresh(
    session: Session, *, now: datetime | None = None, limit: int = DEFAULT_LIMIT,
) -> Counter:
    """One run. Returns counts: refreshed, resampled, resample_failed, gone,
    revoked_users, revoked_routes, no_connection, unknown, rate_limited,
    expired."""
    now = now or datetime.now(UTC)
    counts: Counter = Counter()
    due = list(session.scalars(
        _imported_older_than(now - REFRESH_AFTER)
        .order_by(Route.strava_fetched_at.asc().nulls_first(), Route.id)
    ))
    by_user: dict[int, dict[int, list[Route]]] = {}
    for route in due:
        by_user.setdefault(route.owner_id, {}).setdefault(route.source_strava_id, []).append(route)

    calls = 0
    stopped = False
    for user_id, activities in by_user.items():
        if stopped:
            break
        conn = session.scalars(
            select(StravaConnection).where(StravaConnection.user_id == user_id)
        ).first()
        if conn is None:
            ids = [r.id for routes in activities.values() for r in routes]
            strava_data.delete_routes(session, ids)
            session.commit()
            counts["no_connection"] += len(ids)
            continue
        token, state = await strava_svc.access_token_or_state(conn, session)
        if state == strava_svc.REVOKED:
            counts["revoked_routes"] += await strava_data.purge_revoked(session, user_id)
            counts["revoked_users"] += 1
            continue
        if state == strava_svc.RATE_LIMITED:
            counts["rate_limited"] += 1
            break
        if token is None:
            counts["unknown"] += 1
            continue
        for activity_id in activities:
            if calls >= limit:
                stopped = True
                break
            calls += 1
            state, detail = await strava_svc.activity_state(token, activity_id)
            if state == strava_svc.VALID:
                # Every copy of this activity in the account was just fetched too.
                all_routes = list(session.scalars(select(Route).where(
                    Route.owner_id == user_id, Route.source_strava_id == activity_id,
                )))
                _apply(session, all_routes, detail or {}, now, counts)
                session.commit()
            elif state == strava_svc.GONE:
                counts["gone"] += strava_data.delete_activity_routes(session, user_id, activity_id)
                session.commit()
            elif state == strava_svc.REVOKED:
                counts["revoked_routes"] += await strava_data.purge_revoked(session, user_id)
                counts["revoked_users"] += 1
                break
            elif state == strava_svc.RATE_LIMITED:
                counts["rate_limited"] += 1
                stopped = True
                break
            else:
                counts["unknown"] += 1

    expired = [r.id for r in session.scalars(_imported_older_than(now - MAX_AGE))]
    strava_data.delete_routes(session, expired)
    session.commit()
    counts["expired"] += len(expired)
    if calls >= limit:
        left = len(list(session.scalars(_imported_older_than(now - REFRESH_AFTER))))
        logger.warning("hit --limit=%d with %d imported routes still due; raise it or run more often",
                       limit, left)
    return counts


def _dry_run(session: Session) -> int:
    now = datetime.now(UTC)
    due = list(session.scalars(_imported_older_than(now - REFRESH_AFTER)))
    expiring = [r for r in due if r.strava_fetched_at is None or r.strava_fetched_at < now - MAX_AGE]
    activities = {(r.owner_id, r.source_strava_id) for r in due}
    print(f"{len(due)} imported routes due ({len(activities)} Strava calls); "
          f"{len(expiring)} already over 7 days (refreshed now, or deleted if that fails)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"most Strava activity calls this run (default {DEFAULT_LIMIT})")
    parser.add_argument("--dry-run", action="store_true", help="report only; asks Strava nothing")
    args = parser.parse_args(argv)
    with job_session() as session:
        if args.dry_run:
            return _dry_run(session)
        counts = asyncio.run(refresh(session, limit=args.limit))
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing due"
    logger.info("strava-refresh: %s", summary)
    print(summary)
    return 1 if counts["rate_limited"] or counts["unknown"] else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")
    sys.exit(main())
