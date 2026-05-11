// Streets & trails overlay — transparent vector tiles from OpenFreeMap
// (OSM-derived, OpenMapTiles schema). The same data that backs the
// "streets" basemap, but rendered as a thin overlay you can slide on top
// of slope/aspect/satellite/topo so trails and place names stay legible.
//
// Layered for cross-basemap visibility: each line layer has a dark casing
// underneath a lighter fill on top, so it reads against both Esri imagery
// and the existing positron basemap. Labels use white text with a heavy
// dark halo for the same reason.

import maplibregl from "maplibre-gl";

const SOURCE_ID = "trails-source";

export const TRAILS_LAYER_IDS = [
  "trails-road-case",
  "trails-road",
  "trails-minor-case",
  "trails-minor",
  "trails-path-case",
  "trails-path",
  "trails-road-label",
  "trails-minor-label",
  "trails-path-label",
  "trails-peak",
  "trails-place",
] as const;

// TileJSON URL form rather than explicit tiles[]. OpenFreeMap rebuilds
// the planet under a versioned path (e.g. /planet/20260506_001001_pt/…)
// and only exposes the current version via this TileJSON document; the
// unversioned /planet/{z}/{x}/{y}.pbf path returns empty tiles. MapLibre
// fetches the TileJSON on source load and uses whatever path it points at.
const OFM_TILEJSON = "https://tiles.openfreemap.org/planet";

const MAJOR_CLASSES: string[] = ["motorway", "trunk", "primary"];
const MINOR_CLASSES: string[] = ["secondary", "tertiary", "minor", "service"];
const PATH_CLASSES:  string[] = ["track", "path", "pedestrian"];

// Prefer the short highway ref ("I-70") over the long name on major roads;
// fall back to name where ref is absent. Minor roads / trails just use name.
const REF_OR_NAME: maplibregl.ExpressionSpecification = [
  "case",
  ["all", ["has", "ref"], [">", ["length", ["coalesce", ["get", "ref"], ""]], 0]],
  ["get", "ref"],
  ["coalesce", ["get", "name"], ""],
];

