// Shared types for the Map UI. Extracted from a monolithic Map.tsx so
// subcomponents (LayerPanel, InfoPanel, etc.) can share them without
// circular imports.

export type BasemapId = "streets" | "topo" | "satellite";

export type Units = "imperial" | "metric";

/**
 * Per-region metadata served by /regions. Mirrors backend app.regions.Region.
 * Drives the map's initial bounds/center, the region-lock max bounds, and
 * the mask cutout — all of which used to be hardcoded to Colorado.
 */
export interface Region {
  id: string;
  label: string;
  /** lon_min, lat_min, lon_max, lat_max — content extent for raster sources. */
  bbox: [number, number, number, number];
  /** Padded extent used as MapLibre maxBounds when the region lock is on. */
  max_bounds: [number, number, number, number];
  /** Default fly-to (lon, lat). */
  center: [number, number];
  default_zoom: number;
  /** GeoJSON FeatureCollection: world rectangle with the region as a hole. */
  mask_geojson: unknown;
}

export interface Legend {
  gradient?: string;
  stops?: string[];
  swatches?: Array<{ color: string; label: string }>;
}

export interface ActiveLayer {
  id: string;
  label: string;
  tiles: string[];
  // default "raster"; "geojson" and "vector_overlay" both skip addOverlayLayers
  // and have their sources/layers managed by their own helper module.
  kind?: "raster" | "geojson" | "vector_overlay";
  opacity: number;
  defaultVisible: boolean;
  noSlider?: boolean;
  legend?: Legend;
  // Minimum zoom at which the tile source starts loading (default: 6).
  // Set higher for hires layers so MapLibre skips requests at overview zoom levels.
  sourceMinzoom?: number;
  // Maximum zoom for the tile source (default: 16 or 12 when hiresTiles present).
  // Set lower for coarse ArcGIS services that return blank tiles above their native resolution.
  // MapLibre will overzoom the last valid tile instead of requesting blank ones.
  sourceMaxzoom?: number;
  // Extra MapLibre raster paint properties merged in at layer creation (e.g. saturation, resampling).
  blendPaint?: { "raster-saturation"?: number; "raster-resampling"?: "linear" | "nearest" };
  // 1m hires tile URLs — if set, a companion `${id}-hires` layer is added at minzoom 13.
  hiresTiles?: string[];
  // When true, Map.tsx injects a time parameter into the tile URL as the slider moves.
  // "wms"        → appends &TIME=ISO8601Z (NWS GeoServer WMS)
  // "arcgis"     → appends &time=<ms>,<ms> (ArcGIS MapServer time-aware layers)
  // "rainviewer" → picks nearest RainViewer XYZ frame fetched from their API
  timeFmt?: "wms" | "arcgis" | "rainviewer";
  // Kept for quick boolean checks (true when timeFmt is set).
  timeEnabled?: boolean;
}

export interface UpcomingLayer {
  id: string;
  label: string;
}

export interface LayerGroup {
  id: string;
  label: string;
  color: string;
  active: ActiveLayer[];
  upcoming: UpcomingLayer[];
  reorderable?: boolean;
}

export type ActivityCardProps = {
  id: number;
  name: string;
  sport_type: string;
  color: string;
  distance_m: number;
  elapsed_time_s: number;
  total_elevation_gain_m: number;
  start_date: string;
  photo_url: string | null;
};

export interface ForecastPeriod {
  name: string;
  startTime?: string;
  endTime?: string;
  isDaytime?: boolean;
  temperature: number;
  temperatureUnit: string;
  windSpeed: string;
  windDirection: string;
  shortForecast: string;
  probabilityOfPrecipitation?: { value: number | null };
  precipIn?: number;
}

export interface SpotData {
  periods: ForecastPeriod[];
  tempF: number | null;
  snowDepthIn: number | null;
}

export interface PointData {
  lon: number;
  lat: number;
  loading: boolean;
  elevation?: number;
  slope?: number;
  aspect?: number;
  tempF?: number | null;
  snowDepthIn?: number | null;
  locationName?: string | null;
}

export interface SlopeSample {
  distance_m: number;
  elevation_m: number | null;
  slope_deg: number | null;
  aspect_deg?: number | null;
  aspect?: string | null;
}

export interface ProfileSummary {
  distance_m: number;
  avg_slope_deg: number | null;
  max_slope_deg: number | null;
  min_slope_deg: number | null;
  elevation_gain_m: number | null;
  elevation_loss_m: number | null;
  start_elevation_m: number | null;
  end_elevation_m: number | null;
  min_elevation_m?: number | null;
  max_elevation_m?: number | null;
  slope_distribution?: Record<string, number>;
  aspect_distribution?: Record<string, number>;
}

export interface ProfileResponse {
  summary: ProfileSummary;
  samples: SlopeSample[];
}

// --- Saved routes (Phase A) --------------------------------------------------

export type RouteVisibility = "private" | "shared" | "unlisted";

/** GeoJSON LineString; coordinates are [lng, lat] or [lng, lat, z]. */
export interface RouteGeometry {
  type: "LineString";
  coordinates: number[][];
}

/** List entry from GET /routes — no per-point samples. */
export interface RouteListItem {
  id: number;
  owner_id: number;
  name: string;
  notes: string;
  region: string;
  visibility: RouteVisibility;
  geometry: RouteGeometry;
  summary: ProfileSummary;
  created_at: string;
  updated_at: string;
}

/** Full route from GET /routes/{id} — includes the cached profile samples. */
export interface RouteDetail extends RouteListItem {
  samples: SlopeSample[];
}

export interface RouteCreatePayload {
  name: string;
  region: string;
  geometry: RouteGeometry;
  notes?: string;
}

export interface ShareResponse {
  token: string;
}

// --- Trips, parties, friends, waypoints (Phase C) ----------------------------

export type WaypointKind =
  | "parking" | "trailhead" | "transition" | "decision" | "summit" | "hazard" | "other";

export interface Waypoint {
  id: number;
  trip_id: number;
  geometry: { type: "Point"; coordinates: number[] };
  kind: WaypointKind;
  label: string;
  notes: string;
  created_by_id: number;
}

export type TripMemberStatus = "invited" | "accepted" | "declined";
export type TripMemberRole = "owner" | "member";

export interface TripMemberOut {
  id: number;
  user_id: number | null;
  email: string;
  status: TripMemberStatus;
  role: TripMemberRole;
}

export interface TripListItem {
  id: number;
  owner_id: number;
  name: string;
  date: string;          // ISO start date
  num_days: number;
  region: string;
  caic_zone: string | null;
  created_at: string;
  updated_at: string;
}

export interface TripDay {
  day: number;           // 1-based
  date: string;          // ISO date for that day
  routes: RouteDetail[];
}

export interface TripDetail extends TripListItem {
  notes: string;
  /** Frozen CaicZoneDetail (see layers/caic.ts); cast there for rendering. */
  forecast_snapshot: unknown | null;
  days: TripDay[];
  waypoints: Waypoint[];
  members: TripMemberOut[];
}

export interface TripCreatePayload {
  name: string;
  date: string;
  region: string;
  days: { route_ids: number[] }[];
  notes?: string;
}

export interface Friend {
  friendship_id: number;
  user_id: number;
  email: string;
}

export interface FriendsData {
  friends: Friend[];
  incoming: Friend[];
  outgoing: Friend[];
}
