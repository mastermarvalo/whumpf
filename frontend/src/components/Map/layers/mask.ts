import maplibregl from "maplibre-gl";

/**
 * Adds an inverted-polygon mask (world rectangle with a region-shaped hole)
 * that dims out-of-region area. The mask GeoJSON is supplied per-region
 * rather than hardcoded so the same call works for Colorado, Utah, etc.
 */
export function addRegionMask(map: maplibregl.Map, maskGeojson: unknown) {
  if (map.getSource("region-mask")) return;
  map.addSource("region-mask", { type: "geojson", data: maskGeojson as any });
  map.addLayer({
    id: "region-mask-fill",
    type: "fill",
    source: "region-mask",
    paint: { "fill-color": "#000000", "fill-opacity": 1 },
  });
}

export function setMaskVisibility(map: maplibregl.Map | null, visible: boolean) {
  if (!map || !map.getLayer("region-mask-fill")) return;
  map.setLayoutProperty("region-mask-fill", "visibility", visible ? "visible" : "none");
}
