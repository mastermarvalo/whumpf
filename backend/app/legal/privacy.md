# Whumpf Privacy Policy

Last updated: October 23, 2026

Whumpf is a backcountry terrain and trip-planning app for iPhone and the web, run by Christiaan Smith in Colorado, USA ("we", "us"). This policy explains what data Whumpf collects, why, where it goes, how long we keep it and what you can do about it. Questions or requests: {contact_email}.

Whumpf has no ads, no analytics and no tracking. We do not sell your data or share it for advertising.

## The short version

- Your account is your email address and password. Your plans (routes, trips, waypoints, notes) are stored on our server and shown only to you and the people you choose.
- Your location stays on your phone, except when you check a spot: then the point is sent to our server to look up its avalanche zone and to ask the US National Weather Service for a forecast. We don't store those points, and our logs don't record them.
- If you connect Strava, we keep only what's needed to show you your own activities. Imported routes are refreshed or deleted at least every 7 days, and are deleted when you disconnect.
- You can delete your account in the app at any time.

## What stays on your device

The app keeps these on your phone (or in your browser) and they don't leave it: your sign-in (in the iPhone Keychain, or the browser's local storage on the web); a random install id and a short-lived map access token; your map settings, pace settings, inclinometer calibration and whether you've accepted the safety notice; saved copies of forecasts, observations, webcam lists, road conditions and weather data, so they can be shown with no signal; and offline maps you download.

Your location (GPS) is used on the device to show where you are and compute the terrain around you. It is "while using the app" only, and the app does not record or upload a track of where you go. The camera and motion sensor are used live for the inclinometer; no photos are taken or saved.

## What our server stores, and for how long

| What | Why | Kept |
| --- | --- | --- |
| Your email address, a hashed password (never the password itself), sign-up date, whether your email is verified | Your account | Until you delete your account |
| Routes you draw or import: the line, its name, notes and the terrain profile along it | Your plans | Until you delete them or your account |
| Trips: name, dates, region, notes, the routes on each day, waypoints (position, type, label, notes) and a copy of the CAIC forecast from when you planned | Your plans, shared with the trip's members | Until the trip is deleted (see "Deleting your account") |
| Friends: who you asked and who accepted | Sharing with friends | Until either of you removes it, or an account is deleted |
| Trip invitations: the invited email address and its status | Inviting people to a trip | Until the invitation is removed or the trip is deleted |
| Share links you create for a route | Sharing a route | Until you revoke the link or delete the route |
| Offline map requests: the area, the map layers, when you asked | Building the download and giving it to you | Until you delete your account; a pack nobody has asked for in 60 days is removed, and its map files after 30 days |
| Sign-in sessions and 6-digit email codes, each stored only as a hash | Keeping you signed in; confirming your email and changes to your account | Sessions expire after 60 days and codes after 15 minutes; the records are removed when your account is deleted |
| Strava, if you connect it | See "Strava" below | |

People you share with see only what you share: a friend sees a route you shared with them, and trip members see the trip, its routes and its waypoints. Nothing in Whumpf is public, and there is no feed, search or discovery of people or plans.

If someone invites you to a trip or sends you a friend request, we store your email address as part of the invitation and send you an email about it.

## Server logs and rate limits

Like every web service, our server sees your IP address and the address of each request. Our logs record the time, IP address, requested address and response status. They do not record map points you check, your map access token or anything else that ties a request to your account, though the map area you view (map tile numbers) can appear in them. We use logs only to keep the service running and to stop abuse, and keep them for up to 30 days.

To stop abuse we also count requests per IP address, per install and per account. These counters expire within a day.

## Data we send to others on your behalf

Our server fetches public data for you. It sends the other service only what that request needs, and never your email, your account or your IP address:

- **US National Weather Service (api.weather.gov):** the point you checked, rounded to about 100 m, to get its forecast.
- **Colorado Avalanche Information Center (CAIC):** nothing about you. We fetch the whole forecast and work out your zone on our server.
- **NRCS SNOTEL, NOAA (snowfall, snow depth, temperature, wind, radar), USGS (elevation, and topo and imagery for offline maps), Colorado Department of Transportation (road conditions), National Park Service and Windy.com (webcam lists):** nothing about you. Our server fetches their data on a schedule, or for the webcam you open.
- **Strava:** only the requests needed to show you your own activities, refresh imported routes and disconnect. We don't send Strava any other personal data.

## Services that handle data for us

