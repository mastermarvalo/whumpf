import maplibregl from "maplibre-gl";
import { API_URL } from "../constants";
import type { ActiveLayer, LayerGroup } from "../types";
import { cogTiles } from "./basemaps";

// ── weather / snowpack provider config ────────────────────────────────────────
// Swap any URL string here to change the underlying data source.
// MapLibre replaces {bbox-epsg-3857} with the tile's bbox (west,south,east,north, EPSG:3857).
const _NWS = "https://mapservices.weather.noaa.gov/raster/rest/services";
// ArcGIS MapServer/export and ImageServer/exportImage share these params
const _AGS = "bboxSR=3857&imageSR=3857&size=256,256&f=image&format=png32&transparent=true";
// NDFD_temp layers: 9=+3hr (earliest with data), 0=TempF_24Hr forecast
// NOHRSC_Snow_Analysis layers: 0=Snow Depth
// RainViewer radar: proper XYZ tiles, ~2h of past frames + up to 3h nowcast.
// The path token (/v2/radar/<hash>) is fetched fresh in Map.tsx and set via
// setTiles() — the URL here is only used if RainViewer hasn't loaded yet.
export const RV_HOST = "https://tilecache.rainviewer.com";
export const RV_TILE_SUFFIX = "/256/2/1_1/{z}/{x}/{y}.png"; // color=2 (universal blue), smooth+snow=1_1

const WEATHER_SOURCES = {
  // NDFD +3hr temperature (ArcGIS — layer index, not time-scrubable via TIME param)
  tempCurrent:  `${_NWS}/NDFD/NDFD_temp/MapServer/export?bbox={bbox-epsg-3857}&${_AGS}&layers=show:9`,
  // NDFD 24-hr temperature forecast
  tempForecast: `${_NWS}/NDFD/NDFD_temp/MapServer/export?bbox={bbox-epsg-3857}&${_AGS}&layers=show:0`,
  // Radar: RainViewer XYZ tiles (path updated at runtime from their API).
  // Fallback to NWS WMS if RainViewer hasn't loaded yet.
  precipRadar:  `${RV_HOST}/v2/radar/nowrap/256/2/1_1/{z}/{x}/{y}.png`,
  // MRMS QPE — ArcGIS ImageServer, NOT time-aware (timeInfo: null); static current layer.
  precipAccum:  `${_NWS}/obs/mrms_qpe/ImageServer/exportImage?bbox={bbox-epsg-3857}&${_AGS}`,
  // NOHRSC Snow Analysis — ArcGIS MapServer, NOT time-aware (timeInfo: null); static daily layer.
  snowDepth:    `${_NWS}/snow/NOHRSC_Snow_Analysis/MapServer/export?bbox={bbox-epsg-3857}&${_AGS}&layers=show:0`,
};

// ── layer definitions ──────────────────────────────────────────────────────────

export const TERRAIN_LAYER_IDS = ["hillshade", "slope", "aspect"];

// Layers that use 1m DEM data at zoom >= 13 (either via hiresTiles companion or server-side).
export const HIRES_LAYER_IDS = TERRAIN_LAYER_IDS;

/**
 * Build the layer-group registry for a given region. Terrain layers point at
 * `s3://dem-cogs/<regionId>/...` and the API's region-scoped tile endpoints;
 * non-terrain layers (snowpack/avalanche/weather) are region-agnostic.
 *
 * Called once per region change in Map.tsx via useMemo, so the result is
 * stable across renders for a fixed region.
 */
