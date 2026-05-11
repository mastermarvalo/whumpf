import { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import { apiFetch } from "../auth";
import { useFetchWithRetry } from "../hooks/useFetchWithRetry";
import type { StravaStatus } from "../App";

import { API_URL } from "./Map/constants";
import { THEMES, MOBILE_NAV_H } from "./Map/theme";
import type {
  ActiveLayer,
  ActivityCardProps,
  BasemapId,
  ForecastPeriod,
  LayerGroup,
  PointData,
  ProfileResponse,
  SpotData,
  Units,
} from "./Map/types";

import { LayerPanel } from "./Map/LayerPanel";
import { InfoPanel } from "./Map/InfoPanel";
import { MeasurePanel } from "./Map/MeasurePanel";
import { SearchBar } from "./Map/SearchBar";
import { StravaActivityCard } from "./Map/StravaActivityCard";
import { ToolboxPanel } from "./Map/ToolboxPanel";
import { MobileSheet } from "./Map/MobileSheet";
import { MobileNav } from "./Map/MobileNav";

const TITILER_URL = import.meta.env.VITE_TITILER_URL ?? "http://localhost:8001";
const MINIO_BUCKET = "dem-cogs";
const REGION = "colorado";

const INITIAL_CENTER: [number, number] = [-105.5, 39.0];
const INITIAL_ZOOM = 7;
const COLORADO_MTN_BOUNDS: [number, number, number, number] = [-109.06, 37.0, -104.5, 41.0];
// Padded Colorado bbox used as map maxBounds when region lock is on.
const CO_MAX_BOUNDS: [number, number, number, number] = [-109.5, 36.5, -101.5, 41.5];

// Inverted polygon: world rectangle with Colorado cut out as a hole.
// Renders as a black fill masking everything outside Colorado.
const CO_MASK_GEOJSON = {
  type: "FeatureCollection",
  features: [{
    type: "Feature",
    geometry: {
      type: "Polygon",
      coordinates: [
        [[-180, -90], [180, -90], [180, 90], [-180, 90], [-180, -90]],  // outer world
        [[-109.25, 36.80], [-101.85, 36.80], [-101.85, 41.20], [-109.25, 41.20], [-109.25, 36.80]], // CO hole (CW)
      ],
    },
    properties: {},
  }],
};

const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services";
const OMP_GLYPHS = "https://fonts.openmaptiles.org/{fontstack}/{range}.pbf";

const VECTOR_STYLES = {
  light: "https://tiles.openfreemap.org/styles/positron",
  dark:  "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
};

function rasterStyle(
  tiles: string[],
  attribution: string,
  maxzoom?: number,
): maplibregl.StyleSpecification {
  return {
    version: 8,
    // Glyphs needed for SNOTEL symbol layers on raster basemaps.
    glyphs: OMP_GLYPHS,
    sources: {
      basemap: { type: "raster", tiles, tileSize: 256, attribution, ...(maxzoom ? { maxzoom } : {}) },
    },
    layers: [{ id: "basemap", type: "raster", source: "basemap" }],
  };
}

// Satellite + transparent label/road overlay (classic "hybrid" view).
function hybridStyle(): maplibregl.StyleSpecification {
  return {
    version: 8,
    glyphs: OMP_GLYPHS,
    sources: {
      sat: {
        type: "raster",
        tiles: [`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`],
        tileSize: 256,
        maxzoom: 17,
        attribution: "Esri, DigitalGlobe",
      },
      ref: {
        type: "raster",
        // Transparent PNG overlay — labels, roads, boundaries on top of satellite.
        tiles: [`${ESRI}/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}`],
        tileSize: 256,
        maxzoom: 17,
      },
    },
    layers: [
      { id: "basemap-sat", type: "raster", source: "sat" },
      { id: "basemap-ref", type: "raster", source: "ref" },
    ],
  };
}

const RASTER_STYLES: Record<"topo" | "satellite" | "hybrid", maplibregl.StyleSpecification> = {
  // Esri World Topo Map: contours, shaded relief, trails, water — no API key required.
  topo:      rasterStyle([`${ESRI}/World_Topo_Map/MapServer/tile/{z}/{y}/{x}`], "Esri"),
  // maxzoom: 17 — beyond that Esri returns a "not available yet" placeholder JPEG.
  // MapLibre overzooms the z17 tile instead of requesting z18+.
  satellite: rasterStyle([`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`], "Esri, DigitalGlobe", 17),
  hybrid:    hybridStyle(),
};

// Source and layer IDs owned by each raster basemap — used for in-place swaps.
const RASTER_BASEMAP_IDS: Record<"topo" | "satellite" | "hybrid", { sources: string[]; layers: string[] }> = {
  topo:      { sources: ["basemap"],    layers: ["basemap"] },
  satellite: { sources: ["basemap"],    layers: ["basemap"] },
  hybrid:    { sources: ["sat", "ref"], layers: ["basemap-sat", "basemap-ref"] },
};

function getMapStyle(basemap: BasemapId, dark: boolean): string | maplibregl.StyleSpecification {
  if (basemap === "streets") return VECTOR_STYLES[dark ? "dark" : "light"];
  return RASTER_STYLES[basemap];
}

function cogS3(path: string) {
  return `s3://${MINIO_BUCKET}/${path}`;
}

function cogTiles(cogPath: string, extra: Record<string, string> = {}): string[] {
  const params = new URLSearchParams({ url: cogS3(cogPath), ...extra });
  return [`${TITILER_URL}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?${params}`];
}

function getContourUrl(interval: number | null): string {
  const base = `${API_URL}/tiles/contours/{z}/{x}/{y}?region=${REGION}`;
  return interval != null ? `${base}&interval=${interval}` : base;
}

// ── weather / snowpack provider config ────────────────────────────────────────
// Swap any URL string here to change the underlying data source.
// MapLibre replaces {bbox-epsg-3857} with the tile's bbox (west,south,east,north, EPSG:3857).
const _NWS = "https://mapservices.weather.noaa.gov/raster/rest/services";
// ArcGIS MapServer/export and ImageServer/exportImage share these params
const _AGS = "bboxSR=3857&imageSR=3857&size=256,256&f=image&format=png32&transparent=true";
// NDFD_temp layers: 0=TempF_24Hr 9=Temp_03Hr(+3hr, closest to current with data) 41=AptTempF_24Hr
// NOHRSC_Snow_Analysis layers: 0=Snow Depth 4=Snow Water Equivalent
const WEATHER_SOURCES = {
  // NDFD +3hr temperature — layer 0 (0hr) is always empty; 9 (3hr) is the earliest available
  tempCurrent:   `${_NWS}/NDFD/NDFD_temp/MapServer/export?bbox={bbox-epsg-3857}&${_AGS}&layers=show:9`,
  // NDFD 24-hr temperature forecast
  tempForecast:  `${_NWS}/NDFD/NDFD_temp/MapServer/export?bbox={bbox-epsg-3857}&${_AGS}&layers=show:0`,
  // MRMS composite reflectivity — current precipitation radar
  precipRadar:   `https://opengeo.ncep.noaa.gov/geoserver/conus/conus_cref_qcd/ows?service=WMS&version=1.1.1&request=GetMap&layers=conus_cref_qcd&format=image/png&transparent=true&width=256&height=256&srs=EPSG:3857&bbox={bbox-epsg-3857}&styles=`,
  // MRMS QPE — hourly precipitation accumulation
  precipAccum:   `${_NWS}/obs/mrms_qpe/ImageServer/exportImage?bbox={bbox-epsg-3857}&${_AGS}`,
  // NOHRSC analyzed snow depth
  snowDepth:     `${_NWS}/snow/NOHRSC_Snow_Analysis/MapServer/export?bbox={bbox-epsg-3857}&${_AGS}&layers=show:0`,
};

// Swap fetchSpotData to change the spot forecast/conditions provider.
// Calls /points/ once, then fans out to forecast + forecastGridData in parallel.
async function fetchSpotData(lat: number, lng: number): Promise<SpotData> {
  const headers = { "User-Agent": "(whumpf, backcountry-terrain-app)" };
  const meta = await fetch(
    `https://api.weather.gov/points/${lat.toFixed(4)},${lng.toFixed(4)}`,
    { headers },
  ).then((r) => (r.ok ? r.json() : null));
  if (!meta) return { periods: [], tempF: null, snowDepthIn: null };

  const [forecastData, gridData] = await Promise.all([
    meta.properties?.forecast
      ? fetch(meta.properties.forecast, { headers }).then((r) => (r.ok ? r.json() : null))
      : Promise.resolve(null),
    meta.properties?.forecastGridData
      ? fetch(meta.properties.forecastGridData, { headers }).then((r) => (r.ok ? r.json() : null))
      : Promise.resolve(null),
  ]);

  const periods: ForecastPeriod[] = (forecastData?.properties?.periods ?? []).slice(0, 8);
  // gridData values: temperature in °C, snowDepth in metres (wmoUnit:m)
  const tempC: number | null = gridData?.properties?.temperature?.values?.[0]?.value ?? null;
  const snowM: number | null = gridData?.properties?.snowDepth?.values?.[0]?.value ?? null;

  return {
    periods,
    tempF: tempC != null ? tempC * 9 / 5 + 32 : null,
    snowDepthIn: snowM != null ? snowM / 0.0254 : null,
  };
}

// ── layer definitions ──────────────────────────────────────────────────────────

const TERRAIN_LAYER_IDS = ["hillshade", "slope", "aspect", "contours"];

// Layers that use 1m DEM data at zoom >= 13 (either via hiresTiles companion or server-side).
const HIRES_LAYER_IDS = TERRAIN_LAYER_IDS;

const LAYER_GROUPS: LayerGroup[] = [
  {
    id: "terrain",
    label: "Terrain",
    color: "#a07850",
    reorderable: true,
    active: [
      {
        id: "hillshade",
        label: "Hillshade",
        tiles: cogTiles(`${REGION}/hillshade.tif`),
        hiresTiles: cogTiles(`${REGION}/hillshade_hires.tif`),
        opacity: 0.7,
        defaultVisible: true,
      },
      {
        id: "slope",
        label: "Slope angle",
        // Served via API proxy which applies the CalTopo V1 colormap server-side.
        // Backend adds buffer=2 so TiTiler has neighbour context at tile edges.
        tiles: [`${API_URL}/tiles/slope/{z}/{x}/{y}?region=${REGION}`],
        hiresTiles: [`${API_URL}/tiles/slope/{z}/{x}/{y}?region=${REGION}&hires=true`],
        opacity: 0.75,
        defaultVisible: false,
        blendPaint: { "raster-saturation": -0.3 },
        legend: {
          gradient: "linear-gradient(to right, transparent 0%, #1a9641 25%, #ffeb00 45%, #d7191c 67%, #2b7bb9 100%)",
          stops: ["0°", "15°", "27°", "40°", "60°"],
        },
      },
      {
        id: "aspect",
        label: "Aspect",
        // buffer=2: TiTiler fetches extra pixels per edge so hue transitions
        // don't seam at tile boundaries. width/height=512: 2:1 downscale in
        // MapLibre smooths the blocky 10m DEM cells.
        tiles: cogTiles(`${REGION}/aspect.tif`, {
          colormap_name: "hsv",
          rescale: "0,360",
          nodata: "-9999",
          buffer: "2",
          tilesize: "512",
        }),
        hiresTiles: cogTiles(`${REGION}/aspect_hires.tif`, {
          colormap_name: "hsv",
          rescale: "0,360",
          nodata: "-9999",
          buffer: "2",
          tilesize: "512",
        }),
        opacity: 0.7,
        defaultVisible: false,
        blendPaint: { "raster-saturation": -0.4 },
        legend: {
          gradient:
            "linear-gradient(to right, #ff0000, #ffff00, #00ff00, #00ffff, #0000ff, #ff00ff, #ff0000)",
          stops: ["N", "E", "S", "W", "N"],
        },
      },
      {
        id: "contours",
        label: "Contour lines",
        tiles: [`${API_URL}/tiles/contours/{z}/{x}/{y}?region=${REGION}`],
        opacity: 1.0,
        defaultVisible: false,
        noSlider: true,
        sourceMinzoom: 9,
      },
    ],
    upcoming: [],
  },
  {
    id: "snowpack",
    label: "Snowpack",
    color: "#4a90d9",
    active: [
      {
        id: "snotel",
        label: "SNOTEL Stations",
        kind: "geojson",
        tiles: [],
        opacity: 1,
        defaultVisible: false,
        noSlider: true,
        legend: {
          gradient: "linear-gradient(to right, #d7191c, #f4820a, #ffeb00, #78c679, #1a9641)",
          stops: ["<50%", "75%", "100%", "125%", ">125%"],
        },
      },
    ],
    upcoming: [],
  },
  {
    id: "avalanche",
    label: "Avalanche",
    color: "#e05a2b",
    active: [
      {
        id: "caic-danger",
        label: "CAIC Danger Zones",
        kind: "geojson",
        tiles: [],
        opacity: 0.5,
        defaultVisible: false,
        noSlider: true,
        legend: {
          swatches: [
            { color: "#00b200", label: "Low" },
            { color: "#f4e500", label: "Mod" },
            { color: "#ff9933", label: "Consid" },
            { color: "#d7191c", label: "High" },
            { color: "#000000", label: "Extreme" },
          ],
        },
      },
      {
        id: "caic-obs",
        label: "Field Observations",
        kind: "geojson",
        tiles: [],
        opacity: 1,
        defaultVisible: false,
        noSlider: true,
        legend: {
          swatches: [
            { color: "#d7191c", label: "Caught" },
            { color: "#ff9933", label: "Saw avy" },
            { color: "#5ba3f5", label: "Field obs" },
          ],
        },
      },
    ],
    upcoming: [],
  },
  {
    id: "weather",
    label: "Weather",
    color: "#2eaa6e",
    active: [
      {
        id: "temp-current",
        label: "Temp (+3hr, NDFD)",
        tiles: [WEATHER_SOURCES.tempCurrent],
        opacity: 0.75,
        defaultVisible: false,
        noSlider: true,
        // NWS NDFD colormap: cyan = freezing, teal-green = cool, lime = mild, yellow = warm
        legend: {
          gradient: "linear-gradient(to right, #00d0d0, #20e080, #80e020, #c0e000, #e0e000)",
          stops: ["0°F", "32°F", "50°F", "70°F", "90°F"],
        },
      },
      {
        id: "temp-forecast",
        label: "Temp (24hr fcst)",
        tiles: [WEATHER_SOURCES.tempForecast],
        opacity: 0.75,
        defaultVisible: false,
        noSlider: true,
        legend: {
          gradient: "linear-gradient(to right, #00d0d0, #20e080, #80e020, #c0e000, #e0e000)",
          stops: ["0°F", "32°F", "50°F", "70°F", "90°F"],
        },
      },
      {
        id: "precip-radar",
        label: "Precip radar (now)",
        tiles: [WEATHER_SOURCES.precipRadar],
        opacity: 0.8,
        defaultVisible: false,
        noSlider: true,
        // Standard NWS composite reflectivity dBZ colormap
        legend: {
          gradient: "linear-gradient(to right, #00cc00, #ffff00, #ff6600, #cc0000, #cc00cc)",
          stops: ["15 dBZ", "30", "45", "55", "65+"],
        },
      },
      {
        id: "precip-accum",
        label: "Precip accum (1hr)",
        tiles: [WEATHER_SOURCES.precipAccum],
        opacity: 0.75,
        defaultVisible: false,
        noSlider: true,
        // MRMS QPE colormap: bright cyan for trace, darker blue for heavy
        legend: {
          gradient: "linear-gradient(to right, #00e0e0, #00c0e0, #0080d0, #0040b0, #002080)",
          stops: ["0.01\"", "0.1\"", "0.25\"", "0.5\"", "1\"+"],
        },
      },
      {
        id: "snow-depth",
        label: "Snow depth (NOHRSC)",
        tiles: [WEATHER_SOURCES.snowDepth],
        opacity: 0.75,
        defaultVisible: false,
        noSlider: true,
        // NOHRSC snow depth colormap: cyan-blue for shallow, dark blue for deep
        legend: {
          gradient: "linear-gradient(to right, #60c0c0, #60a0c0, #4060c0, #2020c0, #101080)",
          stops: ["Trace", "6\"", "24\"", "48\"", "72\"+"],
        },
      },
    ],
    upcoming: [],
  },
];

const OVERLAY_LAYERS: ActiveLayer[] = LAYER_GROUPS.flatMap((g) => g.active);

function useIsMobile() {
  const [mobile, setMobile] = useState(() => window.innerWidth < 640);
  useEffect(() => {
    const fn = () => setMobile(window.innerWidth < 640);
    window.addEventListener("resize", fn);
    return () => window.removeEventListener("resize", fn);
  }, []);
  return mobile;
}

async function reverseGeocode(lat: number, lon: number): Promise<string | null> {
  try {
    const r = await fetch(`https://photon.komoot.io/reverse?lat=${lat}&lon=${lon}`);
    if (!r.ok) return null;
    const d = await r.json();
    const p = d.features?.[0]?.properties;
    if (!p) return null;
    const parts: string[] = [];
    if (p.name) parts.push(p.name);
    if (p.street && p.street !== p.name) parts.push(p.street);
    if (p.city) parts.push(p.city);
    else if (p.county) parts.push(p.county);
    if (p.state) parts.push(p.state);
    return parts.length ? parts.join(", ") : null;
  } catch {
    return null;
  }
}

// ── measure helpers ────────────────────────────────────────────────────────────

const MEASURE_MARKER_STYLE =
  "background:#e05a2b;color:#fff;border-radius:50%;width:22px;height:22px;" +
  "display:flex;align-items:center;justify-content:center;font-size:11px;" +
  "font-weight:700;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,0.3);cursor:default;";

async function fetchProfile(
  a: [number, number],
  b: [number, number],
): Promise<ProfileResponse> {
  const p = new URLSearchParams({
    start_lng: String(a[0]),
    start_lat: String(a[1]),
    end_lng: String(b[0]),
    end_lat: String(b[1]),
    region: REGION,
    n: "64",
  });
  const r = await apiFetch(`${API_URL}/terrain/profile?${p}`);
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json() as Promise<ProfileResponse>;
}

function addMeasureLayers(map: maplibregl.Map) {
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

function updateMeasureSource(map: maplibregl.Map | null, pts: [number, number][]) {
  if (!map) return;
  const src = map.getSource("measure-line") as maplibregl.GeoJSONSource | undefined;
  if (!src) return;
  src.setData(
    pts.length === 2
      ? { type: "Feature", geometry: { type: "LineString", coordinates: pts }, properties: {} }
      : { type: "FeatureCollection", features: [] },
  );
}

// ── map setup helpers ──────────────────────────────────────────────────────────

function addOverlayLayers(
  map: maplibregl.Map,
  visible: Record<string, boolean>,
  opacity: Record<string, number>,
  tileOverrides?: Record<string, string[]>,
) {
  // On vector basemaps, insert overlays before the first symbol layer so labels stay on top.
  // On hybrid, insert before basemap-ref (the transparent road/label overlay) for the same reason.
  // On plain raster basemaps (topo, satellite) there are no symbol layers — append to top.
  const beforeId: string | undefined =
    map.getLayer("basemap-ref")
      ? "basemap-ref"
      : map.getStyle()?.layers?.find((l) => l.type === "symbol")?.id;
  for (const layer of OVERLAY_LAYERS) {
    if (layer.kind === "geojson") continue; // managed separately
    if (map.getSource(layer.id)) continue;  // already present (e.g. double style.load)
    const tiles = tileOverrides?.[layer.id] ?? layer.tiles;
    map.addSource(layer.id, {
      type: "raster",
      tiles,
      tileSize: 256,
      bounds: COLORADO_MTN_BOUNDS,
      minzoom: layer.sourceMinzoom ?? 6,
      // Cap at z12 when a hires companion takes over at z13 — MapLibre overzooms the z12
      // tile instead of requesting new z13+ tiles from the base (10m) source.
      maxzoom: layer.hiresTiles ? 12 : 16,
      attribution: "USGS 3DEP",
    });
    map.addLayer(
      {
        id: layer.id,
        type: "raster",
        source: layer.id,
        paint: {
          "raster-opacity": opacity[layer.id] ?? layer.opacity,
          "raster-fade-duration": 400,  // tiles crossfade in instead of popping
          "raster-resampling": "linear",
          ...(layer.blendPaint ?? {}),
        },
        layout: { visibility: visible[layer.id] ? "visible" : "none" },
      },
      beforeId,
    );
    if (layer.hiresTiles) {
      const hiresId = `${layer.id}-hires`;
      if (!map.getSource(hiresId)) {
        map.addSource(hiresId, {
          type: "raster",
          tiles: layer.hiresTiles,
          tileSize: 256,
          bounds: COLORADO_MTN_BOUNDS,
          minzoom: 13,
          maxzoom: 16,
          attribution: "USGS 3DEP",
        });
        map.addLayer(
          {
            id: hiresId,
            type: "raster",
            source: hiresId,
            minzoom: 13,
            paint: {
              "raster-opacity": opacity[layer.id] ?? layer.opacity,
              "raster-fade-duration": 400,
              "raster-resampling": "linear",
              ...(layer.blendPaint ?? {}),
            },
            layout: { visibility: visible[layer.id] ? "visible" : "none" },
          },
          beforeId,
        );
      }
    }
  }
}

// ── SNOTEL map layer helpers ───────────────────────────────────────────────────

function addSnotelLayers(map: maplibregl.Map) {
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

function setSnotelData(map: maplibregl.Map | null, geojson: object) {
  if (!map) return;
  const src = map.getSource("snotel") as maplibregl.GeoJSONSource | undefined;
  src?.setData(geojson as Parameters<typeof src.setData>[0]);
}

function setSnotelVisibility(map: maplibregl.Map | null, visible: boolean) {
  if (!map) return;
  const v = visible ? "visible" : "none";
  for (const id of ["snotel-circles", "snotel-names", "snotel-labels"]) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", v);
  }
}

// ── Strava map layer helpers ───────────────────────────────────────────────────

function addStravaLayers(map: maplibregl.Map) {
  if (map.getSource("strava")) return;
  map.addSource("strava", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "strava-lines",
    type: "line",
    source: "strava",
    paint: {
      "line-color": ["get", "color"],
      "line-width": 2,
      "line-opacity": 0.75,
    },
  });
}

function setStravaData(map: maplibregl.Map | null, geojson: object) {
  if (!map) return;
  const src = map.getSource("strava") as maplibregl.GeoJSONSource | undefined;
  src?.setData(geojson as Parameters<typeof src.setData>[0]);
}

function setStravaVisibility(map: maplibregl.Map | null, visible: boolean) {
  if (!map) return;
  if (map.getLayer("strava-lines"))
    map.setLayoutProperty("strava-lines", "visibility", visible ? "visible" : "none");
}

// ── CAIC danger zone map layer helpers ────────────────────────────────────────

function addCaicLayers(map: maplibregl.Map) {
  if (map.getSource("caic-danger")) return;
  map.addSource("caic-danger", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "caic-danger-fill",
    type: "fill",
    source: "caic-danger",
    paint: {
      "fill-color": ["get", "color"],
      "fill-opacity": 0.35,
    },
  });
  map.addLayer({
    id: "caic-danger-line",
    type: "line",
    source: "caic-danger",
    paint: {
      "line-color": ["get", "color"],
      "line-width": 1.5,
      "line-opacity": 0.8,
    },
  });
}

