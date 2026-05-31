import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import { apiFetch } from "../auth";
import { useFetchWithRetry } from "../hooks/useFetchWithRetry";
import { showToast } from "./Toast";
import type { StravaStatus, UserSummary } from "../App";

import { API_URL, TITILER_URL } from "./Map/constants";
import { THEMES, MOBILE_NAV_H } from "./Map/theme";
import { Z } from "./Map/zIndex";
import type {
  ActivityCardProps,
  BasemapId,
  ForecastPeriod,
  FriendsData,
  PointData,
  ProfileResponse,
  Region,
  RouteDetail,
  RouteListItem,
  TripDetail,
  TripListItem,
  WaypointKind,
  Units,
} from "./Map/types";
import { fetchSpotData, fetchWindDirection, reverseGeocode } from "./Map/services";

import {
  cogS3,
  getMapStyle,
  getTerrainFilterUrl,
  getTerrainSource,
  swapRasterBasemap,
  type TerrainFilterSettings,
} from "./Map/layers/basemaps";
import {
  HIRES_LAYER_IDS,
  TERRAIN_LAYER_IDS,
  RV_HOST,
  RV_TILE_SUFFIX,
  addOverlayLayers,
  applyTerrainOrder,
  buildLayerGroups,
  buildOverlayLayers,
} from "./Map/layers/overlays";
import {
  addSnotelLayers,
  buildSnotelPopupHtml,
  buildSnotelSparklineSvg,
  setSnotelData,
  setSnotelVisibility,
  type SnotelHistoryRow,
} from "./Map/layers/snotel";
import {
  addStravaLayers,
  applyStravaHighlight,
  setStravaData,
  setStravaVisibility,
} from "./Map/layers/strava";
import {
  addCaicLayers,
  buildCaicDetailHtml,
  setCaicData,
  setCaicVisibility,
} from "./Map/layers/caic";
import {
  addObsLayers,
  buildObsPopupHtml,
  setObsData,
  setObsVisibility,
} from "./Map/layers/obs";
import {
  addStreetsLayers,
  addTrailsLayers,
  setStreetsOpacity,
  setStreetsVisibility,
  setTrailsOpacity,
  setTrailsVisibility,
} from "./Map/layers/osm";
import { readUrlState, writeUrlState } from "./Map/urlState";
import {
  MEASURE_MARKER_STYLE,
  addMeasureLayers,
  fetchProfile,
  updateMeasureSource,
} from "./Map/layers/measure";
import {
  ROUTE_VERTEX_STYLE,
  addRouteLayers,
  addSharedRouteLayer,
  applyRouteHighlight,
  cloneRoute,
  createRoute,
  createShare,
  deleteRoute,
  fetchRoutes,
  fetchSharedRoute,
  importStravaRoute,
  lineStringFrom,
  revokeShare,
  routesToGeoJSON,
  setRouteData,
  setSharedRouteData,
  shareUrl,
  sharedRouteToGeoJSON,
  updateRoute,
  updateRouteDraftSource,
} from "./Map/layers/routes";

import { TimeSlider, NOW_STEP, stepToDate } from "./Map/TimeSlider";
import { LayerPanel } from "./Map/LayerPanel";
import { InfoPanel } from "./Map/InfoPanel";
import { MeasurePanel } from "./Map/MeasurePanel";
import { RouteBuilderPanel } from "./Map/RouteBuilderPanel";
import { SavedRoutesPanel } from "./Map/SavedRoutesPanel";
import { SharedRoutePanel } from "./Map/SharedRoutePanel";
import { TripsPanel } from "./Map/TripsPanel";
import { TripView } from "./Map/TripView";
import {
  addTripLayers,
  addTripRoute,
  applyTripRouteHighlight,
  addWaypoint,
  createTrip,
  deleteTrip,
  deleteWaypoint,
  fetchFriends,
  fetchTrip,
  fetchTripInvites,
  fetchTrips,
  inviteMember,
  removeFriend,
  removeTripRoute,
  respondFriendRequest,
  respondInvite,
  sendFriendRequest,
  setTripData,
} from "./Map/layers/trips";
import { SlopeFilterPanel } from "./Map/SlopeFilterPanel";
import { SearchBar } from "./Map/SearchBar";
import { StravaActivityCard } from "./Map/StravaActivityCard";
import { StartHint } from "./Map/StartHint";
import { ToolboxPanel } from "./Map/ToolboxPanel";
import { MobileSheet } from "./Map/MobileSheet";
import { MobileNav } from "./Map/MobileNav";



function useIsMobile() {
  const [mobile, setMobile] = useState(() => window.innerWidth < 640);
  useEffect(() => {
    const fn = () => setMobile(window.innerWidth < 640);
    window.addEventListener("resize", fn);
    return () => window.removeEventListener("resize", fn);
  }, []);
  return mobile;
}

