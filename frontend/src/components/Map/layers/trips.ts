import maplibregl from "maplibre-gl";
import { apiFetch } from "../../../auth";
import { API_URL } from "../constants";
import type {
  FriendsData,
  TripCreatePayload,
  TripDetail,
  TripListItem,
  TripMemberOut,
  Waypoint,
  WaypointKind,
} from "../types";

const TRIP_ROUTE_COLOR = "#ff8c1a";
const WAYPOINT_COLOR = "#ffd24d";

// --- Map layers (a trip's routes + waypoints, shown while viewing a trip) ----

export function addTripLayers(map: maplibregl.Map) {
  if (!map.getSource("trip-routes")) {
    map.addSource("trip-routes", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({
      id: "trip-routes-line",
      type: "line",
      source: "trip-routes",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": TRIP_ROUTE_COLOR, "line-width": 4, "line-opacity": 0.95 },
    });
  }
  if (!map.getSource("trip-waypoints")) {
    map.addSource("trip-waypoints", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({
      id: "trip-waypoints-dot",
      type: "circle",
      source: "trip-waypoints",
      paint: {
        "circle-radius": 6,
        "circle-color": WAYPOINT_COLOR,
        "circle-stroke-width": 2,
        "circle-stroke-color": "#000",
      },
    });
    map.addLayer({
      id: "trip-waypoints-label",
      type: "symbol",
      source: "trip-waypoints",
      layout: {
        "text-field": ["get", "label"],
        "text-size": 11,
        "text-offset": [0, 1.1],
        "text-anchor": "top",
      },
      paint: { "text-color": "#fff", "text-halo-color": "#000", "text-halo-width": 1.2 },
    });
  }
}

export function tripRoutesGeoJSON(detail: TripDetail): object {
  return {
    type: "FeatureCollection",
    features: detail.days.flatMap((d) =>
      d.routes.map((r) => ({
        type: "Feature",
        geometry: r.geometry,
        properties: { id: r.id, name: r.name, day: d.day },
      })),
    ),
  };
}

export function waypointsGeoJSON(waypoints: Waypoint[]): object {
  return {
    type: "FeatureCollection",
    features: waypoints.map((w) => ({
      type: "Feature",
      geometry: w.geometry,
      properties: { id: w.id, label: w.label || w.kind, kind: w.kind },
    })),
  };
}

export function setTripData(map: maplibregl.Map | null, detail: TripDetail | null) {
  if (!map) return;
  const routes = map.getSource("trip-routes") as maplibregl.GeoJSONSource | undefined;
  const wps = map.getSource("trip-waypoints") as maplibregl.GeoJSONSource | undefined;
  const empty = { type: "FeatureCollection", features: [] };
  routes?.setData((detail ? tripRoutesGeoJSON(detail) : empty) as Parameters<typeof routes.setData>[0]);
  wps?.setData((detail ? waypointsGeoJSON(detail.waypoints) : empty) as Parameters<typeof wps.setData>[0]);
}

// --- Trips API ---------------------------------------------------------------

async function _json<T>(r: Response): Promise<T> {
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json() as Promise<T>;
}

export async function fetchTrips(): Promise<TripListItem[]> {
  return _json(await apiFetch(`${API_URL}/trips`));
}

export async function fetchTripInvites(): Promise<TripListItem[]> {
  return _json(await apiFetch(`${API_URL}/trips/invites`));
}

export async function fetchTrip(id: number): Promise<TripDetail> {
  return _json(await apiFetch(`${API_URL}/trips/${id}`));
}

export async function createTrip(payload: TripCreatePayload): Promise<TripDetail> {
  return _json(await apiFetch(`${API_URL}/trips`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }));
}

export async function updateTrip(id: number, patch: { name?: string; date?: string; notes?: string }): Promise<TripDetail> {
  return _json(await apiFetch(`${API_URL}/trips/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  }));
}

export async function deleteTrip(id: number): Promise<void> {
  const r = await apiFetch(`${API_URL}/trips/${id}`, { method: "DELETE" });
  if (!r.ok && r.status !== 404) throw new Error(`${r.status}`);
}

export async function inviteMember(tripId: number, email: string): Promise<TripMemberOut> {
  return _json(await apiFetch(`${API_URL}/trips/${tripId}/members`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  }));
}

export async function respondInvite(tripId: number, action: "accept" | "decline"): Promise<void> {
  const r = await apiFetch(`${API_URL}/trips/${tripId}/members/respond`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
  if (!r.ok) throw new Error(`${r.status}`);
}

export async function addWaypoint(
  tripId: number,
  wp: { lng: number; lat: number; elevation_m?: number; kind: WaypointKind; label?: string; notes?: string },
): Promise<Waypoint> {
  return _json(await apiFetch(`${API_URL}/trips/${tripId}/waypoints`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(wp),
  }));
}

export async function deleteWaypoint(tripId: number, wid: number): Promise<void> {
  const r = await apiFetch(`${API_URL}/trips/${tripId}/waypoints/${wid}`, { method: "DELETE" });
  if (!r.ok && r.status !== 404) throw new Error(`${r.status}`);
}

// --- Friends API -------------------------------------------------------------

export async function fetchFriends(): Promise<FriendsData> {
  return _json(await apiFetch(`${API_URL}/friends`));
}

export async function sendFriendRequest(email: string): Promise<void> {
  const r = await apiFetch(`${API_URL}/friends/request`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detail?.detail ?? `${r.status}`);
  }
}

export async function respondFriendRequest(friendshipId: number, action: "accept" | "decline"): Promise<void> {
  const r = await apiFetch(`${API_URL}/friends/${friendshipId}/respond`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
  if (!r.ok) throw new Error(`${r.status}`);
}

export async function removeFriend(friendshipId: number): Promise<void> {
  const r = await apiFetch(`${API_URL}/friends/${friendshipId}`, { method: "DELETE" });
  if (!r.ok && r.status !== 404) throw new Error(`${r.status}`);
}
