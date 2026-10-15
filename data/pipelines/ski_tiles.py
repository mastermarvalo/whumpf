#!/usr/bin/env python3
"""
Ski run vector tiles for Whumpf.

OSM extract (Geofabrik) -> tilemaker (run by `make ski-tiles-build`) ->
ski_<region>.mbtiles, served by Martin and proxied by the API at
/tiles/ski/{z}/{x}/{y}?region=<region>.

Usage (normally through make, see docs/runbook.md "Ski run tiles"):
    python ski_tiles.py fetch    --region colorado --workdir /data/ski-work
    python ski_tiles.py bbox     --region colorado
    python ski_tiles.py validate --region colorado --workdir /data/ski-work
    python ski_tiles.py publish  --region colorado --workdir /data/ski-work \
                                 --tiles-dir /data/martin-tiles

Workdir layout:
    <workdir>/<region>/download/   the .osm.pbf and its .md5 (untrusted input)
    <workdir>/out/ski_<region>.mbtiles   the latest build
    <workdir>/prev/ski_<region>.mbtiles  what was served before the last publish
    <workdir>/<region>/published.json    feature counts of the served file

Needs ogrinfo (gdal-bin) for feature counts; stdlib otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("ski_tiles")

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
REGIONS = json.loads((HERE / "ski" / "regions.json").read_text())
GEOFABRIK = "https://download.geofabrik.de"
MARTIN_URL = "http://127.0.0.1:3000"

# The contract with the apps. Keep in sync with ski/process.lua and the runbook.
# Every field is a String (osm_id too: see set_id in process.lua).
EXPECTED_FIELDS = {
    layer: dict.fromkeys(fields, "String")
    for layer, fields in {
        "ski_runs": ("piste_type", "difficulty", "grooming", "name", "osm_id", "osm_type"),
        "ski_lifts": ("lift_type", "name", "osm_id", "osm_type"),
        "ski_areas": ("name", "osm_id", "osm_type"),
    }.items()
}
ATTRIBUTION = "© OpenStreetMap contributors"
# A build may lose at most this share of any layer's features versus the file
# being served; more than that is more likely a broken extract than real edits.
MAX_DROP = 0.5


def region_config(region: str) -> dict:
    if region.startswith("_") or region not in REGIONS:
        sys.exit(f"unknown region {region!r}; add it to {HERE / 'ski' / 'regions.json'}")
    return REGIONS[region]


def built_path(workdir: Path, region: str) -> Path:
    return workdir / "out" / f"ski_{region}.mbtiles"


# ── fetch ─────────────────────────────────────────────────────────────────────

def _get(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "whumpf-ski-tiles"})
    with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def fetch(region: str, workdir: Path) -> None:
    cfg = region_config(region)
    name = Path(cfg["geofabrik"]).name + "-latest.osm.pbf"
    base = f"{GEOFABRIK}/{cfg['geofabrik']}-latest.osm.pbf"
    ddir = workdir / region / "download"
    ddir.mkdir(parents=True, exist_ok=True)
    pbf = ddir / name

    md5_tmp = ddir / (name + ".md5.tmp")
    _get(base + ".md5", md5_tmp)
    match = re.match(r"([0-9a-f]{32})\b", md5_tmp.read_text())
    if not match:
        sys.exit(f"unexpected .md5 contents from {base}.md5")
    want = match.group(1)

    if pbf.exists() and _md5(pbf) == want:
        log.info("%s is current (md5 %s), not downloading", name, want)
    else:
        tmp = ddir / (name + ".tmp")
        log.info("downloading %s", base)
        _get(base, tmp)
        got = _md5(tmp)
        if got != want:
            tmp.unlink()
            sys.exit(f"md5 mismatch for {name}: got {got}, expected {want}")
        tmp.replace(pbf)
        log.info("downloaded %s (%.0f MB)", name, pbf.stat().st_size / 1e6)
    md5_tmp.replace(ddir / (name + ".md5"))


# ── validate ──────────────────────────────────────────────────────────────────

def feature_counts(mbtiles: Path) -> dict[str, int]:
    """Features per layer at z14, counted per tile (a line crossing two tiles
    counts twice). Only compared with counts made the same way."""
    counts = {}
    for layer in EXPECTED_FIELDS:
        out = subprocess.run(
            ["ogrinfo", "-ro", "-so", "-oo", "ZOOM_LEVEL=14", str(mbtiles), layer],
            capture_output=True, text=True, check=True,
        ).stdout
        match = re.search(r"Feature Count: (\d+)", out)
        if not match:
            raise ValueError(f"ogrinfo gave no feature count for {layer}")
        counts[layer] = int(match.group(1))
    return counts


def check(mbtiles: Path, region: str, previous: dict[str, int] | None) -> dict[str, int]:
    """Raise ValueError if `mbtiles` is not fit to serve; return its counts."""
    cfg = region_config(region)
    if not mbtiles.exists():
        raise ValueError(f"{mbtiles} does not exist")
    con = sqlite3.connect(f"file:{mbtiles}?mode=ro", uri=True)
    try:
        meta = dict(con.execute("SELECT name, value FROM metadata"))
        tiles = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    finally:
        con.close()

    if meta.get("format") != "pbf":
        raise ValueError(f"format is {meta.get('format')!r}, expected pbf")
    if ATTRIBUTION not in meta.get("attribution", ""):
        raise ValueError("attribution metadata is missing")
    if tiles == 0:
        raise ValueError("no tiles")

    layers = {
        layer["id"]: layer["fields"]
        for layer in json.loads(meta.get("json", "{}")).get("vector_layers", [])
    }
    if layers != EXPECTED_FIELDS:
        raise ValueError(f"layers/fields differ from the contract: {layers}")

    w, s, e, n = (float(v) for v in meta.get("bounds", "0,0,0,0").split(","))
    rw, rs, re_, rn = cfg["bbox"]
    if not (rw - 0.01 <= w < e <= re_ + 0.01 and rs - 0.01 <= s < n <= rn + 0.01):
        raise ValueError(f"bounds {w},{s},{e},{n} are outside the region bbox {cfg['bbox']}")

    counts = feature_counts(mbtiles)
    for layer, count in counts.items():
        floor = cfg["min_features"][layer]
        if count < floor:
            raise ValueError(f"{layer}: {count} features, minimum is {floor}")
        if previous and count < previous.get(layer, 0) * (1 - MAX_DROP):
            raise ValueError(
                f"{layer}: {count} features, down from {previous[layer]} in the served file"
            )
    return counts


def _published_record(workdir: Path, region: str) -> Path:
    return workdir / region / "published.json"


def _previous_counts(workdir: Path, region: str) -> dict[str, int] | None:
    record = _published_record(workdir, region)
    if record.exists():
        return json.loads(record.read_text())["counts"]
    return None


def validate(region: str, workdir: Path) -> dict[str, int]:
    mbtiles = built_path(workdir, region)
    try:
        counts = check(mbtiles, region, _previous_counts(workdir, region))
    except (ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(f"validation failed for {mbtiles}: {exc}")
    log.info("valid: %s %s", mbtiles, counts)
    return counts


# ── publish ───────────────────────────────────────────────────────────────────

def _martin_has(source: str, martin_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{martin_url}/catalog", timeout=5) as resp:
            return source in json.load(resp).get("tiles", {})
    except (OSError, ValueError):
        return False


def _restart_martin(source: str, martin_url: str, wait_s: float = 60) -> bool:
    subprocess.run(["make", "-C", str(REPO), "restart-martin"], check=True)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if _martin_has(source, martin_url):
            return True
        time.sleep(1)
    return False


def publish(region: str, workdir: Path, tiles_dir: Path, martin_url: str = MARTIN_URL) -> None:
    counts = validate(region, workdir)
    source = f"ski_{region}"
    built = built_path(workdir, region)
    dest = tiles_dir / f"{source}.mbtiles"
    prev = workdir / "prev" / f"{source}.mbtiles"
    prev.parent.mkdir(parents=True, exist_ok=True)

    # rename(2) is only atomic within one filesystem.
    if built.stat().st_dev != tiles_dir.stat().st_dev:
        sys.exit(f"{built} and {tiles_dir} are on different filesystems; "
                 "put the workdir on the same disk as the Martin tiles")

    # Keep the served file for rollback. A hard link, not a move, so dest
    # exists the whole time. prev/ is outside tiles_dir: Martin would serve
    # any .mbtiles left in there as an extra source.
    if dest.exists():
        prev.unlink(missing_ok=True)
        os.link(dest, prev)
    built.replace(dest)
    log.info("swapped %s into place; restarting Martin", dest)

    if not _restart_martin(source, martin_url):
        log.error("Martin does not list %s after restart; rolling back", source)
        if prev.exists():
            os.link(prev, dest.with_suffix(".rollback"))
            dest.with_suffix(".rollback").replace(dest)
        else:
            dest.unlink()
        if not _restart_martin(source, martin_url):
            sys.exit(f"publish failed, previous file restored, but Martin still does not "
                     f"list {source}: check `make logs` and {martin_url}/catalog")
        sys.exit("publish failed, previous file restored and served")

    _published_record(workdir, region).write_text(json.dumps({
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "counts": counts,
    }, indent=2) + "\n")
    log.info("published %s %s", source, counts)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["fetch", "bbox", "validate", "publish"])
    ap.add_argument("--region", required=True)
    ap.add_argument("--workdir", type=Path, default=Path("/data/ski-work"))
    ap.add_argument("--tiles-dir", type=Path, default=Path("/data/martin-tiles"))
    # Where publish checks that Martin is back. Pointing it at a dead port is
    # how the rollback path is tested (docs/runbook.md §7 "Rollback").
    ap.add_argument("--martin-url", default=MARTIN_URL)
    args = ap.parse_args()

    if args.command == "fetch":
        fetch(args.region, args.workdir)
    elif args.command == "bbox":
        print(",".join(str(v) for v in region_config(args.region)["bbox"]))
    elif args.command == "validate":
        validate(args.region, args.workdir)
    else:
        publish(args.region, args.workdir, args.tiles_dir, args.martin_url)


if __name__ == "__main__":
    main()
