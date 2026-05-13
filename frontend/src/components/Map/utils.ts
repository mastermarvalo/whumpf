// Small, shared, pure helpers used by multiple subcomponents.

import type { CSSProperties } from "react";
import { Z } from "./zIndex";
import type { Units } from "./types";

// ── floating panel styles ──────────────────────────────────────────────────────

// Properties shared by every floating panel regardless of layout.
export function panelShared(theme: { panel: string; text: string }): CSSProperties {
  return {
    position: "fixed",
    background: theme.panel,
    fontFamily: "ui-sans-serif, system-ui, sans-serif",
    color: theme.text,
    zIndex: Z.FLOATING_PANEL,
  };
}

// Full mobile layout: full-width sheet anchored above the nav bar.
export function mobilePanelStyle(
  mobileBottom: number | undefined,
  theme: { panel: string; text: string },
  overrides?: CSSProperties,
): CSSProperties {
  return {
    ...panelShared(theme),
    bottom: mobileBottom,
    left: 8,
    right: 8,
    borderRadius: 12,
    fontSize: 13,
    boxShadow: "0 2px 16px rgba(0,0,0,0.28)",
    ...overrides,
  };
}

// ── unit conversion helpers ───────────────────────────────────────────────────

export function fmtTempF(f: number, units: Units): string {
  return units === "imperial"
    ? `${Math.round(f)}°F`
    : `${Math.round((f - 32) * 5 / 9)}°C`;
}

// Converts NWS-style wind speed string ("10 to 15 mph") to the requested units.
export function fmtWindSpeed(speedStr: string, units: Units): string {
  if (units === "imperial") return speedStr;
  return speedStr.replace(/\d+/g, (n) => String(Math.round(Number(n) * 1.60934))).replace("mph", "km/h");
}

export function slopeColor(deg: number): string {
  if (deg < 15) return "#1a9641";
  if (deg < 27) return "#c8a800";
  if (deg < 40) return "#d7191c";
  return "#2b7bb9";
}

export function aspectCompass(deg: number): string {
  return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(deg / 45) % 8];
}

// ── coord search + Photon geocoder helpers ─────────────────────────────────────

export interface PhotonFeature {
  geometry: { coordinates: [number, number] };
  properties: {
    name?: string;
    street?: string;
    city?: string;
    county?: string;
    state?: string;
    type?: string;
    osm_type?: string;
  };
}

export function parseCoords(raw: string): [number, number] | null {
  const parts = raw.trim().split(/[\s,]+/).filter(Boolean);
  if (parts.length !== 2) return null;
  const [a, b] = parts.map(Number);
  if (isNaN(a) || isNaN(b)) return null;
  if (a >= -90 && a <= 90 && b >= -180 && b <= 180) return [a, b];
  return null;
}

export function photonLabel(f: PhotonFeature): string {
  const p = f.properties;
  const parts: string[] = [];
  if (p.name) parts.push(p.name);
  if (p.street && p.street !== p.name) parts.push(p.street);
  if (p.city && p.city !== p.name) parts.push(p.city);
  else if (p.county) parts.push(p.county);
  return parts.join(", ");
}

export function photonSub(f: PhotonFeature): string {
  const p = f.properties;
  const type = p.type ?? p.osm_type ?? "";
  const county = p.county ?? "";
  return [type, county].filter(Boolean).join(" · ");
}
