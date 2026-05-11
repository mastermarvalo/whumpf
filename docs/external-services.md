# External services setup

Whumpf integrates with several external providers. The integration code is
already wired up — each one is a no-op until you sign up, paste a credential
into `.env`, and rebuild/restart the relevant container.

This doc walks through them in **order of importance for launch**. Skip down
to whichever one you're tackling.

> **Convention:** every section ends with a **Verify** step. Don't move on
> until that passes — silent misconfigurations show up as "the feature
> mysteriously doesn't work" at the worst possible time (typically: when
> a real user hits it).

## At a glance

| Service          | What it does               | Free tier                 | Priority for launch |
|------------------|----------------------------|---------------------------|----------------------|
| Resend           | Send verify / reset emails | 3k emails/mo, 1 domain    | **Required**         |
| Sentry           | JS error capture           | 5k events/mo              | **Recommended**      |
| Plausible        | Pageview analytics         | 30-day trial, then $9/mo  | Recommended          |
| Backblaze B2     | Off-host backups           | 10 GB free                | **Required**         |
| GitHub Actions   | CI on push                 | 2,000 min/mo (public free)| **Required**         |
| UptimeRobot      | Uptime alerts              | 50 monitors, 5-min checks | Recommended          |
| Strava API       | Activity import            | Free, rate-limited        | Already done         |

Total monthly cost to launch: **$0** (assuming public GitHub repo and you skip
Plausible during the trial). Plan on roughly $9-30/mo once Plausible is paid
and Sentry/B2 outgrow their free tiers, depending on traffic.

---

## 1. Resend — transactional email

**What:** sends the verify-email and password-reset emails.
**Without it:** the `console` mail provider just logs the verify/reset URL to
the api container's stdout. Fine for dev, broken for actual users.

### Sign up
1. Create an account at <https://resend.com>.
2. **Add a domain** — Resend → Domains → Add Domain. Use a subdomain you can
   point DNS at, e.g. `mail.whumpf.co`. Subdomain (not the apex) keeps DNS
   simple and isolates email reputation from anything else on `whumpf.co`.
3. Resend shows you 3 DNS records (SPF TXT, DKIM TXT, MX). Add them on your
   DNS host. Wait for the dashboard to flip the domain to "Verified" (usually
   a minute or two).
4. Resend → API Keys → Create API Key. Scope: "Sending access" for the
   verified domain only. Copy the `re_…` key.

### Configure
Edit `.env`:
```sh
MAIL_PROVIDER=resend
MAIL_FROM=whumpf <no-reply@mail.whumpf.co>   # must match the verified domain
RESEND_API_KEY=re_xxxxxxxxxxxxxxxxxxxxxxxx
```

Restart the api:
```sh
make restart   # or:  docker compose restart api
```

### Verify
Trigger a real send and check the logs + your inbox:
```sh
# request a password reset for yourself
curl -X POST https://api.whumpf.co/auth/password-reset/request \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com"}'

# look for an SMTP success line, NOT [email/console]
make logs-api | grep -i email
```
You should see the email land in your inbox within seconds. If it goes to
spam, your SPF/DKIM/DMARC isn't set up cleanly yet — re-check the DNS
records Resend listed.

### Gotchas
- Resend's free tier allows only one verified domain. If you bring more
  domains, you'll need a paid plan.
- DMARC (`_dmarc.<your-domain>` TXT) is *optional* on Resend but heavily
  recommended for inbox placement: `v=DMARC1; p=none; rua=mailto:dmarc@whumpf.co`.
- `MAIL_FROM` must use the verified domain. `no-reply@whumpf.co` doesn't
  work if you only verified `mail.whumpf.co`.

---

## 2. Sentry — error capture

**What:** captures unhandled JS exceptions and renders them with a stack
trace + breadcrumbs. The frontend `ErrorBoundary` already routes through
`captureError()`, which is a no-op when no DSN is set.
**Without it:** errors show up as `console.error` only — invisible unless
a user happens to share their console with you.

### Sign up
1. <https://sentry.io> → Sign up (free tier).
2. Create a new project → Platform: **React**. Name it `whumpf-frontend`.
3. Copy the DSN — looks like `https://<key>@o<orgid>.ingest.us.sentry.io/<projectid>`.

### Configure
Edit `.env`:
```sh
VITE_SENTRY_DSN=https://abc...@o123456.ingest.us.sentry.io/789012
```

Rebuild the frontend (Vite bakes the DSN at build time):
```sh
make up   # rebuilds + recreates anything that needs it
```

