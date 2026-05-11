# -----------------------------------------------------------------------------
# Whumpf frontend — multi-stage build.
#
# Targets:
#   dev  — Vite dev server (HMR, source maps, no minification). 5173.
#   prod — nginx serving a built bundle. 5173 (same port for upstream parity).
#
# Pick one with `target:` in compose.yml. The prod target inlines the
# VITE_* env vars at build time via ARG, since Vite bakes them as string
# constants into the bundle and there's no way to override at runtime.
# -----------------------------------------------------------------------------
FROM docker.io/library/node:22-alpine AS deps

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install

# ── dev: hot-reload via volume mount in compose.yml ───────────────────────────
FROM deps AS dev

COPY . .
EXPOSE 5173
# --host 0.0.0.0 so the port binding is reachable from outside the container;
# Vite defaults to 127.0.0.1.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

# ── build: produces dist/ that the prod stage serves ──────────────────────────
FROM deps AS build

ARG VITE_API_URL=http://localhost:8000
ARG VITE_MARTIN_URL=http://localhost:3000
ARG VITE_TITILER_URL=http://localhost:8001
ARG VITE_SENTRY_DSN=
ARG VITE_PLAUSIBLE_DOMAIN=
ARG VITE_PLAUSIBLE_SRC=
ENV VITE_API_URL=$VITE_API_URL \
    VITE_MARTIN_URL=$VITE_MARTIN_URL \
    VITE_TITILER_URL=$VITE_TITILER_URL \
    VITE_SENTRY_DSN=$VITE_SENTRY_DSN \
    VITE_PLAUSIBLE_DOMAIN=$VITE_PLAUSIBLE_DOMAIN \
    VITE_PLAUSIBLE_SRC=$VITE_PLAUSIBLE_SRC

COPY . .
RUN npm run build

# ── prod: nginx serving the built bundle ──────────────────────────────────────
FROM docker.io/library/nginx:1.27-alpine AS prod

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 5173