function setCaicData(map: maplibregl.Map | null, geojson: object) {
  if (!map) return;
  const src = map.getSource("caic-danger") as maplibregl.GeoJSONSource | undefined;
  src?.setData(geojson as Parameters<typeof src.setData>[0]);
}

function setCaicVisibility(map: maplibregl.Map | null, visible: boolean) {
  if (!map) return;
  const v = visible ? "visible" : "none";
  for (const id of ["caic-danger-fill", "caic-danger-line"]) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", v);
  }
}

// ── CAIC field observations map layer helpers ─────────────────────────────────

const OBS_COLORS: Record<string, string> = {
  caught: "#d7191c",
  avy:    "#ff9933",
  field:  "#5ba3f5",
};

function addObsLayers(map: maplibregl.Map) {
  if (map.getSource("caic-obs")) return;
  map.addSource("caic-obs", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "caic-obs-circles",
    type: "symbol",
    source: "caic-obs",
    layout: {
      "text-field": "▲",
      "text-size": 18,
      "text-allow-overlap": true,
      "text-ignore-placement": true,
      "text-anchor": "center",
    },
    paint: {
      "text-color": ["match", ["get", "obs_type"],
        "caught", OBS_COLORS.caught,
        "avy",    OBS_COLORS.avy,
        OBS_COLORS.field,
      ],
      "text-halo-color": "rgba(255,255,255,0.9)",
      "text-halo-width": 1.5,
    },
  });
}

