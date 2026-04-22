# Feed ingest jobs

Phase 3 jobs that poll external feeds (CAIC, SNOTEL, NDFD) on a schedule
and write into PostGIS. Each job is a standalone Python script so it can
be scheduled via cron, systemd timer, or a future Celery/APScheduler
setup inside the API.
