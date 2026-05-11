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
  PointData,
  ProfileResponse,
  Region,
  Units,
} from "./Map/types";
import { fetchSpotData, reverseGeocode } from "./Map/services";

import {
  cogS3,
  getContourUrl,
  getMapStyle,
  swapRasterBasemap,
} from "./Map/layers/basemaps";
import {
  HIRES_LAYER_IDS,
  TERRAIN_LAYER_IDS,
  addOverlayLayers,
  applyTerrainOrder,
  buildLayerGroups,
  buildOverlayLayers,
} from "./Map/layers/overlays";
import {
  addSnotelLayers,
  buildSnotelPopupHtml,
  setSnotelData,
  setSnotelVisibility,
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
import { addRegionMask, setMaskVisibility } from "./Map/layers/mask";
import { readUrlState, writeUrlState } from "./Map/urlState";
import {
  MEASURE_MARKER_STYLE,
  addMeasureLayers,
  fetchProfile,
  updateMeasureSource,
} from "./Map/layers/measure";

import { LayerPanel } from "./Map/LayerPanel";
import { InfoPanel } from "./Map/InfoPanel";
import { MeasurePanel } from "./Map/MeasurePanel";
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

  const [dark, setDark] = useState(true);
  const [basemap, setBasemap] = useState<BasemapId>(() => {
    if (initialUrlState.basemap) return initialUrlState.basemap;
    try {
      const s = localStorage.getItem("whumpf:basemap");
      if (s === "streets" || s === "topo" || s === "satellite" || s === "hybrid") return s;
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
      return stored ? { ...defaults, ...JSON.parse(stored) } : defaults;
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
  const [measurePts, setMeasurePts] = useState<[number, number][]>([]);
  const [profile, setProfile] = useState<ProfileResponse | null>(null);
  const [profileLoading, setProfileLoading] = useState(false);
  const [units, setUnits] = useState<Units>(() => initialUrlState.units ?? "imperial");
  const [forecast, setForecast] = useState<ForecastPeriod[] | null>(null);
  const [forecastLoading, setForecastLoading] = useState(false);
  const [snotelLoaded, setSnotelLoaded] = useState(false);
  const [caicLoaded, setCaicLoaded] = useState(false);
  const [obsLoaded, setObsLoaded] = useState(false);
  const obsDataRef = useRef<object | null>(null);
  const [boundsLocked, setBoundsLocked] = useState(true);
  const [aboveHiresZoom, setAboveHiresZoom] = useState(region.default_zoom >= 13);
  const [layerPanelCollapsed, setLayerPanelCollapsed] = useState(false);
  const caicDataRef = useRef<object | null>(null);
  const [stravaVisible, setStravaVisible] = useState(true);
  const [stravaLoaded, setStravaLoaded] = useState(false);
  const stravaDataRef = useRef<object | null>(null);
  const selectedStravaIdRef = useRef<number | null>(null);
  const [stravaCard, setStravaCard] = useState<{ activities: ActivityCardProps[]; index: number } | null>(null);

  const [contourInterval, setContourInterval] = useState<number | null>(null);
  const contourIntervalRef = useRef<number | null>(null);
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
  useEffect(() => { contourIntervalRef.current = contourInterval; }, [contourInterval]);
  useEffect(() => { terrainOrderRef.current = terrainOrder; }, [terrainOrder]);

  const snotelDataRef = useRef<object | null>(null);
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
      maxBounds: region.max_bounds,
      renderWorldCopies: false,
    });

    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "imperial" }), "bottom-left");

    // Single permanent handler for both initial load and every subsequent setStyle call.
    // All addXxx helpers are idempotent (guard on getSource), so re-firing is safe.
    map.on("style.load", () => {
      addOverlayLayers(map, overlayLayers, region.bbox, visibleRef.current, opacityRef.current, {
        contours: [getContourUrl(region.id, contourIntervalRef.current)],
      });
      applyTerrainOrder(map, terrainOrderRef.current);
      addRegionMask(map, region.mask_geojson);
      addMeasureLayers(map);
      updateMeasureSource(map, measurePtsRef.current);
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
      new maplibregl.Popup({ closeButton: true, maxWidth: "260px" })
        .setLngLat(lngLat)
        .setHTML(buildSnotelPopupHtml(p, unitsRef.current))
        .addTo(map);
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
      swapRasterBasemap(map, prev as "topo" | "satellite" | "hybrid", basemap as "topo" | "satellite" | "hybrid");
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
      if (map.getLayer(id))
        map.setLayoutProperty(id, "visibility", isVis ? "visible" : "none");
      if (map.getLayer(`${id}-hires`))
        map.setLayoutProperty(`${id}-hires`, "visibility", isVis ? "visible" : "none");
    }
  }, [visible]);

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

  // Sync visibility immediately; fetch lazily via useFetchWithRetry below.
  const snotelVisible = visible["snotel"];
  const caicVisible   = visible["caic-danger"];
  const obsVisible    = visible["caic-obs"];

  useEffect(() => { setSnotelVisibility(mapRef.current, !!snotelVisible); }, [snotelVisible]);
  useEffect(() => { setCaicVisibility(mapRef.current, !!caicVisible); },     [caicVisible]);
  useEffect(() => { setObsVisibility(mapRef.current, !!obsVisible); },       [obsVisible]);

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

  // Persist layer selections and basemap to localStorage, debounced.
  // Without the debounce, dragging the opacity slider triggered ~30 writes/sec.
  useEffect(() => {
    const t = setTimeout(() => {
      try {
        localStorage.setItem("whumpf:basemap", basemap);
        localStorage.setItem("whumpf:layer-visible", JSON.stringify(visible));
        localStorage.setItem("whumpf:layer-opacity", JSON.stringify(opacity));
        localStorage.setItem("whumpf:terrain-order", JSON.stringify(terrainOrder));
      } catch {
        // quota / private mode — best-effort, no point alerting the user
      }
    }, 250);
    return () => clearTimeout(t);
  }, [basemap, visible, opacity, terrainOrder]);

  // Sync the layer/basemap/units pieces of URL state. Viewport is handled by
  // the map's moveend listener; these effects cover everything else.
  useEffect(() => {
    const ids = Object.entries(visible).filter(([, v]) => v).map(([id]) => id);
    writeUrlState({ visibleLayers: ids });
  }, [visible]);
  useEffect(() => { writeUrlState({ basemap }); }, [basemap]);
  useEffect(() => { writeUrlState({ units }); }, [units]);

  // When the contour interval changes, swap the MapLibre source+layer with the new tile URL.
  // Raster sources don't support in-place tile URL updates, so we remove and re-add.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.getLayer("contours")) return;
    const url = getContourUrl(region.id, contourInterval);
    const isVis = visibleRef.current["contours"] ?? false;
    const op = opacityRef.current["contours"] ?? 1.0;
    const beforeId: string | undefined =
      map.getLayer("basemap-ref")
        ? "basemap-ref"
        : map.getStyle()?.layers?.find((l) => l.type === "symbol")?.id;
    map.removeLayer("contours");
    map.removeSource("contours");
    map.addSource("contours", {
      type: "raster",
      tiles: [url],
      tileSize: 256,
      bounds: region.bbox,
      minzoom: 9,  // matches buildLayerGroups sourceMinzoom for contours
      maxzoom: 16,
      attribution: "USGS 3DEP",
    });
    map.addLayer({
      id: "contours",
      type: "raster",
      source: "contours",
      paint: { "raster-opacity": op, "raster-fade-duration": 400 },
      layout: { visibility: isVis ? "visible" : "none" },
    }, beforeId);
    // Re-apply terrain order since we removed and re-added the contours layer.
    applyTerrainOrder(map, terrainOrderRef.current);
  }, [contourInterval]);

  // Reorder terrain layers in MapLibre whenever the sidebar order changes.
  // Guard with isStyleLoaded(): on initial mount the effect fires before the async style fetch
  // completes, at which point getStyle() returns null and applyTerrainOrder would throw.
  // The correct initial order is applied by applyTerrainOrder inside the style.load handler.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    applyTerrainOrder(map, terrainOrder);
  }, [terrainOrder]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    map.setMaxBounds(boundsLocked ? region.max_bounds : null);
    setMaskVisibility(map, boundsLocked);
  }, [boundsLocked, region.max_bounds]);

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
    contourInterval,
    onContourInterval: setContourInterval,
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
          layerPanelCollapsed={layerPanelCollapsed}
          theme={theme}
          onMeasureToggle={() => setMeasureMode((m) => !m)}
        />
      )}

      {/* Mobile bottom nav */}
      {isMobile && (
        <MobileNav
          theme={theme}
          layersOpen={mobilePanelOpen}
          measureActive={measureMode}
          onLayersToggle={() => setMobilePanelOpen((o) => !o)}
          onMeasureToggle={() => setMeasureMode((m) => !m)}
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
          onClose={() => setMeasureMode(false)}
        />
      )}
      {!measureMode && point && (
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
          units={units}
          theme={theme}
          mobile={isMobile}
          mobileBottom={mobileBottom}
        />
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

      {/* Region lock toggle — bottom-right, above MapLibre attribution */}
      <button
        onClick={() => setBoundsLocked((l) => !l)}
        title={boundsLocked ? `Expand map beyond ${region.label}` : `Lock map to ${region.label}`}
        aria-label={boundsLocked ? `Expand map beyond ${region.label}` : `Lock map to ${region.label}`}
        aria-pressed={boundsLocked}
        style={{
          position: "fixed",
          bottom: 28,
          right: 10,
          zIndex: Z.MAP_OVERLAY,
          display: "flex",
          alignItems: "center",
          gap: 5,
          padding: "5px 10px",
          borderRadius: 5,
          border: `1px solid ${boundsLocked ? theme.accent : theme.divider}`,
          background: theme.panel,
          color: boundsLocked ? theme.accent : theme.muted,
          fontSize: 12,
          fontFamily: "ui-sans-serif, system-ui, sans-serif",
          cursor: "pointer",
          boxShadow: "0 1px 6px rgba(0,0,0,0.3)",
          userSelect: "none",
        }}
      >
        <span style={{ fontSize: 13 }}>{boundsLocked ? "🔒" : "🌐"}</span>
        {boundsLocked ? region.label : "Unlocked"}
      </button>
    </>
  );
}
