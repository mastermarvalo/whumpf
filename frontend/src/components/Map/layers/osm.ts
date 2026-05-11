// OSM reference overlays — split into separately toggleable Streets and
// Trails layers, both backed by the same OpenFreeMap vector tile source.
//
// Streets layer: roads (major + minor) + their labels + place names. The
// "where am I in the world" group.
//
// Trails layer:  tracks / paths / pedestrian ways + their labels + mountain
// peaks. The "backcountry decision-making" group.
//
// Mountain peaks sit with trails on purpose — they're the most useful
// orienting feature when you're off-road and reading slope shading. Place
// names sit with streets for the same reason in the opposite direction.

import maplibregl from "maplibre-gl";

const SOURCE_ID = "osm-source";
const OFM_TILEJSON = "https://tiles.openfreemap.org/planet";

export const STREETS_LAYER_IDS = [
  "streets-road-case",
  "streets-road",
  "streets-minor-case",
  "streets-minor",
  "streets-shield",
  "streets-road-label",
  "streets-minor-label",
  "streets-place",
] as const;

export const TRAILS_LAYER_IDS = [
  "trails-path-case",
  "trails-path",
  "trails-path-label",
  "trails-peak",
] as const;

const MAJOR_CLASSES: string[] = ["motorway", "trunk", "primary", "secondary", "tertiary"];
const MINOR_CLASSES: string[] = ["minor", "service"];
const TRACK_PATH_CLASSES: string[] = ["track", "path", "pedestrian"];

const NAME_OR_REF: maplibregl.ExpressionSpecification = [
  "coalesce", ["get", "name"], ["get", "ref"], "",
];

function ensureSource(map: maplibregl.Map): void {
  if (map.getSource(SOURCE_ID)) return;
  map.addSource(SOURCE_ID, {
    type: "vector",
    url: OFM_TILEJSON,
    attribution: "© OpenStreetMap, OpenFreeMap",
  });
}

// ── Streets ──────────────────────────────────────────────────────────────────

export function addStreetsLayers(
  map: maplibregl.Map,
  opacity: number,
  beforeId?: string,
): void {
  if (map.getLayer("streets-road")) return; // already mounted
  ensureSource(map);

  const lineCapJoin = { "line-cap": "round" as const, "line-join": "round" as const };

  map.addLayer({
    id: "streets-road-case",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "transportation",
    filter: ["match", ["get", "class"], MAJOR_CLASSES, true, false],
    layout: lineCapJoin,
    paint: {
      "line-color": "#000",
      "line-width": ["interpolate", ["linear"], ["zoom"], 6, 2.5, 14, 9],
      "line-opacity": opacity,
    },
  }, beforeId);
  map.addLayer({
    id: "streets-road",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "transportation",
    filter: ["match", ["get", "class"], MAJOR_CLASSES, true, false],
    layout: lineCapJoin,
    paint: {
      "line-color": "#ffd34d",
      "line-width": ["interpolate", ["linear"], ["zoom"], 6, 1.2, 14, 6],
      "line-opacity": opacity,
    },
  }, beforeId);

  map.addLayer({
    id: "streets-minor-case",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "transportation",
    filter: ["match", ["get", "class"], MINOR_CLASSES, true, false],
    layout: lineCapJoin,
    paint: {
      "line-color": "#000",
      "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1.6, 14, 5],
      "line-opacity": opacity,
    },
  }, beforeId);
  map.addLayer({
    id: "streets-minor",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "transportation",
    filter: ["match", ["get", "class"], MINOR_CLASSES, true, false],
    layout: lineCapJoin,
    paint: {
      "line-color": "#fafafa",
      "line-width": ["interpolate", ["linear"], ["zoom"], 8, 0.8, 14, 3],
      "line-opacity": opacity,
    },
  }, beforeId);

  // Highway shields (point-placed refs like "I 70" recurring along the road).
  map.addLayer({
    id: "streets-shield",
    type: "symbol",
    source: SOURCE_ID,
    "source-layer": "transportation_name",
    filter: ["all",
      ["has", "ref"],
      ["match", ["get", "class"], MAJOR_CLASSES, true, false],
    ],
    minzoom: 9,
    layout: {
      "text-field": ["to-string", ["get", "ref"]],
      "text-size": ["interpolate", ["linear"], ["zoom"], 9, 11, 14, 13],
      "text-font": ["Noto Sans Regular"],
      "symbol-placement": "line",
      "text-rotation-alignment": "viewport",
      "text-pitch-alignment": "viewport",
      "symbol-spacing": 240,
      "text-padding": 2,
    },
    paint: {
      "text-color": "#000",
      "text-halo-color": "#ffd34d",
      "text-halo-width": 4,
      "text-halo-blur": 0.5,
    },
  }, beforeId);

  map.addLayer({
    id: "streets-road-label",
    type: "symbol",
    source: SOURCE_ID,
    "source-layer": "transportation_name",
    filter: ["match", ["get", "class"], MAJOR_CLASSES, true, false],
    minzoom: 11,
    layout: {
      "text-field": NAME_OR_REF,
      "text-size": ["interpolate", ["linear"], ["zoom"], 11, 11, 16, 14],
      "text-font": ["Noto Sans Regular"],
      "symbol-placement": "line",
      "text-rotation-alignment": "map",
      "text-pitch-alignment": "viewport",
      "symbol-spacing": 350,
      "text-padding": 2,
    },
    paint: {
      "text-color": "#fff",
      "text-halo-color": "#000",
      "text-halo-width": 2,
      "text-halo-blur": 0.5,
    },
  }, beforeId);

  map.addLayer({
    id: "streets-minor-label",
    type: "symbol",
    source: SOURCE_ID,
    "source-layer": "transportation_name",
    filter: ["match", ["get", "class"], MINOR_CLASSES, true, false],
    minzoom: 13,
    layout: {
      "text-field": ["coalesce", ["get", "name"], ""],
      "text-size": ["interpolate", ["linear"], ["zoom"], 13, 10, 16, 12],
      "text-font": ["Noto Sans Regular"],
      "symbol-placement": "line",
      "text-rotation-alignment": "map",
      "text-pitch-alignment": "viewport",
      "symbol-spacing": 280,
    },
    paint: {
      "text-color": "#fff",
      "text-halo-color": "#000",
      "text-halo-width": 1.5,
      "text-halo-blur": 0.5,
    },
  }, beforeId);

  map.addLayer({
    id: "streets-place",
    type: "symbol",
    source: SOURCE_ID,
    "source-layer": "place",
    filter: ["match", ["get", "class"], ["city", "town", "village", "hamlet"], true, false],
    layout: {
      "text-field": ["coalesce", ["get", "name"], ""],
      "text-size": ["interpolate", ["linear"], ["zoom"], 6, 10, 14, 14],
      "text-font": ["Noto Sans Regular"],
      "text-max-width": 8,
      "symbol-sort-key": ["case",
        ["==", ["get", "class"], "city"], 0,
        ["==", ["get", "class"], "town"], 1,
        ["==", ["get", "class"], "village"], 2,
        3,
      ],
    },
    paint: {
      "text-color": "#fff",
      "text-halo-color": "#000",
      "text-halo-width": 1.5,
      "text-halo-blur": 0.5,
    },
  }, beforeId);
}

