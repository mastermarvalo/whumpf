// Streets & trails overlay — transparent vector tiles from OpenFreeMap
// (OSM-derived, OpenMapTiles schema). The same data that backs the
// "streets" basemap, but rendered as a thin overlay you can slide on top
// of slope/aspect/satellite/topo so trails and place names stay legible.
//
// Filter / placement / minzoom values track what openfreemap's own
// positron style uses for transportation_name (its `highway-name-*`
// layers), so we know they actually surface data at the right zooms.

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
  "trails-shield",
  "trails-peak",
  "trails-place",
] as const;

// TileJSON URL form: OpenFreeMap rebuilds the planet under a versioned
// path (/planet/20260506_001001_pt/…) and only exposes the current
// version via this TileJSON document; the unversioned tile path returns
// empty tiles.
const OFM_TILEJSON = "https://tiles.openfreemap.org/planet";

// Match positron's groupings — secondary/tertiary live with the bigger
// roads, not the small ones. The eye-test confirms it: tertiary roads
// in Colorado are usually paved feeder roads, not driveways.
const MAJOR_CLASSES: string[] = ["motorway", "trunk", "primary", "secondary", "tertiary"];
const MINOR_CLASSES: string[] = ["minor", "service"];
const TRACK_PATH_CLASSES: string[] = ["track", "path", "pedestrian"];

// Cleaner than my prior REF_OR_NAME case-tree: prefer name; if there's
// no name, show the ref (e.g. unnamed interstates show "I 70"); failing
// that, render nothing.
const NAME_OR_REF: maplibregl.ExpressionSpecification = [
  "coalesce", ["get", "name"], ["get", "ref"], "",
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

  const lineCapJoin = { "line-cap": "round" as const, "line-join": "round" as const };

  // ── Major roads — wide dark casing + bright yellow fill ─────────────────
  map.addLayer({
    id: "trails-road-case",
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
    id: "trails-road",
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

  // ── Minor roads — thinner, brighter white ──────────────────────────────
  map.addLayer({
    id: "trails-minor-case",
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
    id: "trails-minor",
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

  // ── Trails / paths / tracks — bright orange dashed with dark casing ─────
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

  // ── Line-following labels ───────────────────────────────────────────────
  // Single Noto Sans Regular — matches the openmaptiles glyph server and
  // positron's own usage. Bold/italic variants were silently failing on
  // some tiles, which dropped entire label layers.

  map.addLayer({
    id: "trails-road-label",
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
    id: "trails-minor-label",
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

  // ── Highway shield refs (point-placed) ──────────────────────────────────
  // Separate layer with point placement so route refs ("I 70", "US 285",
  // "CO 119") appear as recurring badges along the road, not blended into
  // the road name. Filtered to ref-having US-style networks only.
  map.addLayer({
    id: "trails-shield",
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

  // ── Mountain peaks ──────────────────────────────────────────────────────
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

  // ── Place names — towns/villages, halo'd to read on any basemap ─────────
  map.addLayer({
    id: "trails-place",
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