### Verify
Open the app, then in the browser console:
```js
throw new Error("sentry smoke test");
```
Reload, check Sentry dashboard → Issues. The error should appear within
~30s. If not, check the Network tab for `sentry.io` calls — they should be
returning 200.

### Gotchas
- Sentry runs in **error-only mode** (`tracesSampleRate: 0`). To capture
  performance traces, edit `src/observability.ts` and pick a sample rate.
- The DSN is a *public* token — fine to commit to a public repo, even
  though we keep it in `.env` for cleanliness.
- 5k events/mo isn't a lot if you hit a noisy bug. Set up issue alerts in
  Sentry so a flood of duplicates pages you, not silently consumes quota.

---

## 3. Plausible — analytics

**What:** privacy-friendly pageview counter and custom event tracker. No
cookies, no PII, GDPR-friendly. Renders as a single chart you can stare at
to see what's resonating.
**Without it:** zero visibility into who's using the app, when, and what
they're looking at.

### Sign up
1. <https://plausible.io> → 30-day trial (no card required).
2. Add a site: domain `whumpf.co`.

### Configure
Edit `.env`:
```sh
VITE_PLAUSIBLE_DOMAIN=whumpf.co
# Leave VITE_PLAUSIBLE_SRC empty unless self-hosting.
VITE_PLAUSIBLE_SRC=
```

Rebuild the frontend:
```sh
make up
```

### Verify
Open whumpf.co in a browser (incognito to skip any DNT/ad-blocker). Open
the Plausible dashboard — you should appear as one current visitor within
60 seconds.

### Custom events (optional)
`src/observability.ts` exports a `track(event, props)` helper. Call it from
any component to fire a Plausible custom event — e.g. in
`Map.tsx` you could wrap the layer toggle:
```ts
import { track } from "../observability";

onToggle: (id) => {
  if (!visible[id]) track("Layer Enabled", { id });
  setVisible(v => ({ ...v, [id]: !v[id] }));
},
```
The call is a no-op until Plausible loads, so it's safe to add even before
flipping the env var.

### Gotchas
- Free trial ends after 30 days; pricing is $9/mo for 10k pageviews and
  scales from there.
- Self-hosting is free but adds a postgres+clickhouse container pair —
  worth it only if your analytics traffic dwarfs your app traffic.

---

## 4. Backblaze B2 — off-host backups

**What:** off-host destination for `scripts/backup.sh`. Dumps Postgres +
user-uploads (and optionally dem-cogs) on a schedule. **The single most
important thing to set up before launch** — without it, a failed disk on
the NAS wipes the entire user base.

Any S3-compatible target works (AWS S3, Wasabi, Cloudflare R2, MinIO on
another host). Backblaze B2 is recommended for cost: 10 GB free, $6/TB/mo
after, no egress fee to many destinations.

### Sign up
1. <https://www.backblaze.com/cloud-storage> → Sign up.
2. Buckets → Create a Bucket:
   - Name: `whumpf-backups` (must be globally unique; try
     `whumpf-backups-<some-suffix>`)
   - Files: **Private**
   - Default Encryption: enable (Backblaze-managed key is fine)
   - Object Lock: optional (consider for ransomware protection — adds cost)
3. App Keys → Add a New Application Key:
   - Name: `whumpf-backup`
   - Allow access to: the bucket you just created
   - Read/Write capabilities
4. Copy `keyID`, `applicationKey`, and the **S3 Endpoint** shown on the
   bucket detail page (e.g. `https://s3.us-west-002.backblazeb2.com`).

### Configure
Edit `.env`:
```sh
BACKUP_S3_ENDPOINT=https://s3.us-west-002.backblazeb2.com
BACKUP_S3_BUCKET=whumpf-backups-xyz
BACKUP_S3_ACCESS_KEY=<keyID>
BACKUP_S3_SECRET_KEY=<applicationKey>
# Optional: also back up the DEM COGs bucket. Skip unless you're going to
# pay $0.18/GB/mo to store rebuildable derived data.
BACKUP_INCLUDE_COGS=false
```

### Test it
```sh
make backup
```
Output should end with `whumpf backup … complete`. On Backblaze, the
bucket should now contain:
- `postgres/postgres-<timestamp>.sql.gz`
- `user-uploads/...` (mirrored)

### Schedule daily
```sh
crontab -e
```
Add (3am UTC daily):
```
0 3 * * * /home/ronkerflonk/whumpf/scripts/backup.sh >> /var/log/whumpf-backup.log 2>&1
```
The script self-prunes postgres dumps older than 30 days. `user-uploads/`
mirrors are kept indefinitely (only new/changed files are uploaded each
run — cheap incremental).

