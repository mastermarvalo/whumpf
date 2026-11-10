#!/usr/bin/env bash
# The Expo web app (whumpf-ios's static export), served by the web service
# from /data/web/current. docs/runbook.md §17.
#
#   scripts/web.sh build [REF]   fetch whumpf-ios at REF (branch, tag or
#                                commit; default main), build it, switch to it
#   scripts/web.sh rollback      switch back to the release before this one
#   scripts/web.sh status        what is served, the kept releases, HTTP check
#   scripts/web.sh health        one line for make health (never fails)
#
# Layout under WEB_DIR (default /data/web):
#   repo.git/                 bare clone, fetched over SSH with the deploy key
#   npm-cache/                npm's download cache, shared between builds
#   releases/<sha12>/dist/    the export; nginx's root when current
#   releases/<sha12>/nginx-routes.conf   dynamic routes (scripts/web_routes.py)
#   releases/<sha12>/release.json        commit, ref, build time, Node image
#   current -> releases/<sha12>
#   history                   one sha12 per deploy, newest last
#
# The repo is reached through the github-whumpf-ios host alias in
# ~/.ssh/config (the read-only deploy key); no token is used or stored.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WEB_DIR="${WEB_DIR:-/data/web}"
WEB_REPO="${WEB_REPO:-git@github-whumpf-ios:mastermarvalo/whumpf-ios.git}"
WEB_KEEP="${WEB_KEEP:-5}"
WEB_BUILD_MEM="${WEB_BUILD_MEM:-4g}"
# Node 24, as whumpf-ios's CI (.nvmrc). Bump both together.
WEB_NODE_IMAGE="${WEB_NODE_IMAGE:-docker.io/library/node:24.22.2-bookworm-slim}"
WEB_PORT="${WEB_PORT:-8080}"
CONTAINER="${CONTAINER:-podman}"
COMPOSE="${COMPOSE:-$HOME/.docker/cli-plugins/docker-compose}"

RELEASES="$WEB_DIR/releases"
REPO="$WEB_DIR/repo.git"
HISTORY="$WEB_DIR/history"

die() { echo "web: $*" >&2; exit 1; }

# The web service's image, read from compose.yml so the config test below
# runs the same nginx as the service.
nginx_image() {
  awk '/^  web:/{w=1; next} w && /^  [a-z]/{exit} w && $1 == "image:" {print $2; exit}' "$ROOT/compose.yml"
}

lock() {
  mkdir -p "$RELEASES"
  exec 9>"$WEB_DIR/.lock"
  flock -n 9 || die "another web build or rollback is running"
}

current() {
  [ -L "$WEB_DIR/current" ] || return 0
  basename "$(readlink "$WEB_DIR/current")"
}

switch_to() {
  ln -sfn "releases/$1" "$WEB_DIR/.current.tmp"
  mv -T "$WEB_DIR/.current.tmp" "$WEB_DIR/current"
}

web_running() {
  [ "$("$CONTAINER" inspect --format '{{.State.Running}}' whumpf-web 2>/dev/null)" = true ]
}

# Picks up the new release's nginx-routes.conf. Files are served through the
# symlink, so pages switch even without it.
reload() {
  if web_running; then
    "$COMPOSE" exec -T web nginx -t -q && "$COMPOSE" exec -T web nginx -s reload
  else
    echo "web: the web service isn't running; make up starts it"
  fi
}

# nginx -t on web/nginx.conf with this release as current, before switching.
check_config() {
  local image
  image="$(nginx_image)"
  [ -n "$image" ] || die "no web service image in compose.yml"
  "$CONTAINER" run --rm --network none --entrypoint nginx \
    -v "$ROOT/web/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
    -v "$RELEASES/$1:/data/web/current:ro" \
    "$image" -t -q \
    || die "nginx rejects the config with release $1; nothing was switched"
}

prune() {
  local keep cur d name
  cur="$(current)"
  keep="$( (tac "$HISTORY" 2>/dev/null || true) | awk 'NF && !seen[$0]++' | head -n "$WEB_KEEP")"
  for d in "$RELEASES"/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    [ "$name" = "$cur" ] && continue
    grep -qxF "$name" <<<"$keep" && continue
    rm -rf "$d"
    echo "web: removed old release $name"
  done
}

resolve() {
  local ref="$1" sha
  for cand in "refs/remotes/origin/$ref" "refs/tags/$ref"; do
    if sha="$(git -C "$REPO" rev-parse --verify -q "$cand^{commit}")"; then echo "$sha"; return; fi
  done
  if [[ "$ref" =~ ^[0-9a-f]{7,40}$ ]] && sha="$(git -C "$REPO" rev-parse --verify -q "$ref^{commit}")"; then
    echo "$sha"; return
  fi
  die "no branch, tag or commit '$ref' in whumpf-ios"
}

fetch() {
  if [ ! -d "$REPO" ]; then
    git init -q --bare "$REPO"
    git -C "$REPO" remote add origin "$WEB_REPO"
  fi
  git -C "$REPO" remote set-url origin "$WEB_REPO"
  # BatchMode: fail instead of prompting for a passphrase or an unknown host key.
  GIT_SSH_COMMAND="ssh -o BatchMode=yes" GIT_TERMINAL_PROMPT=0 \
    git -C "$REPO" fetch -q --prune --tags origin '+refs/heads/*:refs/remotes/origin/*' \
    || die "can't fetch $WEB_REPO; is the deploy key set up? (docs/runbook.md §17)"
}

