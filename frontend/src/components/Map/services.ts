// External data fetchers used by the Map click handler.
//
// fetchSpotData → api.weather.gov for spot forecast + grid conditions.
// reverseGeocode → photon.komoot.io for a human-readable place name.

import type { ForecastPeriod, SpotData } from "./types";

// Parse ISO 8601 duration like "PT6H", "P1D", "P1DT12H" → milliseconds.
function parseDurationMs(dur: string): number {
  const m = dur.match(/P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?/);
  if (!m) return 0;
  return ((parseInt(m[1] ?? "0") * 24 + parseInt(m[2] ?? "0")) * 60 + parseInt(m[3] ?? "0")) * 60_000;
}

type PrecipInterval = { start: number; end: number; totalMm: number };

function parsePrecipGrid(values: Array<{ validTime: string; value: number | null }>): PrecipInterval[] {
  return values.flatMap((v) => {
    if (!v.value || v.value <= 0) return [];
    const slash = v.validTime.indexOf("/");
    if (slash < 0) return [];
    const start = new Date(v.validTime.slice(0, slash)).getTime();
    const dur = parseDurationMs(v.validTime.slice(slash + 1));
    if (!dur) return [];
    return [{ start, end: start + dur, totalMm: v.value }];
  });
}

function precipForPeriod(intervals: PrecipInterval[], t0: number, t1: number): number {
  let mm = 0;
  for (const iv of intervals) {
    if (iv.end <= t0 || iv.start >= t1) continue;
    const overlapMs = Math.min(iv.end, t1) - Math.max(iv.start, t0);
    mm += iv.totalMm * (overlapMs / (iv.end - iv.start));
  }
  return mm / 25.4; // → inches
}

// Swap fetchSpotData to change the spot forecast/conditions provider.
// Calls /points/ once, then fans out to forecast + forecastGridData in parallel.
export async function fetchSpotData(lat: number, lng: number): Promise<SpotData> {
  const headers = { "User-Agent": "(whumpf, backcountry-terrain-app)" };
  const meta = await fetch(
    `https://api.weather.gov/points/${lat.toFixed(4)},${lng.toFixed(4)}`,
    { headers },
  ).then((r) => (r.ok ? r.json() : null));
  if (!meta) return { periods: [], tempF: null, snowDepthIn: null };

  const [forecastData, gridData] = await Promise.all([
    meta.properties?.forecast
      ? fetch(meta.properties.forecast, { headers }).then((r) => (r.ok ? r.json() : null))
      : Promise.resolve(null),
    meta.properties?.forecastGridData
      ? fetch(meta.properties.forecastGridData, { headers }).then((r) => (r.ok ? r.json() : null))
      : Promise.resolve(null),
  ]);

  const rawPrecip: Array<{ validTime: string; value: number | null }> =
    gridData?.properties?.quantitativePrecipitation?.values ?? [];
  const precipIntervals = parsePrecipGrid(rawPrecip);

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const periods: ForecastPeriod[] = (forecastData?.properties?.periods ?? []).slice(0, 8).map((p: any) => {
    const t0 = p.startTime ? new Date(p.startTime).getTime() : 0;
    const t1 = p.endTime   ? new Date(p.endTime).getTime()   : 0;
    const pIn = t0 && t1 ? precipForPeriod(precipIntervals, t0, t1) : 0;
    return { ...p, precipIn: pIn >= 0.01 ? pIn : undefined } as ForecastPeriod;
  });

  // gridData values: temperature in °C, snowDepth in metres (wmoUnit:m)
  const tempC: number | null = gridData?.properties?.temperature?.values?.[0]?.value ?? null;
  const snowM: number | null = gridData?.properties?.snowDepth?.values?.[0]?.value ?? null;

  return {
    periods,
    tempF: tempC != null ? tempC * 9 / 5 + 32 : null,
    snowDepthIn: snowM != null ? snowM / 0.0254 : null,
  };
}

/**
 * Wind direction at a location, in degrees ("from" — the meteorological
 * convention). null if NWS doesn't cover the point or grid data is missing.
 *
 * Two-hop: /points/ → meta.properties.forecastGridData → windDirection.values[0].
 */
export async function fetchWindDirection(lat: number, lng: number): Promise<number | null> {
  const headers = { "User-Agent": "(whumpf, backcountry-terrain-app)" };
  const meta = await fetch(
    `https://api.weather.gov/points/${lat.toFixed(4)},${lng.toFixed(4)}`,
    { headers },
  ).then((r) => (r.ok ? r.json() : null)).catch(() => null);
  if (!meta?.properties?.forecastGridData) return null;
  const grid = await fetch(meta.properties.forecastGridData, { headers })
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null);
  const value = grid?.properties?.windDirection?.values?.[0]?.value;
  return typeof value === "number" ? value : null;
}

export async function reverseGeocode(lat: number, lon: number): Promise<string | null> {
  try {
    const r = await fetch(`https://photon.komoot.io/reverse?lat=${lat}&lon=${lon}`);
    if (!r.ok) return null;
    const d = await r.json();
    const p = d.features?.[0]?.properties;
    if (!p) return null;
    const parts: string[] = [];
    if (p.name) parts.push(p.name);
    if (p.street && p.street !== p.name) parts.push(p.street);
    if (p.city) parts.push(p.city);
    else if (p.county) parts.push(p.county);
    if (p.state) parts.push(p.state);
    return parts.length ? parts.join(", ") : null;
  } catch {
    return null;
  }
}
