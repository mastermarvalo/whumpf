"""Monthly job: the NAIP file list for each region, from Planetary Computer.

    make naip-index                      (monthly host cron, docs/runbook.md §18)
    make naip-index ARGS=--dry-run
    make naip-index ARGS="--dry-run --probe 18/54047/99433"
    python -m app.jobs.naip_index [--region colorado] [--dry-run] [--force] [--probe Z/X/Y]
                                  [--probe-out DIR]

Asks Planetary Computer's STAC API for every NAIP file in the region's box
(in half-degree cells, so no one search is big), keeps the newest file of
each quarter quad, and publishes the list as a new revision when it changed.
A list much shorter than the served one is not published (a partial STAC
answer would leave holes); --force publishes it anyway. --probe renders the
given tiles from the new list and logs their size and time, writing nothing
to the tile cache. Exits 1 if anything failed.

Layout and pointer format: app/services/naip.py.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from app.http_retry import call_with_resilience
from app.regions import REGIONS
from app.services import naip, rasterlayer
from app.services.rasterlayer import DryRunStore, S3Store, Store

logger = logging.getLogger("whumpf.naip_index")

CELL_DEG = 0.5
PAGE = 1000
MAX_PAGES = 20          # per cell; a cell has a few hundred files
FIRST_YEAR = 2019       # older years are never the newest anywhere we cover
MAX_GSD_M = 1.0
MIN_SHARE = 0.9         # of the served list's files, or it isn't published
KEEP = timedelta(days=60)


def cells(bbox: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    west, south, east, north = bbox
    nx, ny = math.ceil((east - west) / CELL_DEG), math.ceil((north - south) / CELL_DEG)
    return [(west + i * CELL_DEG, south + j * CELL_DEG,
             min(west + (i + 1) * CELL_DEG, east), min(south + (j + 1) * CELL_DEG, north))
            for i in range(nx) for j in range(ny)]


async def search_cell(client: httpx.AsyncClient, cell: tuple[float, float, float, float],
                      url: str = naip.STAC_SEARCH_URL) -> list[dict]:
    """Every NAIP STAC item touching `cell` since FIRST_YEAR."""
    body: dict | None = {"collections": [naip.COLLECTION], "bbox": list(cell),
                         "datetime": f"{FIRST_YEAR}-01-01T00:00:00Z/..", "limit": PAGE}
    features: list[dict] = []
    for _ in range(MAX_PAGES):
        async def _do(body=body, url=url) -> httpx.Response:
            r = await client.post(url, json=body)
            r.raise_for_status()
            return r
        page = (await call_with_resilience("planetary_computer", _do, attempts=4, retry_429=True)).json()
        features.extend(page.get("features", []))
        nxt = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
        if nxt is None:
            return features
        url, body = nxt["href"], nxt.get("body", body)
    raise RuntimeError(f"cell {cell}: more than {MAX_PAGES} pages")


def newest_per_quarter_quad(features: list[dict]) -> list[dict]:
    """The newest usable file of each quarter quad, as index items."""
    best: dict[str, dict] = {}
    for f in features:
        props = f.get("properties", {})
        href = f.get("assets", {}).get("image", {}).get("href", "")
        qq = naip.quarter_quad(f.get("id", ""))
        gsd = props.get("gsd")
        if not (qq and href.startswith(naip.HREF_PREFIX) and isinstance(gsd, int | float)
                and gsd <= MAX_GSD_M and f.get("bbox") and props.get("datetime")):
            continue
        item = {"id": f["id"], "href": href, "bbox": [round(v, 6) for v in f["bbox"][:4]],
                "date": props["datetime"][:10], "gsd": gsd}
        if qq not in best or (item["date"], item["id"]) > (best[qq]["date"], best[qq]["id"]):
            best[qq] = item
    return sorted(best.values(), key=lambda i: i["id"])


def summary(items: list[dict]) -> dict:
    years: dict[str, int] = {}
    for item in items:
        years[item["date"][:4]] = years.get(item["date"][:4], 0) + 1
    dates = sorted(item["date"] for item in items)
    return {"items": len(items), "years": dict(sorted(years.items())),
            "dates": [dates[0], dates[-1]] if dates else None}


async def refresh_region(client: httpx.AsyncClient, store: Store, region: str, current: dict | None,
                         now: datetime, force: bool = False) -> tuple[dict | None, list[dict] | None]:
    """(the new pointer entry or None if nothing is published, the item list)."""
    found: dict[str, dict] = {}
    for cell in cells(REGIONS[region].bbox):
        for f in await search_cell(client, cell):
            found[f["id"]] = f
    items = newest_per_quarter_quad(list(found.values()))
    stats = summary(items)
    logger.info("%s: %d STAC items, %d quarter quads, years %s", region, len(found), len(items), stats["years"])
    if not items:
        raise RuntimeError(f"{region}: no NAIP files found")

    body = json.dumps({"region": region, "items": items}, separators=(",", ":"), sort_keys=True).encode()
    digest = hashlib.sha256(body).hexdigest()[:8]
    if current and current.get("sha") == digest:
        logger.info("%s: unchanged (revision %s)", region, current["revision"])
        return dict(current, valid=rasterlayer.iso(now)), items
    if current and not force and len(items) < MIN_SHARE * current.get("items", 0):
        raise RuntimeError(f"{region}: {len(items)} files, the served list has {current['items']};"
                           " not publishing (--force to publish anyway)")
    revision = f"{now:%Y%m%d}-{digest}"
    key = naip.index_key(region, revision)
    store.put(key, body, "application/json")
    logger.info("%s: published revision %s", region, revision)
    return {"revision": revision, "key": key, "sha": digest, "valid": rasterlayer.iso(now),
            "fetched": rasterlayer.iso(now), **stats}, items


def probe(region: str, items: list[dict], tiles: list[str], out_dir: str | None = None) -> bool:
    """Render tiles from `items` and log size and time. Nothing is stored,
    except the JPEGs in out_dir when given (to look at)."""
    index = naip.NaipIndex("probe", "probe", naip.items_from_json({"items": items}))
    ok = True
    for spec in tiles:
        z, x, y = (int(v) for v in spec.split("/"))
        files = index.covering(naip.tile_lonlat_bounds(z, x, y))
        start = time.monotonic()
        try:
            data = naip.render_tile(files, z, x, y)
        except Exception:
            logger.exception("%s: probe %s failed", region, spec)
            ok = False
            continue
        if out_dir and data:
            Path(out_dir, f"naip_{z}_{x}_{y}.jpg").write_bytes(data)
        logger.info("%s: probe %s: %d bytes from %d files (%s) in %.2f s", region, spec, len(data),
                    len(files), ", ".join(sorted({f.date for f in files})), time.monotonic() - start)
    return ok


async def run(store: Store, client: httpx.AsyncClient, regions: list[str], now: datetime | None = None,
              force: bool = False, probe_tiles: list[str] | None = None, probe_out: str | None = None) -> bool:
    """Refresh every region. Returns True if all went well."""
    now = now or datetime.now(UTC)
    ok = True
    for region in regions:
        pointer = rasterlayer.load_pointer(store, naip.pointer_key(region), {"region": region})
        try:
            new, items = await refresh_region(client, store, region, pointer.get("current"), now, force)
        except Exception:
            logger.exception("%s: refresh failed", region)
            new, items, ok = None, None, False
        if new is not None:
            pointer["current"] = new
            rasterlayer.save_pointer(store, naip.pointer_key(region), pointer, now)
        if probe_tiles and items:
            ok = await asyncio.to_thread(probe, region, items, probe_tiles, probe_out) and ok
        try:
            keep = {naip.pointer_key(region)} | ({pointer["current"]["key"]} if "current" in pointer else set())
            if n := rasterlayer.cleanup(store, f"{naip.prefix(region)}/", keep, now - KEEP, suffix=".json"):
                logger.info("%s: deleted %d old revisions", region, n)
        except Exception:
            logger.exception("%s: cleanup failed", region)
            ok = False
    return ok


async def _main(regions: list[str], dry_run: bool, force: bool, probe_tiles: list[str],
                probe_out: str | None) -> bool:
    store: Store = DryRunStore(S3Store()) if dry_run else S3Store()
    headers = {"User-Agent": naip.user_agent()}
    async with httpx.AsyncClient(headers=headers, timeout=60.0) as client:
        return await run(store, client, regions, force=force, probe_tiles=probe_tiles, probe_out=probe_out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", choices=sorted(REGIONS), action="append",
                        help="default: every region")
    parser.add_argument("--dry-run", action="store_true", help="search, but write nothing to MinIO")
    parser.add_argument("--force", action="store_true",
                        help="publish even if the list is much shorter than the served one")
    parser.add_argument("--probe", action="append", default=[], metavar="Z/X/Y",
                        help="render this tile from the new list and log its size and time")
    parser.add_argument("--probe-out", metavar="DIR", help="also write the probe tiles' JPEGs here")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(0 if asyncio.run(_main(args.region or sorted(REGIONS), args.dry_run, args.force, args.probe,
                                       args.probe_out))
             else 1)


if __name__ == "__main__":
    main()
