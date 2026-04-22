/**
 * Base map component.
 *
 * Phase 1 goal: a MapLibre GL JS map centered on Colorado, using OSM raster
 * tiles as a placeholder basemap. In Phase 2 we replace the style with a
 * custom one that pulls vector tiles from Martin and raster (hillshade,
 * slope) from TiTiler.
 */
import { useEffect, useRef } from "react";
import maplibregl, { type StyleSpecification } from "maplibre-gl";

// Initial view: San Juan Mountains, Colorado. Change this freely; it is only
// the startup view, not the map's constraint.
const INITIAL_CENTER: [number, number] = [-107.67, 37.81];
const INITIAL_ZOOM = 8;

// Placeholder style — raster OSM tiles so we can confirm the map renders at
// all before wiring up Martin/TiTiler. Respect the OSM tile usage policy:
// this is fine for personal dev; switch to self-hosted tiles before going
// public.
const PLACEHOLDER_STYLE: StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors",
      maxzoom: 19,
    },
  },
  layers: [
    {
      id: "osm",
      type: "raster",
      source: "osm",
    },
  ],
};

export function Map() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: PLACEHOLDER_STYLE,
      center: INITIAL_CENTER,
      zoom: INITIAL_ZOOM,
      // Stop the map from wrapping horizontally — annoying when scrolling.
      renderWorldCopies: false,
    });

    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "imperial" }), "bottom-left");

    mapRef.current = map;

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  return <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />;
}
