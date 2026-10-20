"""The pack worker: builds queued offline packs into R2, one at a time.

    compose service pack-worker      python -m app.jobs.pack_worker run
    make pack-queue ARGS="--bbox W,S,E,N [--region colorado] [--layers topo,slope]"
    make pack-list                   the newest packs with their status
    make pack-build-once             build the oldest queued pack, then exit
                                     (only while the service is stopped)

It must run as one process: B1's limits on USGS and on our tile makers hold
within a process, and the SQLite cache files have one writer. A Postgres
advisory lock makes a second worker exit. On start, packs left building by
a crash are queued again (failed after 3 starts). SIGTERM stops the current
build, deletes its uploads and queues it again. docs/runbook.md §12.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import signal
import sys
from pathlib import Path

from sqlalchemy import Connection, select, text
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.db import get_engine, open_session
from app.models import Pack
from app.services import packs
from app.services.pack_cache import PackCache

logger = logging.getLogger("whumpf.pack_worker")

LOCK_KEY = 0x7061636B  # "pack"
POLL_S = 5.0


def _take_lock() -> Connection | None:
    """Holds the worker lock for as long as the returned connection is open;
    None if another worker has it."""
    conn = get_engine().connect()
    got = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}).scalar()
    conn.commit()  # a session lock outlives the transaction; don't sit idle in one
    if got:
        return conn
    conn.close()
    return None


def _build_root() -> Path:
    return Path(get_settings().pack_cache_dir) / "build"


async def _wait_for_table(session) -> None:
    """The api's entrypoint runs the migrations; on a deploy this can start
    first."""
    while True:
        try:
            if session.execute(text("SELECT to_regclass('packs')")).scalar() is not None:
                session.rollback()
                return
            logger.info("waiting for the packs table (the api runs the migrations)")
        except OperationalError as exc:
            logger.info("waiting for the database (%s)", type(exc).__name__)
        session.rollback()
        await asyncio.sleep(POLL_S)


async def _start(session) -> tuple[PackCache, Path]:
    await _wait_for_table(session)
    root = _build_root()
    shutil.rmtree(root, ignore_errors=True)
    requeued, failed = packs.requeue_interrupted(session)
    if requeued or failed:
        logger.info("interrupted packs: %d queued again, %d failed", requeued, failed)
    return PackCache(), root


async def run(stop: asyncio.Event) -> None:
    session = open_session()
    cache, root = await _start(session)
    logger.info("pack worker ready")
    try:
        while not stop.is_set():
            try:
                pack = packs.claim_next(session)
            except OperationalError as exc:
                session.rollback()
                logger.warning("database unavailable (%s); trying again", type(exc).__name__)
                pack = None
            if pack is None:
                try:
                    await asyncio.wait_for(stop.wait(), POLL_S)
                except TimeoutError:
                    pass
                continue
            logger.info("pack %s: building %s for %s (start %d)", pack.id,
                        ",".join(pack.layers), pack.region, pack.attempts)
            await packs.build_pack(session, pack, cache, packs.R2Store, root)
    finally:
        await cache.aclose()
        session.close()


async def once() -> bool:
    """Builds the oldest queued pack. False if it failed."""
    session = open_session()
    cache, root = await _start(session)
    try:
        pack = packs.claim_next(session)
        if pack is None:
            print("no queued pack")
            return True
        await packs.build_pack(session, pack, cache, packs.R2Store, root)
        print(f"pack {pack.id}: {pack.status}" + (f" ({pack.error})" if pack.error else ""))
        return pack.status == "ready"
    finally:
        await cache.aclose()
        session.close()


def _serve() -> int:
    lock = _take_lock()
    if lock is None:
        logger.error("another pack worker is running")
        return 2

    async def main() -> None:
        stop = asyncio.Event()
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()

        def on_signal() -> None:
            logger.info("stopping")
            stop.set()
            assert task is not None
            task.cancel()  # a build in progress is queued again

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, on_signal)
        try:
            await run(stop)
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(main())
    finally:
        lock.close()
    return 0


def _once() -> int:
    lock = _take_lock()
    if lock is None:
        print("the pack-worker service is running and will build queued packs itself; "
              "watch make logs-pack-worker", file=sys.stderr)
        return 2
    try:
        return 0 if asyncio.run(once()) else 1
    finally:
        lock.close()


def _queue(args: argparse.Namespace) -> int:
    try:
        bbox = [float(v) for v in args.bbox.split(",")]
    except ValueError:
        print("--bbox must be W,S,E,N in degrees", file=sys.stderr)
        return 2
    layers = args.layers.split(",") if args.layers else None
    session = open_session()
    try:
        box_, names = packs.check_request(args.region, bbox, layers)
        pack = packs.queue_pack(session, args.region, box_, names)
        base = packs.grid_range(box_)
        width, height = packs.side_lengths_m(box_)
        rounded = packs.side_lengths_m(base.bounds())
        print(f"queued pack {pack.id}: {width / 1000:.1f} x {height / 1000:.1f} km, "
              f"rounded to {rounded[0] / 1000:.1f} x {rounded[1] / 1000:.1f} km")
        for name in names:
            print(f"  {name}: {len(packs.layer_tiles(name, base))} tiles")
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    finally:
        session.close()
    return 0


def _list(limit: int) -> int:
    session = open_session()
    try:
        rows = session.scalars(select(Pack).order_by(Pack.created_at.desc()).limit(limit)).all()
        for p in rows:
            size = sum(f["bytes"] for f in p.files)
            print(f"{p.id}  {p.status:8}  {p.tiles_done}/{p.tiles_total} tiles  {size / 1e6:.1f} MB  "
                  f"{p.region}  {','.join(p.layers)}  created {p.created_at:%Y-%m-%d %H:%M}Z"
                  + (f"  error: {p.error}" if p.error else ""))
            for f in p.files:
                print(f"    {f['layer']:9} {f['bytes']:>12,} bytes  md5 {f['md5']}  {f['key']}")
    finally:
        session.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="the service: build queued packs until stopped")
    sub.add_parser("once", help="build the oldest queued pack, then exit")
    q = sub.add_parser("queue", help="queue a pack")
    q.add_argument("--bbox", required=True, help="W,S,E,N in degrees")
    q.add_argument("--region", default="colorado")
    q.add_argument("--layers", help="comma-separated; default all")
    ls = sub.add_parser("list", help="the newest packs")
    ls.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for name in ("botocore", "boto3", "s3transfer"):  # never near the R2 credentials
        logging.getLogger(name).setLevel(logging.WARNING)
    if args.command == "run":
        sys.exit(_serve())
    if args.command == "once":
        sys.exit(_once())
    if args.command == "queue":
        sys.exit(_queue(args))
    sys.exit(_list(args.limit))


if __name__ == "__main__":
    main()
