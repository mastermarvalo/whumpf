// Ski runs, lifts and ski area outlines — our own OSM-derived vector tiles,
// built weekly (make ski-tiles-refresh) and proxied by the API at /tiles/ski.
// Replaces the OpenSnowMap raster stopgap.
//
// Source layers and fields (all strings):
//   ski_runs   piste_type (downhill|nordic|skitour), difficulty, grooming, name, osm_id, osm_type
//              lines, plus polygons for runs mapped as areas
//   ski_lifts  lift_type, name, osm_id, osm_type
//   ski_areas  name, osm_id, osm_type — polygon outline + a point for the label

import maplibregl from "maplibre-gl";
import { API_URL } from "../constants";

const SOURCE_ID = "ski-source";

export const SKI_LAYER_IDS = [
  "ski-area-outline",
  "ski-run-area",
  "ski-run-case",
  "ski-run",
  "ski-trail",
  "ski-lift",
  "ski-run-label",
  "ski-lift-label",
  "ski-area-label",
] as const;

// North American trail signage; freeride/extreme are off-piste-ish so orange/red.
const DIFFICULTY_COLOR: maplibregl.ExpressionSpecification = [
  "match", ["get", "difficulty"],
  ["novice", "easy"], "#2e9e45",
  "intermediate", "#1f6fd1",
  ["advanced", "expert"], "#111111",
  "freeride", "#ff8a00",
  "extreme", "#d62020",
  "#7a7a7a",
];

// Match the Multi* types too: tiles carry MultiLineStrings where a way crosses back into a tile.
const IS_LINE: maplibregl.ExpressionSpecification =
  ["match", ["geometry-type"], ["LineString", "MultiLineString"], true, false];
const IS_POLYGON: maplibregl.ExpressionSpecification =
  ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false];
const IS_POINT: maplibregl.ExpressionSpecification =
  ["match", ["geometry-type"], ["Point", "MultiPoint"], true, false];
const DOWNHILL: maplibregl.ExpressionSpecification = ["==", ["get", "piste_type"], "downhill"];

export function skiTiles(regionId: string): string[] {
  return [`${API_URL}/tiles/ski/{z}/{x}/{y}?region=${encodeURIComponent(regionId)}`];
}

