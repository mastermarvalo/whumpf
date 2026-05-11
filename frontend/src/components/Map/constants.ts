export const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
export const TITILER_URL = import.meta.env.VITE_TITILER_URL ?? "http://localhost:8001";

export const MINIO_BUCKET = "dem-cogs";
export const REGION = "colorado";

export const INITIAL_CENTER: [number, number] = [-105.5, 39.0];
export const INITIAL_ZOOM = 7;
export const COLORADO_MTN_BOUNDS: [number, number, number, number] = [-109.06, 37.0, -104.5, 41.0];
// Padded Colorado bbox used as map maxBounds when region lock is on.
export const CO_MAX_BOUNDS: [number, number, number, number] = [-109.5, 36.5, -101.5, 41.5];

// Inverted polygon: world rectangle with Colorado cut out as a hole.
// Renders as a black fill masking everything outside Colorado.
export const CO_MASK_GEOJSON = {
  type: "FeatureCollection",
  features: [{
    type: "Feature",
    geometry: {
      type: "Polygon",
      coordinates: [
        [[-180, -90], [180, -90], [180, 90], [-180, 90], [-180, -90]],  // outer world
        [[-109.25, 36.80], [-101.85, 36.80], [-101.85, 41.20], [-109.25, 41.20], [-109.25, 36.80]], // CO hole (CW)
      ],
    },
    properties: {},
  }],
};
