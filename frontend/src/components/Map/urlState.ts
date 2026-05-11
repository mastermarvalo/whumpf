// Shareable URL state. Encodes the current map viewport + visible layers in
// the address bar so users can copy/paste a link and land their friend on the
// same slope they're looking at.
//
// Schema:  ?lat=<lat>&lng=<lng>&z=<zoom>&l=<csv>&b=<basemap>&u=<units>
//
// On load these win over localStorage; layer opacity / terrain order are still
// per-device preferences (not in the URL).

import type { BasemapId, Units } from "./types";

export interface UrlState {
  center?: [number, number]; // [lng, lat]
  zoom?: number;
  visibleLayers?: string[];
  basemap?: BasemapId;
  units?: Units;
}

const BASEMAPS: readonly BasemapId[] = ["streets", "topo", "satellite", "hybrid"];
const UNITS: readonly Units[] = ["imperial", "metric"];

export function readUrlState(): UrlState {
  const p = new URLSearchParams(window.location.search);
  const lat = parseFloat(p.get("lat") ?? "");
  const lng = parseFloat(p.get("lng") ?? "");
  const zoom = parseFloat(p.get("z") ?? "");
  const layersStr = p.get("l");
  const basemap = p.get("b");
  const units = p.get("u");
  return {
    center: !Number.isNaN(lat) && !Number.isNaN(lng) ? [lng, lat] : undefined,
    zoom: !Number.isNaN(zoom) ? zoom : undefined,
    visibleLayers: layersStr != null ? layersStr.split(",").filter(Boolean) : undefined,
    basemap: BASEMAPS.includes(basemap as BasemapId) ? (basemap as BasemapId) : undefined,
    units: UNITS.includes(units as Units) ? (units as Units) : undefined,
  };
}

/**
 * Patch a subset of URL params via replaceState. Unspecified keys are left
 * untouched so callers can update the viewport without clobbering layer state
 * and vice-versa.
 */
export function writeUrlState(patch: UrlState): void {
  const url = new URL(window.location.href);
  if (patch.center) {
    url.searchParams.set("lat", patch.center[1].toFixed(4));
    url.searchParams.set("lng", patch.center[0].toFixed(4));
  }
  if (patch.zoom != null) {
    url.searchParams.set("z", patch.zoom.toFixed(2));
  }
  if (patch.visibleLayers != null) {
    if (patch.visibleLayers.length === 0) {
      url.searchParams.delete("l");
    } else {
      url.searchParams.set("l", patch.visibleLayers.join(","));
    }
  }
  if (patch.basemap) {
    url.searchParams.set("b", patch.basemap);
  }
  if (patch.units) {
    url.searchParams.set("u", patch.units);
  }
  window.history.replaceState({}, "", url.toString());
}