export function addSkiLayers(
  map: maplibregl.Map,
  regionId: string,
  opacity: number,
  beforeId?: string,
): void {
  if (map.getLayer("ski-run")) return;
  if (!map.getSource(SOURCE_ID)) {
    map.addSource(SOURCE_ID, {
      type: "vector",
      tiles: skiTiles(regionId),
      minzoom: 8,
      maxzoom: 14, // overzoom past 14
      attribution: '<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors</a>',
    });
  }
  const lineCapJoin = { "line-cap": "round" as const, "line-join": "round" as const };

  map.addLayer({
    id: "ski-area-outline",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "ski_areas",
    filter: IS_POLYGON,
    paint: {
      "line-color": "#d9d9ff",
      "line-width": ["interpolate", ["linear"], ["zoom"], 8, 0.8, 14, 1.5],
      "line-dasharray": [3, 2],
      "line-opacity": opacity * 0.8,
    },
  }, beforeId);

  // Runs mapped as areas: a light wash in the difficulty colour.
  map.addLayer({
    id: "ski-run-area",
    type: "fill",
    source: SOURCE_ID,
    "source-layer": "ski_runs",
    filter: IS_POLYGON,
    paint: {
      "fill-color": DIFFICULTY_COLOR,
      "fill-opacity": opacity * 0.2,
    },
  }, beforeId);

  // White casing keeps black (advanced/expert) runs readable on hillshade.
  map.addLayer({
    id: "ski-run-case",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "ski_runs",
    filter: ["all", IS_LINE, DOWNHILL],
    layout: lineCapJoin,
    paint: {
      "line-color": "#ffffff",
      "line-width": ["interpolate", ["linear"], ["zoom"], 11, 2, 16, 5],
      "line-opacity": opacity * 0.7,
    },
  }, beforeId);
  map.addLayer({
    id: "ski-run",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "ski_runs",
    filter: ["all", IS_LINE, DOWNHILL],
    layout: lineCapJoin,
    paint: {
      "line-color": DIFFICULTY_COLOR,
      "line-width": ["interpolate", ["linear"], ["zoom"], 11, 1, 16, 3],
      "line-opacity": opacity,
    },
  }, beforeId);
  // Nordic and skitour trails dashed, so they read differently from pistes.
  // A separate layer: line-dasharray can't be data-driven.
  map.addLayer({
    id: "ski-trail",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "ski_runs",
    filter: ["all", IS_LINE, ["!", DOWNHILL]],
    paint: {
      "line-color": DIFFICULTY_COLOR,
      "line-width": ["interpolate", ["linear"], ["zoom"], 11, 1, 16, 2.5],
      "line-dasharray": [2, 1.5],
      "line-opacity": opacity,
    },
  }, beforeId);

  map.addLayer({
    id: "ski-lift",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "ski_lifts",
    paint: {
      "line-color": "#c0262d",
      "line-width": ["interpolate", ["linear"], ["zoom"], 10, 1, 16, 2.5],
      "line-opacity": opacity,
    },
  }, beforeId);

  const halo = { "text-halo-color": "#000", "text-halo-width": 1.5, "text-halo-blur": 0.5 };
  map.addLayer({
    id: "ski-run-label",
    type: "symbol",
    source: SOURCE_ID,
    "source-layer": "ski_runs",
    filter: IS_LINE,
    minzoom: 13,
    layout: {
      "text-field": ["coalesce", ["get", "name"], ""],
      "text-size": ["interpolate", ["linear"], ["zoom"], 13, 10, 17, 13],
      "text-font": ["Noto Sans Regular"],
      "symbol-placement": "line",
      "text-rotation-alignment": "map",
      "symbol-spacing": 250,
    },
    paint: { "text-color": "#fff", ...halo },
  }, beforeId);
  map.addLayer({
    id: "ski-lift-label",
    type: "symbol",
    source: SOURCE_ID,
    "source-layer": "ski_lifts",
    minzoom: 13,
    layout: {
      "text-field": ["coalesce", ["get", "name"], ""],
      "text-size": ["interpolate", ["linear"], ["zoom"], 13, 10, 17, 12],
      "text-font": ["Noto Sans Regular"],
      "symbol-placement": "line",
      "text-rotation-alignment": "map",
      "symbol-spacing": 300,
    },
    paint: { "text-color": "#ffb3b6", ...halo },
  }, beforeId);
  // Resort names at overview zooms; runs carry the detail once zoomed in.
  map.addLayer({
    id: "ski-area-label",
    type: "symbol",
    source: SOURCE_ID,
    "source-layer": "ski_areas",
    filter: IS_POINT,
    minzoom: 8,
    maxzoom: 13,
    layout: {
      "text-field": ["coalesce", ["get", "name"], ""],
      "text-size": ["interpolate", ["linear"], ["zoom"], 8, 10, 12, 13],
      "text-font": ["Noto Sans Regular"],
      "text-max-width": 8,
    },
    paint: { "text-color": "#e6e6ff", ...halo },
  }, beforeId);
}

export function setSkiVisibility(map: maplibregl.Map | null, visible: boolean): void {
  if (!map) return;
  const v = visible ? "visible" : "none";
  for (const id of SKI_LAYER_IDS) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", v);
  }
}

export function setSkiOpacity(map: maplibregl.Map | null, opacity: number): void {
  if (!map) return;
  const set = (id: string, prop: string, factor = 1) => {
    if (map.getLayer(id)) map.setPaintProperty(id, prop, opacity * factor);
  };
  set("ski-area-outline", "line-opacity", 0.8);
  set("ski-run-area", "fill-opacity", 0.2);
  set("ski-run-case", "line-opacity", 0.7);
  set("ski-run", "line-opacity");
  set("ski-trail", "line-opacity");
  set("ski-lift", "line-opacity");
  // Symbols stay fully opaque — already halo'd, dimming hurts readability.
}

export function setSkiRegion(map: maplibregl.Map | null, regionId: string): void {
  const src = map?.getSource(SOURCE_ID) as maplibregl.VectorTileSource | undefined;
  src?.setTiles(skiTiles(regionId));
}