- **Cloudflare** sits in front of our server, so all app traffic passes through it, and stores offline map files and copies of our database backups (Cloudflare R2).
- **Resend** sends our emails (sign-up codes, password resets, security notices, invitations, Strava deletion confirmations). It receives your email address and the message.
- **Sentry** (iPhone app only) receives crash reports: the error, device model, iOS and app version, and recent app events. It does not receive your account, your email, map coordinates or your map access token, and it does not store your IP address.
- **Expo** (iPhone app only) checks for and delivers app updates. It receives the app's version, a random install id and your IP address.
- **Apple** distributes the app. During beta testing, TestFlight sends Apple crash logs and any feedback you choose to send.

The app also loads some things directly, and those services see your IP address and what you request: map tiles and fonts from USGS (basemap.nationalmap.gov) and OpenFreeMap; place search from Photon by komoot (what you type, and the area you're looking at); and webcam pictures from Windy.com and the National Park Service. A link you tap opens the linked site, which sees your visit.

## Where your data is

Our server is in Colorado, USA. Cloudflare, Resend, Sentry and Expo are US companies. If you use Whumpf from outside the United States, your data is transferred to and stored in the United States.

## Backups

We back up the database every day. Backups are kept for 14 days on our server and for 28 days in Cloudflare R2, then deleted. Data you delete is gone from the live service at once, and from our backups within 28 days. We use backups only to recover from a failure.

## Strava

Connecting Strava is optional. When you connect, you sign in on Strava's own site and Strava gives Whumpf read access to your activities. Whumpf never sees your Strava password.

**What we store:** your Strava athlete id, name and profile picture address, the access tokens Strava gives us (encrypted), the permission you granted and when you connected. When you import an activity as a route, we store its track, its name, its Strava id and the terrain profile along it.

**What we show, and to whom:** your activity list and details are fetched from Strava when you look at them and are not stored. Strava data is shown only to you: routes imported from Strava can't be shared, copied by others or shown to other trip members.

**How long:** about every 6 days we check each imported route with Strava again. If the activity still exists it is refreshed; if it was deleted on Strava, or you removed Whumpf's access, the route is deleted. No imported route is kept more than 7 days without being refreshed. An activity you delete on Strava is removed from Whumpf within 48 hours.

**Disconnecting:** disconnect Strava in the app (You → Strava), or remove Whumpf at strava.com/settings/apps. Either way we delete your Strava tokens and every route imported from Strava (copies included), and email you a confirmation. Deleting your Whumpf account does the same. Deleted Strava data leaves our backups within 28 days, so it is gone from every system we control within 30 days.

**No AI or analytics:** we do not use Strava data for artificial intelligence or machine learning of any kind, or for analytics, research or improving our products.

**Strava's own collection:** Strava may monitor and collect usage data about how Whumpf uses the Strava API, and may use it for any business purpose, including improving the Strava API and platform, providing support and making sure developers follow its terms. Strava's handling of your data is governed by the Strava Privacy Policy (strava.com/legal/privacy), which controls if it conflicts with this policy.

## Deleting your account

You can delete your account in the app (You → Delete account, with your password). This deletes at once: your account, email address and password hash; your sign-in sessions and email codes; your routes and their share links; your Strava connection (we also ask Strava to revoke our access) and every imported route; your friendships, trip memberships and invitations sent to your email; your offline map requests (map files that others asked for too stay, but are no longer linked to you); and trips you own that have no other members.

A trip you own that other people have joined passes to the member who joined first, with its notes, its waypoints (including ones you added) and the routes other members put on it, so the rest of the party keeps their plan. Copies of your routes that friends saved to their own accounts stay with them. Emails already sent can't be recalled. Deleted data leaves our backups within 28 days.

## Your rights

Wherever you live, you can ask us to show you, correct, export or delete your data, and we'll do it. Most of this you can do yourself in the app; for anything else, email {contact_email}. We'll answer within 30 days.

If you are in the European Economic Area, the UK or Switzerland, the GDPR and the UK GDPR give you the right to access your data, have it corrected or erased, restrict or object to its processing, receive it in a portable form, and withdraw consent at any time (which doesn't affect what happened before). You can also complain to your local data protection authority.

Our legal bases are: **contract**, to provide the service you signed up for (your account, plans, sharing, offline maps and emails about your account); **consent**, for Strava, which you choose to connect and can disconnect at any time; and **legitimate interests**, for security, logs, rate limits, backups and crash reports, to keep Whumpf working and safe. You can object to these.

## Children

Whumpf is not directed at children. Don't use Whumpf if you are under 13 (under 16 in the EEA and UK). If we learn that a child has given us personal data, we'll delete it.

## Security

Passwords are stored only as salted hashes, and Strava tokens are encrypted. All connections use HTTPS, and access to the server is restricted. No system is perfectly secure; if a breach affects your data, we'll tell you as the law requires.

## Changes

We'll update this page when what we collect or how we use it changes, and change the date at the top. For significant changes we'll tell you in the app or by email before they take effect.

## Contact

Christiaan Smith, Colorado, USA. {contact_email}