function setObsData(map: maplibregl.Map | null, geojson: object) {
  if (!map) return;
  const src = map.getSource("caic-obs") as maplibregl.GeoJSONSource | undefined;
  src?.setData(geojson as Parameters<typeof src.setData>[0]);
}

function setObsVisibility(map: maplibregl.Map | null, visible: boolean) {
  if (!map || !map.getLayer("caic-obs-circles")) return;
  map.setLayoutProperty("caic-obs-circles", "visibility", visible ? "visible" : "none");
}

function buildObsPopupHtml(p: Record<string, unknown>): string {
  const obsType = String(p.obs_type ?? "field");
  const color = OBS_COLORS[obsType] ?? OBS_COLORS.field;
  const typeLabel = obsType === "caught" ? "Caught in Avalanche"
    : obsType === "avy" ? "Avalanche Observed"
    : "Field Observation";
  const observer = p.is_anonymous ? "Anonymous" : String(p.observer ?? "Unknown");
  const org = p.organization ? `<span style="color:#aaa"> · ${p.organization}</span>` : "";
  const date = p.observed_at
    ? new Date(String(p.observed_at)).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
    : "";
  const zone = p.zone ? `<div style="font-size:11px;color:#aaa;margin-top:1px">${p.zone}</div>` : "";
  const route = p.route ? `<div style="font-size:11px;color:#ccc;margin-top:4px"><b>Route:</b> ${String(p.route).slice(0, 120)}</div>` : "";
  const desc = p.description
    ? `<div style="font-size:12px;color:#ddd;margin-top:8px;line-height:1.45">${String(p.description).slice(0, 300)}${String(p.description).length > 300 ? "…" : ""}</div>`
    : "";
  const avyBadge = Number(p.avy_count) > 0
    ? `<span style="background:${OBS_COLORS.avy};color:#fff;border-radius:3px;padding:1px 6px;font-size:10px;margin-left:6px">${p.avy_count} avy obs</span>`
    : "";
  const link = p.link
    ? `<div style="margin-top:10px"><a href="${p.link}" target="_blank" rel="noopener" style="color:#5ba3f5;font-size:11px;text-decoration:none">View full report →</a></div>`
    : "";
  return `
    <div style="font-family:ui-sans-serif,system-ui,sans-serif;min-width:220px;max-width:290px">
      <div style="display:flex;align-items:center;gap:7px;margin-bottom:6px">
        <span style="width:9px;height:9px;border-radius:50%;background:${color};flex-shrink:0;border:1.5px solid #fff"></span>
        <b style="font-size:13px;color:#eee">${typeLabel}</b>${avyBadge}
      </div>
      <div style="font-size:12px;color:#ccc">${observer}${org}</div>
      <div style="font-size:11px;color:#888">${date}</div>
      ${zone}${route}${desc}${link}
    </div>`;
}