export function Map({
  user,
  region,
  onLogout,
  stravaStatus,
  onStravaStatusChange,
  onResendVerification,
  onDeleteAccount,
}: {
  user: UserSummary;
  region: Region;
  onLogout: () => void;
  stravaStatus: StravaStatus;
  onStravaStatusChange: () => void;
  onResendVerification: () => void;
  onDeleteAccount: () => void;
}) {
  // Region-derived layer registry. Stable across renders for a fixed region;
  // recomputed when the user picks a different region.
  const layerGroups = useMemo(() => buildLayerGroups(region.id), [region.id]);
  const overlayLayers = useMemo(() => buildOverlayLayers(layerGroups), [layerGroups]);

  // URL state takes precedence over localStorage for the initial render so
  // shared links land on the exact viewport the sharer intended.
  const initialUrlState = useMemo(() => readUrlState(), []);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const searchMarkerRef = useRef<maplibregl.Marker | null>(null);

  const isMobile = useIsMobile();
  const [mobilePanelOpen, setMobilePanelOpen] = useState(false);
  const [mobileToolsOpen, setMobileToolsOpen] = useState(false);

  const [dark, setDark] = useState(true);
  const [basemap, setBasemap] = useState<BasemapId>(() => {
    if (initialUrlState.basemap) return initialUrlState.basemap;
    try {
      const s = localStorage.getItem("whumpf:basemap");
      if (s === "streets" || s === "topo" || s === "satellite") return s;
    } catch { /* ignore */ }
    return "streets";
  });
  const [loadingLayers, setLoadingLayers] = useState<Set<string>>(new Set());
  const [visible, setVisible] = useState<Record<string, boolean>>(() => {
    const defaults = Object.fromEntries(overlayLayers.map((l) => [l.id, l.defaultVisible]));
    // URL wins — and the URL list is authoritative: anything not listed is OFF.
    if (initialUrlState.visibleLayers) {
      const out: Record<string, boolean> = Object.fromEntries(
        overlayLayers.map((l) => [l.id, false]),
      );
      for (const id of initialUrlState.visibleLayers) {
        if (id in out) out[id] = true;
      }
      return out;
    }
    try {
      const stored = localStorage.getItem("whumpf:layer-visible");
      // terrain-filter is owned by slopeFilterMode — never restore it from localStorage.
      return stored ? { ...defaults, ...JSON.parse(stored), "terrain-filter": false } : defaults;
    } catch { return defaults; }
  });
  const [opacity, setOpacity] = useState<Record<string, number>>(() => {
    const defaults = Object.fromEntries(overlayLayers.map((l) => [l.id, l.opacity]));
    try {
      const stored = localStorage.getItem("whumpf:layer-opacity");
      return stored ? { ...defaults, ...JSON.parse(stored) } : defaults;
    } catch { return defaults; }
  });
  const [point, setPoint] = useState<PointData | null>(null);
  const [measureMode, setMeasureMode] = useState(false);
  const [slopeFilterMode, setSlopeFilterMode] = useState(false);
  const slopeFilterModeRef = useRef(false);
  const [terrain3d, setTerrain3d] = useState(false);
  const terrain3dRef = useRef(false);
  const [show3dHint, setShow3dHint] = useState(false);
  const [measurePts, setMeasurePts] = useState<[number, number][]>([]);
  const [profile, setProfile] = useState<ProfileResponse | null>(null);
  const [profileLoading, setProfileLoading] = useState(false);

  // Route builder + saved routes (Phase A).
  const [routeBuilderMode, setRouteBuilderMode] = useState(false);
  const [routeVertices, setRouteVertices] = useState<[number, number][]>([]);
  const [routeSaving, setRouteSaving] = useState(false);
  const [savedRoutesOpen, setSavedRoutesOpen] = useState(false);
  const [savedRoutes, setSavedRoutes] = useState<RouteListItem[]>([]);
  const [savedRoutesLoading, setSavedRoutesLoading] = useState(false);
  const [selectedRouteId, setSelectedRouteId] = useState<number | null>(null);
  const routeBuilderModeRef = useRef(false);
  const routeVerticesRef = useRef<[number, number][]>([]);
  const routeMarkersRef = useRef<maplibregl.Marker[]>([]);
  const savedRoutesRef = useRef<RouteListItem[]>([]);
  const selectedRouteIdRef = useRef<number | null>(null);
  // A route opened via a share link (Phase B) — kept entirely separate from the
  // owned-routes list/layer/highlight; only enters savedRoutes on clone.
  const [sharedRoute, setSharedRoute] = useState<RouteDetail | null>(null);
  const [sharedRouteToken, setSharedRouteToken] = useState<string | null>(null);
  const [cloning, setCloning] = useState(false);
  const sharedRouteRef = useRef<RouteDetail | null>(null);

  // Trips, parties, friends (Phase C).
  const [tripsOpen, setTripsOpen] = useState(false);
  const [trips, setTrips] = useState<TripListItem[]>([]);
  const [tripInvites, setTripInvites] = useState<TripListItem[]>([]);
  const [friends, setFriends] = useState<FriendsData>({ friends: [], incoming: [], outgoing: [] });
  const [tripsLoading, setTripsLoading] = useState(false);
  const [selectedTripId, setSelectedTripId] = useState<number | null>(null);
  const [tripDetail, setTripDetail] = useState<TripDetail | null>(null);
  const [selectedTripRouteId, setSelectedTripRouteId] = useState<number | null>(null);
  const [waypointMode, setWaypointMode] = useState(false);
  const [waypointKind, setWaypointKind] = useState<WaypointKind>("other");
  const tripDetailRef = useRef<TripDetail | null>(null);
  const waypointModeRef = useRef(false);
  const waypointKindRef = useRef<WaypointKind>("other");
  const selectedTripIdRef = useRef<number | null>(null);
  const selectedTripRouteIdRef = useRef<number | null>(null);
  const [units, setUnits] = useState<Units>(() => initialUrlState.units ?? "imperial");
  const [forecast, setForecast] = useState<ForecastPeriod[] | null>(null);
  const [forecastLoading, setForecastLoading] = useState(false);
  const [snotelLoaded, setSnotelLoaded] = useState(false);
  const [caicLoaded, setCaicLoaded] = useState(false);
  const [obsLoaded, setObsLoaded] = useState(false);
  const obsDataRef = useRef<object | null>(null);
  const [aboveHiresZoom, setAboveHiresZoom] = useState(region.default_zoom >= 13);
  const [layerPanelCollapsed, setLayerPanelCollapsed] = useState(false);
  const [mapTimeStep, setMapTimeStep] = useState(NOW_STEP);
  const mapTimeStepRef = useRef(NOW_STEP);
  const [sliderDismissed, setSliderDismissed] = useState(false);
  const caicDataRef = useRef<object | null>(null);
  const [stravaVisible, setStravaVisible] = useState(true);
  const [stravaLoaded, setStravaLoaded] = useState(false);
  const stravaDataRef = useRef<object | null>(null);
  const selectedStravaIdRef = useRef<number | null>(null);
  const [stravaCard, setStravaCard] = useState<{ activities: ActivityCardProps[]; index: number } | null>(null);

  const [terrainFilter, setTerrainFilter] = useState<TerrainFilterSettings>(() => {
    try {
      const stored = localStorage.getItem("whumpf:terrain-filter");
      if (stored) {
        const p = JSON.parse(stored) as Partial<TerrainFilterSettings>;
        const aspects = Array.isArray(p.aspects) && p.aspects.length > 0
          ? p.aspects.filter((a) => typeof a === "string") : ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
        const slopeMin = typeof p.slopeMin === "number" ? p.slopeMin : 30;
        const slopeMax = typeof p.slopeMax === "number" ? p.slopeMax : 45;
        return { aspects, slopeMin, slopeMax };
      }
    } catch { /* ignore */ }
    return { aspects: ["N", "NE", "E", "SE", "S", "SW", "W", "NW"], slopeMin: 30, slopeMax: 45 };
  });
  const terrainFilterRef = useRef(terrainFilter);
  useEffect(() => { terrainFilterRef.current = terrainFilter; }, [terrainFilter]);
  const [mapBearing, setMapBearing] = useState(0);
  const [terrainOrder, setTerrainOrder] = useState<string[]>(() => {
    try {
      const stored = localStorage.getItem("whumpf:terrain-order");
      if (stored) {
        const parsed = JSON.parse(stored) as string[];
        if (TERRAIN_LAYER_IDS.every((id) => parsed.includes(id))) return parsed;
      }
    } catch { /* ignore */ }
    return [...TERRAIN_LAYER_IDS];
  });
  const terrainOrderRef = useRef(terrainOrder);

  // Refs so style-load callbacks can read current state without stale closures.
  const visibleRef = useRef(visible);
  const opacityRef = useRef(opacity);
  useEffect(() => { visibleRef.current = visible; }, [visible]);
  useEffect(() => { opacityRef.current = opacity; }, [opacity]);
  useEffect(() => { terrainOrderRef.current = terrainOrder; }, [terrainOrder]);
  useEffect(() => { mapTimeStepRef.current = mapTimeStep; }, [mapTimeStep]);
  useEffect(() => { slopeFilterModeRef.current = slopeFilterMode; }, [slopeFilterMode]);
  useEffect(() => { terrain3dRef.current = terrain3d; }, [terrain3d]);

  const snotelDataRef = useRef<object | null>(null);
  // RainViewer radar frames — fetched async, used by the time injection effect.
  const rainViewerRef = useRef<{time: number; path: string}[]>([]);
  const prevBasemapRef = useRef<BasemapId>(basemap);
  // Tracks the most-recently *requested* basemap so the permanent style.load handler
  // can update prevBasemapRef to the correct value even after rapid switches.
  const basemapRef = useRef<BasemapId>(basemap);
  const basemapDarkMounted = useRef(false);
  const measureModeRef = useRef(false);
  const measurePtsRef = useRef<[number, number][]>([]);
  const measureMarkersRef = useRef<maplibregl.Marker[]>([]);
  const unitsRef = useRef<Units>("imperial");

  // Initialise map once.
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: getMapStyle(basemap, dark),
      center: initialUrlState.center ?? region.center,
      zoom: initialUrlState.zoom ?? region.default_zoom,
      renderWorldCopies: false,
      maxTileCacheSize: 500,
    });

    map.addControl(new maplibregl.ScaleControl({ unit: "imperial" }), "bottom-left");

    // Single permanent handler for both initial load and every subsequent setStyle call.
    // All addXxx helpers are idempotent (guard on getSource), so re-firing is safe.
    map.on("style.load", () => {
      addOverlayLayers(map, overlayLayers, region.bbox, visibleRef.current, opacityRef.current, {
        "terrain-filter": [getTerrainFilterUrl(region.id, terrainFilterRef.current)],
      });
      // If RainViewer frames were fetched before the style finished loading, apply them now
      // so precip-radar doesn't show the stale fallback URL on first use.
      const rvFrames = rainViewerRef.current;
      if (rvFrames.length > 0) {
        const rvSrc = map.getSource("precip-radar") as maplibregl.RasterTileSource | undefined;
        if (rvSrc) {
          const targetSec = mapTimeStepRef.current === NOW_STEP
            ? Date.now() / 1000
            : stepToDate(mapTimeStepRef.current).getTime() / 1000;
          const nearest = rvFrames.reduce((best, f) =>
            Math.abs(f.time - targetSec) < Math.abs(best.time - targetSec) ? f : best
          );
          rvSrc.setTiles([`${RV_HOST}${nearest.path}${RV_TILE_SUFFIX}`]);
        }
      }
      applyTerrainOrder(map, terrainOrderRef.current);
      // terrain-filter visibility is driven by the Slope Filter tool, not the layer panel.
      if (map.getLayer("terrain-filter"))
        map.setLayoutProperty("terrain-filter", "visibility", slopeFilterModeRef.current ? "visible" : "none");
      // Streets + trails sit above the rasters but below interactive geojson
      // points so SNOTEL/CAIC markers stay clickable. Same insertion anchor
      // as addOverlayLayers — the first symbol layer of the active basemap.
      const osmBeforeId = map.getStyle()?.layers?.find((l) => l.type === "symbol")?.id;
      addStreetsLayers(map, opacityRef.current["streets"] ?? 0.9, osmBeforeId);
      setStreetsVisibility(map, visibleRef.current["streets"] ?? false);
      addTrailsLayers(map, opacityRef.current["trails"] ?? 0.9, osmBeforeId);
      setTrailsVisibility(map, visibleRef.current["trails"] ?? false);
      addMeasureLayers(map);
      updateMeasureSource(map, measurePtsRef.current);
      // Terrain-rgb source: always loaded, never removed — setTerrain() is the only toggle.
      if (!map.getSource("terrain-rgb")) {
        map.addSource("terrain-rgb", getTerrainSource());
      }
      // MapLibre v5: sky is a style-level property, not a layer.
      map.setSky({
        "sky-color": "#87ceeb",
        "horizon-color": "#ffffff",
        "fog-color": "#d0e8ff",
        "fog-ground-blend": 0.5,
        "sky-horizon-blend": 0.8,
      });
      if (terrain3dRef.current) {
        map.setTerrain({ source: "terrain-rgb", exaggeration: 1 });
        map.setMaxPitch(75);
        map.setMaxZoom(17);
      }
      addSnotelLayers(map);
      if (snotelDataRef.current) setSnotelData(map, snotelDataRef.current);
      setSnotelVisibility(map, visibleRef.current["snotel"] ?? false);
      addCaicLayers(map);
      if (caicDataRef.current) setCaicData(map, caicDataRef.current);
      setCaicVisibility(map, visibleRef.current["caic-danger"] ?? false);
      addObsLayers(map);
      if (obsDataRef.current) setObsData(map, obsDataRef.current);
      setObsVisibility(map, visibleRef.current["caic-obs"] ?? false);
      addStravaLayers(map);
      if (stravaDataRef.current) setStravaData(map, stravaDataRef.current);
      applyStravaHighlight(map, selectedStravaIdRef.current);
      addRouteLayers(map);
      setRouteData(map, routesToGeoJSON(savedRoutesRef.current));
      applyRouteHighlight(map, selectedRouteIdRef.current);
      updateRouteDraftSource(map, routeVerticesRef.current);
      addSharedRouteLayer(map);
      if (sharedRouteRef.current) setSharedRouteData(map, sharedRouteToGeoJSON(sharedRouteRef.current));
      addTripLayers(map);
      setTripData(map, tripDetailRef.current);
      applyTripRouteHighlight(map, selectedTripRouteIdRef.current);
      map.on("mouseenter", "trip-routes-line", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "trip-routes-line", () => { if (!measureModeRef.current) map.getCanvas().style.cursor = ""; });
      // Sync prevBasemapRef to whichever basemap was last requested.
      prevBasemapRef.current = basemapRef.current;
    });

    // CAIC danger zone popup — fetch full danger rose + problems from AVID.
    map.on("click", "caic-danger-fill", (e) => {
      if (measureModeRef.current) return;
      e.originalEvent.stopPropagation();
      if (!e.features?.[0]) return;
      const { lat, lng } = e.lngLat;
      const popup = new maplibregl.Popup({ closeButton: true, maxWidth: "320px" })
        .setLngLat(e.lngLat)
        .setHTML(`<div style="font-family:sans-serif;font-size:12px;color:#ccc;padding:4px">Loading…</div>`)
        .addTo(map);
      fetch(`${API_URL}/avalanche/zone_detail?lat=${lat}&lng=${lng}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((detail) => {
          if (!detail) return;
          popup.setHTML(buildCaicDetailHtml(detail));
        })
        .catch(() => { /* leave loading popup */ });
    });
    map.on("mouseenter", "caic-danger-fill", () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", "caic-danger-fill", () => {
      if (!measureModeRef.current) map.getCanvas().style.cursor = "";
    });

    // SNOTEL station popup — look up from the ref to bypass MapLibre's tile serialization.
    // Shared handler: fires on click of circle, name label, or SWE label.
    const openSnotelPopup = (feat: maplibregl.MapGeoJSONFeature, lngLat: maplibregl.LngLat) => {
      // feat.id is used by MapLibre v5+ which promotes properties.id to the feature id
      const triplet = String(feat.properties?.id ?? feat.id ?? "");
      type SnotelFC = { features: Array<{ properties: Record<string, unknown> }> };
      const stored = snotelDataRef.current as SnotelFC | null;
      const refProps = stored?.features.find((f) => String(f.properties.id) === triplet)?.properties;
      const p = refProps ?? (feat.properties as Record<string, unknown>);
      const chartId = `snotel-chart-${triplet.replace(/[^a-z0-9]/gi, "-")}`;
      const popup = new maplibregl.Popup({ closeButton: true, maxWidth: "240px" })
        .setLngLat(lngLat)
        .setHTML(buildSnotelPopupHtml(p, unitsRef.current, chartId))
        .addTo(map);
      // Async: fetch 30-day history and inject sparkline into the placeholder div.
      apiFetch(`${API_URL}/snowpack/stations/history?triplet=${encodeURIComponent(triplet)}&days=30`)
        .then((r) => r.json() as Promise<SnotelHistoryRow[]>)
        .then((rows) => {
          if (!popup.isOpen()) return;
          const el = document.getElementById(chartId);
          if (el) el.outerHTML = buildSnotelSparklineSvg(rows);
        })
        .catch(() => {
          const el = document.getElementById(chartId);
          if (el) el.textContent = "";
        });
    };

    for (const layerId of ["snotel-circles", "snotel-names", "snotel-labels"] as const) {
      map.on("click", layerId, (e) => {
        const feat = e.features?.[0];
        if (!feat) return;
        openSnotelPopup(feat, e.lngLat);
        e.originalEvent.stopPropagation();
      });
      map.on("mouseenter", layerId, () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", layerId, () => {
        if (!measureModeRef.current) map.getCanvas().style.cursor = "";
      });
    }

    // CAIC field observation click — popup with obs details.
    map.on("click", "caic-obs-circles", (e) => {
      if (measureModeRef.current) return;
      e.originalEvent.stopPropagation();
      const feat = e.features?.[0];
      if (!feat) return;
      const p = feat.properties as Record<string, unknown>;
      new maplibregl.Popup({ closeButton: true, maxWidth: "300px" })
        .setLngLat(e.lngLat)
        .setHTML(buildObsPopupHtml(p))
        .addTo(map);
    });
    map.on("mouseenter", "caic-obs-circles", () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", "caic-obs-circles", () => {
      if (!measureModeRef.current) map.getCanvas().style.cursor = "";
    });

    // Strava activity click — open card with nearby runs.
    map.on("click", "strava-lines", (e) => {
      if (measureModeRef.current) return;
      e.originalEvent.stopPropagation();
      const px = e.point;
      const bbox: [maplibregl.PointLike, maplibregl.PointLike] = [
        [px.x - 12, px.y - 12],
        [px.x + 12, px.y + 12],
      ];
      const feats = map.queryRenderedFeatures(bbox, { layers: ["strava-lines"] });
      const seen = new Set<number>();
      const activities: ActivityCardProps[] = [];
      for (const feat of feats) {
        const p = feat.properties as Record<string, unknown>;
        const id = Number(p.id);
        if (!id || seen.has(id)) continue;
        seen.add(id);
        activities.push({
          id,
          name: String(p.name ?? ""),
          sport_type: String(p.sport_type ?? ""),
          color: String(p.color ?? "#95a5a6"),
          distance_m: Number(p.distance_m ?? 0),
          elapsed_time_s: Number(p.elapsed_time_s ?? 0),
          total_elevation_gain_m: Number(p.total_elevation_gain_m ?? 0),
          start_date: String(p.start_date ?? ""),
          photo_url: typeof p.photo_url === "string" ? p.photo_url : null,
        });
      }
      if (activities.length > 0) setStravaCard({ activities, index: 0 });
    });
    map.on("mouseenter", "strava-lines", () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", "strava-lines", () => {
      if (!measureModeRef.current) map.getCanvas().style.cursor = "";
    });

    map.on("click", async (e) => {
      const { lng, lat } = e.lngLat;

      // Don't open InfoPanel when the click landed on a SNOTEL feature — the
      // layer-specific handler already opened the popup.
      const onSnotel = map.queryRenderedFeatures(e.point, {
        layers: ["snotel-circles", "snotel-names", "snotel-labels"],
      });
      if (onSnotel.length > 0) return;

      const onCaic = map.queryRenderedFeatures(e.point, { layers: ["caic-danger-fill"] });
      if (onCaic.length > 0) return;

      const onObs = map.queryRenderedFeatures(e.point, { layers: ["caic-obs-circles"] });
      if (onObs.length > 0) return;

      const onStrava = map.queryRenderedFeatures(e.point, { layers: ["strava-lines"] });
      if (onStrava.length > 0) return;

      // Click a trip route line → highlight it (and deselect if clicking again).
      if (selectedTripIdRef.current != null && !measureModeRef.current) {
        const onTripRoute = map.queryRenderedFeatures(e.point, { layers: ["trip-routes-line"] });
        if (onTripRoute.length > 0) {
          const id = Number(onTripRoute[0].properties?.id);
          if (!Number.isNaN(id)) {
            setSelectedTripRouteId((cur) => (cur === id ? null : id));
          }
          return;
        }
      }

      // Click a saved route line (when not drawing/measuring) → select it and
      // open the saved-routes panel to its stored profile.
      if (!routeBuilderModeRef.current && !measureModeRef.current) {
        const onRoute = map.queryRenderedFeatures(e.point, { layers: ["route-lines"] });
        if (onRoute.length > 0) {
          const id = Number(onRoute[0].properties?.id);
          if (!Number.isNaN(id)) {
            setSelectedRouteId(id);
            setSavedRoutesOpen(true);
          }
          return;
        }
      }

      if (routeBuilderModeRef.current) {
        const newPts: [number, number][] = [...routeVerticesRef.current, [lng, lat]];
        routeVerticesRef.current = newPts;
        setRouteVertices(newPts);
        return;
      }

      if (waypointModeRef.current && selectedTripIdRef.current != null) {
        const tripId = selectedTripIdRef.current;
        const label = window.prompt("Waypoint label (optional)") ?? "";
        addWaypoint(tripId, { lng, lat, kind: waypointKindRef.current, label })
          .then(() => reloadTripDetail(tripId))
          .catch(() => showToast("Couldn't add waypoint.", "error"));
        return;
      }

      if (measureModeRef.current) {
        const pts = measurePtsRef.current;
        const newPts: [number, number][] =
          pts.length < 2 ? [...pts, [lng, lat]] : [[lng, lat]];
        if (newPts.length === 1) setProfile(null);
        measurePtsRef.current = newPts;
        setMeasurePts(newPts);
        return;
      }

      setPoint({ lon: lng, lat, loading: true, locationName: null });
      setForecast(null);
      setForecastLoading(true);

      // Drop a marker at the clicked location (same style as search marker).
      searchMarkerRef.current?.remove();
      searchMarkerRef.current = new maplibregl.Marker({ color: "#4a90d9" })
        .setLngLat([lng, lat])
        .addTo(map);

      const zoom = map.getZoom();
      const pick = async (name: string): Promise<number | undefined> => {
        const doFetch = (fname: string) =>
          fetch(`${TITILER_URL}/cog/point/${lng},${lat}?url=${encodeURIComponent(cogS3(`${region.id}/${fname}`))}`)
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => d?.values?.[0] as number | undefined)
            .catch(() => undefined);
        if (zoom >= 13) {
          const v = await doFetch(`${name}_hires.tif`);
          if (v != null) return v;
        }
        return doFetch(`${name}.tif`);
      };

      const [[elevation, slope, aspect], spotData, locationName] = await Promise.all([
        Promise.all([pick("dem"), pick("slope"), pick("aspect")]),
        fetchSpotData(lat, lng).catch(() => ({ periods: [] as ForecastPeriod[], tempF: null, snowDepthIn: null })),
        reverseGeocode(lat, lng),
      ]);

      setPoint({ lon: lng, lat, loading: false, elevation, slope, aspect, tempF: spotData.tempF, snowDepthIn: spotData.snowDepthIn, locationName });
      setForecast(spotData.periods.length ? spotData.periods : null);
      setForecastLoading(false);
    });

    // Clear all loading spinners once the map is fully idle (all tiles rendered).
    // The useState setter is referentially stable across renders, so calling it
    // directly from this handler — set up exactly once — is safe.
    map.on("idle", () => {
      setLoadingLayers((prev) => (prev.size === 0 ? prev : new Set()));
    });

    // Track whether we're at hires zoom — only re-renders when crossing z13.
    map.on("zoom", () => setAboveHiresZoom(map.getZoom() >= 13));
    map.on("rotate", () => setMapBearing(map.getBearing()));

    // Sync viewport into the URL so the page is always shareable. Debounced
    // because moveend fires on every pan/zoom interaction.
    let urlTimer: ReturnType<typeof setTimeout> | null = null;
    map.on("moveend", () => {
      if (urlTimer) clearTimeout(urlTimer);
      urlTimer = setTimeout(() => {
        const c = map.getCenter();
        writeUrlState({ center: [c.lng, c.lat], zoom: map.getZoom() });
      }, 400);
    });

    // Basemap CDN failure: maplibre fires "error" once per failed tile request,
    // so a broken upstream can emit hundreds of events. Throttle to one toast
    // per 10s and ignore the noise from cancelled requests during basemap swaps.
    let lastTileErrorAt = 0;
    map.on("error", (e: { error?: { status?: number; name?: string; message?: string } }) => {
      const err = e.error;
      if (!err || err.name === "AbortError") return;
      const status = err.status;
      const isAuthOrServer = status === 401 || status === 403 || (status != null && status >= 500);
      if (!isAuthOrServer) return;
      const now = Date.now();
      if (now - lastTileErrorAt < 10_000) return;
      lastTileErrorAt = now;
      showToast(`Basemap tiles returned ${status} — try a different basemap.`, "error");
    });

    mapRef.current = map;
    return () => { map.remove(); mapRef.current = null; };
  }, []);

  // Switch basemap on change. Raster→raster swaps only the basemap source/layer in place so
  // overlays are completely undisturbed. Any transition involving the vector streets style
  // needs a full setStyle; the permanent style.load handler in the init effect re-adds all
  // overlays from refs automatically. Dark mode only affects the streets vector style.
  useEffect(() => {
    // Skip initial mount — the permanent style.load handler fires for the initial load.
    if (!basemapDarkMounted.current) { basemapDarkMounted.current = true; return; }
    const map = mapRef.current;
    if (!map) return;

    basemapRef.current = basemap;
    const prev = prevBasemapRef.current;

    if (prev !== "streets" && basemap !== "streets" && map.isStyleLoaded()) {
      // Raster → raster with the current style fully loaded: swap only the basemap.
      // Guard on isStyleLoaded() so we don't call swapRasterBasemap mid-setStyle.
      swapRasterBasemap(map, prev as "topo" | "satellite", basemap as "topo" | "satellite");
      prevBasemapRef.current = basemap;
      return;
    }

    // Full style swap — the permanent style.load handler re-adds all overlays.
    map.setStyle(getMapStyle(basemap, dark));
  }, [basemap, dark]);

  // Sync visibility state → MapLibre.
  // Don't gate on isStyleLoaded() — it returns false while tiles load (style layers still exist).
  // Layers that don't exist yet (e.g., during a style switch) are skipped; addOverlayLayers
  // adds them back with the correct visibility from visibleRef.current on style.load.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    for (const [id, isVis] of Object.entries(visible)) {
      // terrain-filter visibility is owned by slopeFilterMode, not this state.
      // Letting it through would re-show stale localStorage values on every toggle.
      if (id === "terrain-filter") continue;
      if (map.getLayer(id))
        map.setLayoutProperty(id, "visibility", isVis ? "visible" : "none");
      if (map.getLayer(`${id}-hires`))
        map.setLayoutProperty(`${id}-hires`, "visibility", isVis ? "visible" : "none");
    }
  }, [visible]);

  // Slope filter tool — drives terrain-filter layer visibility independently of
  // the layer panel (which no longer shows terrain-filter as a toggle).
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.getLayer("terrain-filter")) return;
    map.setLayoutProperty("terrain-filter", "visibility", slopeFilterMode ? "visible" : "none");
  }, [slopeFilterMode]);

  // 3D terrain toggle — setTerrain on/off; never removes the source.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.getSource("terrain-rgb")) return;
    if (terrain3d) {
      map.setTerrain({ source: "terrain-rgb", exaggeration: 1 });
      map.setMaxPitch(75);
      map.setMaxZoom(17);
      if (map.getPitch() < 20) map.easeTo({ pitch: 60, duration: 600 });
    } else {
      map.setTerrain(null);
      map.setMaxPitch(85);
      map.setMaxZoom(22);
      map.easeTo({ pitch: 0, duration: 400 });
    }
  }, [terrain3d]);

  // Show keyboard-controls hint whenever 3D is enabled; auto-dismiss after 6s.
  useEffect(() => {
    if (!terrain3d) return;
    setShow3dHint(true);
    const t = setTimeout(() => setShow3dHint(false), 6000);
    return () => clearTimeout(t);
  }, [terrain3d]);

  // 3D camera controls — pitch and bearing via WASD/arrows; north reset via compass rose.
  const adjustPitch = useCallback((delta: number) => {
    const map = mapRef.current;
    if (!map) return;
    map.easeTo({ pitch: Math.max(0, Math.min(75, map.getPitch() + delta)), duration: 250 });
  }, []);
  const adjustBearing = useCallback((delta: number) => {
    const map = mapRef.current;
    if (!map) return;
    map.easeTo({ bearing: map.getBearing() + delta, duration: 250 });
  }, []);

  // Arrow key camera navigation — always active (not gated on terrain3d).
  // Plain: fly. Ctrl or Shift + arrows/WASD: tilt/rotate in 3D mode.
  // Both captured before MapLibre so default pan behaviour is fully replaced.
  const flyCamera = useCallback((fwd: number, right: number) => {
    const map = mapRef.current;
    if (!map) return;
    const speed = 4;
    map.panBy([right * speed, -fwd * speed], { animate: false });
  }, []);

  useEffect(() => {
    const pressed = new Set<string>();
    let raf = 0;
    const ARROW_KEYS = ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"];
    const WASD_KEYS  = ["w", "a", "s", "d"];

    const tick = () => {
      const map = mapRef.current;
      let fwd = 0, right = 0;
      if (pressed.has("ArrowUp")    || pressed.has("w")) fwd   += 1;
      if (pressed.has("ArrowDown")  || pressed.has("s")) fwd   -= 1;
      if (pressed.has("ArrowRight") || pressed.has("d")) right += 1;
      if (pressed.has("ArrowLeft")  || pressed.has("a")) right -= 1;
      if (fwd !== 0 || right !== 0) flyCamera(fwd, right);
      if (map && terrain3d) {
        if (pressed.has("space-up"))   map.jumpTo({ zoom: Math.min(14, map.getZoom() - 0.025) });
        if (pressed.has("space-down")) map.jumpTo({ zoom: Math.max(1,  map.getZoom() + 0.025) });
      }
      raf = requestAnimationFrame(tick);
    };

    const onDown = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable) return;
      const isArrow  = ARROW_KEYS.includes(e.key);
      const isWasd   = WASD_KEYS.includes(e.key.toLowerCase());
      const isSpace  = e.key === " " && terrain3d;
      if (!isArrow && !isWasd && !isSpace) return;
      e.preventDefault();
      e.stopPropagation();
      if (e.shiftKey && terrain3d && !isSpace) {
        const k = isWasd ? e.key.toLowerCase() : e.key;
        if (k === "ArrowUp"    || k === "w") adjustPitch(10);
        if (k === "ArrowDown"  || k === "s") adjustPitch(-10);
        if (k === "ArrowLeft"  || k === "a") adjustBearing(-15);
        if (k === "ArrowRight" || k === "d") adjustBearing(15);
        return;
      }
      const key = isSpace
        ? (e.ctrlKey ? "space-down" : "space-up")
        : (isWasd ? e.key.toLowerCase() : e.key);
      const wasEmpty = pressed.size === 0;
      pressed.add(key);
      if (wasEmpty) raf = requestAnimationFrame(tick);
    };

    const onUp = (e: KeyboardEvent) => {
      pressed.delete(e.key);
      pressed.delete(e.key.toLowerCase());
      if (e.key === " ") { pressed.delete("space-up"); pressed.delete("space-down"); }
      if (pressed.size === 0) { cancelAnimationFrame(raf); raf = 0; }
    };

    window.addEventListener("keydown", onDown, { capture: true });
    window.addEventListener("keyup", onUp, { capture: true });
    return () => {
      window.removeEventListener("keydown", onDown, { capture: true });
      window.removeEventListener("keyup", onUp, { capture: true });
      cancelAnimationFrame(raf);
    };
  }, [terrain3d, flyCamera, adjustBearing, adjustPitch]);

  // Sync opacity state → MapLibre.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    for (const [id, op] of Object.entries(opacity)) {
      if (map.getLayer(id)) map.setPaintProperty(id, "raster-opacity", op);
      if (map.getLayer(`${id}-hires`)) map.setPaintProperty(`${id}-hires`, "raster-opacity", op);
    }
  }, [opacity]);

  useEffect(() => { unitsRef.current = units; }, [units]);

  // Cursor + cleanup when measure mode toggles.
  useEffect(() => {
    measureModeRef.current = measureMode;
    const canvas = mapRef.current?.getCanvas();
    if (canvas) canvas.style.cursor = measureMode ? "crosshair" : "";
    if (!measureMode) {
      measureMarkersRef.current.forEach((m) => m.remove());
      measureMarkersRef.current = [];
      setMeasurePts([]);
      measurePtsRef.current = [];
      setProfile(null);
      updateMeasureSource(mapRef.current, []);
    }
  }, [measureMode]);

  // Markers, line, and profile fetch when measure points change.
  useEffect(() => {
    measurePtsRef.current = measurePts;
    const map = mapRef.current;
    if (!map) return;

    measureMarkersRef.current.forEach((m) => m.remove());
    measureMarkersRef.current = measurePts.map((pt, i) => {
      const el = document.createElement("div");
      el.textContent = i === 0 ? "A" : "B";
      el.style.cssText = MEASURE_MARKER_STYLE;
      return new maplibregl.Marker({ element: el }).setLngLat(pt).addTo(map);
    });

    updateMeasureSource(map, measurePts);

    if (measurePts.length === 2) {
      setProfileLoading(true);
      setProfile(null);
      fetchProfile(measurePts[0], measurePts[1], region.id)
        .then((r) => { setProfile(r); setProfileLoading(false); })
        .catch(() => setProfileLoading(false));
    }
  }, [measurePts]);

  // --- Route builder ---------------------------------------------------------
  // Load the caller's saved routes once on mount.
  const reloadRoutes = useCallback(() => {
    setSavedRoutesLoading(true);
    fetchRoutes()
      .then((rs) => {
        savedRoutesRef.current = rs;
        setSavedRoutes(rs);
        setRouteData(mapRef.current, routesToGeoJSON(rs));
      })
      .catch(() => showToast("Couldn't load saved routes.", "error"))
      .finally(() => setSavedRoutesLoading(false));
  }, []);

  useEffect(() => { reloadRoutes(); }, [reloadRoutes]);

  // Keep the saved-routes source + highlight in sync with state.
  useEffect(() => {
    savedRoutesRef.current = savedRoutes;
    setRouteData(mapRef.current, routesToGeoJSON(savedRoutes));
  }, [savedRoutes]);

  useEffect(() => {
    selectedRouteIdRef.current = selectedRouteId;
    applyRouteHighlight(mapRef.current, selectedRouteId);
    // Fly to the selected route so it's framed on screen.
    const map = mapRef.current;
    if (selectedRouteId == null || !map) return;
    const route = savedRoutesRef.current.find((r) => r.id === selectedRouteId);
    const coords = route?.geometry?.coordinates ?? [];
    if (coords.length < 2) return;
    const bounds = new maplibregl.LngLatBounds();
    coords.forEach((c) => bounds.extend([c[0], c[1]]));
    map.fitBounds(bounds, { padding: 80, maxZoom: 15, duration: 800 });
  }, [selectedRouteId]);

  // Cursor + cleanup when route-builder mode toggles.
  useEffect(() => {
    routeBuilderModeRef.current = routeBuilderMode;
    const canvas = mapRef.current?.getCanvas();
    if (canvas) canvas.style.cursor = routeBuilderMode ? "crosshair" : "";
    if (!routeBuilderMode) {
      routeMarkersRef.current.forEach((m) => m.remove());
      routeMarkersRef.current = [];
      setRouteVertices([]);
      routeVerticesRef.current = [];
      updateRouteDraftSource(mapRef.current, []);
    }
  }, [routeBuilderMode]);

  // Markers + draft line when the drawn vertices change.
  useEffect(() => {
    routeVerticesRef.current = routeVertices;
    const map = mapRef.current;
    if (!map) return;
    routeMarkersRef.current.forEach((m) => m.remove());
    routeMarkersRef.current = routeVertices.map((pt, i) => {
      const el = document.createElement("div");
      el.textContent = String(i + 1);
      el.style.cssText = ROUTE_VERTEX_STYLE;
      return new maplibregl.Marker({ element: el }).setLngLat(pt).addTo(map);
    });
    updateRouteDraftSource(map, routeVertices);
  }, [routeVertices]);

  const handleSaveRoute = useCallback((name: string) => {
    const pts = routeVerticesRef.current;
    if (pts.length < 2 || !name) return;
    setRouteSaving(true);
    createRoute({ name, region: region.id, geometry: lineStringFrom(pts) })
      .then((saved) => {
        setRouteSaving(false);
        setRouteBuilderMode(false);   // clears vertices/markers via the toggle effect
        setSavedRoutes((prev) => [saved, ...prev]);
        setSavedRoutesOpen(true);
        setSelectedRouteId(saved.id);
        showToast(`Saved “${saved.name}”.`, "success");
      })
      .catch(() => {
        setRouteSaving(false);
        showToast("Couldn't save route.", "error");
      });
  }, [region.id]);

  const handleDeleteRoute = useCallback((id: number) => {
    deleteRoute(id)
      .then(() => {
        setSavedRoutes((prev) => prev.filter((r) => r.id !== id));
        setSelectedRouteId((cur) => (cur === id ? null : cur));
      })
      .catch(() => showToast("Couldn't delete route.", "error"));
  }, []);

  const handleRenameRoute = useCallback((id: number, name: string) => {
    updateRoute(id, { name })
      .then((updated) => {
        setSavedRoutes((prev) => prev.map((r) => (r.id === id ? { ...r, name: updated.name } : r)));
      })
      .catch(() => showToast("Couldn't rename route.", "error"));
  }, []);

  const handleShareRoute = useCallback(async (id: number) => {
    try {
      const { token } = await createShare(id);
      await navigator.clipboard.writeText(shareUrl(id, token));
      // Generating a link flips visibility to unlisted server-side; reflect it.
      setSavedRoutes((prev) => prev.map((r) => (r.id === id ? { ...r, visibility: "unlisted" } : r)));
      showToast("Share link copied to clipboard.", "success");
    } catch {
      showToast("Couldn't create a share link.", "error");
    }
  }, []);

  const handleRevokeShare = useCallback(async (id: number) => {
    try {
      // Share is idempotent → fetch the active token, then revoke it.
      const { token } = await createShare(id);
      await revokeShare(id, token);
      setSavedRoutes((prev) => prev.map((r) => (r.id === id ? { ...r, visibility: "private" } : r)));
      showToast("Share link revoked.", "success");
    } catch {
      showToast("Couldn't revoke the share link.", "error");
    }
  }, []);

  // Render the shared route on its own layer + fly to it whenever it changes.
  useEffect(() => {
    sharedRouteRef.current = sharedRoute;
    const map = mapRef.current;
    if (!map) return;
    setSharedRouteData(map, sharedRoute ? sharedRouteToGeoJSON(sharedRoute) : null);
    const coords = sharedRoute?.geometry?.coordinates ?? [];
    if (coords.length < 2) return;
    const bounds = new maplibregl.LngLatBounds();
    coords.forEach((c) => bounds.extend([c[0], c[1]]));
    map.fitBounds(bounds, { padding: 80, maxZoom: 15, duration: 800 });
  }, [sharedRoute]);

  // Resolve a ?route=<id>&route_token=<tok> share link once on mount. The Map
  // only mounts when authed, so this always runs as a logged-in user.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const idStr = params.get("route");
    const token = params.get("route_token");
    if (!idStr || !token) return;
    // Strip the params so a refresh / map move doesn't re-trigger resolution.
    const url = new URL(window.location.href);
    url.searchParams.delete("route");
    url.searchParams.delete("route_token");
    window.history.replaceState({}, "", url.toString());
    const id = Number(idStr);
    if (Number.isNaN(id)) return;
    fetchSharedRoute(id, token)
      .then((detail) => { setSharedRouteToken(token); setSharedRoute(detail); })
      .catch(() => showToast("This shared route link is invalid or was revoked.", "error"));
  }, []);

  const handleCloneShared = useCallback(async () => {
    if (!sharedRoute) return;
    setCloning(true);
    try {
      const clone = await cloneRoute(sharedRoute.id, sharedRouteToken ?? undefined);
      setSavedRoutes((prev) => [clone, ...prev.filter((r) => r.id !== clone.id)]);
      setSharedRoute(null);
      setSharedRouteToken(null);
      setSavedRoutesOpen(true);
      setSelectedRouteId(clone.id);   // flies to it via the selectedRouteId effect
      showToast(`Cloned “${clone.name}” to your routes.`, "success");
    } catch {
      showToast("Couldn't clone this route.", "error");
    } finally {
      setCloning(false);
    }
  }, [sharedRoute, sharedRouteToken]);

  // --- Trips, parties, friends ----------------------------------------------
  const reloadTrips = useCallback(() => {
    setTripsLoading(true);
    fetchTrips().then(setTrips).catch(() => {}).finally(() => setTripsLoading(false));
  }, []);
  const reloadInvites = useCallback(() => {
    fetchTripInvites().then(setTripInvites).catch(() => {});
  }, []);
  const reloadFriends = useCallback(() => {
    fetchFriends().then(setFriends).catch(() => {});
  }, []);
  const reloadTripDetail = useCallback((id: number) => {
    fetchTrip(id).then(setTripDetail).catch(() => {});
  }, []);

  useEffect(() => { reloadTrips(); reloadInvites(); reloadFriends(); }, [reloadTrips, reloadInvites, reloadFriends]);

  // On load: open the trips panel if arriving via an invite link, and notify
  // about any pending invites.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("trip_invite")) {
      const url = new URL(window.location.href);
      url.searchParams.delete("trip_invite");
      window.history.replaceState({}, "", url.toString());
      setTripsOpen(true);
    }
    fetchTripInvites()
      .then((inv) => {
        if (inv.length) {
          showToast(`You have ${inv.length} pending trip invite${inv.length > 1 ? "s" : ""}.`, "info");
        }
      })
      .catch(() => {});
  }, []);

  useEffect(() => { waypointKindRef.current = waypointKind; }, [waypointKind]);
  useEffect(() => { waypointModeRef.current = waypointMode; }, [waypointMode]);

  // Keep trip route highlight ref + map paint in sync with state.
  useEffect(() => {
    selectedTripRouteIdRef.current = selectedTripRouteId;
    applyTripRouteHighlight(mapRef.current, selectedTripRouteId);
  }, [selectedTripRouteId]);

  // Render the selected trip's routes + waypoints; keep ref in sync.
  useEffect(() => {
    tripDetailRef.current = tripDetail;
    setTripData(mapRef.current, tripDetail);
  }, [tripDetail]);

  // Load detail (and fly to it) when a trip is selected; clear otherwise.
  useEffect(() => {
    selectedTripIdRef.current = selectedTripId;
    if (selectedTripId == null) { setTripDetail(null); setWaypointMode(false); setSelectedTripRouteId(null); return; }
    let cancelled = false;
    fetchTrip(selectedTripId).then((d) => {
      if (cancelled) return;
      setTripDetail(d);
      const map = mapRef.current;
      const coords: number[][] = [
        ...d.days.flatMap((day) => day.routes.flatMap((r) => r.geometry.coordinates)),
        ...d.waypoints.map((w) => w.geometry.coordinates),
      ];
      if (map && coords.length >= 1) {
        const b = new maplibregl.LngLatBounds();
        coords.forEach((c) => b.extend([c[0], c[1]]));
        map.fitBounds(b, { padding: 90, maxZoom: 15, duration: 800 });
      }
    }).catch(() => { if (!cancelled) showToast("Couldn't load trip.", "error"); });
    return () => { cancelled = true; };
  }, [selectedTripId]);

  const handleCreateTrip = useCallback((p: { name: string; date: string; days: { route_ids: number[] }[] }) => {
    createTrip({ ...p, region: region.id })
      .then((trip) => {
        reloadTrips();
        setSelectedTripId(trip.id);
        showToast(`Created “${trip.name}”.`, "success");
      })
      .catch(() => showToast("Couldn't create trip.", "error"));
  }, [region.id, reloadTrips]);

  const handleRespondInvite = useCallback((tripId: number, action: "accept" | "decline") => {
    respondInvite(tripId, action)
      .then(() => { reloadTrips(); reloadInvites(); if (action === "accept") setSelectedTripId(tripId); })
      .catch(() => showToast("Couldn't respond to invite.", "error"));
  }, [reloadTrips, reloadInvites]);

  const handleDeleteTrip = useCallback(() => {
    const id = selectedTripIdRef.current;
    if (id == null) return;
    deleteTrip(id)
      .then(() => { setSelectedTripId(null); reloadTrips(); showToast("Trip deleted.", "info"); })
      .catch(() => showToast("Couldn't delete trip.", "error"));
  }, [reloadTrips]);

  const handleInviteMember = useCallback((email: string) => {
    const id = selectedTripIdRef.current;
    if (id == null) return;
    inviteMember(id, email)
      .then(() => { reloadTripDetail(id); showToast(`Invited ${email}.`, "success"); })
      .catch(() => showToast("Couldn't invite — owner only.", "error"));
  }, [reloadTripDetail]);

  const handleDeleteWaypoint = useCallback((wid: number) => {
    const id = selectedTripIdRef.current;
    if (id == null) return;
    deleteWaypoint(id, wid).then(() => reloadTripDetail(id)).catch(() => showToast("Couldn't delete waypoint.", "error"));
  }, [reloadTripDetail]);

  const handleAddTripRoute = useCallback((routeId: number, day: number) => {
    const id = selectedTripIdRef.current;
    if (id == null) return;
    addTripRoute(id, routeId, day)
      .then((d) => setTripDetail(d))
      .catch(() => showToast("Couldn't add route.", "error"));
  }, []);

  const handleRemoveTripRoute = useCallback((tripRouteId: number) => {
    const id = selectedTripIdRef.current;
    if (id == null) return;
    removeTripRoute(id, tripRouteId).then(() => reloadTripDetail(id)).catch(() => showToast("Couldn't remove route.", "error"));
  }, [reloadTripDetail]);

  const handleSendFriendRequest = useCallback((email: string) => {
    sendFriendRequest(email)
      .then(() => { reloadFriends(); showToast(`Friend request sent to ${email}.`, "success"); })
      .catch((e) => showToast(e?.message || "Couldn't send friend request.", "error"));
  }, [reloadFriends]);

  const handleRespondFriend = useCallback((fid: number, action: "accept" | "decline") => {
    respondFriendRequest(fid, action).then(reloadFriends).catch(() => showToast("Couldn't respond.", "error"));
  }, [reloadFriends]);

  const handleRemoveFriend = useCallback((fid: number) => {
    removeFriend(fid).then(reloadFriends).catch(() => showToast("Couldn't remove friend.", "error"));
  }, [reloadFriends]);

  const handleImportStravaRoute = useCallback(async (activityId: number, name: string) => {
    try {
      const saved = await importStravaRoute(activityId, region.id);
      // The endpoint is idempotent: if this activity was already imported it
      // returns the existing route rather than creating a duplicate.
      const alreadyImported = savedRoutesRef.current.some((r) => r.id === saved.id);
      setSavedRoutes((prev) => [saved, ...prev.filter((r) => r.id !== saved.id)]);
      setSavedRoutesOpen(true);
      setSelectedRouteId(saved.id);   // opens its profile + flies to it
      showToast(
        alreadyImported
          ? `“${saved.name}” is already saved — opening it.`
          : `Imported “${saved.name}” as a route.`,
        "success",
      );
    } catch {
      showToast(`Couldn't import “${name}”.`, "error");
    }
  }, [region.id]);

  // Sync visibility immediately; fetch lazily via useFetchWithRetry below.
  const snotelVisible = visible["snotel"];
  const caicVisible   = visible["caic-danger"];
  const obsVisible    = visible["caic-obs"];

  useEffect(() => { setSnotelVisibility(mapRef.current, !!snotelVisible); }, [snotelVisible]);
  useEffect(() => { setCaicVisibility(mapRef.current, !!caicVisible); },     [caicVisible]);
  useEffect(() => { setObsVisibility(mapRef.current, !!obsVisible); },       [obsVisible]);
  // Vector overlays — same pattern as the geojson layers above; the
  // visibility/opacity loops further up don't touch them because they have
  // multiple sub-layer ids (streets-road, trails-path, …) under a single
  // panel id.
  const streetsVisible = visible["streets"];
  const streetsOpacity = opacity["streets"];
  const trailsVisible = visible["trails"];
  const trailsOpacity = opacity["trails"];
  useEffect(() => { setStreetsVisibility(mapRef.current, !!streetsVisible); }, [streetsVisible]);
  useEffect(() => { setStreetsOpacity(mapRef.current, streetsOpacity ?? 0.9); }, [streetsOpacity]);
  useEffect(() => { setTrailsVisibility(mapRef.current, !!trailsVisible); }, [trailsVisible]);
  useEffect(() => { setTrailsOpacity(mapRef.current, trailsOpacity ?? 0.9); }, [trailsOpacity]);

  useFetchWithRetry<object>({
    enabled: !!snotelVisible,
    done: snotelLoaded,
    fetcher: () => apiFetch(`${API_URL}/snowpack/stations`),
    onSuccess: (geojson) => {
      snotelDataRef.current = geojson;
      setSnotelData(mapRef.current, geojson);
      setSnotelLoaded(true);
    },
    onError: () => showToast("SNOTEL data unavailable — try again later.", "error"),
    label: "SNOTEL",
    deps: [snotelVisible, snotelLoaded],
  });

  useFetchWithRetry<object>({
    enabled: !!caicVisible,
    done: caicLoaded,
    fetcher: () => fetch(`${API_URL}/avalanche/forecast`),
    onSuccess: (geojson) => {
      caicDataRef.current = geojson;
      setCaicData(mapRef.current, geojson);
      setCaicLoaded(true);
    },
    onError: () => showToast("CAIC forecast unavailable — try again later.", "error"),
    label: "CAIC",
    deps: [caicVisible, caicLoaded],
  });

  useFetchWithRetry<object>({
    enabled: !!obsVisible,
    done: obsLoaded,
    fetcher: () => fetch(`${API_URL}/avalanche/observations`),
    onSuccess: (geojson) => {
      obsDataRef.current = geojson;
      setObsData(mapRef.current, geojson);
      setObsLoaded(true);
    },
    onError: () => showToast("CAIC observations unavailable — try again later.", "error"),
    label: "CAIC observations",
    deps: [obsVisible, obsLoaded],
  });

  // Highlight selected Strava route; dim all others.
  useEffect(() => {
    const selectedId = stravaCard ? (stravaCard.activities[stravaCard.index]?.id ?? null) : null;
    selectedStravaIdRef.current = selectedId;
    applyStravaHighlight(mapRef.current, selectedId);
  }, [stravaCard]);

  // Fetch Strava activities when connected; sync visibility.
  useEffect(() => {
    setStravaVisibility(mapRef.current, stravaVisible);
  }, [stravaVisible]);

  // Clear cached Strava data when the user disconnects.
  useEffect(() => {
    if (stravaStatus.connected) return;
    stravaDataRef.current = null;
    setStravaData(mapRef.current, { type: "FeatureCollection", features: [] });
    setStravaLoaded(false);
  }, [stravaStatus.connected]);

  useFetchWithRetry<object>({
    enabled: stravaStatus.connected,
    done: stravaLoaded,
    fetcher: () => apiFetch(`${API_URL}/strava/activities`),
    onSuccess: (geojson) => {
      stravaDataRef.current = geojson;
      setStravaData(mapRef.current, geojson);
      setStravaLoaded(true);
    },
    onError: () => showToast("Couldn't load Strava activities.", "error"),
    label: "Strava activities",
    deps: [stravaStatus.connected, stravaLoaded],
  });

  const theme = dark ? THEMES.dark : THEMES.light;

  // Show time slider when any time-enabled layer is visible and user hasn't dismissed it.
  // Re-show automatically when a new time-enabled layer is toggled on after dismissal.
  const anyTimeEnabledVisible = overlayLayers.some(
    (l) => l.timeEnabled && (visible[l.id] ?? false),
  );
  useEffect(() => {
    if (anyTimeEnabledVisible) setSliderDismissed(false);
  }, [anyTimeEnabledVisible]);

  // Persist layer selections and basemap to localStorage, debounced.
  // Without the debounce, dragging the opacity slider triggered ~30 writes/sec.
  useEffect(() => {
    const t = setTimeout(() => {
      try {
        localStorage.setItem("whumpf:basemap", basemap);
        // Exclude terrain-filter: its visibility is owned by slopeFilterMode, not visible state.
        const { "terrain-filter": _tf, ...persistVisible } = visible;
        localStorage.setItem("whumpf:layer-visible", JSON.stringify(persistVisible));
        localStorage.setItem("whumpf:layer-opacity", JSON.stringify(opacity));
        localStorage.setItem("whumpf:terrain-order", JSON.stringify(terrainOrder));
        localStorage.setItem("whumpf:terrain-filter", JSON.stringify(terrainFilter));
      } catch {
        // quota / private mode — best-effort, no point alerting the user
      }
    }, 250);
    return () => clearTimeout(t);
  }, [basemap, visible, opacity, terrainOrder, terrainFilter]);

  // Sync the layer/basemap/units pieces of URL state. Viewport is handled by
  // the map's moveend listener; these effects cover everything else.
  useEffect(() => {
    const ids = Object.entries(visible).filter(([, v]) => v).map(([id]) => id);
    writeUrlState({ visibleLayers: ids });
  }, [visible]);
  useEffect(() => { writeUrlState({ basemap }); }, [basemap]);
  useEffect(() => { writeUrlState({ units }); }, [units]);

  // When the terrain-filter settings change, replace the MapLibre source with
  // a new tile URL. Raster sources don't support in-place URL updates so we
  // remove + re-add.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.getLayer("terrain-filter")) return;
    const url = getTerrainFilterUrl(region.id, terrainFilter);
    const isVis = visibleRef.current["terrain-filter"] ?? false;
    const op = opacityRef.current["terrain-filter"] ?? 0.6;
    const beforeId: string | undefined =
      map.getStyle()?.layers?.find((l) => l.type === "symbol")?.id;
    map.removeLayer("terrain-filter");
    map.removeSource("terrain-filter");
    map.addSource("terrain-filter", {
      type: "raster",
      tiles: [url],
      tileSize: 256,
      bounds: region.bbox,
      minzoom: 9,
      maxzoom: 16,
      attribution: "USGS 3DEP",
    });
    map.addLayer({
      id: "terrain-filter",
      type: "raster",
      source: "terrain-filter",
      paint: { "raster-opacity": op, "raster-fade-duration": 400 },
      layout: { visibility: isVis ? "visible" : "none" },
    }, beforeId);
    applyTerrainOrder(map, terrainOrderRef.current);
  }, [terrainFilter, region.id, region.bbox]);

  // Fetch RainViewer radar frames (~2h past, up to 3h nowcast) and keep them
  // fresh. The ref is read by the time injection effect; no state update needed.
  useEffect(() => {
    const RV_API = "https://api.rainviewer.com/public/weather-maps.json";

    async function fetchFrames() {
      try {
        const r = await fetch(RV_API);
        if (!r.ok) return;
        const d = await r.json();
        const past = (d.radar?.past ?? []) as {time: number; path: string}[];
        const nowcast = (d.radar?.nowcast ?? []) as {time: number; path: string}[];
        rainViewerRef.current = [...past, ...nowcast];

        // Immediately update the source with the latest frame if map is ready.
        const map = mapRef.current;
        if (!map || !map.isStyleLoaded() || past.length === 0) return;
        const src = map.getSource("precip-radar") as maplibregl.RasterTileSource | undefined;
        if (src) {
          const latest = past[past.length - 1];
          src.setTiles([`${RV_HOST}${latest.path}${RV_TILE_SUFFIX}`]);
        }
      } catch { /* network failure — keep existing tiles */ }
    }

    fetchFrames();
    const id = setInterval(fetchFrames, 5 * 60 * 1000);
    return () => clearInterval(id);
  }, []);

  // Wind preset — fetches NDFD wind at the current map center and flips the
  // aspect filter to the leeward quadrant (where snow loads). Wind direction
  // is "from" (meteorological); leeward = +180°.
  async function applyWindPreset() {
    const map = mapRef.current;
    if (!map) return;
    const c = map.getCenter();
    const fromDeg = await fetchWindDirection(c.lat, c.lng);
    if (fromDeg == null) {
      showToast("No NDFD wind forecast for this location.", "error");
      return;
    }
    const leeward = (fromDeg + 180) % 360;
    // Pick all aspect buckets within ±67.5° of the leeward direction — that's
    // 3 contiguous buckets, the realistic "loaded zone" for slabs.
    const ASPECT_CENTERS_ARR: Array<{ name: string; deg: number }> = [
      { name: "N", deg: 0 }, { name: "NE", deg: 45 }, { name: "E", deg: 90 },
      { name: "SE", deg: 135 }, { name: "S", deg: 180 }, { name: "SW", deg: 225 },
      { name: "W", deg: 270 }, { name: "NW", deg: 315 },
    ];
    const loadedAspects = ASPECT_CENTERS_ARR.filter(({ deg }) => {
      const diff = Math.min(Math.abs(deg - leeward), 360 - Math.abs(deg - leeward));
      return diff <= 67.5;
    }).map((a) => a.name);
    setTerrainFilter((f) => ({ ...f, aspects: loadedAspects }));
    const cardinal = ASPECT_CENTERS_ARR.reduce((best, a) => {
      const diff = Math.min(Math.abs(a.deg - fromDeg), 360 - Math.abs(a.deg - fromDeg));
      return diff < best.diff ? { name: a.name, diff } : best;
    }, { name: "?", diff: Infinity }).name;
    showToast(`Wind from ${cardinal} (${Math.round(fromDeg)}°) — loaded on ${loadedAspects.join(", ")}.`, "success");
  }

  // When the time slider moves, update tile URLs on time-enabled weather layers.
  // setTiles() swaps URLs in-place so MapLibre crossfades rather than blanking.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    const mapTime = mapTimeStep === NOW_STEP ? null : stepToDate(mapTimeStep);

    for (const layer of overlayLayers) {
      if (!layer.timeEnabled || !layer.timeFmt) continue;
      const src = map.getSource(layer.id) as maplibregl.RasterTileSource | undefined;
      if (!src) continue;

      if (layer.timeFmt === "rainviewer") {
        const frames = rainViewerRef.current;
        if (frames.length === 0) continue; // not yet fetched — leave existing tiles
        const targetSec = mapTime === null ? Date.now() / 1000 : mapTime.getTime() / 1000;
        const nearest = frames.reduce((best, f) =>
          Math.abs(f.time - targetSec) < Math.abs(best.time - targetSec) ? f : best
        );
        src.setTiles([`${RV_HOST}${nearest.path}${RV_TILE_SUFFIX}`]);
        continue;
      }

      // WMS TIME injection (kept for any future WMS time-aware layers)
      const tiles = mapTime === null
        ? layer.tiles
        : layer.tiles.map((t) => `${t}&TIME=${mapTime.toISOString().slice(0, 19)}Z`);
      src.setTiles(tiles);
    }
  }, [mapTimeStep, overlayLayers]);

  // Reorder terrain layers in MapLibre whenever the sidebar order changes.
  // Guard with isStyleLoaded(): on initial mount the effect fires before the async style fetch
  // completes, at which point getStyle() returns null and applyTerrainOrder would throw.
  // The correct initial order is applied by applyTerrainOrder inside the style.load handler.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    applyTerrainOrder(map, terrainOrder);
  }, [terrainOrder]);

  // Subscribe the start-hint to first-interaction events. Stable identity so
  // StartHint's effect doesn't re-bind on every parent render.
  const bindStartHintDismiss = useCallback((dismiss: () => void) => {
    const map = mapRef.current;
    if (!map) return () => {};
    map.once("zoomstart", dismiss);
    map.once("dragstart", dismiss);
    map.once("click", dismiss);
    return () => {
      map.off("zoomstart", dismiss);
      map.off("dragstart", dismiss);
      map.off("click", dismiss);
    };
  }, []);

  function flyToCoords(lat: number, lon: number) {
    const map = mapRef.current;
    if (!map) return;
    map.flyTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), 13) });
    searchMarkerRef.current?.remove();
    searchMarkerRef.current = new maplibregl.Marker({ color: theme.accent })
      .setLngLat([lon, lat])
      .addTo(map);
  }

  const [locating, setLocating] = useState(false);

  function locateMe() {
    if (!navigator.geolocation) {
      showToast("Geolocation isn't supported by this browser.", "error");
      return;
    }
    setLocating(true);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setLocating(false);
        const map = mapRef.current;
        if (!map) return;
        const { latitude, longitude } = pos.coords;
        const [w, s, e, n] = region.bbox;
        const inside = longitude >= w && longitude <= e && latitude >= s && latitude <= n;
        if (!inside) {
          showToast(`You're outside ${region.label}, but here you are.`, "info");
        }
        flyToCoords(latitude, longitude);
      },
      (err) => {
        setLocating(false);
        const msg =
          err.code === err.PERMISSION_DENIED ? "Location permission denied"
          : err.code === err.TIMEOUT          ? "Location request timed out"
          : "Couldn't get your location";
        showToast(msg, "error");
      },
      { enableHighAccuracy: true, timeout: 8000, maximumAge: 60_000 },
    );
  }

  const layerPanelProps = {
    groups: layerGroups,
    visible,
    opacity,
    dark,
    basemap,
    units,
    theme,
    onToggle: (id: string) => {
      if (!visible[id]) setLoadingLayers(prev => new Set([...prev, id]));
      setVisible(v => ({ ...v, [id]: !v[id] }));
    },
    onOpacity: (id: string, val: number) => setOpacity((o) => ({ ...o, [id]: val })),
    onDarkToggle: () => setDark(d => !d),
    onBasemapChange: setBasemap,
    onUnitsToggle: () => setUnits((u) => (u === "imperial" ? "metric" : "imperial")),
    onLogout,
    stravaStatus,
    stravaVisible,
    onStravaToggle: () => setStravaVisible((v) => !v),
    onStravaConnect: async () => {
      const r = await apiFetch(`${API_URL}/auth/strava/authorize`);
      if (r.ok) { const { url } = await r.json(); window.location.href = url; }
    },
    onStravaDisconnect: async () => {
      await apiFetch(`${API_URL}/auth/strava/disconnect`, { method: "DELETE" });
      setStravaLoaded(false);
      onStravaStatusChange();
    },
    collapsed: layerPanelCollapsed,
    onCollapsedChange: setLayerPanelCollapsed,
    loadingLayers,
    layerOrder: { terrain: terrainOrder },
    onLayerReorder: (_groupId: string, newOrder: string[]) => setTerrainOrder(newOrder),
    terrainFilter,
    onTerrainFilterChange: setTerrainFilter,
    onApplyWindPreset: applyWindPreset,
    emailVerified: user.email_verified,
    onResendVerification,
    onDeleteAccount,
  };

  // On mobile, bottom-floating panels sit above the nav bar.
  const mobileBottom = MOBILE_NAV_H + 8;

  return (
    <>
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />

      <SearchBar theme={theme} mobile={isMobile} onSearch={flyToCoords} />

      {/* Desktop: fixed top-left panel */}
      {!isMobile && <LayerPanel {...layerPanelProps} />}

      {/* Mobile: bottom sheet */}
      {isMobile && (
        <MobileSheet open={mobilePanelOpen} onClose={() => setMobilePanelOpen(false)} theme={theme}>
          <LayerPanel {...layerPanelProps} mobile />
        </MobileSheet>
      )}

      {/* Desktop only: toolbox panel (below hamburger) */}
      {!isMobile && (
        <ToolboxPanel
          measureActive={measureMode}
          slopeFilterActive={slopeFilterMode}
          terrain3dActive={terrain3d}
          routeBuilderActive={routeBuilderMode}
          savedRoutesActive={savedRoutesOpen}
          tripsActive={tripsOpen}
          layerPanelCollapsed={layerPanelCollapsed}
          theme={theme}
          onMeasureToggle={() => { setRouteBuilderMode(false); setMeasureMode((m) => !m); }}
          onSlopeFilterToggle={() => setSlopeFilterMode((m) => !m)}
          onTerrain3dToggle={() => setTerrain3d((t) => !t)}
          onRouteBuilderToggle={() => { setMeasureMode(false); setRouteBuilderMode((m) => !m); }}
          onSavedRoutesToggle={() => setSavedRoutesOpen((o) => !o)}
          onTripsToggle={() => setTripsOpen((o) => !o)}
        />
      )}


      {/* 3D keyboard-controls hint — shown for 6s when 3D is first enabled */}
      {show3dHint && !isMobile && (
        <div
          role="status"
          style={{
            position: "fixed",
            bottom: 28,
            left: "50%",
            transform: "translateX(-50%)",
            zIndex: Z.MAP_OVERLAY,
            background: theme.panel,
            color: theme.text,
            padding: "6px 14px",
            borderRadius: 16,
            fontSize: 12,
            fontFamily: "ui-sans-serif, system-ui, sans-serif",
            boxShadow: "0 2px 8px rgba(0,0,0,0.25)",
            border: `1px solid ${theme.divider}`,
            userSelect: "none",
            pointerEvents: "none",
            opacity: 0.92,
            whiteSpace: "nowrap",
          }}
        >
          WASD / arrows to fly · right-click drag or Shift+arrows to tilt &amp; spin
        </div>
      )}

      {/* Mobile bottom nav */}
      {isMobile && (
        <MobileNav
          theme={theme}
          layersOpen={mobilePanelOpen}
          toolsActive={measureMode || slopeFilterMode || terrain3d || mobileToolsOpen}
          onLayersToggle={() => { setMobilePanelOpen((o) => !o); setMobileToolsOpen(false); }}
          onToolsToggle={() => { setMobileToolsOpen((o) => !o); setMobilePanelOpen(false); }}
        />
      )}

      {/* Mobile tools sheet */}
      {isMobile && (
        <MobileSheet open={mobileToolsOpen} onClose={() => setMobileToolsOpen(false)} theme={theme}>
          <div style={{ padding: "8px 16px 24px", fontFamily: "ui-sans-serif, system-ui, sans-serif" }}>
            <div style={{ fontSize: 11, color: theme.muted, textTransform: "uppercase", letterSpacing: "0.07em", marginBottom: 10 }}>
              Tools
            </div>
            {/* Measure Slope */}
            <button
              onClick={() => { setMeasureMode((m) => !m); setMobileToolsOpen(false); }}
              aria-pressed={measureMode}
              style={{
                display: "flex", alignItems: "center", gap: 12, width: "100%",
                background: measureMode ? "rgba(224,90,43,0.12)" : "transparent",
                color: measureMode ? theme.accent : theme.text,
                border: `1px solid ${measureMode ? theme.accent : theme.divider}`,
                borderRadius: 10, padding: "13px 14px", cursor: "pointer",
                fontSize: 14, fontWeight: 500, fontFamily: "inherit", textAlign: "left",
                marginBottom: 8,
              }}
            >
              <svg width="18" height="18" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                <rect x="1" y="4.5" width="12" height="5" rx="1"/>
                <line x1="4" y1="4.5" x2="4" y2="7"/>
                <line x1="7" y1="4.5" x2="7" y2="6.2"/>
                <line x1="10" y1="4.5" x2="10" y2="7"/>
              </svg>
              <div>
                <div>Measure Slope</div>
                <div style={{ fontSize: 11, color: theme.muted, fontWeight: 400, marginTop: 1 }}>
                  Draw a line to sample elevation and slope
                </div>
              </div>
            </button>
            {/* Slope Filter */}
            <button
              onClick={() => { setSlopeFilterMode((m) => !m); setMobileToolsOpen(false); }}
              aria-pressed={slopeFilterMode}
              style={{
                display: "flex", alignItems: "center", gap: 12, width: "100%",
                background: slopeFilterMode ? "rgba(160,120,80,0.12)" : "transparent",
                color: slopeFilterMode ? "#a07850" : theme.text,
                border: `1px solid ${slopeFilterMode ? "#a07850" : theme.divider}`,
                borderRadius: 10, padding: "13px 14px", cursor: "pointer",
                fontSize: 14, fontWeight: 500, fontFamily: "inherit", textAlign: "left",
                marginBottom: 8,
              }}
            >
              <svg width="18" height="18" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                <path d="M1 13 L7 2 L13 13 Z"/>
                <path d="M4.5 9.5 L7 7 L9.5 9.5"/>
              </svg>
              <div>
                <div>Slope Filter</div>
                <div style={{ fontSize: 11, color: theme.muted, fontWeight: 400, marginTop: 1 }}>
                  Highlight terrain by angle and aspect
                </div>
              </div>
            </button>
            {/* 3D Terrain */}
            <button
              onClick={() => { setTerrain3d((t) => !t); setMobileToolsOpen(false); }}
              aria-pressed={terrain3d}
              style={{
                display: "flex", alignItems: "center", gap: 12, width: "100%",
                background: terrain3d ? "rgba(30,120,255,0.10)" : "transparent",
                color: terrain3d ? "#4a9eff" : theme.text,
                border: `1px solid ${terrain3d ? "#4a9eff" : theme.divider}`,
                borderRadius: 10, padding: "13px 14px", cursor: "pointer",
                fontSize: 14, fontWeight: 500, fontFamily: "inherit", textAlign: "left",
                marginBottom: 8,
              }}
            >
              <svg width="18" height="18" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                <path d="M1 10 L4 4 L7 8 L10 5 L13 10 Z"/>
                <path d="M1 10 L13 10" opacity="0.4"/>
              </svg>
              <div>
                <div>3D Terrain</div>
                <div style={{ fontSize: 11, color: theme.muted, fontWeight: 400, marginTop: 1 }}>
                  {terrain3d ? "Tap tilt/rotate buttons to adjust view" : "Enable 3D terrain view"}
                </div>
              </div>
            </button>
            {/* Copy share link */}
            <button
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(window.location.href);
                  showToast("Map link copied to clipboard.", "success");
                } catch {
                  showToast("Couldn't copy — select the address bar manually.", "error");
                }
                setMobileToolsOpen(false);
              }}
              style={{
                display: "flex", alignItems: "center", gap: 12, width: "100%",
                background: "transparent", color: theme.text,
                border: `1px solid ${theme.divider}`,
                borderRadius: 10, padding: "13px 14px", cursor: "pointer",
                fontSize: 14, fontWeight: 500, fontFamily: "inherit", textAlign: "left",
              }}
            >
              <svg width="18" height="18" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                <path d="M6 8a3 3 0 0 0 4.24 0l1.41-1.41a3 3 0 1 0-4.24-4.24L6.7 3.07"/>
                <path d="M8 6a3 3 0 0 0-4.24 0L2.35 7.41a3 3 0 1 0 4.24 4.24L7.3 10.93"/>
              </svg>
              <div>
                <div>Copy share link</div>
                <div style={{ fontSize: 11, color: theme.muted, fontWeight: 400, marginTop: 1 }}>
                  Share the current map view
                </div>
              </div>
            </button>
          </div>
        </MobileSheet>
      )}

      {slopeFilterMode && (
        <SlopeFilterPanel
          filter={terrainFilter}
          onChange={setTerrainFilter}
          onApplyWindPreset={applyWindPreset}
          onClose={() => setSlopeFilterMode(false)}
          theme={theme}
          mobile={isMobile}
          mobileBottom={mobileBottom}
          siblingActive={measureMode}
        />
      )}
      {measureMode && (
        <MeasurePanel
          pts={measurePts}
          loading={profileLoading}
          profile={profile}
          units={units}
          theme={theme}
          mobile={isMobile}
          mobileBottom={mobileBottom}
          siblingActive={slopeFilterMode}
          onClose={() => setMeasureMode(false)}
        />
      )}
      {routeBuilderMode && (
        <RouteBuilderPanel
          vertices={routeVertices}
          saving={routeSaving}
          units={units}
          theme={theme}
          mobile={isMobile}
          mobileBottom={mobileBottom}
          siblingActive={slopeFilterMode}
          onUndo={() => setRouteVertices((pts) => pts.slice(0, -1))}
          onClear={() => setRouteVertices([])}
          onSave={handleSaveRoute}
          onClose={() => setRouteBuilderMode(false)}
        />
      )}
      {savedRoutesOpen && (
        <SavedRoutesPanel
          routes={savedRoutes}
          loading={savedRoutesLoading}
          selectedId={selectedRouteId}
          units={units}
          theme={theme}
          mobile={isMobile}
          mobileBottom={mobileBottom}
          siblingActive={slopeFilterMode}
          onSelect={setSelectedRouteId}
          onDelete={handleDeleteRoute}
          onRename={handleRenameRoute}
          onShare={handleShareRoute}
          onRevoke={handleRevokeShare}
          onClose={() => { setSavedRoutesOpen(false); setSelectedRouteId(null); }}
        />
      )}
      {sharedRoute && (
        <SharedRoutePanel
          detail={sharedRoute}
          cloning={cloning}
          units={units}
          theme={theme}
          mobile={isMobile}
          mobileBottom={mobileBottom}
          onClone={handleCloneShared}
          onClose={() => { setSharedRoute(null); setSharedRouteToken(null); }}
        />
      )}
      {tripsOpen && selectedTripId == null && (
        <TripsPanel
          trips={trips}
          invites={tripInvites}
          friends={friends}
          savedRoutes={savedRoutes}
          loading={tripsLoading}
          theme={theme}
          mobile={isMobile}
          mobileBottom={mobileBottom}
          onSelectTrip={setSelectedTripId}
          onCreateTrip={handleCreateTrip}
          onRespondInvite={handleRespondInvite}
          onSendFriendRequest={handleSendFriendRequest}
          onRespondFriend={handleRespondFriend}
          onRemoveFriend={handleRemoveFriend}
          onClose={() => setTripsOpen(false)}
        />
      )}
      {tripsOpen && selectedTripId != null && tripDetail && (
        <TripView
          detail={tripDetail}
          friends={friends.friends}
          savedRoutes={savedRoutes}
          currentUserId={user.id}
          selectedTripRouteId={selectedTripRouteId}
          onSelectTripRoute={(id) => setSelectedTripRouteId((cur) => (cur === id ? null : id))}
          units={units}
          theme={theme}
          mobile={isMobile}
          mobileBottom={mobileBottom}
          waypointMode={waypointMode}
          waypointKind={waypointKind}
          onToggleWaypointMode={() => setWaypointMode((m) => !m)}
          onWaypointKindChange={setWaypointKind}
          onInvite={handleInviteMember}
          onDeleteWaypoint={handleDeleteWaypoint}
          onAddRoute={handleAddTripRoute}
          onRemoveRoute={handleRemoveTripRoute}
          onDeleteTrip={handleDeleteTrip}
          onClose={() => { setSelectedTripId(null); setWaypointMode(false); }}
        />
      )}
      {!measureMode && !routeBuilderMode && point && (
        <InfoPanel
          data={point}
          forecast={forecast}
          forecastLoading={forecastLoading}
          units={units}
          theme={theme}
          mobile={isMobile}
          mobileBottom={mobileBottom}
          onClose={() => { setPoint(null); setForecast(null); searchMarkerRef.current?.remove(); searchMarkerRef.current = null; }}
        />
      )}
      {stravaCard && (
        <StravaActivityCard
          activities={stravaCard.activities}
          index={stravaCard.index}
          onIndexChange={(i) => setStravaCard((c) => c ? { ...c, index: i } : null)}
          onClose={() => setStravaCard(null)}
          onImportRoute={handleImportStravaRoute}
          units={units}
          theme={theme}
          mobile={isMobile}
          mobileBottom={mobileBottom}
        />
      )}

      {/* Time slider — visible when a time-enabled layer is on and not dismissed */}
      {anyTimeEnabledVisible && !sliderDismissed && (
        <TimeSlider
          step={mapTimeStep}
          onChange={setMapTimeStep}
          onDismiss={() => setSliderDismissed(true)}
          theme={theme}
          mobile={isMobile}
          mobileBottom={mobileBottom}
          layerPanelCollapsed={layerPanelCollapsed}
        />
      )}
      {/* Restore button shown when slider is dismissed but a time layer is still on */}
      {anyTimeEnabledVisible && sliderDismissed && (
        <button
          onClick={() => setSliderDismissed(false)}
          title="Show time slider"
          style={{
            position: "fixed",
            bottom: isMobile ? mobileBottom + 8 : 36,
            right: 56,
            zIndex: Z.MAP_OVERLAY,
            background: theme.panel,
            border: `1px solid ${theme.divider}`,
            borderRadius: 8,
            padding: "5px 10px",
            fontSize: 12,
            color: theme.muted,
            cursor: "pointer",
            fontFamily: "ui-sans-serif, system-ui, sans-serif",
            boxShadow: "0 2px 6px rgba(0,0,0,0.25)",
          }}
        >
          ⏱ Time
        </button>
      )}

      {/* First-load hint nudging zoom-in / layer enablement */}
      <StartHint
        theme={theme}
        mobile={isMobile}
        mobileBottom={mobileBottom}
        onDismissBind={bindStartHintDismiss}
      />

      {/* Resolution pill — appears bottom-left when a terrain layer is using 1m data */}
      {aboveHiresZoom && HIRES_LAYER_IDS.some((id) => visible[id]) && (
        <div
          title="Viewing 1m high-resolution terrain data"
          style={{
            position: "fixed",
            bottom: isMobile ? mobileBottom + 4 : 28,
            left: 80,
            zIndex: Z.MAP_OVERLAY,
            display: "flex",
            alignItems: "center",
            gap: 4,
            padding: "3px 8px",
            borderRadius: 10,
            border: `1px solid ${theme.accent}`,
            background: theme.panel,
            color: theme.accent,
            fontSize: 10,
            fontWeight: 700,
            letterSpacing: "0.07em",
            fontFamily: "ui-sans-serif, system-ui, sans-serif",
            userSelect: "none",
            boxShadow: "0 1px 4px rgba(0,0,0,0.2)",
            pointerEvents: "none",
          }}
        >
          1m
        </div>
      )}

      {/* Compass rose — bottom-left; rotates with map bearing; click to reset north */}
      {!isMobile && (
        <div
          title="Click to reset north"
          onClick={() => mapRef.current?.easeTo({ bearing: 0, duration: 400 })}
          style={{
            position: "fixed",
            bottom: 52,
            left: 10,
            zIndex: Z.MAP_OVERLAY,
            width: 72,
            height: 72,
            cursor: "pointer",
            userSelect: "none",
          }}
        >
          <svg
            width="72" height="72" viewBox="0 0 36 36"
            style={{ transform: `rotate(${-mapBearing}deg)`, transition: "transform 80ms linear" }}
          >
            <circle cx="18" cy="18" r="16" fill={theme.panel} stroke={theme.divider} strokeWidth="1.2"/>
            {/* N needle — red */}
            <path d="M18 4 L21.5 18 L18 22 L14.5 18 Z" fill="#d7191c"/>
            {/* S needle — muted */}
            <path d="M18 32 L21.5 18 L18 14 L14.5 18 Z" fill={theme.muted} opacity="0.55"/>
            {/* Centre dot */}
            <circle cx="18" cy="18" r="2" fill={theme.panel} stroke={theme.divider} strokeWidth="1"/>
            {/* N label */}
            <text x="18" y="3.5" textAnchor="middle" fontSize="4.5" fontWeight="800" fill="#d7191c" fontFamily="ui-sans-serif,system-ui,sans-serif">N</text>
          </svg>
        </div>
      )}

      {/* My location — bottom-right, above MapLibre attribution */}
      <button
        onClick={locateMe}
        disabled={locating}
        title="Show my location"
        aria-label="Show my location"
        style={{
          position: "fixed",
          bottom: 28,
          right: 10,
          zIndex: Z.MAP_OVERLAY,
          width: 32,
          height: 32,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: 0,
          borderRadius: 5,
          border: `1px solid ${theme.divider}`,
          background: theme.panel,
          color: locating ? theme.muted : theme.accent,
          cursor: locating ? "wait" : "pointer",
          boxShadow: "0 1px 6px rgba(0,0,0,0.3)",
        }}
      >
        {/* Crosshair / locate icon */}
        <svg
          width="16" height="16" viewBox="0 0 16 16" fill="none"
          stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"
          style={{
            animation: locating ? "whumpf-spin 1.2s linear infinite" : undefined,
          }}
          aria-hidden="true"
        >
          <circle cx="8" cy="8" r="3"/>
          <circle cx="8" cy="8" r="6"/>
          <line x1="8" y1="0.5" x2="8" y2="3"/>
          <line x1="8" y1="13" x2="8" y2="15.5"/>
          <line x1="0.5" y1="8" x2="3" y2="8"/>
          <line x1="13" y1="8" x2="15.5" y2="8"/>
        </svg>
      </button>

    </>
  );
}
