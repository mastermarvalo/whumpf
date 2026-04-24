import { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import { apiFetch } from "../auth";
import type { StravaStatus } from "../App";

const TITILER_URL = import.meta.env.VITE_TITILER_URL ?? "http://localhost:8001";
const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
const MINIO_BUCKET = "dem-cogs";
const REGION = "sanjuans";

const INITIAL_CENTER: [number, number] = [-105.5, 39.0];
const INITIAL_ZOOM = 7;
const COLORADO_MTN_BOUNDS: [number, number, number, number] = [-109.06, 37.0, -104.5, 41.0];

// "streets" = vector basemap (light or dark per theme toggle).
// The other three are raster basemaps independent of the theme.
type BasemapId = "streets" | "topo" | "satellite" | "hybrid";

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


// ── types ──────────────────────────────────────────────────────────────────────

interface Legend {
  gradient: string;
  stops: string[];
}

interface ActiveLayer {
  id: string;
  label: string;
  tiles: string[];
  kind?: "raster" | "geojson"; // default "raster"; geojson layers skip addOverlayLayers
  opacity: number;
  defaultVisible: boolean;
  noSlider?: boolean;
  legend?: Legend;
  // Extra MapLibre raster paint properties merged in at layer creation (e.g. saturation, resampling).
  blendPaint?: { "raster-saturation"?: number; "raster-resampling"?: "linear" | "nearest" };
}

interface UpcomingLayer {
  id: string;
  label: string;
}

type ActivityCardProps = {
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

interface SpotData {
  periods: ForecastPeriod[];
  tempF: number | null;
  snowDepthIn: number | null;
}

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

// ── terrain profile types ──────────────────────────────────────────────────────

interface SlopeSample {
  distance_m: number;
  elevation_m: number | null;
  slope_deg: number | null;
}

interface ProfileSummary {
  distance_m: number;
  avg_slope_deg: number | null;
  max_slope_deg: number | null;
  min_slope_deg: number | null;
  elevation_gain_m: number | null;
  elevation_loss_m: number | null;
  start_elevation_m: number | null;
  end_elevation_m: number | null;
}

interface ProfileResponse {
  summary: ProfileSummary;
  samples: SlopeSample[];
}

// ── forecast + units types ─────────────────────────────────────────────────────

type Units = "imperial" | "metric";

interface ForecastPeriod {
  name: string;
  temperature: number;
  temperatureUnit: string;
  windSpeed: string;
  windDirection: string;
  shortForecast: string;
  probabilityOfPrecipitation?: { value: number | null };
}

interface LayerGroup {
  id: string;
  label: string;
  color: string;
  active: ActiveLayer[];
  upcoming: UpcomingLayer[];
}

// ── layer definitions ──────────────────────────────────────────────────────────

const LAYER_GROUPS: LayerGroup[] = [
  {
    id: "terrain",
    label: "Terrain",
    color: "#a07850",
    active: [
      {
        id: "hillshade",
        label: "Hillshade",
        tiles: cogTiles(`${REGION}/hillshade.tif`),
        opacity: 0.7,
        defaultVisible: true,
      },
      {
        id: "slope",
        label: "Slope angle",
        // Served via API proxy which applies the CalTopo V1 colormap server-side.
        // Backend adds buffer=2 so TiTiler has neighbour context at tile edges.
        tiles: [`${API_URL}/tiles/slope/{z}/{x}/{y}?region=${REGION}`],
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
        // buffer=2: TiTiler fetches 2 extra pixels per edge so hue transitions
        // don't seam at tile boundaries when resampled.
        tiles: cogTiles(`${REGION}/aspect.tif`, {
          colormap_name: "hsv",
          rescale: "0,360",
          nodata: "-9999",
          buffer: "2",
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
    active: [],
    upcoming: [
      { id: "caic-danger", label: "CAIC Danger Rose" },
      { id: "caic-obs", label: "Field Observations" },
    ],
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

// ── theme ──────────────────────────────────────────────────────────────────────

type Theme = typeof THEMES.light;
const THEMES = {
  light: {
    panel: "rgba(255,255,255,0.95)",
    text: "#1a1a1a",
    muted: "#777",
    divider: "rgba(0,0,0,0.08)",
    soonBg: "rgba(0,0,0,0.06)",
    soonText: "#aaa",
    accent: "#4a90d9",
  },
  dark: {
    panel: "rgba(18,18,28,0.96)",
    text: "#e8e8e8",
    muted: "#666",
    divider: "rgba(255,255,255,0.08)",
    soonBg: "rgba(255,255,255,0.05)",
    soonText: "#555",
    accent: "#5ba3f0",
  },
};

// ── coord search ──────────────────────────────────────────────────────────────

function parseCoords(raw: string): [number, number] | null {
  const parts = raw.trim().split(/[\s,]+/).filter(Boolean);
  if (parts.length !== 2) return null;
  const [a, b] = parts.map(Number);
  if (isNaN(a) || isNaN(b)) return null;
  if (a >= -90 && a <= 90 && b >= -180 && b <= 180) return [a, b];
  return null;
}

function SearchBar({
  theme,
  onSearch,
}: {
  theme: Theme;
  onSearch: (lat: number, lon: number) => void;
}) {
  const [value, setValue] = useState("");
  const [error, setError] = useState(false);

  function submit() {
    const coords = parseCoords(value);
    if (!coords) { setError(true); return; }
    setError(false);
    onSearch(coords[0], coords[1]);
  }

  return (
    <div
      style={{
        position: "fixed",
        top: 10,
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: 1000,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        gap: 4,
      }}
    >
      <form
        onSubmit={(e) => { e.preventDefault(); submit(); }}
        style={{ display: "flex", gap: 4 }}
      >
        <input
          value={value}
          onChange={(e) => { setValue(e.target.value); setError(false); }}
          placeholder="lat, lon — e.g. 37.95, −107.75"
          style={{
            width: 260,
            padding: "7px 12px",
            borderRadius: 6,
            border: `1.5px solid ${error ? "#e05a2b" : theme.divider}`,
            background: theme.panel,
            color: theme.text,
            fontFamily: "ui-sans-serif, system-ui, sans-serif",
            fontSize: 13,
            outline: "none",
            boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
          }}
        />
        <button
          type="submit"
          style={{
            padding: "7px 13px",
            borderRadius: 6,
            border: "none",
            background: theme.accent,
            color: "#fff",
            fontFamily: "ui-sans-serif, system-ui, sans-serif",
            fontSize: 13,
            cursor: "pointer",
            boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
          }}
        >
          Go
        </button>
      </form>
      {error && (
        <div
          style={{
            background: theme.panel,
            color: "#e05a2b",
            fontSize: 11,
            padding: "3px 10px",
            borderRadius: 4,
            boxShadow: "0 1px 4px rgba(0,0,0,0.15)",
          }}
        >
          Enter lat, lon — e.g. 37.95, −107.75
        </div>
      )}
    </div>
  );
}

// ── point readout ──────────────────────────────────────────────────────────────

interface PointData {
  lon: number;
  lat: number;
  loading: boolean;
  elevation?: number;
  slope?: number;
  aspect?: number;
  tempF?: number | null;
  snowDepthIn?: number | null;
}

function aspectCompass(deg: number): string {
  return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(deg / 45) % 8];
}

// ── measure helpers ────────────────────────────────────────────────────────────

const MEASURE_MARKER_STYLE =
  "background:#e05a2b;color:#fff;border-radius:50%;width:22px;height:22px;" +
  "display:flex;align-items:center;justify-content:center;font-size:11px;" +
  "font-weight:700;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,0.3);cursor:default;";

function slopeColor(deg: number): string {
  if (deg < 15) return "#1a9641";
  if (deg < 27) return "#c8a800";
  if (deg < 40) return "#d7191c";
  return "#2b7bb9";
}

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
) {
  const firstLabelId = map.getStyle().layers.find((l) => l.type === "symbol")?.id;
  for (const layer of OVERLAY_LAYERS) {
    if (layer.kind === "geojson") continue; // managed separately
    map.addSource(layer.id, {
      type: "raster",
      tiles: layer.tiles,
      tileSize: 256,
      bounds: COLORADO_MTN_BOUNDS,
      minzoom: 6,
      maxzoom: 16,
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
      firstLabelId,
    );
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

  const [dark, setDark] = useState(true);
  const [basemap, setBasemap] = useState<BasemapId>("streets");
  const [visible, setVisible] = useState<Record<string, boolean>>(
    () => Object.fromEntries(OVERLAY_LAYERS.map((l) => [l.id, l.defaultVisible])),
  );
  const [opacity, setOpacity] = useState<Record<string, number>>(
    () => Object.fromEntries(OVERLAY_LAYERS.map((l) => [l.id, l.opacity])),
  );
  const [point, setPoint] = useState<PointData | null>(null);
  const [measureMode, setMeasureMode] = useState(false);
  const [measurePts, setMeasurePts] = useState<[number, number][]>([]);
  const [profile, setProfile] = useState<ProfileResponse | null>(null);
  const [profileLoading, setProfileLoading] = useState(false);
  const [units, setUnits] = useState<Units>("imperial");
  const [forecast, setForecast] = useState<ForecastPeriod[] | null>(null);
  const [forecastLoading, setForecastLoading] = useState(false);
  const [snotelLoaded, setSnotelLoaded] = useState(false);
  const [stravaVisible, setStravaVisible] = useState(true);
  const [stravaLoaded, setStravaLoaded] = useState(false);
  const stravaDataRef = useRef<object | null>(null);
  const selectedStravaIdRef = useRef<number | null>(null);
  const [stravaCard, setStravaCard] = useState<{ activities: ActivityCardProps[]; index: number } | null>(null);

  // Refs so style-load callbacks can read current state without stale closures.
  const visibleRef = useRef(visible);
  const opacityRef = useRef(opacity);
  useEffect(() => { visibleRef.current = visible; }, [visible]);
  useEffect(() => { opacityRef.current = opacity; }, [opacity]);

  const snotelDataRef = useRef<object | null>(null);
  const measureModeRef = useRef(false);
  const measurePtsRef = useRef<[number, number][]>([]);
  const measureMarkersRef = useRef<maplibregl.Marker[]>([]);
  const unitsRef = useRef<Units>("imperial");

  // Initialise map once.
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: getMapStyle("streets", true),
      center: INITIAL_CENTER,
      zoom: INITIAL_ZOOM,
      renderWorldCopies: false,
    });

    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "imperial" }), "bottom-left");

    map.on("load", () => {
      addOverlayLayers(map, visibleRef.current, opacityRef.current);
      addMeasureLayers(map);
      addSnotelLayers(map);
      setSnotelVisibility(map, visibleRef.current["snotel"] ?? false);
      addStravaLayers(map);
      if (stravaDataRef.current) setStravaData(map, stravaDataRef.current);
      applyStravaHighlight(map, selectedStravaIdRef.current);
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

      setPoint({ lon: lng, lat, loading: true });
      setForecast(null);
      setForecastLoading(true);

      const pick = (name: string) =>
        fetch(
          `${TITILER_URL}/cog/point/${lng},${lat}?url=${encodeURIComponent(cogS3(`${REGION}/${name}.tif`))}`,
        )
          .then((r) => (r.ok ? r.json() : null))
          .then((d) => d?.values?.[0] as number | undefined)
          .catch(() => undefined);

      const [[elevation, slope, aspect], spotData] = await Promise.all([
        Promise.all([pick("dem"), pick("slope"), pick("aspect")]),
        fetchSpotData(lat, lng).catch(() => ({ periods: [] as ForecastPeriod[], tempF: null, snowDepthIn: null })),
      ]);

      setPoint({ lon: lng, lat, loading: false, elevation, slope, aspect, tempF: spotData.tempF, snowDepthIn: spotData.snowDepthIn });
      setForecast(spotData.periods.length ? spotData.periods : null);
      setForecastLoading(false);
    });

    mapRef.current = map;
    return () => { map.remove(); mapRef.current = null; };
  }, []);

  // Switch basemap style when basemap or dark mode changes; re-add overlay layers after.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    map.setStyle(getMapStyle(basemap, dark));
    map.once("style.load", () => {
      addOverlayLayers(map, visibleRef.current, opacityRef.current);
      addMeasureLayers(map);
      updateMeasureSource(map, measurePtsRef.current);
      addSnotelLayers(map);
      if (snotelDataRef.current) setSnotelData(map, snotelDataRef.current);
      setSnotelVisibility(map, visibleRef.current["snotel"] ?? false);
      addStravaLayers(map);
      if (stravaDataRef.current) setStravaData(map, stravaDataRef.current);
      applyStravaHighlight(map, selectedStravaIdRef.current);
    });
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
    }
  }, [visible]);

  // Sync opacity state → MapLibre.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    for (const [id, op] of Object.entries(opacity)) {
      if (map.getLayer(id)) map.setPaintProperty(id, "raster-opacity", op);
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

  // Fetch SNOTEL data the first time the layer is enabled.
  useEffect(() => {
    const map = mapRef.current;
    if (!visible["snotel"]) {
      setSnotelVisibility(map, false);
      return;
    }
    setSnotelVisibility(map, true);
    if (snotelLoaded) return;
    apiFetch(`${API_URL}/snowpack/stations`)
      .then((r) => (r.ok ? r.json() : null))
      .then((geojson) => {
        if (!geojson) return;
        snotelDataRef.current = geojson;
        setSnotelData(mapRef.current, geojson);
        setSnotelLoaded(true);
      })
      .catch((err) => console.warn("SNOTEL fetch failed", err));
  }, [visible, snotelLoaded]);

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

  useEffect(() => {
    if (!stravaStatus.connected) {
      stravaDataRef.current = null;
      setStravaData(mapRef.current, { type: "FeatureCollection", features: [] });
      setStravaLoaded(false);
      return;
    }
    if (stravaLoaded) return;
    apiFetch(`${API_URL}/strava/activities`)
      .then((r) => (r.ok ? r.json() : null))
      .then((geojson) => {
        if (!geojson) return;
        stravaDataRef.current = geojson;
        setStravaData(mapRef.current, geojson);
        setStravaLoaded(true);
      })
      .catch((err) => console.warn("Strava fetch failed", err));
  }, [stravaStatus.connected, stravaLoaded]);

  const theme = dark ? THEMES.dark : THEMES.light;

  // Auto-enable hillshade at a sensible opacity when switching to a raster basemap.
  useEffect(() => {
    if (basemap !== "streets") {
      setVisible(v => ({ ...v, hillshade: true }));
      const op = basemap === "satellite" ? 0.4 : basemap === "hybrid" ? 0.35 : 0.45;
      setOpacity(o => ({ ...o, hillshade: op }));
    }
  }, [basemap]);

  function flyToCoords(lat: number, lon: number) {
    const map = mapRef.current;
    if (!map) return;
    map.flyTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), 13) });
    searchMarkerRef.current?.remove();
    searchMarkerRef.current = new maplibregl.Marker({ color: theme.accent })
      .setLngLat([lon, lat])
      .addTo(map);
  }

  return (
    <>
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />
      <SearchBar theme={theme} onSearch={flyToCoords} />
      <LayerPanel
        groups={LAYER_GROUPS}
        visible={visible}
        opacity={opacity}
        dark={dark}
        basemap={basemap}
        units={units}
        theme={theme}
        onToggle={(id) => setVisible((v) => ({ ...v, [id]: !v[id] }))}
        onOpacity={(id, val) => setOpacity((o) => ({ ...o, [id]: val }))}
        onDarkToggle={() => setDark(d => !d)}
        onBasemapChange={setBasemap}
        onUnitsToggle={() => setUnits((u) => (u === "imperial" ? "metric" : "imperial"))}
        onLogout={onLogout}
        stravaStatus={stravaStatus}
        stravaVisible={stravaVisible}
        onStravaToggle={() => setStravaVisible((v) => !v)}
        onStravaConnect={async () => {
          const r = await apiFetch(`${API_URL}/auth/strava/authorize`);
          if (r.ok) { const { url } = await r.json(); window.location.href = url; }
        }}
        onStravaDisconnect={async () => {
          await apiFetch(`${API_URL}/auth/strava/disconnect`, { method: "DELETE" });
          setStravaLoaded(false);
          onStravaStatusChange();
        }}
      />
      <MeasureButton
        active={measureMode}
        theme={theme}
        onToggle={() => setMeasureMode((m) => !m)}
      />
      {measureMode && (
        <MeasurePanel
          pts={measurePts}
          loading={profileLoading}
          profile={profile}
          units={units}
          theme={theme}
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
          onClose={() => { setPoint(null); setForecast(null); }}
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
        />
      )}
    </>
  );
}