export function buildLayerGroups(regionId: string): LayerGroup[] {
  return [
    {
      id: "reference",
      label: "Reference",
      color: "#888",
      active: [
        {
          id: "streets",
          label: "Streets",
          kind: "vector_overlay",
          tiles: [],
          opacity: 0.9,
          defaultVisible: false,
        },
        {
          id: "trails",
          label: "Trails & peaks",
          kind: "vector_overlay",
          tiles: [],
          opacity: 0.9,
          defaultVisible: false,
        },
        {
          id: "ski-runs",
          label: "Ski resort runs",
          tiles: ["https://tiles.opensnowmap.org/pistes/{z}/{x}/{y}.png"],
          opacity: 0.85,
          defaultVisible: false,
        },
      ],
      upcoming: [],
    },
    {
      id: "terrain",
      label: "Terrain",
      color: "#a07850",
      reorderable: true,
      active: [
        {
          id: "hillshade",
          label: "Hillshade",
          tiles: cogTiles(`${regionId}/hillshade.tif`),
          hiresTiles: cogTiles(`${regionId}/hillshade_hires.tif`),
          opacity: 0.7,
          defaultVisible: true,
        },
        {
          id: "slope",
          label: "Slope angle",
          // Served via API proxy which applies the CalTopo V1 colormap server-side.
          // Backend adds buffer=2 so TiTiler has neighbour context at tile edges.
          tiles: [`${API_URL}/tiles/slope/{z}/{x}/{y}?region=${regionId}`],
          hiresTiles: [`${API_URL}/tiles/slope/{z}/{x}/{y}?region=${regionId}&hires=true`],
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
          tiles: cogTiles(`${regionId}/aspect.tif`, {
            colormap_name: "hsv",
            rescale: "0,360",
            nodata: "-9999",
            buffer: "2",
            tilesize: "512",
          }),
          hiresTiles: cogTiles(`${regionId}/aspect_hires.tif`, {
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
          id: "terrain-filter",
          label: "Slope filter",
          // Tiles are populated dynamically — Map.tsx replaces the source
          // every time the user changes aspects or slope range. The default
          // values here match the panel's initial state for first render.
          tiles: [`${API_URL}/tiles/terrain_filter/{z}/{x}/{y}?region=${regionId}&slope_min=30&slope_max=45&aspects=N,NE,E,SE,S,SW,W,NW`],
          opacity: 0.6,
          defaultVisible: false,
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
          label: "Precip radar",
          tiles: [WEATHER_SOURCES.precipRadar],
          opacity: 0.8,
          defaultVisible: false,
          noSlider: true,
          timeEnabled: true,
          timeFmt: "rainviewer",
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
          // ArcGIS ImageServer is NOT time-aware (timeInfo: null) — static current-frame layer.
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
          // ArcGIS MapServer is NOT time-aware (timeInfo: null) — static daily layer.
          legend: {
            gradient: "linear-gradient(to right, #60c0c0, #60a0c0, #4060c0, #2020c0, #101080)",
            stops: ["Trace", "6\"", "24\"", "48\"", "72\"+"],
          },
        },
      ],
      upcoming: [],
    },
  ];
}

export function buildOverlayLayers(groups: LayerGroup[]): ActiveLayer[] {
  return groups.flatMap((g) => g.active);
}

/**
 * `bounds` is the region content extent; raster sources cap their requests
 * to that bbox so the map doesn't fire tile fetches for areas without data.
 */
export function addOverlayLayers(
  map: maplibregl.Map,
  layers: ActiveLayer[],
  bounds: [number, number, number, number],
  visible: Record<string, boolean>,
  opacity: Record<string, number>,
  tileOverrides?: Record<string, string[]>,
) {
  // On vector basemaps (streets), insert overlays before the first symbol
  // layer so basemap labels stay on top. On plain raster basemaps (topo,
  // satellite) there are no symbol layers — overlays just append to top.
  const beforeId: string | undefined =
    map.getStyle()?.layers?.find((l) => l.type === "symbol")?.id;
  for (const layer of layers) {
    // Both geojson and vector_overlay layers manage their own source/layers
    // in dedicated helper modules — addOverlayLayers only handles rasters.
    if (layer.kind === "geojson" || layer.kind === "vector_overlay") continue;
    if (map.getSource(layer.id)) continue;  // already present (e.g. double style.load)
    const tiles = tileOverrides?.[layer.id] ?? layer.tiles;
    map.addSource(layer.id, {
      type: "raster",
      tiles,
      tileSize: 256,
      bounds,
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
          bounds,
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

export function applyTerrainOrder(map: maplibregl.Map, order: string[]) {
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
