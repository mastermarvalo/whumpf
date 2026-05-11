import maplibregl from "maplibre-gl";
import type { Units } from "../types";

export function addSnotelLayers(map: maplibregl.Map) {
  if (map.getSource("snotel")) return;
  map.addSource("snotel", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "snotel-circles",
    type: "circle",
    source: "snotel",
    paint: {
      "circle-color": ["get", "color"],
      "circle-radius": 9,
      "circle-stroke-width": 1.5,
      "circle-stroke-color": "#fff",
    },
  });
  map.addLayer({
    id: "snotel-names",
    type: "symbol",
    source: "snotel",
    layout: {
      "text-field": ["get", "name"],
      "text-size": 9,
      "text-offset": [0, -1.6],
      "text-anchor": "bottom",
      "text-font": ["Open Sans Regular", "Arial Unicode MS Regular"],
    },
    paint: { "text-color": "#333", "text-halo-color": "#fff", "text-halo-width": 1.5 },
  });
  map.addLayer({
    id: "snotel-labels",
    type: "symbol",
    source: "snotel",
    layout: {
      "text-field": ["get", "label"],
      "text-size": 9,
      "text-offset": [0, 1.6],
      "text-anchor": "top",
      "text-font": ["Open Sans Bold", "Arial Unicode MS Bold"],
    },
    paint: { "text-color": "#333", "text-halo-color": "#fff", "text-halo-width": 1 },
  });
}

export function setSnotelData(map: maplibregl.Map | null, geojson: object) {
  if (!map) return;
  const src = map.getSource("snotel") as maplibregl.GeoJSONSource | undefined;
  src?.setData(geojson as Parameters<typeof src.setData>[0]);
}

export function setSnotelVisibility(map: maplibregl.Map | null, visible: boolean) {
  if (!map) return;
  const v = visible ? "visible" : "none";
  for (const id of ["snotel-circles", "snotel-names", "snotel-labels"]) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", v);
  }
}

function coerceNum(v: unknown): number | null {
  if (v == null || v === "null" || v === "") return null;
  const n = Number(v);
  return isNaN(n) || n === -9999 ? null : n;
}

export function buildSnotelPopupHtml(p: Record<string, unknown>, units: Units): string {
  const imp = units === "imperial";
  const swe   = coerceNum(p.swe_in);
  const depth = coerceNum(p.snow_depth_in);
  const temp  = coerceNum(p.temp_f);
  const pct   = coerceNum(p.swe_pct_normal);
  const elev  = coerceNum(p.elevation_ft);

  const sweStr = swe != null ? (imp ? `${swe.toFixed(1)}"` : `${(swe * 25.4).toFixed(0)} mm`) : "—";
  const depthStr = depth != null ? (imp ? `${depth.toFixed(0)}"` : `${(depth * 2.54).toFixed(0)} cm`) : "—";
  const tempStr = temp != null ? (imp ? `${temp.toFixed(0)}°F` : `${((temp - 32) * 5 / 9).toFixed(1)}°C`) : "—";
  const elevStr = elev != null ? (imp ? `${elev.toFixed(0)} ft` : `${(elev * 0.3048).toFixed(0)} m`) : "—";
  const pctStr = pct != null ? `${pct.toFixed(0)}% of normal` : "% of normal unavailable";
  const color = String(p.color ?? "#888");

  return `<div style="font-family:ui-sans-serif,system-ui,sans-serif;font-size:13px;min-width:180px;color:#eee">
    <div style="font-weight:700;margin-bottom:6px">${p.name ?? "Station"}</div>
    <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
      <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${color};border:1.5px solid rgba(255,255,255,0.2)"></span>
      <span style="color:${color};font-weight:600">${pctStr}</span>
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <tr><td style="color:#888;padding:2px 0">SWE</td><td style="text-align:right;font-weight:600">${sweStr}</td></tr>
      <tr><td style="color:#888;padding:2px 0">Snow depth</td><td style="text-align:right;font-weight:600">${depthStr}</td></tr>
      <tr><td style="color:#888;padding:2px 0">Temperature</td><td style="text-align:right;font-weight:600">${tempStr}</td></tr>
      <tr><td style="color:#888;padding:2px 0">Elevation</td><td style="text-align:right">${elevStr}</td></tr>
    </table>
    <div style="color:#666;font-size:10px;margin-top:6px">Updated ${p.updated ?? "—"}</div>
  </div>`;
}