export function setStreetsVisibility(map: maplibregl.Map | null, visible: boolean): void {
  if (!map) return;
  const v = visible ? "visible" : "none";
  for (const id of STREETS_LAYER_IDS) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", v);
  }
}

export function setStreetsOpacity(map: maplibregl.Map | null, opacity: number): void {
  if (!map) return;
  const setLine = (id: string) => {
    if (map.getLayer(id)) map.setPaintProperty(id, "line-opacity", opacity);
  };
  setLine("streets-road-case");
  setLine("streets-road");
  setLine("streets-minor-case");
  setLine("streets-minor");
  // Symbols stay fully opaque — already halo'd, dimming hurts readability.
}

// ── Trails ──────────────────────────────────────────────────────────────────

export function addTrailsLayers(
  map: maplibregl.Map,
  opacity: number,
  beforeId?: string,
): void {
  if (map.getLayer("trails-path")) return;
  ensureSource(map);

  const lineCapJoin = { "line-cap": "round" as const, "line-join": "round" as const };

  map.addLayer({
    id: "trails-path-case",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "transportation",
    filter: ["match", ["get", "class"], TRACK_PATH_CLASSES, true, false],
    layout: lineCapJoin,
    paint: {
      "line-color": "#000",
      "line-width": ["interpolate", ["linear"], ["zoom"], 9, 1.6, 14, 4],
      "line-opacity": opacity * 0.85,
    },
  }, beforeId);
  map.addLayer({
    id: "trails-path",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "transportation",
    filter: ["match", ["get", "class"], TRACK_PATH_CLASSES, true, false],
    layout: lineCapJoin,
    paint: {
      "line-color": "#ff7a3a",
      "line-width": ["interpolate", ["linear"], ["zoom"], 9, 0.9, 14, 2.5],
      "line-dasharray": [2, 1.5],
      "line-opacity": opacity,
    },
  }, beforeId);

  map.addLayer({
    id: "trails-path-label",
    type: "symbol",
    source: SOURCE_ID,
    "source-layer": "transportation_name",
    filter: ["match", ["get", "class"], TRACK_PATH_CLASSES, true, false],
    minzoom: 13,
    layout: {
      "text-field": ["coalesce", ["get", "name"], ""],
      "text-size": ["interpolate", ["linear"], ["zoom"], 13, 10, 18, 13],
      "text-font": ["Noto Sans Regular"],
      "symbol-placement": "line",
      "text-rotation-alignment": "map",
      "text-pitch-alignment": "viewport",
      "symbol-spacing": 220,
    },
    paint: {
      "text-color": "#ff9d6e",
      "text-halo-color": "#000",
      "text-halo-width": 2,
      "text-halo-blur": 0.5,
    },
  }, beforeId);

  map.addLayer({
    id: "trails-peak",
    type: "symbol",
    source: SOURCE_ID,
    "source-layer": "mountain_peak",
    minzoom: 8,
    layout: {
      "text-field": ["coalesce", ["get", "name"], ""],
      "text-size": ["interpolate", ["linear"], ["zoom"], 8, 9, 14, 12],
      "text-font": ["Noto Sans Regular"],
      "text-anchor": "top",
      "text-offset": [0, 0.6],
      "text-max-width": 8,
      "symbol-sort-key": ["coalesce", ["-", ["to-number", ["get", "ele"]]], 0],
    },
    paint: {
      "text-color": "#fff",
      "text-halo-color": "#000",
      "text-halo-width": 1.5,
      "text-halo-blur": 0.5,
    },
  }, beforeId);
}

export function setTrailsVisibility(map: maplibregl.Map | null, visible: boolean): void {
  if (!map) return;
  const v = visible ? "visible" : "none";
  for (const id of TRAILS_LAYER_IDS) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", v);
  }
}

export function setTrailsOpacity(map: maplibregl.Map | null, opacity: number): void {
  if (!map) return;
  const setLine = (id: string, factor = 1) => {
    if (map.getLayer(id)) map.setPaintProperty(id, "line-opacity", opacity * factor);
  };
  setLine("trails-path-case", 0.85);
  setLine("trails-path");
}
