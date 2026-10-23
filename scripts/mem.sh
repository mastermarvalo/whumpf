#!/usr/bin/env bash
# Memory report for the stack's containers (docs/runbook.md §13). Read-only.
#
#   scripts/mem.sh                 usage vs limit, peak, RestartCount, oom_kill
#   scripts/mem.sh --check-limits  the same, but exit 1 if a running service has
#                                  no memory limit (HostConfig.Memory 0)
#
# oom_kill and RestartCount only reset when the container is recreated, so a
# past kill is a warning, never a failure. An OOM kill takes out one process,
# usually a uvicorn worker, which uvicorn respawns: the container keeps running
# and RestartCount stays 0, so oom_kill is the number to watch.
# Usage includes page cache the kernel can reclaim; the peak is the high-water
# mark since the container started (kernel memory.peak).
set -u

CONTAINER="${CONTAINER:-podman}"
SKI_WORKDIR="${SKI_WORKDIR:-/data/ski-work}"
CGROUP_ROOT=/sys/fs/cgroup/user.slice

# container name -> the .env variable that sets its limit
SERVICES=(
  "whumpf-postgis:POSTGIS_MEM_LIMIT"
  "whumpf-redis:REDIS_MEM_LIMIT"
  "whumpf-minio-svc:MINIO_MEM_LIMIT"
  "whumpf-titiler:TITILER_MEM_LIMIT"
  "whumpf-martin:MARTIN_MEM_LIMIT"
  "whumpf-api:API_MEM_LIMIT"
  "whumpf-pack-worker:PACK_WORKER_MEM_LIMIT"
  "whumpf-frontend:FRONTEND_MEM_LIMIT"
)

check_limits=0
[ "${1:-}" = "--check-limits" ] && check_limits=1

mib() { echo $(( $1 / 1048576 )); }

inspect() { "$CONTAINER" inspect --format "$2" "$1" 2>/dev/null; }

printf '%-20s %9s %9s %9s %5s %8s %8s\n' CONTAINER USED LIMIT PEAK USED% RESTARTS OOM_KILL
unlimited=()
killed=()
missing=()

for entry in "${SERVICES[@]}"; do
  name=${entry%%:*}
  var=${entry##*:}
  if ! id=$(inspect "$name" '{{.Id}}') || [ -z "$id" ]; then
    printf '%-20s %s\n' "$name" "not found"
    missing+=("$name")
    continue
  fi
  state=$(inspect "$name" '{{.State.Status}}')
  limit=$(inspect "$name" '{{.HostConfig.Memory}}')
  restarts=$(inspect "$name" '{{.RestartCount}}')
  limit=${limit:-0}
  if [ "$state" != running ]; then
    printf '%-20s %s\n' "$name" "not running ($state)"
    missing+=("$name")
    continue
  fi
  [ "$limit" -eq 0 ] && unlimited+=("$name:$var")

  cg=$(find "$CGROUP_ROOT" -maxdepth 4 -type d -name "libpod-$id.scope" 2>/dev/null | head -1)
  used="-"; peak="-"; oom="-"; pct="-"
  if [ -n "$cg" ]; then
    cur=$(cat "$cg/memory.current" 2>/dev/null || echo 0)
    used="$(mib "$cur")M"
    [ -r "$cg/memory.peak" ] && peak="$(mib "$(cat "$cg/memory.peak")")M"
    oom=$(awk '$1 == "oom_kill" {print $2}' "$cg/memory.events" 2>/dev/null)
    oom=${oom:--}
    [ "$limit" -gt 0 ] && pct=$(( cur * 100 / limit ))
    [ "$oom" != "-" ] && [ "$oom" -gt 0 ] && killed+=("$name:$var:$oom")
  fi
  if [ "$limit" -eq 0 ]; then limit_s="NONE"; else limit_s="$(mib "$limit")M"; fi
  printf '%-20s %9s %9s %9s %5s %8s %8s\n' "$name" "$used" "$limit_s" "$peak" "$pct" "${restarts:-?}" "$oom"
done

echo
status=0
for e in "${killed[@]+"${killed[@]}"}"; do
  IFS=: read -r name var n <<<"$e"
  echo "WARN: $name had $n process(es) OOM-killed since it was created (the counter resets when the container is recreated)."
  echo "      Raise $var in .env and run make up, or find what grew; see docs/runbook.md §13."
done
for name in "${missing[@]+"${missing[@]}"}"; do
  echo "WARN: $name is not running."
done
for e in "${unlimited[@]+"${unlimited[@]}"}"; do
  IFS=: read -r name var <<<"${e}"
  echo "NO MEMORY LIMIT: $name (HostConfig.Memory is 0). Recreate it with make up; the limit is $var in compose.yml."
  status=1
done

# The weekly ski rebuild leaves this file when it fails (make ski-tiles-refresh).
if [ -f "$SKI_WORKDIR/FAILED" ]; then
  echo "WARN: the last ski tile rebuild FAILED (the served tiles are unchanged):"
  sed 's/^/      /' "$SKI_WORKDIR/FAILED"
fi

[ "$check_limits" -eq 1 ] && exit "$status"
exit 0