function addColoradoMask(map: maplibregl.Map) {
  if (map.getSource("co-mask")) return;
  map.addSource("co-mask", { type: "geojson", data: CO_MASK_GEOJSON as any });
  map.addLayer({ id: "co-mask-fill", type: "fill", source: "co-mask",
    paint: { "fill-color": "#000000", "fill-opacity": 1 },
  });
}

function setMaskVisibility(map: maplibregl.Map | null, visible: boolean) {
  if (!map || !map.getLayer("co-mask-fill")) return;
  map.setLayoutProperty("co-mask-fill", "visibility", visible ? "visible" : "none");
}

function applyStravaHighlight(map: maplibregl.Map | null, selectedId: number | null) {
  if (!map || !map.getLayer("strava-lines")) return;
  if (selectedId == null) {
    map.setPaintProperty("strava-lines", "line-opacity", 0.75);
    map.setPaintProperty("strava-lines", "line-width", 2);
  } else {
    map.setPaintProperty("strava-lines", "line-opacity", [
      "case", ["==", ["get", "id"], selectedId], 1.0, 0.15,
    ]);
    map.setPaintProperty("strava-lines", "line-width", [
      "case", ["==", ["get", "id"], selectedId], 4, 1.5,
    ]);
  }
}

function applyTerrainOrder(map: maplibregl.Map, order: string[]) {
  const beforeId: string | undefined =
    map.getLayer("basemap-ref")
      ? "basemap-ref"
      : map.getStyle()?.layers?.find((l) => l.type === "symbol")?.id;
  if (!beforeId) return;
  // Build the stack bottom-to-top: order[0] at bottom, order[last] just below symbols.
  // Iterate reversed so each layer is slotted into place before the one above it.
  // Hires companion (${id}-hires) sits immediately above its standard layer.
  let nextAbove: string | undefined = beforeId;
  for (const id of [...order].reverse()) {
    const hiresId = `${id}-hires`;
    if (map.getLayer(hiresId)) {
      map.moveLayer(hiresId, nextAbove);
      nextAbove = hiresId;
    }
    if (map.getLayer(id)) {
      map.moveLayer(id, nextAbove);
      nextAbove = id;
    }
  }
}

