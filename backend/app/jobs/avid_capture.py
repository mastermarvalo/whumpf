"""Catch a real CAIC forecast that has avalanche problems, for a test fixture.

    make avid-capture            (host cron, hourly; docs/runbook.md, Troubleshooting)

AVID only serves the current issue, and CAIC posts no problems on many days,
so a real example has to be caught on the day it exists. This reads
/avid/products/all and prints, to stdout, a small JSON document
({"products": [one forecast], "areas": {... its area ...}}) for the first
forecast whose day-0 problem list is not empty. Prints nothing if there is
none. Logs go to stderr. Read-only: two GETs to CAIC, no database or storage.

The make target writes stdout to /data/caic-fixtures/avid-<date>.json and
stops once one is saved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from app.routers import avalanche

logger = logging.getLogger("whumpf.avid_capture")

KEEP_POLYGONS = 3  # the polygon id list is long and not read by the app


def pick(products: list, areas: dict) -> dict | None:
    for product in products:
        days = (product.get("avalancheProblems") or {}).get("days")
        if not (isinstance(days, list) and days and isinstance(days[0], list) and days[0]):
            continue
        feature = next(
            (f for f in areas.get("features", [])
             if (f.get("properties") or {}).get("id") == product.get("areaId")),
            None,
        )
        if feature is None:
            continue
        return {
            "products": [{**product, "polygons": (product.get("polygons") or [])[:KEEP_POLYGONS]}],
            "areas": {"type": areas.get("type", "FeatureCollection"), "features": [feature]},
        }
    return None


async def _run() -> int:
    try:
        products, areas = await avalanche._do_fetch_avid()
    except Exception as exc:
        logger.error("AVID fetch failed: %s", exc)
        return 1
    found = pick(products, areas)
    if found is None:
        logger.info("no forecast with problems today (%d forecasts)", len(products))
        return 0
    json.dump(found, sys.stdout, separators=(",", ":"))
    logger.info("captured forecast for area %s", found["products"][0].get("areaId"))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