export function addTrailsLayers(
  map: maplibregl.Map,
  opacity: number,
  beforeId?: string,
): void {
  if (map.getSource(SOURCE_ID)) return;
  map.addSource(SOURCE_ID, {
    type: "vector",
    url: OFM_TILEJSON,
    attribution: "© OpenStreetMap, OpenFreeMap",
  });

  // ── Major roads — wide dark casing + bright yellow fill ─────────────────
  map.addLayer({
    id: "trails-road-case",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "transportation",
    filter: ["in", ["get", "class"], ["literal", MAJOR_CLASSES]],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": "#000",
      "line-width": ["interpolate", ["linear"], ["zoom"], 6, 2.5, 14, 9],
      "line-opacity": opacity,
    },
  }, beforeId);
  map.addLayer({
    id: "trails-road",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "transportation",
    filter: ["in", ["get", "class"], ["literal", MAJOR_CLASSES]],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": "#ffd34d",
      "line-width": ["interpolate", ["linear"], ["zoom"], 6, 1.2, 14, 6],
      "line-opacity": opacity,
    },
  }, beforeId);

  // ── Minor roads — thinner, brighter white ──────────────────────────────
  map.addLayer({
    id: "trails-minor-case",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "transportation",
    filter: ["in", ["get", "class"], ["literal", MINOR_CLASSES]],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": "#000",
      "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1.6, 14, 5],
      "line-opacity": opacity,
    },
  }, beforeId);
  map.addLayer({
    id: "trails-minor",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "transportation",
    filter: ["in", ["get", "class"], ["literal", MINOR_CLASSES]],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": "#fafafa",
      "line-width": ["interpolate", ["linear"], ["zoom"], 8, 0.8, 14, 3],
      "line-opacity": opacity,
    },
  }, beforeId);

  // ── Trails / paths / tracks ─ bright orange, dashed, with a dark casing
  // for legibility over satellite imagery + slope shading. Wider than before.
  map.addLayer({
    id: "trails-path-case",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "transportation",
    filter: ["in", ["get", "class"], ["literal", PATH_CLASSES]],
    layout: { "line-cap": "round", "line-join": "round" },
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
    filter: ["in", ["get", "class"], ["literal", PATH_CLASSES]],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": "#ff7a3a",
      "line-width": ["interpolate", ["linear"], ["zoom"], 9, 0.9, 14, 2.5],
      "line-dasharray": [2, 1.5],
      "line-opacity": opacity,
    },
  }, beforeId);

  // ── Line-following labels ───────────────────────────────────────────────
  // symbol-placement: line makes the text track the road's curve. The
  // openfreemap glyphs server has Noto Sans Regular/Bold; we fall back to
  // Open Sans (positron basemap) and Arial Unicode (everything else).

  map.addLayer({
    id: "trails-road-label",
    type: "symbol",
    source: SOURCE_ID,
    "source-layer": "transportation_name",
    filter: ["in", ["get", "class"], ["literal", MAJOR_CLASSES]],
    minzoom: 10,
    layout: {
      "text-field": REF_OR_NAME,
      "text-size": ["interpolate", ["linear"], ["zoom"], 10, 11, 16, 14],
      "text-font": ["Noto Sans Bold", "Open Sans Bold", "Arial Unicode MS Bold"],
      "symbol-placement": "line",
      "text-rotation-alignment": "map",
      "text-pitch-alignment": "viewport",
      "symbol-spacing": 350,
      "text-max-angle": 30,
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
    id: "trails-minor-label",
    type: "symbol",
    source: SOURCE_ID,
    "source-layer": "transportation_name",
    filter: ["in", ["get", "class"], ["literal", MINOR_CLASSES]],
    minzoom: 13,
    layout: {
      "text-field": ["coalesce", ["get", "name"], ""],
      "text-size": ["interpolate", ["linear"], ["zoom"], 13, 10, 16, 12],
      "text-font": ["Noto Sans Regular", "Open Sans Regular", "Arial Unicode MS Regular"],
      "symbol-placement": "line",
      "text-rotation-alignment": "map",
      "text-pitch-alignment": "viewport",
      "symbol-spacing": 280,
      "text-max-angle": 35,
    },
    paint: {
      "text-color": "#fff",
      "text-halo-color": "#000",
      "text-halo-width": 1.5,
      "text-halo-blur": 0.5,
    },
  }, beforeId);

  map.addLayer({
    id: "trails-path-label",
    type: "symbol",
    source: SOURCE_ID,
    "source-layer": "transportation_name",
    filter: ["in", ["get", "class"], ["literal", PATH_CLASSES]],
    minzoom: 14,
    layout: {
      "text-field": ["coalesce", ["get", "name"], ""],
      "text-size": ["interpolate", ["linear"], ["zoom"], 14, 10, 18, 13],
      "text-font": ["Noto Sans Italic", "Open Sans Italic", "Noto Sans Regular", "Open Sans Regular"],
      "symbol-placement": "line",
      "text-rotation-alignment": "map",
      "text-pitch-alignment": "viewport",
      "symbol-spacing": 250,
      "text-max-angle": 40,
    },
    paint: {
      "text-color": "#ff7a3a",
      "text-halo-color": "#000",
      "text-halo-width": 2,
      "text-halo-blur": 0.5,
    },
  }, beforeId);

  // ── Mountain peaks — name labels with halo, sorted by elevation ─────────
  map.addLayer({
    id: "trails-peak",
    type: "symbol",
    source: SOURCE_ID,
    "source-layer": "mountain_peak",
    minzoom: 8,
    layout: {
      "text-field": ["coalesce", ["get", "name"], ""],
      "text-size": ["interpolate", ["linear"], ["zoom"], 8, 9, 14, 12],
      "text-font": ["Noto Sans Bold", "Open Sans Bold", "Arial Unicode MS Bold"],
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

  // ── Place names — towns/villages, halo'd to read on any basemap ─────────
  map.addLayer({
    id: "trails-place",
    type: "symbol",
    source: SOURCE_ID,
    "source-layer": "place",
    filter: ["in", ["get", "class"], ["literal", ["city", "town", "village", "hamlet"]]],
    layout: {
      "text-field": ["coalesce", ["get", "name"], ""],
      "text-size": ["interpolate", ["linear"], ["zoom"], 6, 10, 14, 14],
      "text-font": ["Noto Sans Regular", "Open Sans Regular", "Arial Unicode MS Regular"],
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

export function setTrailsVisibility(map: maplibregl.Map | null, visible: boolean): void {
  if (!map) return;
  const v = visible ? "visible" : "none";
  for (const id of TRAILS_LAYER_IDS) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", v);
  }
}

export function setTrailsOpacity(map: maplibregl.Map | null, opacity: number): void {
  if (!map) return;
  const adjust = (id: string, factor = 1) => {
    if (map.getLayer(id)) map.setPaintProperty(id, "line-opacity", opacity * factor);
  };
  adjust("trails-road-case");
  adjust("trails-road");
  adjust("trails-minor-case");
  adjust("trails-minor");
  adjust("trails-path-case", 0.85);
  adjust("trails-path");
  // Symbols stay fully opaque — they're already halo'd and dimming them
  // hurts readability more than it helps.
}