// ── LayerPanel ─────────────────────────────────────────────────────────────────

function LayerPanel({
  groups,
  visible,
  opacity,
  dark,
  basemap,
  units,
  theme,
  onToggle,
  onOpacity,
  onDarkToggle,
  onBasemapChange,
  onUnitsToggle,
  onLogout,
  stravaStatus,
  stravaVisible,
  onStravaToggle,
  onStravaConnect,
  onStravaDisconnect,
}: {
  groups: LayerGroup[];
  visible: Record<string, boolean>;
  opacity: Record<string, number>;
  dark: boolean;
  basemap: BasemapId;
  units: Units;
  theme: Theme;
  onToggle: (id: string) => void;
  onOpacity: (id: string, val: number) => void;
  onDarkToggle: () => void;
  onBasemapChange: (id: BasemapId) => void;
  onUnitsToggle: () => void;
  onLogout: () => void;
  stravaStatus: StravaStatus;
  stravaVisible: boolean;
  onStravaToggle: () => void;
  onStravaConnect: () => void;
  onStravaDisconnect: () => void;
}) {
  return (
    <div
      style={{
        position: "fixed",
        top: 10,
        left: 10,
        background: theme.panel,
        borderRadius: 8,
        padding: "12px 14px",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: 13,
        color: theme.text,
        boxShadow: "0 2px 12px rgba(0,0,0,0.18)",
        display: "flex",
        flexDirection: "column",
        gap: 0,
        zIndex: 1000,
        width: 210,
        maxHeight: "calc(100vh - 20px)",
        overflowY: "auto",
        userSelect: "none",
      }}
    >
      {/* Header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 7,
        }}
      >
        <span style={{ fontWeight: 700, fontSize: 12, letterSpacing: "0.08em", color: theme.muted }}>
          LAYERS
        </span>
        <div style={{ display: "flex", gap: 4 }}>
          <button
            onClick={onUnitsToggle}
            title={`Switch to ${units === "imperial" ? "metric" : "imperial"}`}
            style={{
              background: "none",
              border: `1px solid ${theme.divider}`,
              borderRadius: 4,
              cursor: "pointer",
              fontSize: 10,
              fontWeight: 700,
              padding: "2px 5px",
              color: theme.muted,
              lineHeight: 1,
              letterSpacing: "0.04em",
            }}
          >
            {units === "imperial" ? "metric" : "imperial"}
          </button>
          <button
            onClick={onDarkToggle}
            title={dark ? "Switch to light mode" : "Switch to dark mode"}
            style={{
              background: "none",
              border: "none",
              cursor: "pointer",
              fontSize: 15,
              padding: "2px 4px",
              borderRadius: 4,
              color: theme.text,
              lineHeight: 1,
            }}
          >
            {dark ? "☀️" : "🌙"}
          </button>
        </div>
      </div>

      {/* Basemap picker — Streets follows the light/dark toggle; raster options are independent */}
      <div style={{ display: "flex", gap: 3, marginBottom: 10 }}>
        {(["streets", "topo", "satellite", "hybrid"] as const).map((id) => (
          <button
            key={id}
            onClick={() => onBasemapChange(id)}
            style={{
              flex: 1,
              padding: "3px 0",
              border: `1.5px solid ${basemap === id ? theme.accent : theme.divider}`,
              borderRadius: 4,
              background: basemap === id ? theme.accent : "none",
              color: basemap === id ? "#fff" : theme.muted,
              fontFamily: "ui-sans-serif, system-ui, sans-serif",
              fontSize: 9,
              fontWeight: basemap === id ? 700 : 400,
              cursor: "pointer",
              letterSpacing: "0.02em",
            }}
          >
            {id === "streets" ? "Streets" : id === "satellite" ? "Sat" : id === "hybrid" ? "Hyb" : "Topo"}
          </button>
        ))}
      </div>

      {/* Groups */}
      {groups.map((group, gi) => (
        <div key={group.id} style={{ marginBottom: gi < groups.length - 1 ? 12 : 0 }}>
          {/* Group header */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              marginBottom: 6,
              paddingBottom: 5,
              borderBottom: `1px solid ${theme.divider}`,
            }}
          >
            <span
              style={{
                width: 7,
                height: 7,
                borderRadius: "50%",
                background: group.color,
                flexShrink: 0,
              }}
            />
            <span
              style={{
                fontSize: 11,
                fontWeight: 600,
                letterSpacing: "0.06em",
                color: theme.muted,
                textTransform: "uppercase",
              }}
            >
              {group.label}
            </span>
          </div>

          {/* Active layers */}
          {group.active.map((layer) => (
            <div key={layer.id} style={{ marginBottom: 8 }}>
              <label
                style={{ display: "flex", alignItems: "center", gap: 7, cursor: "pointer", marginBottom: 3 }}
              >
                <input
                  type="checkbox"
                  checked={visible[layer.id] ?? false}
                  onChange={() => onToggle(layer.id)}
                  style={{ accentColor: group.color, cursor: "pointer" }}
                />
                <span>{layer.label}</span>
              </label>
              {!layer.noSlider && (
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={opacity[layer.id] ?? layer.opacity}
                  onChange={(e) => onOpacity(layer.id, parseFloat(e.target.value))}
                  style={{ width: "100%", accentColor: group.color, margin: 0, display: "block" }}
                />
              )}
              {layer.legend && visible[layer.id] && (
                <div style={{ marginTop: 4 }}>
                  <div style={{ height: 7, borderRadius: 3, background: layer.legend.gradient }} />
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      fontSize: 10,
                      color: theme.muted,
                      marginTop: 2,
                    }}
                  >
                    {layer.legend.stops.map((s, i) => <span key={i}>{s}</span>)}
                  </div>
                </div>
              )}
            </div>
          ))}

          {/* Upcoming layers */}
          {group.upcoming.map((layer) => (
            <div
              key={layer.id}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                marginBottom: 5,
                opacity: 0.5,
              }}
            >
              <span style={{ display: "flex", alignItems: "center", gap: 7 }}>
                <span
                  style={{
                    width: 13,
                    height: 13,
                    borderRadius: 2,
                    border: `1.5px solid ${theme.muted}`,
                    flexShrink: 0,
                  }}
                />
                <span style={{ color: theme.text }}>{layer.label}</span>
              </span>
              <span
                style={{
                  fontSize: 9,
                  fontWeight: 600,
                  letterSpacing: "0.05em",
                  color: theme.muted,
                  background: theme.soonBg,
                  borderRadius: 3,
                  padding: "2px 5px",
                }}
              >
                SOON
              </span>
            </div>
          ))}
        </div>
      ))}

      {/* Strava section */}
      <div style={{ marginTop: 12, paddingTop: 10, borderTop: `1px solid ${theme.divider}` }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="#fc4c02" style={{ flexShrink: 0 }}>
            <path d="M15.387 17.944l-2.089-4.116h-3.065L15.387 24l5.15-10.172h-3.066m-7.008-5.599l2.836 5.598h4.172L10.463 0l-7 13.828h4.169" />
          </svg>
          <span style={{ fontSize: 11, fontWeight: 600, letterSpacing: "0.06em", color: theme.muted, textTransform: "uppercase" }}>
            Strava
          </span>
        </div>
        {stravaStatus.connected ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <label style={{ display: "flex", alignItems: "center", gap: 7, cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={stravaVisible}
                onChange={onStravaToggle}
                style={{ accentColor: "#fc4c02", cursor: "pointer" }}
              />
              <span style={{ fontSize: 13 }}>Activities</span>
            </label>
            <div style={{ fontSize: 11, color: theme.muted }}>
              {stravaStatus.athlete_name ?? "Connected"}
            </div>
            <button
              onClick={onStravaDisconnect}
              style={{
                padding: "4px 0",
                border: `1px solid ${theme.divider}`,
                borderRadius: 4,
                background: "none",
                color: theme.muted,
                fontFamily: "ui-sans-serif, system-ui, sans-serif",
                fontSize: 11,
                cursor: "pointer",
              }}
            >
              Disconnect
            </button>
          </div>
        ) : (
          <button
            onClick={onStravaConnect}
            style={{
              width: "100%",
              padding: "6px 0",
              border: "none",
              borderRadius: 5,
              background: "#fc4c02",
              color: "#fff",
              fontFamily: "ui-sans-serif, system-ui, sans-serif",
              fontSize: 12,
              fontWeight: 600,
              cursor: "pointer",
              letterSpacing: "0.02em",
            }}
          >
            Connect Strava
          </button>
        )}
      </div>

      {/* Sign out */}
      <button
        onClick={onLogout}
        style={{
          marginTop: 10,
          width: "100%",
          padding: "6px 0",
          border: `1px solid ${theme.divider}`,
          borderRadius: 5,
          background: "none",
          color: theme.muted,
          fontFamily: "ui-sans-serif, system-ui, sans-serif",
          fontSize: 11,
          cursor: "pointer",
          letterSpacing: "0.04em",
        }}
      >
        Sign out
      </button>
    </div>
  );
}