build_release() {
  local sha="$1" ref="$2" short="${1:0:12}" tmp rc
  tmp="$RELEASES/.build-$short"
  rm -rf "$RELEASES"/.build-* "$RELEASES"/.old-*
  mkdir -p "$tmp/src" "$WEB_DIR/npm-cache"
  trap 'rm -rf "$tmp"' EXIT
  git -C "$REPO" archive "$sha" | tar -x -C "$tmp/src"

  echo "web: building $short ($ref) in $WEB_NODE_IMAGE"
  "$CONTAINER" run --rm --memory="$WEB_BUILD_MEM" \
    -e CI=1 -e EXPO_NO_TELEMETRY=1 -e npm_config_cache=/npm-cache \
    -v "$tmp/src:/app" -v "$WEB_DIR/npm-cache:/npm-cache" -w /app \
    "$WEB_NODE_IMAGE" \
    sh -c 'npm ci --no-audit --no-fund && npx expo export -p web --output-dir dist' \
    || { rc=$?; [ $rc -eq 137 ] && echo "web: the build was OOM-killed at WEB_BUILD_MEM=$WEB_BUILD_MEM; raise it"; exit $rc; }

  for f in index.html +not-found.html _expo/static; do
    [ -e "$tmp/src/dist/$f" ] || die "the export has no $f; not deploying it"
  done
  mv "$tmp/src/dist" "$tmp/dist"
  python3 "$ROOT/scripts/web_routes.py" "$tmp/dist" > "$tmp/nginx-routes.conf"
  python3 - "$tmp/release.json" "$sha" "$ref" "$WEB_NODE_IMAGE" <<'EOF'
import json, sys, datetime
path, sha, ref, image = sys.argv[1:]
built = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
json.dump({"commit": sha, "ref": ref, "built_at": built, "node_image": image}, open(path, "w"), indent=1)
EOF
  rm -rf "$tmp/src"

  if [ -d "$RELEASES/$short" ]; then mv "$RELEASES/$short" "$RELEASES/.old-$short"; fi
  mv "$tmp" "$RELEASES/$short"
  rm -rf "$RELEASES/.old-$short"
  trap - EXIT
}

cmd_build() {
  local ref="${1:-main}" sha short
  lock
  fetch
  sha="$(resolve "$ref")"
  short="${sha:0:12}"
  if [ -f "$RELEASES/$short/dist/index.html" ] && [ "${FORCE:-}" != 1 ]; then
    echo "web: $short is already built; switching to it (FORCE=1 rebuilds)"
  else
    build_release "$sha" "$ref"
  fi
  check_config "$short"
  switch_to "$short"
  if [ "$(tail -n 1 "$HISTORY" 2>/dev/null)" != "$short" ]; then echo "$short" >> "$HISTORY"; fi
  reload
  prune
  echo "web: deployed $short ($sha, $ref)"
}

cmd_rollback() {
  local cur n target
  lock
  cur="$(current)"
  [ -n "$cur" ] || die "nothing is deployed"
  mapfile -t h < <(grep -v '^$' "$HISTORY" 2>/dev/null || true)
  n=${#h[@]}
  [ "$n" -ge 1 ] && [ "${h[n-1]}" = "$cur" ] || die "current ($cur) isn't the last deploy in $HISTORY; use make web-build REF=<commit>"
  [ "$n" -ge 2 ] || die "no earlier release to go back to"
  target="${h[n-2]}"
  [ -f "$RELEASES/$target/dist/index.html" ] || die "release $target was pruned; make web-build REF=$target rebuilds it"
  check_config "$target"
  switch_to "$target"
  printf '%s\n' "${h[@]:0:n-1}" > "$HISTORY.tmp" && mv "$HISTORY.tmp" "$HISTORY"
  reload
  echo "web: rolled back from $cur to $target ($(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["commit"])' "$RELEASES/$target/release.json"))"
}

http_code() {
  curl -s -o /dev/null -m 5 -w '%{http_code}' "http://localhost:$WEB_PORT/" || true
}

cmd_status() {
  local cur d name mark
  cur="$(current)"
  if [ -n "$cur" ]; then
    echo "current: $cur"
    cat "$RELEASES/$cur/release.json"; echo
  else
    echo "current: nothing deployed"
  fi
  echo "releases (newest first):"
  for d in $(ls -1td "$RELEASES"/*/ 2>/dev/null); do
    name="$(basename "$d")"
    mark=" "; [ "$name" = "$cur" ] && mark="*"
    echo "  $mark $name  $(python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print(r["built_at"], r["ref"])' "$d/release.json" 2>/dev/null)"
  done
  echo "http://localhost:$WEB_PORT/ -> $(http_code)"
}

cmd_health() {
  local cur code
  cur="$(current)"
  code="$(http_code)"
  if [ "$code" = 200 ]; then
    echo "web: serving ${cur:-?} (HTTP 200 on :$WEB_PORT)"
  elif [ -z "$cur" ]; then
    echo "WARN: web: nothing deployed yet (make web-build)"
  else
    echo "WARN: web: release $cur, but http://localhost:$WEB_PORT/ gave ${code:-no answer}"
  fi
}

case "${1:-}" in
  build) cmd_build "${2:-main}" ;;
  rollback) cmd_rollback ;;
  status) cmd_status ;;
  health) cmd_health ;;
  *) echo "usage: $0 build [REF] | rollback | status | health" >&2; exit 2 ;;
esac
