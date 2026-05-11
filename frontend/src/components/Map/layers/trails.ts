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
  "trails-path",
  "trails-peak",
  "trails-place",
] as const;

const OFM_TILES = ["https://tiles.openfreemap.org/planet/{z}/{x}/{y}.pbf"];

export function addTrailsLayers(
  map: maplibregl.Map,
  opacity: number,
  beforeId?: string,
): void {
  if (map.getSource(SOURCE_ID)) return;
  map.addSource(SOURCE_ID, {
    type: "vector",
    tiles: OFM_TILES,
    minzoom: 0,
    maxzoom: 14,
    attribution: "© OpenStreetMap, OpenFreeMap",
  });

  // Major roads — wide casing + bright fill.
  map.addLayer({
    id: "trails-road-case",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "transportation",
    filter: ["in", ["get", "class"], ["literal", ["motorway", "trunk", "primary"]]],
    paint: {
      "line-color": "#000",
      "line-width": ["interpolate", ["linear"], ["zoom"], 6, 1.4, 14, 5],
      "line-opacity": opacity,
    },
  }, beforeId);
  map.addLayer({
    id: "trails-road",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "transportation",
    filter: ["in", ["get", "class"], ["literal", ["motorway", "trunk", "primary"]]],
    paint: {
      "line-color": "#f6c45c",
      "line-width": ["interpolate", ["linear"], ["zoom"], 6, 0.6, 14, 3],
      "line-opacity": opacity,
    },
  }, beforeId);

  // Minor roads — same idea, thinner + white.
  map.addLayer({
    id: "trails-minor-case",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "transportation",
    filter: ["in", ["get", "class"], ["literal", ["secondary", "tertiary", "minor", "service"]]],
    paint: {
      "line-color": "#000",
      "line-width": ["interpolate", ["linear"], ["zoom"], 9, 0.8, 14, 2.5],
      "line-opacity": opacity * 0.85,
    },
  }, beforeId);
  map.addLayer({
    id: "trails-minor",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "transportation",
    filter: ["in", ["get", "class"], ["literal", ["secondary", "tertiary", "minor", "service"]]],
    paint: {
      "line-color": "#fff",
      "line-width": ["interpolate", ["linear"], ["zoom"], 9, 0.3, 14, 1.2],
      "line-opacity": opacity * 0.85,
    },
  }, beforeId);

  // Trails / paths / tracks — backcountry-relevant. Dashed rust-orange.
  map.addLayer({
    id: "trails-path",
    type: "line",
    source: SOURCE_ID,
    "source-layer": "transportation",
    filter: ["in", ["get", "class"], ["literal", ["track", "path", "pedestrian"]]],
    paint: {
      "line-color": "#d96a3a",
      "line-width": ["interpolate", ["linear"], ["zoom"], 9, 0.6, 14, 1.6],
      "line-dasharray": [2, 2],
      "line-opacity": opacity,
    },
  }, beforeId);

  // Mountain peaks — name + elevation, halo'd for visibility.
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

  // Place names — towns, hamlets, suburbs. Same halo treatment.
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
  adjust("trails-minor-case", 0.85);
  adjust("trails-minor", 0.85);
  adjust("trails-path");
  // Symbols stay fully opaque — they're already halo'd and dimming them
  // hurts readability more than it helps.
}