### Restoring (rehearse this once!)
```sh
# 1. Download the dump
podman run --rm --network host \
    -e ENDPOINT=$BACKUP_S3_ENDPOINT -e KEY=$BACKUP_S3_ACCESS_KEY \
    -e SECRET=$BACKUP_S3_SECRET_KEY -e BUCKET=$BACKUP_S3_BUCKET \
    -v /tmp:/out:Z quay.io/minio/mc:latest sh -c \
    'mc alias set backup "$ENDPOINT" "$KEY" "$SECRET" >/dev/null && \
     mc cp "backup/$BUCKET/postgres/postgres-LATEST.sql.gz" /out/restore.sql.gz'

# 2. Restore into a fresh postgres (or replace existing — destructive!)
gunzip -c /tmp/restore.sql.gz | podman exec -i whumpf-postgis psql -U whumpf -d whumpf
```
**Rehearse this with a throwaway database before you need it for real.**

### Gotchas
- Backblaze's S3-compatible API endpoint is per-region. Use the one
  shown on the bucket detail page, not the generic `b2.backblazeb2.com`.
- The script uses `pg_dump --clean --if-exists` so restoring overwrites
  existing tables. Don't restore into a live database.
- For >100 GB of user-uploads, consider lifecycle rules on the bucket
  (versions older than 90 days → cold storage) to manage cost.

---

## 5. GitHub Actions — CI on push

**What:** runs `ruff` + `pytest` on the backend and `tsc + vite build` on
the frontend every time you push or open a PR. Catches regressions before
they hit prod.

### Setup
Nothing to do — `.github/workflows/ci.yml` is already in the repo. Push
to GitHub and Actions runs automatically. First push: check
`https://github.com/<your-org>/whumpf/actions` to confirm the workflow ran.

### Configure (optional)
- Repo Settings → Branches → add a protection rule for `main` requiring
  the `Backend` and `Frontend` checks to pass before merge.
- Repo Settings → Actions → Workflow permissions: set to **Read** unless
  you wire in deploy steps later.

### Gotchas
- The free tier is 2,000 minutes/mo for private repos, unlimited for
  public ones. CI usage here is small (a couple of minutes per push).
- `pytest` runs against an ephemeral Postgres service container. If you
  add tests that need MinIO, add a `minio` service block to the workflow.

---

## 6. UptimeRobot — uptime alerts

**What:** pings `/readyz` every 5 minutes from outside your network and
emails/SMS you when it stops returning 200.
**Without it:** an outage at 3am goes undetected until a user complains.

### Sign up
1. <https://uptimerobot.com> → Sign up (free).
2. Add New Monitor:
   - Type: HTTP(s)
   - URL: `https://api.whumpf.co/readyz`
   - Interval: 5 minutes (free tier minimum)
   - Notifications: add your email + optional SMS/Slack/Discord webhook

### Verify
Stop the api temporarily to confirm alerts fire:
```sh
docker compose stop api
# Wait 5-10 minutes; UptimeRobot should email "DOWN".
docker compose start api
# Should see "UP" shortly after.
```

### Gotchas
- Free tier is 50 monitors. You'll only need a few: api `/readyz`, the
  frontend `/`, plus maybe martin and titiler for completeness.
- The HTTP check follows redirects but doesn't validate the response
  body. To check `"ready": true`, use a paid plan's "keyword" monitor.

---

## 7. Strava API — already configured

`STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET` in `.env` are already set
from a prior session. Two things to remember when the app's domain
changes:

1. **Authorization Callback Domain** in
   <https://www.strava.com/settings/api> must match `STRAVA_REDIRECT_URI`'s
   host. Currently `api.whumpf.co`.
2. The **client secret in `.env` was visible to a previous Claude
   session** during the security audit. It never left the prod host but
   is worth rotating before launch if you want a clean slate:
   strava.com/settings/api → "Reset Client Secret", update `.env`,
   restart the api.

---

## Pre-launch checklist

When you're getting close to opening signups:

- [ ] Resend domain verified, real email lands in inbox
- [ ] Sentry capturing a synthetic test error
- [ ] First successful `make backup` run, and a one-time **restore rehearsal**
  into a throwaway database
- [ ] Cron entry confirmed running (`grep CRON /var/log/syslog`)
- [ ] UptimeRobot monitors green for at least 24h
- [ ] CI passing on `main`
- [ ] Strava client secret rotated (optional but recommended)
- [ ] Plausible installed and pageviews showing up
