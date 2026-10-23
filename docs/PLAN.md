# Whumpf — Gap Analysis and Plan

Exported Oct 13, 2026 from the Whumpf — Gap Analysis and Plan doc. The doc is the live version; re-export after big changes.

## Where things stand

The app runs on a real iPhone, but nothing has been tested end to end against the real API, and two fixes made on the laptop are not in GitHub. Checked against `whumpf-ios` at commit 1816ecd (Oct 12) and `whumpf` at fd6ff51.

| Item | STATUS.md says | Actually |
| --- | --- | --- |
| First iPhone build (Next up #2) | Not done | Done Oct 12. Move to Done |
| Real iPhone run | Not yet | Ran Oct 12. The map loads, but `terrain_rgb` tiles time out |
| Smoke test against the real API (Next up #1) | Next | Still open. Only tiles were hit |
| fast-png `latin1` crash on Hermes | Not listed | Fixed on the laptop only. No `src/polyfills/` in the repo |
| Duplicate `maplibre-gl` in package.json | Not listed | Fixed and pushed |
| Offline chip and Record button | Placeholders | Still placeholders ("Coming in a later level") |
| Stray `app/index.tsx` at repo root | Not listed | A "Hello World" leftover outside `src/`. Delete it |
| `README.txt` | Not listed | Zip-install notes from Oct 10. Stale; delete or replace |
| Last updated | Oct 10 | Three days and one iPhone run behind |

## Backend status (Oct 14, 2026)

Checked against `whumpf` at 65846e8. This covers the backend repo only; the app screens in `whumpf-ios` are not checked here.

- **Phase 0 network hardening:** PostGIS, Redis, MinIO and Martin bind to 127.0.0.1, and Redis requires `REDIS_PASSWORD` (174b508).
- **Phase 1, backend done:** Strava routes are owner-only (a5168f5); account deletion works for users with data (ff9b806); signed tile tokens with per-token rate limits (a7348c6).
- **Phase 2, backend complete:** rotating refresh tokens (0ad0703, 4d0cf7b); 6-digit email codes for verify-email and password reset, change-password and change-email (8f01a13); `DELETE /auth/me` requires the password (fd87e0a); Strava connect takes an allowlisted `return_to` carried in a signed state (ba17c5d). An expired state redirects back with `reason=expired`; a forged one is a 400.
- **Alembic, done:** schema changes go through `make makemigration`; `0001_baseline` is stamped on prod; tests run on `whumpf_test` (97bc465, `docs/runbook.md` §6).
- **Own ski tiles, done:** `/tiles/ski` serves our own OSM ski run tiles, rebuilt weekly on Tuesdays at 04:00 with rollback if Martin doesn't come back (0a52151, `docs/runbook.md` §7).
- **Backups, done:** daily at 03:00 from cron; a local dump kept 14 days plus Cloudflare R2, each read back and checked by sha256 (3a86dca, 65846e8).
- **Cloudflare `/tiles/*` cache, done and verified** (MISS then HIT; 65e9f50).
- **Weather backend, done (Oct 16):** a shared raster layer module (5e77765); snow depth from NOHRSC SNODAS (8ba8c50), current temperature from NOAA RTMA (8662bf8), radar from NOAA MRMS in 10-minute frames for 2 h (d7dde54), and the spot forecast `GET /weather/forecast` from api.weather.gov (0cfa61d). `docs/runbook.md` §9.
- **Road conditions backend, done (Oct 18):** CDOT COtrip conditions, incidents and planned events at `GET /roads`, lines at `/tiles/roads/segments`, every 5 min; `make roads-sample` records CDOT's values for a week before chain law, traction law and avalanche control get mapped. `docs/runbook.md` §11.
- **Where we are (Oct 23):** Phase 3 is done except a laptop docs pass; Phase 4 is in progress. Decided Oct 23: TestFlight for a few friends (now Phase 6) and removing the old site (Phase 5) come before the trips work (now Phase 7). whumpf.co will serve the Expo web app from the VM. TestFlight's likely backend part, serving the privacy policy, terms and credits pages, is not built.

**Turn off when the old website is retired:** `TILES_ALLOW_TOKENLESS` and `LEGACY_EMAIL_LINKS`. `STRAVA_RETURN_TO_ALLOW_DEV` stays on for local web testing until before the App Store. See "Settings added in Phase 2" in `docs/runbook.md`.

## Gaps

Many gaps are app screens for backend features that already exist. Your biggest differentiator, CAIC observations and incidents, is served by the backend but not shown in the app.

| Area | Gap | Where | Blocks |
| --- | --- | --- | --- |
| Map reliability | `terrain_rgb` tiles time out on the phone; even zoom 6 failed | Backend `routers/tiles.py` renders tiles on demand, in-memory cache only | Everything on the map |
| Map reliability | A failed tile is cached as "no data" and never retried until evicted | App `data/terrain/terrainSource.ts` (`catch(() => null)` is cached) | Tap readouts after a blip |
| Strava | Imported Strava routes can be shared, cloned and shown to trip members; clone erases the Strava tag | Backend `routers/routes.py`, `routers/trips.py` | Friend play-testing |
| Strava | ~~Imports stored permanently; no 7-day handling, no deauthorization webhook~~ Backend built Oct 23 (not deployed): webhook, disconnect deletes imports, 7-day refresh job (runbook §14). The app's disconnect text must warn that imports are deleted | Backend `routes.py`, `services/strava.py` | Compliance at any scale |
| Strava | Connect finishes on whumpf.co; you come back and tap Refresh | App `features/strava/connectStrava.ts`, backend `STRAVA_SUCCESS_URL` | Smooth sign-up; breaks once the old frontend is gone |
| Accounts | No sign-up, email verification, password reset or delete-account screens | App has sign-in only; backend has `/register`, `/verify-email/*`, `/password-reset/*`, `DELETE /me` | Friends signing up on their phone |
| Accounts | Sign-in lasts 60 min, then you are signed out; no refresh token | Backend `auth/service.py` | Everyday use |
| Accounts | `DELETE /me` likely fails for anyone with saved routes or trips: no cascade on the foreign keys, and no test covers it | Backend `routers/auth.py`, `models/` | App Store review (in-app deletion is required when accounts exist) |
| Differentiator | CAIC field observations and caught-in incidents not shown | Backend has `GET /avalanche/observations`; app never calls it | Your main pitch against onX |
| Sharing | No route share links in the app (backend has share, revoke, clone) | App route screen | Sharing with friends |
| Trips | No editing of trip name, dates or waypoints (backend has `PATCH`) | App `app/trip/[id].tsx` | Real trip planning |
| Backcountry core | Track recording is a placeholder; only "when in use" location permission is set | App main map, `app.json` | Touring use |
| Backcountry core | Offline regions are a placeholder; 3D loads maplibre-gl from unpkg | App `OfflineChip`, `terrain3dHtml.ts` | Use without signal, the main promise |
| Security | Tile endpoints are public with no rate limit; only auth endpoints are limited. Fix decided: signed tile tokens (see Tile access) | Backend `routers/tiles.py` | Server load if anyone scrapes |
| Security | `whumpf-ios` is a public repo | GitHub settings | Decided: making it private |
| Web | Old Vite frontend still serves whumpf.co; email links and the Strava callback point at it | Backend `services/email.py`, `compose.yml`, `frontend/` | Cutover to Expo web |
| Docs | `CLAUDE.md` and `docs/STATUS.md` are stale; the backend has no `CLAUDE.md` | Both repos | Every Claude Code session |

## The plan

Stabilize first, then build iPhone and web together.

| Phase | Work | Gate to move on |
| --- | --- | --- |
| 0. Stabilize (start here) | Push laptop fixes, fix tile retries, diagnose timeouts; smoke test the real API; refresh CLAUDE.md and STATUS.md | Map and taps work with the real API |
| 1. Safe for friends | Strava routes owner-only; account deletion fixed; signed tile tokens and rate limits | No Strava data reaches another user |
| 2. Accounts in the app | Sign-up, verify email, reset password, delete account; refresh tokens; Strava connect returns to the app | A friend signs up on the phone |
| 3. Fix and verify | ~~Backend CI green via a ruff config~~ (done); Strava connect returns to the iPhone app; phone test of the code that was only type-checked; `/docs` off in prod; `whumpf-ios` made private | Everything built so far works on the phone and CI is green |
| 4. Core features (in progress) | Recent snowfall, layer order and opacity, weather, spot forecast, wind arrows, mountain cameras, outage banners, route hazard summary, Munter times, phone inclinometer, slope-above-you runout tap check, Strava 7-day rules and deauth webhook, offline map downloads, offline 3D | Web covers the old site |
| 5. Swap whumpf.co | Parity check, CORS update, email link routes; serve the Expo web app at whumpf.co from the VM, delete old frontend; turn off `TILES_ALLOW_TOKENLESS` and `LEGACY_EMAIL_LINKS` | One codebase serves both |
| 6. TestFlight | App Store Connect record, icon, privacy policy, safety disclaimer, credits, crash reporting, EAS Update; invite a few friends and collect feedback. From here on, API changes stay backward compatible | Feedback sorted into fixes |
| 7. Trips and recording | Trip editing, in-app sharing, GPX, trip briefing, GPS recording, forecast-change alerts | |
| 8. Polish | Before-and-after renders of each screen; pick designs, then prompt the changes | |

Web track: runs alongside phases 1 to 4. Each feature ships on iPhone and web together, with X.web.tsx only where the code must differ. The old whumpf.co stays up until Phase 5, when the VM serves the Expo web app at whumpf.co.

Phase 0 comes first because nothing else can be tested while tiles time out. After that, every feature ships on iPhone and web together, and whumpf.co switches to Expo web in Phase 5 once it covers the old site.

## Prompts for Claude Code

One prompt per change, in order. Each names its repo. Backend prompts let Claude Code change code and tests; you review and deploy.

### Phase 0: Stabilize

```text
[whumpf-ios] Add the Hermes latin1 TextDecoder shim as src/polyfills/textDecoder.ts, imported first in src/app/_layout.tsx. Delete the stray root app/ folder and README.txt. Run the checks in CLAUDE.md, then show me the diff.
```

```text
[whumpf-ios] In src/data/terrain/terrainSource.ts, a failed tile load is cached as null and never retried. Only cache successful tiles and real "no data" (204) answers; let errors retry on the next tap. Explain the change, then run the terrain checks.
```

```text
[whumpf] The iPhone app's terrain_rgb tile requests time out (z6, z10, z11, region=colorado). Read routers/tiles.py and compose.yml and list the likely causes, ranked, with the command I should run on the server to confirm each one. Don't change code.
```

```text
[whumpf-ios] Do STATUS.md "Next up" #1: a smoke-test checklist against the real API (sign in, tap point, CAIC, SNOTEL, routes, trips, waypoints, Strava import). Write it as docs/SMOKE_TEST.md for me to run on the phone.
```

**Network exposure (found Oct 13).** `api.whumpf.co` resolves to the home IP and responses come from Nginx Proxy Manager (`Server: openresty`), so Cloudflare's proxy, cache and WAF are not in the path. `ufw` is inactive, and Postgres, Redis (no password), Martin, TiTiler, MinIO and the API listen on all interfaces. A scanner was already probing the API for `.env` files and keys (all 404).

- [ ] Router forwards only 80 and 443 to Nginx Proxy Manager (192.168.3.123)
- [x] `ufw` on the VM (192.168.1.100), Oct 13: SSH from the LAN, 8000/8001/5173 only from Nginx Proxy Manager, deny the rest. Martin (3000) is closed to the network
- [x] Bind Postgres, Redis, MinIO and Martin to 127.0.0.1 in `compose.yml`, and give Redis a password (174b508). TiTiler stays reachable until the old website, which calls it directly, is gone
- [x] TiTiler locked down in Nginx Proxy Manager (Oct 13): only `/cog/tiles` and `/cog/point`, only `s3://dem-cogs/` URLs, no `..`. Remove titiler.whumpf.co when the old website goes
- [x] Cloudflare proxy on (Oct 13) for api.whumpf.co, whumpf.co and the `*.whumpf.co` wildcard; SSL mode Full (strict). Public DNS now returns Cloudflare addresses
- [x] Nginx Proxy Manager reviewed (Oct 13): martin.whumpf.co disabled (unused), default site set to 404
- [ ] Flush the Pi-hole cache (or check its Local DNS records) so the home network also goes through Cloudflare
- [x] Cache Rule (Oct 13): `https://api.whumpf.co/tiles/*`, eligible for cache. Verified MISS then HIT
- [x] WAF custom rule (Oct 13) under Security rules: blocks paths containing `.env`, `.php`, `.git` or `/.ssh`. Verified 403. Bot Fight Mode left off: on the free plan it can challenge the iPhone app's API calls
- [ ] Router 80/443 limited to Cloudflare IP ranges, or replace the port forward with a Cloudflare Tunnel. The home IP was public while records were DNS only, so this is what stops direct access around Cloudflare
- [ ] Nginx Proxy Manager passes `CF-Connecting-IP` as X-Forwarded-For, so per-IP limits see real users (only after the item above)

```text
[whumpf] Change compose.yml so PostGIS, Redis, MinIO (API and console) and Martin listen on 127.0.0.1 only (not TiTiler yet: the old website calls it directly), and Redis requires a password from .env (REDIS_URL updated). Keep the API on 0.0.0.0:8000. Update .env.example and docs/runbook.md.
```

### Phase 1: Safe for friends

```text
[whumpf] Make a change so Strava-sourced routes can't be shared, cloned or shown to other trip members: a source column on routes, server-side checks on share, clone and trip reads, and tests. Clone of a Strava route must be refused, not untagged. Imports stay saved and owner-only. I'll review and deploy.
```

```text
[whumpf] Write a failing test that deletes a user who owns routes, trips, shares and friendships via DELETE /auth/me. Then fix it (cascades or explicit cleanup) and make the test pass.
```

```text
[whumpf] Add signed tile tokens with per-token rate limits (see Tile access) that a normal map session never hits but scraping does. Show the numbers and how you picked them.
```

### Phase 2: Accounts in the app

```text
[whumpf-ios] Add sign-up, "check your email", forgot password, and delete account (You tab, with confirm) using the existing backend endpoints in routers/auth.py. Build it for iPhone and web together. Match the existing ui.tsx components.
```

```text
[whumpf] Add refresh tokens: a long-lived refresh token, a /auth/refresh endpoint, and rotation. Then [whumpf-ios] make client.ts refresh on 401 before signing out, on iPhone and web.
```

```text
[whumpf] + [whumpf-ios] Make Strava connect return to the app: callback redirects to whumpf://strava?status=..., the app opens it with WebBrowser.openAuthSessionAsync, and the web build keeps a web URL. Build both parts, web included.
```

### Phase 3: Fix and verify

```text
[whumpf] ~~Get backend CI green~~ Done: ruff config, fixes, 241 tests passing, and the old site's lockfile fixed; Actions run is green.
```

```text
[whumpf-ios] Make the Strava iPhone redirect work end to end: connect returns to whumpf://strava?status=... and the app shows the result. List what I should check on the phone.
```

```text
[whumpf-ios] List every change that has only been type-checked and never run on the phone, with a step-by-step checklist for me to run through. Add it to docs/SMOKE_TEST.md.
```

```text
[whumpf] Turn off /docs, /redoc and /openapi.json in prod (keep them in dev), with a test. Add or update the env var in .env.example.
```

Making `whumpf-ios` private is a GitHub setting, not a prompt: do it, then attach the repo with GitHub access for future Claude sessions.

### Phase 4: Core features

Build order is flexible. Suggested grouping: layers (recent snowfall, order and opacity, weather, spot forecast, wind arrows, cameras, outage banners); terrain on the phone (route hazard summary, inclinometer, slope-above-you runout tap check, Munter times); Strava compliance (7-day rules, deauthorization webhook); offline (map downloads, then offline 3D). Trips, GPS recording and alerts moved to Phase 7 (decided Oct 23).

```text
[whumpf-ios] Add the Recent snowfall layer. The backend is done (whumpf docs/runbook.md §8): GET /tiles/snowfall/meta?region=colorado gives per period (24h, 72h) the window start and valid time (UTC), a tile URL template that already carries ?v= (add ?t= in tiles.ts as for every tile), maxzoom 10, bounds, the legend in inches and the NOAA attribution. Add it to the Layers sheet with a 24 h / 72 h switch and the stepped legend, and always show the window in local time (e.g. "72 h to 6 am Oct 15") plus "NOAA/NWS NOHRSC" while it is on. Refetch meta when the app comes to the foreground. In the SNOTEL point sheet show new_snow_24h_in / new_snow_72h_in (and new SWE) with new_snow_as_of; null means unknown, not zero.
```

```text
[whumpf-ios] Add a CAIC observations layer from GET /avalanche/observations: markers by type (caught, avalanche, other), a toggle in the Layers sheet, and a tap card. Show when the data was fetched.
```

```text
[whumpf-ios] Level 12, offline regions: design doc first. PMTiles packs built on the server, download and storage on the phone, tiles.ts switching to local files, and bundling maplibre-gl for 3D.
```

### Phase 5: Swap whumpf.co

```text
[whumpf] + [whumpf-ios] Make a parity list: every feature of the old frontend/ vs the Expo web build, marked done, partial or missing.
```

```text
[whumpf-ios] Add routes for the email links (/verify, /reset, /trip-invite) and plan deploying npx expo export -p web to beta.whumpf.co.
```

### Phase 6: TestFlight

```text
[whumpf-ios] Walk me through a TestFlight build: App Store Connect app record, eas.json production profile, app icon and splash, privacy policy URL, and the App Store privacy answers for location, email and Strava data.
```

### Phase 7: Trips and recording

```text
[whumpf-ios] Level 11, recording: GPS track with background location, hold-to-stop, save as a route with source=recorded. List the app.json permission changes first.
```

```text
[whumpf-ios] Add route sharing on the route screen (create, copy, revoke a link) for routes that are not from Strava. Hide it for Strava routes.
```

### Docs (do first, and after each phase)

```text
[whumpf-ios] Update CLAUDE.md and docs/STATUS.md: this repo is the iPhone app AND the replacement for the whumpf.co website; add the Strava rule (Strava data is only ever shown to its owner); every feature ships on iPhone and web together; Claude Code may edit the backend repo but never deploys; move done items; add the open issues from the Oct 12 iPhone run.
```

```text
[whumpf] Write a CLAUDE.md for the backend: stack, ports, compose rules (host networking, docker-compose), how to run tests, the Strava rule, and that you may change code and tests but I deploy, and production config changes need my approval.
```

## Feature plan: map layers

Most of the old website's layers are not in the app yet. Observations need no backend work. Radar needs a new source, because RainViewer's free API changed.

| Layer | Old website source | In the app | Plan |
| --- | --- | --- | --- |
| CAIC field observations and caught-in incidents | Backend `GET /avalanche/observations` | No | Markers by type (caught, avalanche seen, field obs), tap card with observer, date, zone, description |
| Precipitation radar, past 2 h with time slider | RainViewer tiles | No | Backend done (Oct 16): NOAA MRMS, 10-minute frames for the last 2 h at `/tiles/radar` (runbook §9). RainViewer's free API is personal and educational use only since Jan 1, 2026 ([RainViewer FAQ](https://rainviewer.com/api/transition-faq.html)). The app needs the layer and slider |
| Precipitation total, last hour | NOAA MRMS (mapservices.weather.noaa.gov) | No | Port as is |
| Temperature | NOAA NDFD (+3 h and 24 h forecast) | No | Backend done (Oct 16): current temperature from NOAA RTMA at `/tiles/temperature` (runbook §9). The forecast layers are dropped; the spot forecast covers future temperatures. NBM is in "Later" |
| Snow depth | NOAA NOHRSC | No | Backend done (Oct 16): NOHRSC SNODAS at `/tiles/snowdepth` (runbook §9) |
| Spot forecast on tap | api.weather.gov | Wind only (slope filter) | Backend done (Oct 16): `GET /weather/forecast` (runbook §9). Add a forecast card to the tap sheet |
| Wind arrows | None (new) | No | Backend done (Oct 16): wind now from NOAA RTMA at `/tiles/wind` (runbook §9), one GeoJSON file of arrows per hour. The app needs the layer, and "From wind" should use `/weather/forecast` with a chosen hour (prompt below). Forecast arrows from NBM are in "Later" |
| Mountain cameras | None (new) | No | Backend done (Oct 16): NPS (RMNP) and Windy webcams at `GET /cameras`, a fresh image per tap at `/cameras/{id}/image` (runbook §10). Images load from the source and are never cached; free to every user (Windy terms). The app needs the layer (prompt below). CDOT cameras are tabled ("Later") |
| Road conditions | None (new) | No | Backend done (Oct 18): CDOT COtrip road conditions per segment, incidents and planned events at `GET /roads`, segment lines at `/tiles/roads/segments` (runbook §11). Only values seen in the real feed are classified; the rest shows CDOT's own text. Chain law, traction law and avalanche control are mapped after a week of `make roads-sample`. CDOT's full disclaimer must show in the layer info and the credits page. The app needs the layer (prompt below) |
| Aspect | TiTiler COG | No | Port; it is a natural partner to slope |
| Contours | Backend `/tiles/contours` | No | Port after fixing the lag the website has |
| Trails and peaks, streets | OpenFreeMap vector | No | Port; trails matter most for ski touring |
| Ski resort runs | OpenSnowMap | No | Done on the backend: our own OSM vector tiles at `/tiles/ski` (runbook §7); the app needs the layer |
| Satellite | Esri | No | Use USGS imagery (NAIP, public domain, same USGS server as the topo map), online and in offline packs. Esri's terms forbid offline saving. NAIP is summer imagery, so a winter Sentinel-2 layer could follow |
| Slope, hillshade, CAIC zones, SNOTEL, my routes, Strava | Various | Yes | Keep |

### Layer order and opacity

1. **One layer list.** Every layer is defined once (id, group, tile URL from `tiles.ts`, default opacity, legend, online-only flag). iPhone, web and 3D all draw from it, so a new layer is added in one place.
2. **Settings survive a restart.** Today `mapSettings.tsx` holds them only in memory, so every launch resets them. Save visibility, order and opacity on the phone.
3. **Reorder by dragging** in the Layers sheet, on the phone too (the website allowed it only on desktop, and only within the terrain group). Markers and your own lines always stay on top of raster layers.
4. **An opacity slider** under each layer that is turned on.
5. **3D follows the same settings.** The 3D view is a separate web page, so order and opacity have to be passed into it.
6. **Weather and observations are online-only.** Each shows when it was fetched and a "needs signal" note offline. They never go into offline packs.

```text
[whumpf-ios] Move every map layer into one list (src/data/layers.ts) used by TerrainMap, TerrainMap.web and Terrain3D. Save visibility, order and opacity on the device. Add drag-to-reorder and an opacity slider to the Layers sheet, on iPhone and web. Check how maplibre-react-native 11 orders layers before you start, and tell me.
```

```text
[whumpf-ios] Add the CAIC observations layer from GET /avalanche/observations: markers by type, a tap card, and the fetch time. Default to the last 7 days, adjustable. Match the old website's layers/obs.ts behaviour. Also add a USGS imagery (NAIP) satellite layer from basemap.nationalmap.gov, and confirm its terms allow offline saving.
```

```text
[whumpf-ios] Add the weather layers and the spot forecast. The backend is done (whumpf docs/runbook.md §9); every URL needs ?t= as for every tile. Layers: radar (GET /tiles/radar/meta?region=: frames oldest first, each with time, observed and a tiles template; a time slider over the frames, default to the newest, "radar unavailable" when frames is empty), current temperature (/tiles/temperature/meta, °F, label it as an analysis) and snow depth (/tiles/snowdepth/meta, inches). Each meta gives the legend, maxzoom, bounds and attribution: show the valid time in local time, the attribution and a stale warning while a layer is on, refetch meta on foreground, and mark the layers online-only. Spot forecast: GET /weather/forecast?lat=&lon=&t= on the tap sheet: the next 48 h (temperature, wind and gusts with direction, precipitation chance), snow in its 6 h blocks, the text periods, and "forecast for 10,200 ft (you tapped 12,400 ft)" from grid.elevation_ft and point_elevation_ft. Show "NOAA/National Weather Service", a stale note when stale is true, and handle 404 (no NWS forecast here) and 429.
```

```text
[whumpf-ios] Add the wind arrows layer and move "From wind" onto our backend. The backend is done (whumpf docs/runbook.md §9, "Wind now"); every URL needs ?t= as for every tile. Layer: GET /tiles/wind/meta?region= gives units (mph), direction ("from", degrees true), levels (zoom bands, each with minzoom, maxzoom and max_lvl), attribution, valid, stale and hours (each with time, kind and url). Fetch hours[0].url: gzipped GeoJSON points with dir, mph, gust_mph and lvl. Draw one symbol layer per entry in levels, with that band's minzoom/maxzoom and the filter lvl <= max_lvl, so arrows thin out when zoomed out. Point the arrow downwind (iconRotate dir + 180, rotation aligned to the map), scale or colour it by mph, and show a dot instead where dir is null (calm). A tap on an arrow shows "From W, 22 mph, gusts 34". While the layer is on, show "Wind analysis, 9 pm" in local time from valid, "NOAA/NWS RTMA" and a stale warning. Fetch meta on the same schedule as temperature in weather.ts (every 15 min and on foreground), keep the last file on the phone like temperature, and mark the layer online-only. Write it so later entries in hours (forecast hours, kind "forecast") can drive a time slider without changes. "From wind": in src/data/wind.ts replace both direct api.weather.gov calls with GET /weather/forecast?lat=&lon=&t= at the map centre, through the api client (drop the User-Agent header). Add an hour choice to SlopeFilterSheet (Now, +3 h, +6 h, +12 h, +24 h, +36 h) and use the hours[] entry whose time is the current UTC hour plus that offset. This also fixes a bug: today's code takes windDirection.values[0], the first value NWS lists, which is often hours old. Keep leeAspects exactly as it is (+180°, ±67.5°). The note says e.g. "NWS forecast for 9 am: wind from W (270°), 18 mph, gusts 30. Loaded: NE, E, SE." null wind_dir_deg is "No wind forecast for that hour", 404 is "No NWS forecast here", 429 is "Too many requests, try again in a minute". Offline, show the last answer saved for this region and hour with its age (the plan in wind.ts's header). The arrows (RTMA analysis) and the preset (NWS forecast) can differ by a few degrees; label each with its source. Run the checks in CLAUDE.md, then show me the diff.
```

```text
[whumpf-ios] Add the Cameras layer. The backend is done (whumpf docs/runbook.md §10); every URL needs ?t= as for every tile. GET /cameras?region=colorado&t= gives GeoJSON points with id, name, source (nps, windy), elevation_m, credit, link, updated, image_url and image, plus sources (per source: state, fetched, attribution with text and links). Add a Cameras toggle to the Layers sheet (off by default, online-only, never in offline packs) and draw the pins, clustered when zoomed out. Fetch the list when the layer is turned on and on foreground (it changes at most every few hours). Tapping a pin opens a card: GET the feature's image path (+ &t=) for url, taken and expires, then show the image from url (NPS images come straight from nps.gov, Windy images from Windy; never save or cache image files on the phone), its age from taken in local time ("12 min ago"; "age unknown" when null), the name, elevation in feet, the credit, and an "Open" link to link. The image itself must open link when tapped (Windy requires every image to link to its windy.com page). Re-fetch the image path when the card is reopened after expires, and offer pull-to-refresh. While the layer is on and any Windy pin is showing, show "Webcams provided by windy.com — add a webcam" with both links from sources.windy.attribution; show the NPS credit on NPS cards. Handle 404 (camera gone: drop the pin), 502 ("image unavailable right now") and 429 ("too many requests, try again in a minute"), and say which sources are missing when a state is not ok. This layer must never be behind a paid tier (Windy free-tier terms). iPhone and web together. Run the checks in CLAUDE.md, then show me the diff.
```

```text
[whumpf-ios] Add the Road conditions layer. The backend is done (whumpf docs/runbook.md §11); every URL needs ?t= as for every tile. GET /roads?region=colorado&t= gives: disclaimer (CDOT's required text), attribution (text and links), categories, sources (per feed conditions, incidents, planned: state ok, stale, missing, expired, unavailable, not_configured or not_covered, and fetched), segments (url, revision, fetched; null until the backend has lines), conditions (segment id to name, description, route, category, conditions[] with category, text, source, note, start, end, updated, and forecast with text, start, end) and features (incidents and planned events as GeoJSON MultiPoints with feed, category, type, cdot_category, name, route, direction, severity, message, impacts, lanes, closed_directions, start, end, updated, schedule and active). Fetch segments.url (gzipped GeoJSON LineStrings with id, name, description, route) only when the revision changes and keep it on the phone; join conditions to the lines by id. Add a Road conditions toggle to the Layers sheet (off by default, online-only, never in offline packs). Colour each line by its segment category: closed, chain_law, traction_law and avalanche_control each clearly distinct, other as "see CDOT's report", dry as plain, no_data as grey dashed. Draw incidents and planned events as icons by category (closed, closed_one_direction, crash, construction, other), with upcoming ones (active false) dimmed and active null treated as upcoming. /roads only lists planned events in effect now or starting within 48 h, so say "Planned work: next 48 h" in the layer info. For closed_one_direction, label the event "Closed eastbound" from closed_directions (north, south, east, west to northbound, southbound, eastbound, westbound; join several with "and"), on the icon's tap card and as a short label beside the icon when zoomed in, styled as a closure but distinct from a full closure. Tapping a line opens a card: the name, every entry in conditions with CDOT's text exactly as given (never reworded or summarised), its source and "as of" from updated in local time, then the forecast text. Tapping an event opens a card: "Closed" or "Closed eastbound" first when category is closed or closed_one_direction, then type, CDOT's message verbatim, impacts, lanes, start and end in local time, and "In effect now" or "Starts Tue 9 pm" from active and schedule. Never claim more than CDOT does: no_data is "No report from CDOT", never "open"; there is no "open" state at all; other shows CDOT's text with no icon that implies chain or traction law; treat an unknown category as other, because chain_law, traction_law, avalanche_control and crash will start appearing once the backend maps them. Refetch /roads every 5 min while the layer is on and on foreground. While the layer is on, show "CDOT COtrip, as of 9:05 pm" from sources.conditions.fetched in local time, a stale warning when any state is stale, "Road conditions unavailable" for missing, expired or unavailable (the backend drops expired feeds, so show nothing rather than old data), and hide the layer for not_configured or not_covered. Handle 401 (renew the tile token), 429 ("too many requests, try again in a minute") and 503. The layer info sheet must show the full CDOT disclaimer from disclaimer, word for word, plus the attribution with its link; CDOT's terms require it wherever the data is shown. Add the same full disclaimer, word for word, and the attribution to the credits page as a CDOT entry. Bundle that copy in the app so the credits page works offline, copied exactly from roads.DISCLAIMER in whumpf app/services/roads.py. iPhone and web together. Run the checks in CLAUDE.md, then show me the diff.
```

## Feature plan: accounts in the app

The backend already has nearly every account endpoint. The real work is that every email link opens the old website, so the app needs its own way to finish each flow.

| Step | Backend today | App plan |
| --- | --- | --- |
| Sign up | `POST /auth/register` (30 an hour per IP) | Sign-up screen: email, password, a short safety note |
| Verify email | `/auth/verify-email/request` and `/confirm`; the email links to `whumpf.co/?verify=` | Either a 6-digit code typed in the app (small backend change) or links that open the app (universal links on whumpf.co). Decision below |
| Forgot password | `/auth/password-reset/request` and `/confirm`; same link problem | Same choice as verification |
| Stay signed in | 60-minute token, no refresh | Refresh tokens (Phase 2) |
| Change password or email | No endpoint | Add both |
| Strava connect and disconnect | Callback lands on the website; `DELETE /auth/strava/disconnect` exists | Return to the app after connecting; add a Disconnect button to the You tab |
| Trip and friend invites | Invite email links to `whumpf.co/?trip_invite=` | Open the trip in the app |
| Delete account | `DELETE /auth/me` exists but likely fails for users with routes or trips | Fix and test it first. Then: You tab, Delete account, a screen listing what is deleted, confirm by password |

Apple requires an in-app delete option for any app that lets people create accounts. Email and password sign-in alone does not require Sign in with Apple; adding Google or another social login later would.

**Deleting an account should remove:** routes, trips the user owns (handed to the oldest accepted member, or deleted if there is none), waypoints, shares, friendships, the Strava connection (revoked at Strava first), and the user's email from other people's trip invites.

```text
[whumpf] Write tests for DELETE /auth/me with a user who owns routes, trips, waypoints, share links, friendships, pending invites and a Strava connection. Owned trips pass to the oldest accepted member, or are deleted if there is none. Make them pass. Then add change-password and change-email endpoints with tests.
```

```text
[whumpf] + [whumpf-ios] Replace email links with 6-digit codes for verify-email and password reset (keep the link working for the old website until it is removed). Build the app screens: sign up, enter code, forgot password, change password, delete account, Strava disconnect. iPhone and web.
```

## Also worth planning

The two items that block TestFlight are a privacy policy and a safety disclaimer; the rest can slot into phases as listed. TestFlight is Phase 6, so the TestFlight items are there.

| Item | Why | Phase |
| --- | --- | --- |
| Safety disclaimer and terms | An avalanche terrain tool needs a clear "not a substitute for training, forecasts or judgement" on first run and in the app || 6 |
| Privacy policy | Required for TestFlight and the App Store; must cover location, email and Strava data || 6 |
| Data credits screen | USGS, NOAA, CAIC, CDOT, OpenStreetMap and Strava each expect credit; CDOT's terms require their full disclaimer (`roads.DISCLAIMER`); Strava requires its own "Connect with Strava" button and branding || 6 |
| Crash reporting | The old website used Sentry; the app reports nothing, so friends' crashes would go unseen || 6 |
| Fixes without a rebuild | EAS Update pushes JavaScript-only fixes to testers in minutes || 6 |
| App CI | The backend runs CI on every push; `whumpf-ios` has none. Run the type check and terrain checks on push | 3 |
| Outage notices | The backend has status banners (`/status`, posted with `make banner`, runbook §15); the app never shows them | 4 |
| Server health | Tile cache lives only in memory per worker; add an uptime check (`scripts/backup.sh` now runs daily from cron) | 0 to 1 |
| Place search | The old website searched places in Colorado (Photon); the app cannot | 4 |
| GPX import and export | Lets people share their own tracks without touching Strava data | 7 |
| API types kept in sync | `types.ts` is copied by hand from the backend; generating it from the backend's OpenAPI schema stops drift | 4 |
| Danger-change alerts | A push notification when the CAIC rating changes for a zone in an upcoming trip | 7 |
| Units setting | Feet or meters, °F or °C; `format.ts` already keeps formatting in one place | 8 |

```text
[whumpf-ios] Add a GitHub Actions workflow that runs npx tsc --noEmit and the two terrain check scripts on every push and pull request.
```

```text
[whumpf-ios] Plan the TestFlight basics: first-run safety disclaimer, privacy policy page, a data credits screen (USGS, NOAA, CAIC, CDOT with its full disclaimer, OSM, Strava branding rules), crash reporting, and EAS Update. List what I need to write myself vs what you build.
```

## Backend portability

The backend is close to portable: it runs in containers, reads its settings from `.env`, and stores files through the S3 API, so MinIO can be swapped for AWS S3. The test that proves it: bring the whole stack up on a fresh Linux VM from the repo plus a backup, with no hand edits.

| What ties it to the NAS | Where | Fix |
| --- | --- | --- |
| Elevation files read over plain HTTP from a public MinIO bucket | `routers/tiles.py` `_build_dem_url` (`/vsicurl/`), `compose.yml` (`mc anonymous set download`) | Read through the S3 API with credentials (`/vsis3/`), so the bucket can be private on AWS |
| Health checks hard-code `localhost` | `routers/status.py` | Use the URLs from settings |
| Data folders hard-coded to `/data/pgdata`, `/data/minio`, `/data/martin-tiles` | `compose.yml` | One `DATA_DIR` setting |
| Host networking, with every service URL set to `localhost` | `compose.yml` | Keep it for the NAS; add a bridge-network compose for Docker hosts (service names instead of `localhost`) |
| Images on `:latest` (MinIO, TiTiler); mc is already pinned | `compose.yml` | Pin versions, so a new host gets the same software |
| Elevation processing runs only on the NAS (`/data/dem-work`) | `Makefile`, `data/pipelines/` | Document it as one repeatable command that writes to S3 |
| Tiles rendered live from 1 m elevation files; cache is in memory per worker | `routers/tiles.py` | Pre-build tiles as files (the same PMTiles the offline packs need) and serve them from S3 behind a CDN. Cheaper and faster on any host |
| Cloudflare tunnel and `.env` live only on the VM | Not in the repo | Keep a `.env.example` that lists every setting; document the tunnel or reverse proxy in `docs/runbook.md` |

Backups run daily and go to a local 14-day dump plus any S3-compatible target (Cloudflare R2 today; `scripts/backup.sh`). Restore steps are in docs/external-services.md; a restore from an R2 backup has not been rehearsed (see Later).

**On AWS later:** the simplest move is one EC2 VM running the same compose file, with S3 for files. Managed Postgres (RDS supports PostGIS) and containers on ECS can come after. The biggest cost to watch is serving tiles: rendering them live is heavy on CPU, and downloads count as paid traffic, which is why pre-built tiles behind a CDN matter.

```text
[whumpf] Make the backend portable without changing behaviour: read DEMs via /vsis3/ with credentials (keep /vsicurl/ working for MinIO), use settings URLs in status.py, add a DATA_DIR setting, pin image versions, and add a bridge-network compose override for plain Docker. Test it with a fresh docker-compose up in CI.
```

```text
[whumpf] Write docs/runbook.md "Move to a new host": provision, copy .env, restore the latest backup, run the DEM pipeline or copy COGs to S3, bring up compose, point DNS. Then write a script that tests a restore from backup into a throwaway database.
```

## Tile access

Decided: tile endpoints require a short-lived signed token, and the map still works without an account. Each app install gets its own anonymous token; signing in raises the limits.

1. **Getting a token.** `POST /tiles/token` returns a token signed with the server secret, valid for about an hour. Signed-out installs send a random install id stored on the device; signed-in users send their normal sign-in token.
2. **Using it.** `tiles.ts` adds `?t=<token>` to every tile URL. That one file covers the iPhone map, web, 3D and offline downloads, and the app renews the token before it expires.
3. **Checking it.** The tile endpoints check the signature and expiry with no database lookup, and return 401 before rendering if it fails.
4. **Limits.** Rate limits are per token in Redis, higher for signed-in users. One install or account can be throttled or blocked on its own; a per-IP cap stops anyone minting endless install tokens.
5. **Old website.** It loads tiles with no token. Until it is removed, let tokenless requests through with a strict per-IP limit.

What this does not do: a determined person can pull a token out of the app. It stops random traffic and makes abuse traceable to one token. Apple App Attest can prove requests come from the real app if that is ever needed.

**Cloudflare:** tile URLs have no file extension, and Cloudflare caches by extension by default, so tiles were not cached at first.
- [x] Cache Rule for `https://api.whumpf.co/tiles/*` set to Eligible for cache (done by Oct 14; checked: a repeated slope tile goes `cf-cache-status: MISS` then `HIT`, honouring the API's `Cache-Control: public, max-age=86400`).

Dropping only the `t` parameter from the cache key is listed as an Enterprise option, so on Free or Pro each token gets its own cached copy. That is fine with a few users. At launch, put a Worker in front that checks the token and strips `t`: free up to 100,000 requests a day, then $5 a month for 10 million ([Cache Rules settings](https://developers.cloudflare.com/cache/how-to/cache-rules/settings/), [default cache behavior](https://developers.cloudflare.com/cache/concepts/default-cache-behavior/), [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/)).

```text
[whumpf] + [whumpf-ios] Add signed tile tokens: POST /tiles/token (anonymous install id or signed-in user), HMAC-signed with expiry, checked on every /tiles/* endpoint without a database lookup, rate-limited per token in Redis with higher limits when signed in and a per-IP cap on token creation. Let tokenless requests through with a strict per-IP limit until the old website is removed. In the app, add the token in tiles.ts and renew it before expiry, on iPhone, web and 3D. Tests for expired, forged and missing tokens.
```

## Later

Backend work with no phase yet.

- [ ] **Strava revoke endpoint, before June 1, 2027.** `services/strava.py::deauthorize` calls `POST /oauth/deauthorize` (access token), which Strava says stops working then. Switch to `POST /oauth/revoke`: HTTP Basic auth with client id and secret, `token` (the refresh token revokes its access tokens too), optional `token_type_hint`; 200 with an empty body even for an unknown token, 503 is safe to retry. Used by disconnect and `DELETE /auth/me`. Runbook §14.
- [ ] **Restore rehearsal** from an R2 backup into a throwaway database.
- [ ] **Replace `mc` with `rclone` in backups.** The mc image is pinned and saved to `/data/backups/images/`.
- [ ] **Replace the MinIO server.** The community edition is discontinued and its images are no longer pullable. Options: R2/S3 directly, or Garage. Decide before moving hosts.
- [ ] **`/readyz` shows `"postgis": "0\""`,** so the PostGIS version is read wrong. Fix it.
- [ ] **Phase 5 cleanup:** turn off `TILES_ALLOW_TOKENLESS` and `LEGACY_EMAIL_LINKS` (`STRAVA_RETURN_TO_ALLOW_DEV` stays on for local web testing until before the App Store), then purge `/tiles/*` in Cloudflare.
- [ ] **Phase 5: tile token Worker.** Cloudflare's cache key includes `?t=`, so each token gets its own copy of every tile (checked Oct 15: the same tile with token A then A is MISS then HIT, with token B MISS again). Put a Worker on `/tiles/*` that verifies the token's signature and expiry (same secret as `app/tile_tokens.py`), strips `t`, and caches one copy per tile. A bad or expired token gets a 401 from the Worker. Tokenless requests go to the origin uncached (its strict per-IP limit applies) until `TILES_ALLOW_TOKENLESS` is off, then the Worker rejects them too. Move to the Workers paid plan ($5 a month for 10 million requests) at launch, since the free 100,000 a day is a few hundred map sessions. See Tile access.
- [ ] **Radar rain vs snow:** colour radar frames by precipitation type from MRMS `PrecipFlag` (one small extra file per frame, same 10-minute marks). Runbook §9.
- [ ] **Wind arrows v1.1: forecast hours from NBM.** Add NOAA National Blend of Models 10 m wind (`WDIR`, `WIND`, `GUST` in `blend.tHHz.core.fNNN.co.grib2`, same 2.5 km NDFD grid as RTMA) to the next 48 h as extra entries in `/tiles/wind/meta` `hours` (kind `forecast`), built with the same arrow code as wind now (`to_arrows` in `app/jobs/temperature_refresh.py`). Measured Oct 16: ~4.8 MB per forecast hour in two Range requests (`WDIR`+`GUST` touch, `WIND` is separate), hourly to f036 then every 3 h, so 40 files and ~195 MB per run; every 6 h is ~0.8 GB a day and a few minutes of CPU per run. Also try NBM's `surface - 610 m above ground` wind (about ridge level), which may match wind loading on ridges better than 10 m: either as a second set of arrows or as what "From wind" uses.
- [ ] **Forecast temperature layers:** if wanted later, build them from the NOAA National Blend of Models (NBM) with the same raster layer pipeline as current temperature (`app/services/rasterlayer.py`). The old website's NDFD +3 h / 24 h layers were dropped in Phase 4.
- [ ] **CDOT cameras: tabled, not in the keyed API, no published terms.** The keyed data feeds at `data.cotrip.org` have no camera feed. COtrip's own site uses an open CARS 511 endpoint (`api-511x-co.carsprogram.org/cameras/map-features`, images on `cocam.carsprogram.org`) that has no terms for third parties, and some cameras belong to other agencies. Revisit only with written permission from CDOT (cdot_cotrip@state.co.us): display, credit wording, caching and polling rate. If it comes, add it as a third source in `app/services/cameras.py` (runbook §10).
- [ ] **Road conditions: map chain law, traction law, avalanche control and crashes (after Oct 25, 2026).** `make roads-sample` runs every 15 min for a week from deploy. Then remove its cron line, read `make roads-sample ARGS=--report`, and add only values CDOT actually sent to `roads.CONDITIONS` and `roads.EVENT_TYPES`, each with a test. If the week had no storm, keep sampling. Runbook §11.
- [ ] **1 m slope in the app and packs.** The iPhone app's slope layer asks `/tiles/slope` without `hires`, so offline packs use `slope.tif` at every zoom to match it. If the app moves to `slope_hires.tif` at z13+ (as the web map does), switch the pack `slope` layer too (`pack_cache.LAYERS`, `terrain_tiles.colormap_request`), with a 10 m fallback where hires has no data. Runbook §12.

## Idea backlog

Pull from this list as you go. Wind arrows, recent snowfall, the phone inclinometer, slope above you and time estimates are in Phase 4, and trip briefing and forecast-change alerts in Phase 7; the rest is not scheduled. The ideas marked "on the phone" reuse the terrain math you already have, so they work offline once elevation tiles are saved. Effort is a rough guess.

### Terrain and hazard

| Idea | What the user gets | Data | Effort |
| --- | --- | --- | --- |
| Slope above you | Shades flat ground that sits under 30°+ slopes: runouts and terrain traps a slope map misses | Elevation tiles, on the phone | Medium |
| Sun and shade by time | When a slope gets sun today: corn timing, wet-slide risk, where it stays cold | Elevation tiles + sun position, on the phone | Medium |
| Wind arrows | Forecast wind speed and direction drawn on the map, tied to the lee-aspect filter you already have | NOAA RTMA (now), NBM (forecast, "Later") | Small. Backend for wind now done (Oct 16, runbook §9); app prompt in Phase 4 |
| Recent snowfall | 24 h and 72 h new snow as a layer, plus new snow at each SNOTEL station | NOAA NOHRSC, SNOTEL | Small. Backend done (Oct 15, runbook §8); app prompt in Phase 4 |
| Avalanche paths | Named slide paths overlaid on the map | Check what CAIC or CDOT publish | Unknown |
| ATES ratings | Simple, challenging or complex terrain where ratings exist | Check Colorado coverage | Unknown |
| Phone inclinometer | Lay the phone on the snow to read slope angle | Phone motion sensor | Small |

### Planning

| Idea | What the user gets | Data | Effort |
| --- | --- | --- | --- |
| Time estimates | Moving time for a route by the Munter method, the standard in ski touring | Route distance and gain you already compute | Small |
| Trip briefing | One screen per trip: forecast, avalanche problems, weather, sunrise, sunset, turnaround time, route stats; shareable with the party | Existing trip data | Medium |
| Check-in and check-out | "If I'm not back by 4 pm, alert my partner" | Backend timer + push or email | Medium |
| Watched zones | Favourite zones with today's danger on the home screen, and alerts when it changes | CAIC | Medium |
| Compare routes | Two routes side by side: gain, slope mix, exposure | Existing route data | Small |

### Access and logistics

| Idea | What the user gets | Data | Effort |
| --- | --- | --- | --- |
| Public land and wilderness | Who owns the land; wilderness where you can't snowmobile | PAD-US (public domain) | Small |
| Winter motorized areas | Where snowmobiles are allowed, to avoid conflict | USFS over-snow vehicle maps | Medium |
| Trailheads, huts, pass closures | Parking, 10th Mountain huts, CDOT road and pass status (pass status: Road conditions layer, backend done Oct 18) | OSM, hut listings, CDOT | Medium |
| Cell coverage | Where you will have signal | FCC coverage maps | Medium |

### In the field (UX)

| Idea | What the user gets | Effort |
| --- | --- | --- |
| Field mode | Large glove-friendly buttons, high contrast in sun, a red night mode, battery saver | Medium |
| On-route guidance | Where you are on the route, gain and distance left, bearing to the next waypoint | Medium |
| Photo waypoints | Snap a photo; it's saved at that spot on the trip | Small |
| Lock-screen and watch | Recording stats at a glance; needs small Swift pieces and a Mac | Large |

## Decisions

All ten are settled.

| Question | Decision |
| --- | --- |
| Web cutover timing | iPhone and web are built in parallel, since the screens are nearly identical. The old site is removed when Expo web is ready to replace it |
| Strava imports | Saved routes, owner-only. Sharing, cloning and trip display to others are blocked on the server |
| Claude Code in the backend repo | Edits code and tests; Christiaan reviews and deploys |
| `whumpf-ios` visibility | Going private. Future Claude sessions will need the repo attached with GitHub access |
| Accounts | Everything happens in the app, so friends sign up themselves; no website needed |
| Email verification and password reset | 6-digit codes typed in the app |
| Trip owner deletes their account | The oldest accepted member becomes owner; the trip is deleted only if no one else is on it |
| Observations layer | Shows the last 7 days by default, adjustable |
| Satellite | Free USGS NAIP imagery (public domain), viewable online and downloadable in offline packs. A free winter layer built from Copernicus Sentinel-2 data later |
| Region | Colorado only for now, built so more states can be added without rework |

**Keeping expansion open:** the groundwork exists. The backend already has a regions list (`/regions`) and per-user `allowed_regions`, and the app keeps `REGION` in one file. The rules: no "colorado" outside `config.ts` and the region list; every layer and offline pack is tied to a region id; CAIC is Colorado's avalanche center, so the avalanche layers come from a per-region forecast source (other states use other centers, mostly published through avalanche.org).

## UI/UX polish: renders to make later

When we reach polish, I'll render before-and-after mockups of each screen as a design you can compare and edit. Candidates, in priority order:

| Screen | Today | What to explore |
| --- | --- | --- |
| Map home | Chip, five round buttons, Tools and Record | One-thumb reach, a clear recording state, the offline chip as a real status |
| Tap-a-point sheet | Slope, aspect, elevation, CAIC, SNOTEL in one card | Hazard first at a glance: danger rating and slope band before the numbers |
| Slope and aspect filter | Min/max steppers, aspect toggles | Two-handle slider (already parked), an aspect rose, presets like "under 30°" |
| Layers sheet | Toggle list | Grouping (terrain, hazards, mine), observations layer, a legend per layer |
| Recording | Not built | Live stats, lock-screen glanceability, gloves-on hit targets |
| Route and trip detail | Profile, slope mix, lists | One shareable trip summary; day tabs; Strava routes visibly owner-only |
| Sign-up and onboarding | Sign-in only | First run: location permission, region, what Whumpf is for |
| Web layout | Same screens as the phone | Side panel plus a large map on desktop |
