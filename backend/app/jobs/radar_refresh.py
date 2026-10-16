"""Job every 5 min: MRMS base reflectivity -> 10-minute radar frames in MinIO.

    make radar-refresh          (host cron, docs/runbook.md §9)
    make radar-refresh ARGS=--dry-run
    python -m app.jobs.radar_refresh [--region colorado] [--dry-run]

Frames sit on 10-minute marks. A frame is filled once it is 3 minutes old,
from the newest MRMS file in the 10 minutes up to its mark, and is never
rewritten. Every run fills any frame of the last two hours that is still
missing, so a short outage leaves no gap once the source is back. The NCEP
MRMS server is tried first, then NOAA's AWS bucket. Exits 1 if anything
failed.

Layout and pointer format: app/services/radar.py.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import io
import logging
import math
import re
import sys
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.windows import transform as window_transform

from app.config import get_settings
from app.http_retry import CircuitOpenError
from app.regions import REGIONS
from app.services import rasterlayer
from app.services.radar import (
    FRAME,
    MIN_DBZ,
    NODATA,
    WINDOW,
    cog_key,
    frame_id,
    pointer_key,
    prefix,
)
from app.services.rasterlayer import BadFile, DryRunStore, S3Store, Store

logger = logging.getLogger("whumpf.radar_refresh")

NCEP_BASE = "https://mrms.ncep.noaa.gov/2D/MergedBaseReflectivityQC"
AWS_BASE = "https://noaa-mrms-pds.s3.amazonaws.com"
AWS_PREFIX = "CONUS/MergedBaseReflectivityQC_00.50"
# A frame is filled once its mark is this old, so the files up to it are in.
READY = timedelta(minutes=3)
MAX_LISTING_BYTES = 2 * 1024 * 1024
MAX_BYTES = 8 * 1024 * 1024        # files are ~1.3 MB gzipped
MAX_GRIB_BYTES = 64 * 1024 * 1024
RESOLUTION_DEG = 0.01
MAX_DBZ = 100.0
MIN_VALID_SHARE = 0.5
# Frame files are deleted this long after they were written (frames leave
# latest.json after radar.WINDOW).
KEEP = timedelta(hours=3)

_NAME = re.compile(r"MRMS_MergedBaseReflectivityQC_00\.50_(\d{8})-(\d{6})\.grib2\.gz")


def _stamp(m: re.Match) -> datetime:
    return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def frame_times(now: datetime) -> list[datetime]:
    """Frame marks in the window that are ready to fill, newest first."""
    now = now.astimezone(UTC)
    t = (now - READY).replace(second=0, microsecond=0)
    t -= timedelta(minutes=t.minute % 10)
    out = []
    while t >= now - WINDOW:
        out.append(t)
        t -= FRAME
    return out


def pick_file(files: dict[datetime, str], frame: datetime) -> tuple[datetime, str] | None:
    """The newest file in (frame - 10 min, frame]."""
    inside = [t for t in files if frame - FRAME < t <= frame]
    return (max(inside), files[max(inside)]) if inside else None


# ── listings ──────────────────────────────────────────────────────────────────

async def list_ncep(client: httpx.AsyncClient, base: str, now: datetime) -> dict[datetime, str]:
    """Files in the NCEP directory listing (about the last day)."""
    status, body, _ = await rasterlayer.download(
        client, f"{base}/", upstream="mrms-ncep", max_bytes=MAX_LISTING_BYTES,
    )
    if status == 404:
        return {}
    return {_stamp(m): f"{base}/{m.group(0)}" for m in _NAME.finditer(body.decode("utf-8", "replace"))}


async def list_aws(client: httpx.AsyncClient, base: str, now: datetime) -> dict[datetime, str]:
    """Files in the AWS bucket for the UTC days the window touches."""
    out: dict[datetime, str] = {}
    days = sorted({(now - WINDOW).date(), now.date()})
    for day in days:
        url = f"{base}/?list-type=2&prefix={AWS_PREFIX}/{day:%Y%m%d}/"
        status, body, _ = await rasterlayer.download(
            client, url, upstream="mrms-aws", max_bytes=MAX_LISTING_BYTES,
        )
        if status == 404:
            continue
        for key in re.findall(r"<Key>([^<]+)</Key>", body.decode("utf-8", "replace")):
            if m := _NAME.search(key):
                out[_stamp(m)] = f"{base}/{key}"
    return out


# (upstream, base URL, lister)
SOURCES = (
    ("mrms-ncep", NCEP_BASE, list_ncep),
    ("mrms-aws", AWS_BASE, list_aws),
)


# ── validate + clip ───────────────────────────────────────────────────────────

def _gunzip(raw: bytes) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:
            out = f.read(MAX_GRIB_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise BadFile(f"not gzip: {exc}") from exc
    if len(out) > MAX_GRIB_BYTES:
        raise BadFile(f"unpacks to over {MAX_GRIB_BYTES} bytes")
    return out


def clip_to_cog(
    gz: bytes, observed: datetime, bounds: tuple[float, float, float, float],
) -> tuple[bytes, dict]:
    """Clip a gzipped MRMS reflectivity GRIB2 to `bounds` and return a COG in
    dBZ plus stats. Raises BadFile if it isn't the grid we expect."""
    try:
        with MemoryFile(_gunzip(gz)) as mem, mem.open() as src:
            tags = src.tags(1)
            if src.count != 1 or tags.get("GRIB_UNIT") != "[dBZ]":
                raise BadFile(f"expected one band in [dBZ], got {src.count} in {tags.get('GRIB_UNIT')}")
            valid_time = (tags.get("GRIB_VALID_TIME") or "").split()
            if not valid_time or valid_time[0] != str(int(observed.timestamp())):
                raise BadFile(f"valid time {tags.get('GRIB_VALID_TIME')} is not {observed:%Y-%m-%dT%H:%M:%SZ}")
            if src.crs is None or not src.crs.is_geographic:
                raise BadFile(f"expected a lon/lat grid, got {src.crs}")
            if not all(math.isclose(r, RESOLUTION_DEG, rel_tol=1e-4) for r in src.res):
                raise BadFile(f"expected 0.01° pixels, got {src.res}")
            window = rasterlayer.region_window(src.transform, src.width, src.height, bounds)
            data = src.read(1, window=window).astype(np.float32)
            transform = window_transform(window, src.transform)
    except rasterio.errors.RasterioIOError as exc:
        raise BadFile(f"not a readable GRIB2 file: {exc}") from exc

    # -999 is outside radar coverage, -99 is covered but no echo (checked
    # against a real file: clear-sky Colorado is all -99).
    covered = data > -900
    share = float(covered.mean()) if data.size else 0.0
    if share < MIN_VALID_SHARE:
        raise BadFile(f"only {share:.0%} of the region has radar coverage")
    measured = data > -90  # real reflectivity, which can be slightly negative
    hi = float(data[measured].max()) if measured.any() else None
    if hi is not None and hi > MAX_DBZ:
        raise BadFile(f"max {hi:.1f} dBZ is out of range")

    echo = data >= MIN_DBZ
    out = np.where(echo, data, NODATA)
    cog = rasterlayer.write_cog(out, transform, NODATA)
    return cog, {
        "max_dbz": None if hi is None else round(hi, 1),  # None: no echo anywhere
        "echo_share": round(float(echo.mean()), 3),
    }