// Swap only the basemap source/layers for raster-to-raster transitions without touching overlays.
function swapRasterBasemap(
  map: maplibregl.Map,
  from: "topo" | "satellite" | "hybrid",
  to: "topo" | "satellite" | "hybrid",
) {
  if (from === to) return;
  const fromIds = RASTER_BASEMAP_IDS[from];
  // Capture the first overlay layer ID before removing anything so we can insert below it.
  const firstOverlayId = map.getStyle().layers.find(
    (l) => !fromIds.layers.includes(l.id),
  )?.id;
  for (const id of fromIds.layers) {
    if (map.getLayer(id)) map.removeLayer(id);
  }
  for (const id of fromIds.sources) {
    if (map.getSource(id)) map.removeSource(id);
  }
  const newStyle = RASTER_STYLES[to];
  for (const [id, spec] of Object.entries(newStyle.sources)) {
    map.addSource(id, spec as maplibregl.SourceSpecification);
  }
  for (const layer of newStyle.layers) {
    map.addLayer(layer as maplibregl.LayerSpecification, firstOverlayId);
  }
}

// ── CAIC detail popup HTML ─────────────────────────────────────────────────────

const _DANGER_COLORS: Record<string, string> = {
  low: "#00b200", moderate: "#f4e500", considerable: "#ff9933",
  high: "#d7191c", extreme: "#1a1a1a", noForecast: "#666",
};
const _DANGER_LABELS: Record<string, string> = {
  low: "Low", moderate: "Moderate", considerable: "Considerable",
  high: "High", extreme: "Extreme", noForecast: "No Rating",
};
const _ELEV_LABELS: Record<string, string> = { alp: "Alpine", tln: "Treeline", btl: "Below Treeline" };