// ── InfoPanel ──────────────────────────────────────────────────────────────────

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

  return `<div style="font-family:ui-sans-serif,system-ui,sans-serif;font-size:13px;min-width:180px;color:#1a1a1a;background:#fff;padding:2px">
    <div style="font-weight:700;margin-bottom:6px;color:#1a1a1a">${p.name ?? "Station"}</div>
    <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
      <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${color};border:1.5px solid #fff;box-shadow:0 0 0 1px #ccc"></span>
      <span style="color:${color};font-weight:600">${pctStr}</span>
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <tr><td style="color:#777;padding:2px 0">SWE</td><td style="text-align:right;font-weight:600;color:#1a1a1a">${sweStr}</td></tr>
      <tr><td style="color:#777;padding:2px 0">Snow depth</td><td style="text-align:right;font-weight:600;color:#1a1a1a">${depthStr}</td></tr>
      <tr><td style="color:#777;padding:2px 0">Temperature</td><td style="text-align:right;font-weight:600;color:#1a1a1a">${tempStr}</td></tr>
      <tr><td style="color:#777;padding:2px 0">Elevation</td><td style="text-align:right;color:#1a1a1a">${elevStr}</td></tr>
    </table>
    <div style="color:#aaa;font-size:10px;margin-top:6px">Updated ${p.updated ?? "—"}</div>
  </div>`;
}

