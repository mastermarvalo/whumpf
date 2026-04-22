# Whumpf runbook

Operational reference for the Ubuntu 24.04 VM on the UGREEN DXP4800 Plus NAS
(`n3rvnas`). Covers install, first boot, tear-down, and troubleshooting.

## Host environment assumed

- UGREEN DXP4800 Plus, 64 GB RAM, ~4 TB on `/volume1`
- Ubuntu 24.04 LTS VM: 6 CPU, 16 GB RAM, 100 GB disk
- Nginx Proxy Manager already running on the NAS (handles TLS and routing)

## 1. Install Podman + Docker compatibility layer

**Important caveat:** the `podman` package in Ubuntu 24.04's default repos is
4.9.3, which is too old for clean healthcheck-based `depends_on` with compose
v2. If you run into problems, grab a newer Podman from the Kubic project or
build from source. For most of what Whumpf needs, 4.9.3 works.

```bash
sudo apt update
sudo apt install -y podman podman-docker uidmap slirp4netns fuse-overlayfs
```

`podman-docker` installs a `docker` shim that points at Podman, so the Docker
CLI and Docker Compose v2 work transparently against Podman's API.

Enable the Podman user socket so Docker Compose v2 can talk to it:

```bash
systemctl --user enable --now podman.socket
export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/podman/podman.sock
# make it stick:
echo 'export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/podman/podman.sock' >> ~/.bashrc
```

Install Docker Compose v2 (the plugin binary) so `docker compose` works:

```bash
mkdir -p ~/.docker/cli-plugins
curl -fSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
  -o ~/.docker/cli-plugins/docker-compose
chmod +x ~/.docker/cli-plugins/docker-compose
docker compose version   # should print "Docker Compose version v2.x.x"
```

Confirm user namespaces for rootless:

```bash
grep "^$(whoami):" /etc/subuid /etc/subgid
# If nothing prints, add your user:
#   sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $(whoami)
#   podman system migrate
```

### Alternative: use `podman-compose`

If you'd rather skip the Docker shim, `podman-compose` also works:

```bash
sudo apt install -y podman-compose
make up COMPOSE=podman-compose
```

The trade-off: `podman-compose` has thinner support for compose-spec features
(notably `depends_on: condition: service_healthy`). With Whumpf you may see
the `api` container start before Postgres is ready and restart a few times
before coming up; docker-compose-on-Podman handles this cleanly.

## 2. Clone the repo and configure

```bash
git clone <your-repo-url> whumpf
cd whumpf
cp .env.example .env
```

Edit `.env` and set:

- `POSTGRES_PASSWORD` — any strong string
- `MINIO_ROOT_USER` — at least 3 characters
- `MINIO_ROOT_PASSWORD` — at least 8 characters
- `JWT_SECRET` — generate with `openssl rand -hex 32`

All three are required; the compose file will refuse to start without them.

## 3. First boot

```bash
make up
```

What happens, in order:

1. `postgis` starts. The `scripts/postgis-init.sql` script runs on first boot
   only, creating the `postgis`, `postgis_raster`, `postgis_topology`,
   `pg_trgm`, and `btree_gist` extensions.
2. `minio` starts. The volume is empty.
3. `minio-init` runs `mc mb` to create `dem-raw`, `dem-cogs`, `user-uploads`,
   and `strava-cache` buckets, then exits. It is a one-shot.
4. `titiler` and `martin` start and point at MinIO and Postgres respectively.
5. `api` builds from `backend/` and starts.
6. `frontend` builds from `frontend/` and starts Vite.

Check everything is up:

```bash
make ps
make health
```

`make health` should return `"ready": true` with a PostGIS version string.
If not, see Troubleshooting below.

## 4. Reverse proxy (Nginx Proxy Manager)

From the NAS's NPM instance, point your domains at the VM's IP:

| Domain                    | Upstream                | Purpose           |
|---------------------------|-------------------------|-------------------|
| `whumpf.<your-domain>`    | `http://<vm-ip>:5173`   | Frontend          |
| `api.whumpf.<your-domain>`| `http://<vm-ip>:8000`   | Backend API       |
| `tiles.whumpf.<your-domain>` | `http://<vm-ip>:3000` | Martin (MVT)      |
| `rasters.whumpf.<your-domain>` | `http://<vm-ip>:8001` | TiTiler        |
| `minio.whumpf.<your-domain>` | `http://<vm-ip>:9001` | MinIO console    |

Remember to update `.env` so `CORS_ALLOW_ORIGINS` and the `VITE_*` URLs match
the reverse-proxied origins, then `make restart`.

## 5. Tear-down

```bash
make down            # stop containers, keep data
make nuke            # stop containers AND delete all volumes — destructive
```

`make nuke` wipes Postgres data and every MinIO bucket. The raw DEM files
themselves (typically several GB) live in the `dem-raw` bucket and would need
to be re-downloaded from 3DEP.

## Troubleshooting

### `api` container restarts repeatedly

Usually means `postgis` didn't finish its initdb before `api` tried to
connect. Wait 30 seconds; compose v2 will restart `api` automatically once
the healthcheck passes. If it doesn't settle, `make logs-db` and look for
errors during `CREATE EXTENSION`.

### `titiler` returns 500 when loading COGs

Almost always a MinIO connectivity issue. TiTiler talks to MinIO through
rasterio/GDAL's S3 reader. Check that the env vars in `compose.yml` match
what's in `.env`:

```bash
docker compose exec titiler env | grep AWS_
```

`AWS_S3_ENDPOINT=minio:9000` — not `http://minio:9000`; the scheme is set
separately via `AWS_HTTPS=NO`.

### `martin` can't see new tables

Martin discovers PostGIS tables at startup. After you `CREATE TABLE` or
migrate, run:

```bash
docker compose restart martin
```

Or hit its `/catalog` endpoint and it will return only what's registered.

### `permission denied` on a bind mount

You're not using bind mounts for data (we use named volumes), so this should
not come up. If you added a bind mount: rootless Podman remaps UIDs, so
bind-mounted directories on the host need either the `:Z` SELinux label or
`userns_mode: "keep-id"`. See
<https://www.redhat.com/en/blog/rootless-podman-makes-sense>.

### Out of disk

```bash
docker system df
docker system prune --all --volumes   # nuclear — destroys stopped-container data
```

The VM has 100 GB. DEM COGs for Colorado are ~3 GB; CONUS at 1/3 arc-second
is ~180 GB and should live on the NAS share, not the VM disk, once the
pipeline is built in Phase 2.

### Podman version check

```bash
podman version
```

If you hit issues traceable to Podman itself (esp. `depends_on` timing
races), upgrade to a newer Podman from the Kubic project:

```bash
# Example: OpenSUSE Kubic maintained a newer stream; as of early 2026 the
# supported way on Ubuntu 24.04 is to build from source or grab a nightly
# deb. The Ubuntu 24.04-packaged 4.9.3 is usually good enough for Whumpf.
```

## Reference

- [whumpf-stack.md](../whumpf-stack.md) — the "why" behind every tech choice
- [architecture.md](./architecture.md) — architecture diagrams and flow
- [data-sources.md](./data-sources.md) — feed URLs, schemas, licensing