function _dangerBadge(level: string): string {
  const c = _DANGER_COLORS[level] ?? "#666";
  const fg = level === "moderate" ? "#333" : "#fff";
  const lbl = _DANGER_LABELS[level] ?? level;
  return `<span style="background:${c};color:${fg};border-radius:3px;padding:1px 5px;font-size:10px;font-weight:700">${lbl}</span>`;
}

function _dangerTriangle(danger: { alp: string; tln: string; btl: string }): string {
  // Isoceles triangle W=60 H=60, divided into 3 equal-height bands.
  // At y=20: left=20, right=40. At y=40: left=10, right=50.
  const alp = _DANGER_COLORS[danger.alp] ?? "#666";
  const tln = _DANGER_COLORS[danger.tln] ?? "#666";
  const btl = _DANGER_COLORS[danger.btl] ?? "#666";
  return `<svg width="60" height="60" viewBox="0 0 60 60" style="flex-shrink:0">
    <polygon points="10,40 50,40 60,60 0,60" fill="${btl}"/>
    <polygon points="20,20 40,20 50,40 10,40" fill="${tln}"/>
    <polygon points="30,0 20,20 40,20" fill="${alp}"/>
    <text x="30" y="55" text-anchor="middle" font-size="7" fill="rgba(0,0,0,0.5)" font-family="sans-serif">BTL</text>
    <text x="30" y="35" text-anchor="middle" font-size="7" fill="rgba(0,0,0,0.5)" font-family="sans-serif">TLN</text>
    <text x="30" y="16" text-anchor="middle" font-size="7" fill="rgba(0,0,0,0.45)" font-family="sans-serif">ALP</text>
  </svg>`;
}