// ── InfoPanel ──────────────────────────────────────────────────────────────────

function InfoPanel({
  data,
  forecast,
  forecastLoading,
  units,
  theme,
  onClose,
}: {
  data: PointData;
  forecast: ForecastPeriod[] | null;
  forecastLoading: boolean;
  units: Units;
  theme: Theme;
  onClose: () => void;
}) {
  const imp = units === "imperial";
  const fmt = (n: number | undefined, dec = 0) =>
    n == null || n === -9999 ? "—" : n.toFixed(dec);

  const elevM = data.elevation != null && data.elevation !== -9999 ? data.elevation : null;
  const elevStr = elevM != null
    ? imp
      ? `${(elevM * 3.28084).toFixed(0)} ft`
      : `${elevM.toFixed(0)} m`
    : "—";

  return (
    <div
      style={{
        position: "fixed",
        bottom: 36,
        left: "50%",
        transform: "translateX(-50%)",
        background: theme.panel,
        borderRadius: 8,
        padding: "10px 16px",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: 13,
        color: theme.text,
        boxShadow: "0 2px 12px rgba(0,0,0,0.24)",
        zIndex: 1000,
        maxWidth: "calc(100vw - 40px)",
        minWidth: 320,
      }}
    >
      {/* Terrain row */}
      <div style={{ display: "flex", alignItems: "center", gap: 18, flexWrap: "wrap" }}>
        {data.loading ? (
          <span style={{ color: theme.muted }}>Loading…</span>
        ) : (
          <>
            <span><b>Elev</b> {elevStr}</span>
            <span><b>Slope</b> {fmt(data.slope, 1)}°</span>
            <span>
              <b>Aspect</b>{" "}
              {data.aspect != null && data.aspect !== -9999
                ? `${fmt(data.aspect, 0)}° ${aspectCompass(data.aspect)}`
                : "—"}
            </span>
            {data.tempF != null && (
              <span>
                <b>Temp</b>{" "}
                {imp
                  ? `${Math.round(data.tempF)}°F`
                  : `${Math.round((data.tempF - 32) * 5 / 9)}°C`}
              </span>
            )}
            {data.snowDepthIn != null && data.snowDepthIn > 0 && (
              <span>
                <b>Snow</b>{" "}
                {imp
                  ? `${data.snowDepthIn.toFixed(0)}"`
                  : `${Math.round(data.snowDepthIn * 2.54)} cm`}
              </span>
            )}
            <span style={{ color: theme.muted, fontSize: 11 }}>
              {data.lat.toFixed(4)}°, {data.lon.toFixed(4)}°
            </span>
          </>
        )}
        <button
          onClick={onClose}
          style={{
            marginLeft: "auto",
            background: "none",
            border: "none",
            cursor: "pointer",
            color: theme.muted,
            fontSize: 16,
            lineHeight: 1,
            padding: 0,
            flexShrink: 0,
          }}
        >
          ×
        </button>
      </div>

      {/* Forecast rows */}
      {forecastLoading && (
        <div style={{ marginTop: 8, color: theme.muted, fontSize: 12 }}>Loading forecast…</div>
      )}
      {!forecastLoading && forecast && forecast.length > 0 && (
        <div style={{ marginTop: 8, borderTop: `1px solid ${theme.divider}`, paddingTop: 8 }}>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: `repeat(${Math.min(forecast.length, 4)}, 1fr)`,
              gap: "6px 10px",
            }}
          >
            {forecast.slice(0, 8).map((p, i) => {
              const tempVal = p.temperature;
              const tempStr = imp
                ? `${tempVal}°F`
                : `${((tempVal - 32) * 5 / 9).toFixed(0)}°C`;
              // NWS wind is always mph strings like "15 mph" or "10 to 20 mph"
              const windStr = imp
                ? p.windSpeed
                : p.windSpeed.replace(/\d+/g, (n) => String(Math.round(Number(n) * 1.60934))).replace("mph", "km/h");
              const precip = p.probabilityOfPrecipitation?.value;
              return (
                <div key={i} style={{ fontSize: 11 }}>
                  <div style={{ fontWeight: 700, color: theme.muted, marginBottom: 2 }}>
                    {p.name}
                  </div>
                  <div style={{ fontWeight: 600 }}>{tempStr}</div>
                  <div style={{ color: theme.muted }}>{windStr} {p.windDirection}</div>
                  {precip != null && (
                    <div style={{ color: "#4a90d9" }}>{precip}% precip</div>
                  )}
                  <div style={{ color: theme.muted, marginTop: 1 }}>{p.shortForecast}</div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

// ── MeasureButton ──────────────────────────────────────────────────────────────

function MeasureButton({
  active,
  theme,
  onToggle,
}: {
  active: boolean;
  theme: Theme;
  onToggle: () => void;
}) {
  return (
    <button
      onClick={onToggle}
      title={active ? "Exit slope measurement" : "Measure slope between two points"}
      style={{
        position: "fixed",
        bottom: 36,
        right: 10,
        zIndex: 1000,
        background: active ? theme.accent : theme.panel,
        color: active ? "#fff" : theme.text,
        border: `1.5px solid ${active ? theme.accent : theme.divider}`,
        borderRadius: 6,
        padding: "7px 12px",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: 12,
        fontWeight: 600,
        cursor: "pointer",
        boxShadow: "0 2px 8px rgba(0,0,0,0.18)",
        letterSpacing: "0.03em",
      }}
    >
      Measure Slope
    </button>
  );
}

// ── MeasurePanel ───────────────────────────────────────────────────────────────

function MeasurePanel({
  pts,
  loading,
  profile,
  units,
  theme,
  onClose,
}: {
  pts: [number, number][];
  loading: boolean;
  profile: ProfileResponse | null;
  units: Units;
  theme: Theme;
  onClose: () => void;
}) {
  const imp = units === "imperial";
  let content: React.ReactNode;

  if (loading) {
    content = <span style={{ color: theme.muted }}>Sampling terrain…</span>;
  } else if (profile) {
    const s = profile.summary;
    const distStr = imp
      ? `${(s.distance_m / 1609.344).toFixed(2)} mi`
      : `${(s.distance_m / 1000).toFixed(2)} km`;
    const gainStr = s.elevation_gain_m != null
      ? imp ? `+${Math.round(s.elevation_gain_m * 3.28084)} ft` : `+${Math.round(s.elevation_gain_m)} m`
      : null;
    const lossStr = s.elevation_loss_m != null
      ? imp ? `−${Math.round(s.elevation_loss_m * 3.28084)} ft` : `−${Math.round(s.elevation_loss_m)} m`
      : null;
    const avg = s.avg_slope_deg;
    content = (
      <>
        <span style={{ color: theme.muted, fontSize: 11 }}>A→B</span>
        <span><b>Dist</b> {distStr}</span>
        <span>
          <b>Avg slope</b>{" "}
          <span style={{ color: avg != null ? slopeColor(avg) : theme.muted, fontWeight: 700 }}>
            {avg != null ? `${avg.toFixed(1)}°` : "—"}
          </span>
        </span>
        <span><b>Max</b> {s.max_slope_deg != null ? `${s.max_slope_deg.toFixed(1)}°` : "—"}</span>
        {gainStr && <span style={{ color: "#1a9641" }}>{gainStr}</span>}
        {lossStr && <span style={{ color: theme.muted }}>{lossStr}</span>}
      </>
    );
  } else if (pts.length === 1) {
    content = <span style={{ color: theme.muted }}>Click map to set end point (B)</span>;
  } else {
    content = <span style={{ color: theme.muted }}>Click map to set start point (A)</span>;
  }

  return (
    <div
      style={{
        position: "fixed",
        bottom: 36,
        left: "50%",
        transform: "translateX(-50%)",
        background: theme.panel,
        borderRadius: 8,
        padding: "9px 16px",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: 13,
        color: theme.text,
        boxShadow: "0 2px 10px rgba(0,0,0,0.22)",
        display: "flex",
        alignItems: "center",
        gap: 16,
        zIndex: 1000,
        whiteSpace: "nowrap",
      }}
    >
      {content}
      <button
        onClick={onClose}
        style={{
          background: "none",
          border: "none",
          cursor: "pointer",
          color: theme.muted,
          fontSize: 16,
          lineHeight: 1,
          padding: "0 0 0 4px",
        }}
      >
        ×
      </button>
    </div>
  );
}

// ── StravaActivityCard ─────────────────────────────────────────────────────────

function StravaActivityCard({
  activities,
  index,
  onIndexChange,
  onClose,
  units,
  theme,
}: {
  activities: ActivityCardProps[];
  index: number;
  onIndexChange: (i: number) => void;
  onClose: () => void;
  units: Units;
  theme: Theme;
}) {
  const act = activities[index];
  const descCache = useRef<Record<number, string | null>>({});
  const [description, setDescription] = useState<string | null | "loading">("loading");

  useEffect(() => {
    if (act.id in descCache.current) {
      setDescription(descCache.current[act.id]);
      return;
    }
    setDescription("loading");
    apiFetch(`${API_URL}/strava/activities/${act.id}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        const desc = (data?.description as string | null) ?? null;
        descCache.current[act.id] = desc;
        setDescription(desc);
        if (data?.photo_url && !act.photo_url) {
          act.photo_url = data.photo_url as string;
        }
      })
      .catch(() => {
        descCache.current[act.id] = null;
        setDescription(null);
      });
  }, [act.id]);

  const dist = units === "imperial"
    ? `${(act.distance_m * 0.000621371).toFixed(1)} mi`
    : `${(act.distance_m / 1000).toFixed(1)} km`;

  const elapsed = (() => {
    const h = Math.floor(act.elapsed_time_s / 3600);
    const m = Math.floor((act.elapsed_time_s % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  })();

  const elev = units === "imperial"
    ? `+${Math.round(act.total_elevation_gain_m * 3.28084).toLocaleString()} ft`
    : `+${Math.round(act.total_elevation_gain_m).toLocaleString()} m`;

  const date = act.start_date
    ? new Date(act.start_date).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
    : "";

  const sportLabel = act.sport_type.replace(/([A-Z])/g, " $1").trim();

  return (
    <div
      style={{
        position: "fixed",
        bottom: 80,
        right: 10,
        width: 300,
        background: theme.panel,
        borderRadius: 10,
        boxShadow: "0 4px 24px rgba(0,0,0,0.28)",
        overflow: "hidden",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        zIndex: 1000,
        color: theme.text,
      }}
    >
      {act.photo_url && (
        <img
          src={act.photo_url}
          alt=""
          style={{ width: "100%", height: 160, objectFit: "cover", display: "block" }}
          onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
        />
      )}

      <div style={{ padding: "12px 14px" }}>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 6 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <span style={{
              display: "inline-block",
              background: act.color,
              color: "#fff",
              fontSize: 10,
              fontWeight: 700,
              padding: "2px 7px",
              borderRadius: 4,
              letterSpacing: "0.05em",
              marginBottom: 5,
              textTransform: "uppercase",
            }}>
              {sportLabel}
            </span>
            <div style={{ fontWeight: 700, fontSize: 14, lineHeight: 1.3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {act.name}
            </div>
            <div style={{ fontSize: 12, color: theme.muted, marginTop: 2 }}>{date}</div>
          </div>
          <button
            onClick={onClose}
            style={{ background: "none", border: "none", cursor: "pointer", color: theme.muted, fontSize: 18, lineHeight: 1, padding: "0 0 0 8px", flexShrink: 0 }}
          >×</button>
        </div>

        <div style={{
          display: "flex",
          borderTop: `1px solid ${theme.divider}`,
          borderBottom: `1px solid ${theme.divider}`,
          padding: "8px 0",
          margin: "8px 0",
          textAlign: "center",
        }}>
          {([
            { label: "Distance", value: dist },
            { label: "Time", value: elapsed },
            { label: "Elevation", value: elev },
          ] as const).map(({ label, value }, i) => (
            <div key={label} style={{ flex: 1, borderLeft: i > 0 ? `1px solid ${theme.divider}` : "none", padding: "0 6px" }}>
              <div style={{ fontSize: 14, fontWeight: 700 }}>{value}</div>
              <div style={{ fontSize: 10, color: theme.muted, textTransform: "uppercase", letterSpacing: "0.05em", marginTop: 1 }}>{label}</div>
            </div>
          ))}
        </div>

        {description === "loading" && (
          <div style={{ fontSize: 12, color: theme.muted, marginBottom: 6 }}>Loading…</div>
        )}
        {description && description !== "loading" && (
          <div style={{ fontSize: 12, lineHeight: 1.5, marginBottom: 6, maxHeight: 80, overflowY: "auto", color: theme.text }}>
            {description}
          </div>
        )}

        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <a
            href={`https://www.strava.com/activities/${act.id}`}
            target="_blank"
            rel="noopener noreferrer"
            style={{ fontSize: 12, color: "#FC4C02", textDecoration: "none", fontWeight: 600 }}
          >
            View on Strava ↗
          </a>
          {activities.length > 1 && (
            <div style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: theme.muted }}>
              <button
                onClick={() => onIndexChange(index - 1)}
                disabled={index === 0}
                style={{ background: "none", border: "none", cursor: index === 0 ? "default" : "pointer", color: index === 0 ? theme.muted : theme.text, fontSize: 16, padding: "0 2px", lineHeight: 1 }}
              >‹</button>
              <span>{index + 1} / {activities.length}</span>
              <button
                onClick={() => onIndexChange(index + 1)}
                disabled={index === activities.length - 1}
                style={{ background: "none", border: "none", cursor: index === activities.length - 1 ? "default" : "pointer", color: index === activities.length - 1 ? theme.muted : theme.text, fontSize: 16, padding: "0 2px", lineHeight: 1 }}
              >›</button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
