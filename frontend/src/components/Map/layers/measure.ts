import maplibregl from "maplibre-gl";
import { apiFetch } from "../../../auth";
import { API_URL } from "../constants";
import type { ProfileResponse } from "../types";

export const MEASURE_MARKER_STYLE =
  "background:#e05a2b;color:#fff;border-radius:50%;width:22px;height:22px;" +
  "display:flex;align-items:center;justify-content:center;font-size:11px;" +
  "font-weight:700;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,0.3);cursor:default;";

export async function fetchProfile(
  a: [number, number],
  b: [number, number],
  regionId: string,
): Promise<ProfileResponse> {
  const p = new URLSearchParams({
    start_lng: String(a[0]),
    start_lat: String(a[1]),
    end_lng: String(b[0]),
    end_lat: String(b[1]),
    region: regionId,
    n: "64",
  });
  const r = await apiFetch(`${API_URL}/terrain/profile?${p}`);
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json() as Promise<ProfileResponse>;
}

export function addMeasureLayers(map: maplibregl.Map) {
  if (map.getSource("measure-line")) return;
  map.addSource("measure-line", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer({
    id: "measure-line",
    type: "line",
    source: "measure-line",
    paint: { "line-color": "#e05a2b", "line-width": 2.5, "line-dasharray": [4, 2] },
  });
}

export function updateMeasureSource(map: maplibregl.Map | null, pts: [number, number][]) {
  if (!map) return;
  const src = map.getSource("measure-line") as maplibregl.GeoJSONSource | undefined;
  if (!src) return;
  src.setData(
    pts.length === 2
      ? { type: "Feature", geometry: { type: "LineString", coordinates: pts }, properties: {} }
      : { type: "FeatureCollection", features: [] },
  );
}
