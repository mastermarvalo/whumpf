"""Record the values CDOT's feeds actually use, to build the road mapping from.

    make roads-sample                 (host cron every 15 min for a week, docs/runbook.md §11)
    make roads-sample ARGS=--report   print what has been recorded; fetches nothing

Each run reads roadConditions (without geometry), incidents and
plannedEvents and adds every distinct value of these fields to
dem-cogs/roads/colorado/sample.json:

    conditionDescription           (with its conditionId and sourceType values)
    incident.type                  (with CDOT's category and the category we gave it)
    incident.additionalImpacts
    planned.type
    planned.additionalImpacts

Per value: `count` (records carrying it, summed over runs), `runs` (runs it
appeared in), `first_seen`, `last_seen` and the last `example` (segment
name or traveler message). Chain law, traction law, avalanche control and
crash types get a category in app/services/roads.py only once they show up
here. A feed that fails is skipped for that run (counted in `failed`);
exits 1 if any failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from datetime import UTC, datetime

import httpx

from app.jobs.roads_refresh import client, describe, fetch_feed, setup_logging
from app.services import roads
from app.services.rasterlayer import S3Store, Store, iso

logger = logging.getLogger("whumpf.roads_sample")

REGION = "colorado"
KEY = f"roads/{REGION}/sample.json"
FIELDS = (
    "conditionDescription", "incident.type", "incident.additionalImpacts",
    "planned.type", "planned.additionalImpacts",
)
EXAMPLE_CHARS = 300


def _values(feed: str, features: list[dict]):
    """(field, value, extras, example) for every value in one feed."""
    for feature in features:
        p = feature.get("properties") or {}
        if feed == "conditions":
            for c in p.get("currentConditions") or []:
                yield ("conditionDescription", (c.get("conditionDescription") or "").strip(), {
                    "conditionId": c.get("conditionId"),
                    "sourceType": c.get("sourceType"),
                    "category": roads.condition_category(c.get("conditionDescription") or ""),
                }, p.get("nameId") or p.get("name"))
            continue
        lanes = [roads.decode_lanes(i) for i in p.get("laneImpacts") or []]
        message = p.get("travelerInformationMessage")
        yield (f"{feed}.type", (p.get("type") or "").strip(), {
            "cdot_category": p.get("category"),
            "category": roads.event_category(p, lanes),
        }, message)
        for impact in p.get("additionalImpacts") or []:
            yield (f"{feed}.additionalImpacts", str(impact).strip(), {}, message)


def add(sample: dict, feed: str, features: list[dict], now: datetime) -> None:
    stamp = iso(now)
    seen_this_run: set[tuple[str, str]] = set()
    for field, value, extras, example in _values(feed, features):
        entry = sample["fields"].setdefault(field, {}).setdefault(value, {
            "count": 0, "runs": 0, "first_seen": stamp, "last_seen": stamp,
        })
        entry["count"] += 1
        entry["last_seen"] = stamp
        if (field, value) not in seen_this_run:
            seen_this_run.add((field, value))
            entry["runs"] += 1
        for name, extra in extras.items():
            counts = entry.setdefault(name, {})
            counts[str(extra)] = counts.get(str(extra), 0) + 1
        if example:
            entry["example"] = str(example)[:EXAMPLE_CHARS]


def empty(now: datetime) -> dict:
    return {"region": REGION, "first_run": iso(now), "last_run": None, "runs": 0,
            "failed": {}, "fields": {f: {} for f in FIELDS}}


async def sample_once(store: Store, http: httpx.AsyncClient, now: datetime | None = None) -> bool:
    """One sampling run. True if every feed was read."""
    if not roads.configured():
        logger.info("no COTRIP_API_KEY: nothing sampled")
        return True
    now = now or datetime.now(UTC)
    raw = store.get(KEY)
    sample = json.loads(raw) if raw else empty(now)
    ok = True
    for feed in roads.FEEDS:
        try:
            params = {"skipGeometry": "true"} if feed == "conditions" else {}
            features = await fetch_feed(http, roads.UPSTREAM_FEED[feed], **params)
        except Exception as exc:  # noqa: BLE001 - one feed must never stop the others
            logger.error("sample %s failed: %s", feed, describe(exc))
            sample["failed"][feed] = sample["failed"].get(feed, 0) + 1
            ok = False
            continue
        add(sample, "incident" if feed == "incidents" else feed, features, now)
        logger.info("sample %s: %d records", feed, len(features))
    sample["runs"] += 1
    sample["last_run"] = iso(now)
    store.put(KEY, json.dumps(sample, indent=1, sort_keys=True).encode(), "application/json")
    return ok


def report(sample: dict | None) -> str:
    if not sample:
        return "no sample recorded yet"
    lines = [(f"{sample['runs']} runs, {sample['first_run']} to {sample['last_run']}; "
              f"failed feeds: {sample['failed'] or 'none'}")]
    for field in FIELDS:
        values = sample["fields"].get(field, {})
        lines.append(f"\n{field}: {len(values)} distinct")
        for value, e in sorted(values.items(), key=lambda kv: -kv[1]["count"]):
            mapped = Counter(e.get("category", {})).most_common(1)
            extras = {k: e[k] for k in ("conditionId", "sourceType", "cdot_category") if k in e}
            lines.append(
                f"  {e['count']:>7}  {e['runs']:>4} runs  {e['first_seen']} .. {e['last_seen']}  "
                f"{value!r}" + (f" -> {mapped[0][0]}" if mapped else "") + (f"  {extras}" if extras else "")
            )
    return "\n".join(lines)


async def _main() -> bool:
    async with client() as http:
        return await sample_once(S3Store(), http)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", action="store_true", help="print the recorded values; fetch nothing")
    args = parser.parse_args()
    setup_logging()
    if args.report:
        raw = S3Store().get(KEY)
        print(report(json.loads(raw) if raw else None))
        return
    try:
        ok = asyncio.run(_main())
    except Exception as exc:  # noqa: BLE001 - never let a traceback print a URL
        logger.error("roads sample failed: %s", describe(exc))
        ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
