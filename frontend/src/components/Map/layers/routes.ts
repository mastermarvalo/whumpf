import maplibregl from "maplibre-gl";
import { apiFetch } from "../../../auth";
import { API_URL } from "../constants";
import type {
  RouteCreatePayload,
  RouteDetail,
  RouteGeometry,
  RouteListItem,
  ShareResponse,
} from "../types";

// Saved routes render as a solid colored line; the in-progress builder polyline
// is a separate dashed "draft" layer. Mirrors the strava.ts / measure.ts pattern.
const ROUTE_COLOR = "#7b3fe4";
// A shared route opened via link renders in its own bright color on a dedicated
// layer so it never mixes with the owned-routes source/highlight/list.
const SHARED_ROUTE_COLOR = "#1fb6ff";

export const ROUTE_VERTEX_STYLE =
  "background:#7b3fe4;color:#fff;border-radius:50%;width:18px;height:18px;" +
  "display:flex;align-items:center;justify-content:center;font-size:10px;" +
  "font-weight:700;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,0.3);cursor:default;";

// --- Layers ------------------------------------------------------------------

export function addRouteLayers(map: maplibregl.Map) {
  if (!map.getSource("routes")) {
    map.addSource("routes", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({
      id: "route-lines",
      type: "line",
      source: "routes",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": 3, "line-opacity": 0.85 },
    });
  }
  if (!map.getSource("route-draft")) {
    map.addSource("route-draft", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({
      id: "route-draft-line",
      type: "line",
      source: "route-draft",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ROUTE_COLOR, "line-width": 2.5, "line-dasharray": [3, 2] },
    });
  }
}

export function routesToGeoJSON(routes: RouteListItem[]): object {
  return {
    type: "FeatureCollection",
    features: routes.map((r) => ({
      type: "Feature",
      geometry: r.geometry,
      properties: { id: r.id, name: r.name, color: ROUTE_COLOR },
    })),
  };
}

export function setRouteData(map: maplibregl.Map | null, geojson: object) {
  if (!map) return;
  const src = map.getSource("routes") as maplibregl.GeoJSONSource | undefined;
  src?.setData(geojson as Parameters<typeof src.setData>[0]);
}

export function setRouteVisibility(map: maplibregl.Map | null, visible: boolean) {
  if (!map || !map.getLayer("route-lines")) return;
  map.setLayoutProperty("route-lines", "visibility", visible ? "visible" : "none");
}

export function applyRouteHighlight(map: maplibregl.Map | null, selectedId: number | null) {
  if (!map || !map.getLayer("route-lines")) return;
  if (selectedId == null) {
    map.setPaintProperty("route-lines", "line-opacity", 0.85);
    map.setPaintProperty("route-lines", "line-width", 3);
  } else {
    map.setPaintProperty("route-lines", "line-opacity", [
      "case", ["==", ["get", "id"], selectedId], 1.0, 0.25,
    ]);
    map.setPaintProperty("route-lines", "line-width", [
      "case", ["==", ["get", "id"], selectedId], 5, 2,
    ]);
  }
}

export function updateRouteDraftSource(map: maplibregl.Map | null, pts: [number, number][]) {
  if (!map) return;
  const src = map.getSource("route-draft") as maplibregl.GeoJSONSource | undefined;
  if (!src) return;
  src.setData(
    pts.length >= 2
      ? { type: "Feature", geometry: { type: "LineString", coordinates: pts }, properties: {} }
      : { type: "FeatureCollection", features: [] },
  );
}

// --- Shared-route layer (a route opened via a share link) --------------------

export function addSharedRouteLayer(map: maplibregl.Map) {
  if (map.getSource("shared-route")) return;
  map.addSource("shared-route", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "shared-route-line",
    type: "line",
    source: "shared-route",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": SHARED_ROUTE_COLOR, "line-width": 4, "line-opacity": 0.95 },
  });
}

export function sharedRouteToGeoJSON(detail: RouteDetail): object {
  return { type: "Feature", geometry: detail.geometry, properties: { id: detail.id } };
}

export function setSharedRouteData(map: maplibregl.Map | null, geojson: object | null) {
  if (!map) return;
  const src = map.getSource("shared-route") as maplibregl.GeoJSONSource | undefined;
  src?.setData((geojson ?? { type: "FeatureCollection", features: [] }) as Parameters<typeof src.setData>[0]);
}

// --- API ---------------------------------------------------------------------

export async function fetchRoutes(): Promise<RouteListItem[]> {
  const r = await apiFetch(`${API_URL}/routes`);
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json() as Promise<RouteListItem[]>;
}

export async function fetchRoute(id: number): Promise<RouteDetail> {
  const r = await apiFetch(`${API_URL}/routes/${id}`);
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json() as Promise<RouteDetail>;
}

export async function createRoute(payload: RouteCreatePayload): Promise<RouteDetail> {
  const r = await apiFetch(`${API_URL}/routes`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json() as Promise<RouteDetail>;
}

export async function deleteRoute(id: number): Promise<void> {
  const r = await apiFetch(`${API_URL}/routes/${id}`, { method: "DELETE" });
  if (!r.ok && r.status !== 404) throw new Error(`${r.status}`);
}

export async function importStravaRoute(activityId: number, regionId: string): Promise<RouteDetail> {
  const p = new URLSearchParams({ region: regionId });
  const r = await apiFetch(`${API_URL}/routes/import/strava/${activityId}?${p}`, { method: "POST" });
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json() as Promise<RouteDetail>;
}

export async function updateRoute(id: number, patch: { name?: string; notes?: string }): Promise<RouteDetail> {
  const r = await apiFetch(`${API_URL}/routes/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json() as Promise<RouteDetail>;
}

export async function createShare(id: number): Promise<ShareResponse> {
  const r = await apiFetch(`${API_URL}/routes/${id}/share`, { method: "POST" });
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json() as Promise<ShareResponse>;
}

export async function revokeShare(id: number, token: string): Promise<void> {
  const r = await apiFetch(`${API_URL}/routes/${id}/share/${encodeURIComponent(token)}`, {
    method: "DELETE",
  });
  if (!r.ok && r.status !== 404) throw new Error(`${r.status}`);
}

export async function fetchSharedRoute(id: number, token: string): Promise<RouteDetail> {
  const p = new URLSearchParams({ token });
  const r = await apiFetch(`${API_URL}/routes/${id}?${p}`);
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json() as Promise<RouteDetail>;
}

export async function cloneRoute(id: number, token?: string): Promise<RouteDetail> {
  const qs = token ? `?${new URLSearchParams({ token })}` : "";
  const r = await apiFetch(`${API_URL}/routes/${id}/clone${qs}`, { method: "POST" });
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json() as Promise<RouteDetail>;
}

export function shareUrl(id: number, token: string): string {
  return `${window.location.origin}/?route=${id}&route_token=${encodeURIComponent(token)}`;
}

export function lineStringFrom(pts: [number, number][]): RouteGeometry {
  return { type: "LineString", coordinates: pts.map(([lng, lat]) => [lng, lat]) };
}