function _aspectRose(aspectElevations: string[], color: string): string {
  const cx = 20, cy = 20, ri = 6, ro = 17;
  const dirs = ["n","ne","e","se","s","sw","w","nw"];
  // Which base aspects are hit (any elevation)
  const hit = new Set(aspectElevations.map(ae => ae.split("_")[0]));
  const svgAngles: Record<string, number> = {
    n: -90, ne: -45, e: 0, se: 45, s: 90, sw: 135, w: 180, nw: 225
  };
  let paths = "";
  for (const dir of dirs) {
    const theta = (svgAngles[dir] * Math.PI) / 180;
    const half = (22.5 * Math.PI) / 180;
    const a1 = theta - half, a2 = theta + half;
    const x1i = cx + ri * Math.cos(a1), y1i = cy + ri * Math.sin(a1);
    const x2i = cx + ri * Math.cos(a2), y2i = cy + ri * Math.sin(a2);
    const x1o = cx + ro * Math.cos(a1), y1o = cy + ro * Math.sin(a1);
    const x2o = cx + ro * Math.cos(a2), y2o = cy + ro * Math.sin(a2);
    const f = (n: number) => n.toFixed(1);
    const fill = hit.has(dir) ? color : "rgba(255,255,255,0.1)";
    paths += `<path d="M${f(x1i)},${f(y1i)} A${ri},${ri} 0 0,1 ${f(x2i)},${f(y2i)} L${f(x2o)},${f(y2o)} A${ro},${ro} 0 0,0 ${f(x1o)},${f(y1o)} Z" fill="${fill}" stroke="rgba(255,255,255,0.15)" stroke-width="0.5"/>`;
  }
  return `<svg width="40" height="40" viewBox="0 0 40 40" style="flex-shrink:0">
    ${paths}
    <text x="${cx}" y="${cy - ro - 2}" text-anchor="middle" font-size="6" fill="rgba(255,255,255,0.5)" font-family="sans-serif">N</text>
  </svg>`;
}

interface CaicProblem {
  label: string; likelihood: string; size_min: string; size_max: string;
  aspects: string[]; elevations: string[]; aspect_elevations: string[];
}
interface CaicZoneDetail {
  forecaster: string; valid_date: string;
  danger: { alp: string; tln: string; btl: string };
  problems: CaicProblem[];
  link: string;
}

// Problem type → accent color for the aspect rose
const _PROBLEM_COLORS: Record<string, string> = {
  "Wet Loose": "#ff9933", "Wind Slab": "#4a90d9", "Storm Slab": "#9b59b6",
  "Persistent Slab": "#e05a2b", "Deep Persistent Slab": "#c0392b",
  "Cornice": "#f4e500", "Glide Avalanche": "#2ecc71",
};

function buildCaicDetailHtml(d: CaicZoneDetail): string {
  const meta = [
    d.forecaster ? `<span>${d.forecaster}</span>` : "",
    d.valid_date ? `<span style="color:#888">${d.valid_date}</span>` : "",
  ].filter(Boolean).join(" · ");

  const roseHtml = _dangerTriangle(d.danger);
  const roseLabels = (["alp","tln","btl"] as const).map((k) =>
    `<div style="font-size:11px;line-height:1.6">
      <span style="color:#aaa;font-size:10px">${_ELEV_LABELS[k]}</span><br/>
      ${_dangerBadge(d.danger[k])}
    </div>`
  ).join("");

  const dangerSection = `
    <div style="display:flex;gap:10px;align-items:center;margin:8px 0 10px">
      ${roseHtml}
      <div style="display:flex;flex-direction:column;gap:2px">${roseLabels}</div>
    </div>`;

  const problemsHtml = d.problems.length === 0 ? "" : `
    <div style="font-weight:700;font-size:10px;letter-spacing:.05em;color:#aaa;margin-bottom:4px">AVALANCHE PROBLEMS</div>
    ${d.problems.map((p) => {
      const color = _PROBLEM_COLORS[p.label] ?? "#e05a2b";
      const elevStr = p.elevations.map(e => _ELEV_LABELS[e] ?? e).join(", ");
      const sizeStr = p.size_min && p.size_max ? `Size ${p.size_min}–${p.size_max}` : "";
      return `<div style="display:flex;gap:8px;align-items:flex-start;margin-bottom:8px">
        ${_aspectRose(p.aspect_elevations, color)}
        <div style="line-height:1.5">
          <div style="font-weight:700;font-size:12px;color:${color}">${p.label}</div>
          <div style="font-size:11px">${[p.likelihood, sizeStr].filter(Boolean).join(" · ")}</div>
          ${p.aspects.length ? `<div style="font-size:10px;color:#aaa">${p.aspects.join(", ")}${elevStr ? ` · ${elevStr}` : ""}</div>` : ""}
        </div>
      </div>`;
    }).join("")}`;

  const linkHtml = `<a href="${d.link}" target="_blank" rel="noopener"
    style="color:#e05a2b;font-size:11px;text-decoration:none">CAIC Backcountry Forecast →</a>`;

  return `<div style="font-family:ui-sans-serif,system-ui,sans-serif;font-size:12px;color:#eee;line-height:1.4">
    <div style="font-weight:700;font-size:13px;margin-bottom:2px">CAIC Forecast</div>
    ${meta ? `<div style="font-size:10px;color:#888;margin-bottom:6px">${meta}</div>` : ""}
    ${dangerSection}
    ${problemsHtml}
    ${linkHtml}
  </div>`;
}

