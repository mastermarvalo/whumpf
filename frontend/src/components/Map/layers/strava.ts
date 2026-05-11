import maplibregl from "maplibre-gl";

export function addStravaLayers(map: maplibregl.Map) {
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

export function setStravaData(map: maplibregl.Map | null, geojson: object) {
  if (!map) return;
  const src = map.getSource("strava") as maplibregl.GeoJSONSource | undefined;
  src?.setData(geojson as Parameters<typeof src.setData>[0]);
}

export function setStravaVisibility(map: maplibregl.Map | null, visible: boolean) {
  if (!map) return;
  if (map.getLayer("strava-lines"))
    map.setLayoutProperty("strava-lines", "visibility", visible ? "visible" : "none");
}

export function applyStravaHighlight(map: maplibregl.Map | null, selectedId: number | null) {
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