# ── run ───────────────────────────────────────────────────────────────────────

class Listings:
    """Each source's file list, fetched the first time a frame needs it, so
    AWS is only listed when NCEP doesn't have a frame."""

    def __init__(self, client: httpx.AsyncClient, now: datetime, sources) -> None:
        self._client, self._now, self._sources = client, now, sources
        self._files: dict[str, dict[datetime, str] | None] = {}
        self.unreachable = False

    async def files(self, upstream: str, base: str, lister) -> dict[datetime, str] | None:
        if upstream not in self._files:
            try:
                self._files[upstream] = await lister(self._client, base, self._now)
            except (httpx.HTTPError, CircuitOpenError, BadFile) as exc:
                logger.warning("%s listing: %r", upstream, exc)
                self._files[upstream] = None
                self.unreachable = True
        return self._files[upstream]

    @property
    def any_answered(self) -> bool:
        return any(f is not None for f in self._files.values())


async def fill_frame(
    client: httpx.AsyncClient, frame: datetime, listings: Listings,
    regions: list[str], now: datetime, sources=SOURCES,
) -> tuple[dict[str, tuple[bytes, dict]] | None, bool]:
    """({region: (cog, frame entry)}, failed). None if no source has a file
    for this frame."""
    failed = False
    for upstream, base, lister in sources:
        files = await listings.files(upstream, base, lister)
        picked = pick_file(files or {}, frame)
        if not picked:
            continue
        observed, url = picked
        try:
            status, gz, _ = await rasterlayer.download(client, url, upstream=upstream, max_bytes=MAX_BYTES)
            if status == 404:
                continue
            sha8 = hashlib.sha256(gz).hexdigest()[:8]
            out = {}
            for region in regions:
                cog, stats = clip_to_cog(gz, observed, REGIONS[region].max_bounds)
                out[region] = (cog, {
                    "time": rasterlayer.iso(frame),
                    "observed": rasterlayer.iso(observed),
                    "key": cog_key(region, frame, sha8),
                    "source": upstream,
                    "fetched": rasterlayer.iso(now),
                    **stats,
                })
            return out, failed
        except BadFile as exc:
            logger.error("%s %s: rejected %s: %s", upstream, frame_id(frame), url, exc)
            failed = True
        except (httpx.HTTPError, CircuitOpenError) as exc:
            logger.warning("%s %s: %r", upstream, frame_id(frame), exc)
    return None, failed