// ── SNOTEL popup HTML (plain HTML string for maplibregl.Popup) ─────────────────

function coerceNum(v: unknown): number | null {
  if (v == null || v === "null" || v === "") return null;
  const n = Number(v);
  return isNaN(n) || n === -9999 ? null : n;
}

function buildSnotelPopupHtml(p: Record<string, unknown>, units: Units): string {
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

// ── Map component ──────────────────────────────────────────────────────────────

export function Map({
  onLogout,
  stravaStatus,
  onStravaStatusChange,
}: {
  onLogout: () => void;
  stravaStatus: StravaStatus;
  onStravaStatusChange: () => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const searchMarkerRef = useRef<maplibregl.Marker | null>(null);

  const isMobile = useIsMobile();
  const [mobilePanelOpen, setMobilePanelOpen] = useState(false);

  const [dark, setDark] = useState(true);
  const [basemap, setBasemap] = useState<BasemapId>(() => {
    try {
      const s = localStorage.getItem("whumpf:basemap");
      if (s === "streets" || s === "topo" || s === "satellite" || s === "hybrid") return s;
    } catch { /* ignore */ }
    return "streets";
  });
  const [loadingLayers, setLoadingLayers] = useState<Set<string>>(new Set());
  const [visible, setVisible] = useState<Record<string, boolean>>(() => {
    const defaults = Object.fromEntries(OVERLAY_LAYERS.map((l) => [l.id, l.defaultVisible]));
    try {
      const stored = localStorage.getItem("whumpf:layer-visible");
      return stored ? { ...defaults, ...JSON.parse(stored) } : defaults;
    } catch { return defaults; }
  });
  const [opacity, setOpacity] = useState<Record<string, number>>(() => {
    const defaults = Object.fromEntries(OVERLAY_LAYERS.map((l) => [l.id, l.opacity]));
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
  const [units, setUnits] = useState<Units>("imperial");
  const [forecast, setForecast] = useState<ForecastPeriod[] | null>(null);
  const [forecastLoading, setForecastLoading] = useState(false);
  const [snotelLoaded, setSnotelLoaded] = useState(false);
  const [caicLoaded, setCaicLoaded] = useState(false);
  const [obsLoaded, setObsLoaded] = useState(false);
  const obsDataRef = useRef<object | null>(null);
  const [boundsLocked, setBoundsLocked] = useState(true);
  const [aboveHiresZoom, setAboveHiresZoom] = useState(INITIAL_ZOOM >= 13);
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
      center: INITIAL_CENTER,
      zoom: INITIAL_ZOOM,
      maxBounds: CO_MAX_BOUNDS,
      renderWorldCopies: false,
    });

    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "imperial" }), "bottom-left");

    // Single permanent handler for both initial load and every subsequent setStyle call.
    // All addXxx helpers are idempotent (guard on getSource), so re-firing is safe.
    map.on("style.load", () => {
      addOverlayLayers(map, visibleRef.current, opacityRef.current, {
        contours: [getContourUrl(contourIntervalRef.current)],
      });
      applyTerrainOrder(map, terrainOrderRef.current);
      addColoradoMask(map);
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
          fetch(`${TITILER_URL}/cog/point/${lng},${lat}?url=${encodeURIComponent(cogS3(`${REGION}/${fname}`))}`)
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
      fetchProfile(measurePts[0], measurePts[1])
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

  // When the contour interval changes, swap the MapLibre source+layer with the new tile URL.
  // Raster sources don't support in-place tile URL updates, so we remove and re-add.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.getLayer("contours")) return;
    const url = getContourUrl(contourInterval);
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
      bounds: COLORADO_MTN_BOUNDS,
      minzoom: 9,  // matches LAYER_GROUPS sourceMinzoom for contours
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
    map.setMaxBounds(boundsLocked ? CO_MAX_BOUNDS : null);
    setMaskVisibility(map, boundsLocked);
  }, [boundsLocked]);

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
    groups: LAYER_GROUPS,
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

      {/* Resolution pill — appears bottom-left when a terrain layer is using 1m data */}
      {aboveHiresZoom && HIRES_LAYER_IDS.some((id) => visible[id]) && (
        <div
          title="Viewing 1m high-resolution terrain data"
          style={{
            position: "fixed",
            bottom: isMobile ? mobileBottom + 4 : 28,
            left: 80,
            zIndex: 900,
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
        title={boundsLocked ? "Expand map beyond Colorado" : "Lock map to Colorado"}
        style={{
          position: "fixed",
          bottom: 28,
          right: 10,
          zIndex: 900,
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
        {boundsLocked ? "Colorado" : "Unlocked"}
      </button>
    </>
  );
}
