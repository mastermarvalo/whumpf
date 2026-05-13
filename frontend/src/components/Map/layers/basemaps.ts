import maplibregl from "maplibre-gl";
import { API_URL, MARTIN_URL, MINIO_BUCKET, TITILER_URL } from "../constants";
import type { BasemapId } from "../types";

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
  background = "#e8dfc8",
): maplibregl.StyleSpecification {
  return {
    version: 8,
    // Glyphs needed for SNOTEL symbol layers on raster basemaps.
    glyphs: OMP_GLYPHS,
    sources: {
      basemap: { type: "raster", tiles, tileSize: 256, attribution, ...(maxzoom ? { maxzoom } : {}) },
    },
    // background prevents black void on terrain mesh where tiles haven't loaded yet.
    layers: [
      { id: "background", type: "background", paint: { "background-color": background } },
      { id: "basemap", type: "raster", source: "basemap" },
    ],
  };
}

const RASTER_STYLES: Record<"topo" | "satellite", maplibregl.StyleSpecification> = {
  // Esri World Topo Map: contours, shaded relief, trails, water — no API key required.
  topo:      rasterStyle([`${ESRI}/World_Topo_Map/MapServer/tile/{z}/{y}/{x}`], "Esri", undefined, "#e8dfc8"),
  // maxzoom: 17 — beyond that Esri returns a "not available yet" placeholder JPEG.
  // MapLibre overzooms the z17 tile instead of requesting z18+.
  satellite: rasterStyle([`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`], "Esri, DigitalGlobe", 17, "#12182b"),
};

// Source and layer IDs owned by each raster basemap — used for in-place swaps.
const RASTER_BASEMAP_IDS: Record<"topo" | "satellite", { sources: string[]; layers: string[] }> = {
  topo:      { sources: ["basemap"], layers: ["background", "basemap"] },
  satellite: { sources: ["basemap"], layers: ["background", "basemap"] },
};

export function getMapStyle(basemap: BasemapId, dark: boolean): string | maplibregl.StyleSpecification {
  if (basemap === "streets") return VECTOR_STYLES[dark ? "dark" : "light"];
  return RASTER_STYLES[basemap];
}

export function cogS3(path: string) {
  return `s3://${MINIO_BUCKET}/${path}`;
}

export function cogTiles(cogPath: string, extra: Record<string, string> = {}): string[] {
  const params = new URLSearchParams({ url: cogS3(cogPath), ...extra });
  return [`${TITILER_URL}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?${params}`];
}

export interface TerrainFilterSettings {
  aspects: string[];        // subset of N, NE, E, SE, S, SW, W, NW
  slopeMin: number;
  slopeMax: number;
}

export function getTerrainSource(_regionId: string): maplibregl.RasterDEMSourceSpecification {
  return {
    type: "raster-dem",
    tiles: [`${MARTIN_URL}/terrain_rgb/{z}/{x}/{y}`],
    tileSize: 256,
    encoding: "terrarium",
    minzoom: 5,
    maxzoom: 14,
  };
}

export function getTerrainFilterUrl(regionId: string, s: TerrainFilterSettings): string {
  const p = new URLSearchParams({
    region: regionId,
    slope_min: String(s.slopeMin),
    slope_max: String(s.slopeMax),
    aspects: s.aspects.join(","),
  });
  return `${API_URL}/tiles/terrain_filter/{z}/{x}/{y}?${p}`;
}

// Swap only the basemap source/layers for raster-to-raster transitions without touching overlays.
export function swapRasterBasemap(
  map: maplibregl.Map,
  from: "topo" | "satellite",
  to: "topo" | "satellite",
) {
  if (from === to) return;
  const fromIds = RASTER_BASEMAP_IDS[from];
  // Capture the first overlay layer ID before removing anything so we can insert below it.
  const firstOverlayId = map.getStyle().layers.find(
    (l) => !fromIds.layers.includes(l.id),
  )?.id;
  for (const id of fromIds.layers) {
    if (map.getLayer(id)) map.removeLayer(id);
  }
  for (const id of fromIds.sources) {
    if (map.getSource(id)) map.removeSource(id);
  }
  const newStyle = RASTER_STYLES[to];
  for (const [id, spec] of Object.entries(newStyle.sources)) {
    map.addSource(id, spec as maplibregl.SourceSpecification);
  }
  for (const layer of newStyle.layers) {
    map.addLayer(layer as maplibregl.LayerSpecification, firstOverlayId);
  }
}