async def run(
    store: Store, client: httpx.AsyncClient, regions: list[str],
    now: datetime | None = None, sources=SOURCES,
) -> bool:
    """Fill missing frames for every region and drop old ones. Returns True
    if all went well."""
    now = now or datetime.now(UTC)
    pointers = {
        region: rasterlayer.load_pointer(store, pointer_key(region), {"region": region, "frames": []})
        for region in regions
    }
    have = {
        region: {f["time"] for f in p["frames"]} for region, p in pointers.items()
    }
    wanted = [
        t for t in frame_times(now)
        if any(rasterlayer.iso(t) not in have[r] for r in regions)
    ]
    ok = True
    listings = Listings(client, now, sources)
    added = 0
    for frame in wanted:
        filled, failed = await fill_frame(client, frame, listings, regions, now, sources)
        ok = ok and not failed
        if filled is None:
            # The newest mark may simply not have a file yet on a slow day;
            # anything older means MRMS has a gap.
            if frame < now - READY - FRAME:
                logger.warning("%s: no MRMS file for this frame", frame_id(frame))
            continue
        for region, (cog, entry) in filled.items():
            if entry["time"] in have[region]:
                continue
            store.put(entry["key"], cog, "image/tiff")
            pointers[region]["frames"].append(entry)
            have[region].add(entry["time"])
        added += 1

    cutoff = now - WINDOW
    for region, pointer in pointers.items():
        frames = sorted(
            (f for f in pointer["frames"] if datetime.fromisoformat(f["time"]) >= cutoff),
            key=lambda f: f["time"],
        )
        if added or len(frames) != len(pointer["frames"]):
            pointer["frames"] = frames
            rasterlayer.save_pointer(store, pointer_key(region), pointer, now)
            if frames:
                last = frames[-1]
                logger.info("%s: %d frames, newest %s (max %s dBZ)",
                            region, len(frames), last["time"], last["max_dbz"])
        try:
            keep = {f["key"] for f in frames}
            rasterlayer.cleanup(store, f"{prefix(region)}/", keep, now - KEEP)
        except Exception:
            logger.exception("%s: cleanup failed", region)
            ok = False
    if wanted and not listings.any_answered:
        logger.error("no MRMS source answered")
        ok = False
    # One source down is fine while the other fills the frames.
    if listings.unreachable and not added:
        ok = False
    return ok


async def _main(regions: list[str], dry_run: bool) -> bool:
    headers = {"User-Agent": get_settings().weather_user_agent}
    store: Store = DryRunStore(S3Store()) if dry_run else S3Store()
    async with httpx.AsyncClient(headers=headers, timeout=60.0, follow_redirects=True) as client:
        return await run(store, client, regions)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", choices=sorted(REGIONS), action="append",
                        help="default: every region")
    parser.add_argument("--dry-run", action="store_true",
                        help="download and validate, but write nothing to MinIO")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(0 if asyncio.run(_main(args.region or sorted(REGIONS), args.dry_run)) else 1)


if __name__ == "__main__":
    main()
