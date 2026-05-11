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

export interface SnotelHistoryRow {
  date: string;
  swe_in: number | null;
  depth_in: number | null;
  swe_pct_normal: number | null;
}

/** Build an inline SVG sparkline for the popup chart placeholder. */
export function buildSnotelSparklineSvg(rows: SnotelHistoryRow[]): string {
  const W = 200, H = 68, PAD = { t: 4, r: 4, b: 18, l: 30 };
  const iW = W - PAD.l - PAD.r;
  const iH = H - PAD.t - PAD.b;

  // SWE line (solid), depth line (dashed)
  const sweVals = rows.map((r) => r.swe_in);
  const depVals = rows.map((r) => r.depth_in);
  const allVals = [...sweVals, ...depVals].filter((v): v is number => v !== null);
  if (allVals.length === 0) return `<div style="color:#666;font-size:10px;margin-top:6px">No history available</div>`;

  const maxV = Math.max(...allVals, 0.1);
  const n = rows.length;

  const xOf = (i: number) => PAD.l + (n <= 1 ? iW / 2 : (i / (n - 1)) * iW);
  const yOf = (v: number) => PAD.t + iH - (v / maxV) * iH;

  function polyline(vals: (number | null)[]) {
    const pts = vals
      .map((v, i) => (v === null ? null : `${xOf(i).toFixed(1)},${yOf(v).toFixed(1)}`))
      .filter(Boolean);
    return pts.join(" ");
  }

  const swePoints = polyline(sweVals);
  const depPoints = polyline(depVals);

  // Y-axis ticks
  const maxLabel = maxV >= 10 ? Math.round(maxV) : maxV.toFixed(1);
  const midV = maxV / 2;
  const midLabel = midV >= 10 ? Math.round(midV) : midV.toFixed(1);

  // X-axis: show first and last date labels
  const firstLabel = rows[0]?.date.slice(5).replace("-", "/") ?? "";
  const lastLabel = rows[n - 1]?.date.slice(5).replace("-", "/") ?? "";

  const svgLines: string[] = [];

  // Grid line at 50%
  const yMid = yOf(midV);
  svgLines.push(`<line x1="${PAD.l}" y1="${yMid.toFixed(1)}" x2="${W - PAD.r}" y2="${yMid.toFixed(1)}" stroke="#444" stroke-width="0.5" stroke-dasharray="2,2"/>`);

  // Depth line (dashed, light)
  if (depPoints) {
    svgLines.push(`<polyline points="${depPoints}" fill="none" stroke="#6090c0" stroke-width="1.2" stroke-dasharray="3,2" stroke-linejoin="round"/>`);
  }
  // SWE line (solid, brighter)
  if (swePoints) {
    svgLines.push(`<polyline points="${swePoints}" fill="none" stroke="#4ab0ff" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>`);
  }

  // Y-axis labels
  svgLines.push(`<text x="${PAD.l - 3}" y="${(PAD.t + 3).toFixed(1)}" text-anchor="end" font-size="7" fill="#777">${maxLabel}"</text>`);
  svgLines.push(`<text x="${PAD.l - 3}" y="${(yMid + 3).toFixed(1)}" text-anchor="end" font-size="7" fill="#666">${midLabel}"</text>`);

  // X-axis labels
  svgLines.push(`<text x="${PAD.l}" y="${H - 3}" font-size="7" fill="#666">${firstLabel}</text>`);
  svgLines.push(`<text x="${W - PAD.r}" y="${H - 3}" text-anchor="end" font-size="7" fill="#666">${lastLabel}</text>`);

  // Legend
  svgLines.push(`<line x1="${PAD.l}" y1="${H - 11}" x2="${PAD.l + 10}" y2="${H - 11}" stroke="#4ab0ff" stroke-width="1.8"/>`);
  svgLines.push(`<text x="${PAD.l + 13}" y="${H - 8}" font-size="7" fill="#aaa">SWE</text>`);
  svgLines.push(`<line x1="${PAD.l + 40}" y1="${H - 11}" x2="${PAD.l + 50}" y2="${H - 11}" stroke="#6090c0" stroke-width="1.2" stroke-dasharray="3,2"/>`);
  svgLines.push(`<text x="${PAD.l + 53}" y="${H - 8}" font-size="7" fill="#aaa">depth</text>`);

  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" style="overflow:visible;margin-top:8px;display:block">
  ${svgLines.join("\n  ")}
</svg>`;
}

function coerceNum(v: unknown): number | null {
  if (v == null || v === "null" || v === "") return null;
  const n = Number(v);
  return isNaN(n) || n === -9999 ? null : n;
}

export function buildSnotelPopupHtml(p: Record<string, unknown>, units: Units, chartId?: string): string {
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

  const chartPlaceholder = chartId
    ? `<div id="${chartId}" style="color:#666;font-size:10px;margin-top:8px;min-height:20px">Loading history…</div>`
    : "";

  return `<div style="font-family:ui-sans-serif,system-ui,sans-serif;font-size:13px;min-width:200px;color:#eee">
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
    ${chartPlaceholder}
  </div>`;
}
