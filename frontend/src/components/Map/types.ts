// Shared types for the Map UI. Extracted from a monolithic Map.tsx so
// subcomponents (LayerPanel, InfoPanel, etc.) can share them without
// circular imports.

export type BasemapId = "streets" | "topo" | "satellite" | "hybrid";

export type Units = "imperial" | "metric";

export interface Legend {
  gradient?: string;
  stops?: string[];
  swatches?: Array<{ color: string; label: string }>;
}

export interface ActiveLayer {
  id: string;
  label: string;
  tiles: string[];
  kind?: "raster" | "geojson"; // default "raster"; geojson layers skip addOverlayLayers
  opacity: number;
  defaultVisible: boolean;
  noSlider?: boolean;
  legend?: Legend;
  // Minimum zoom at which the tile source starts loading (default: 6).
  // Set higher for hires layers so MapLibre skips requests at overview zoom levels.
  sourceMinzoom?: number;
  // Extra MapLibre raster paint properties merged in at layer creation (e.g. saturation, resampling).
  blendPaint?: { "raster-saturation"?: number; "raster-resampling"?: "linear" | "nearest" };
  // 1m hires tile URLs — if set, a companion `${id}-hires` layer is added at minzoom 13.
  hiresTiles?: string[];
}

export interface UpcomingLayer {
  id: string;
  label: string;
}

export interface LayerGroup {
  id: string;
  label: string;
  color: string;
  active: ActiveLayer[];
  upcoming: UpcomingLayer[];
  reorderable?: boolean;
}

export type ActivityCardProps = {
  id: number;
  name: string;
  sport_type: string;
  color: string;
  distance_m: number;
  elapsed_time_s: number;
  total_elevation_gain_m: number;
  start_date: string;
  photo_url: string | null;
};

export interface ForecastPeriod {
  name: string;
  temperature: number;
  temperatureUnit: string;
  windSpeed: string;
  windDirection: string;
  shortForecast: string;
  probabilityOfPrecipitation?: { value: number | null };
}

export interface SpotData {
  periods: ForecastPeriod[];
  tempF: number | null;
  snowDepthIn: number | null;
}

export interface PointData {
  lon: number;
  lat: number;
  loading: boolean;
  elevation?: number;
  slope?: number;
  aspect?: number;
  tempF?: number | null;
  snowDepthIn?: number | null;
  locationName?: string | null;
}

export interface SlopeSample {
  distance_m: number;
  elevation_m: number | null;
  slope_deg: number | null;
}

export interface ProfileSummary {
  distance_m: number;
  avg_slope_deg: number | null;
  max_slope_deg: number | null;
  min_slope_deg: number | null;
  elevation_gain_m: number | null;
  elevation_loss_m: number | null;
  start_elevation_m: number | null;
  end_elevation_m: number | null;
}

export interface ProfileResponse {
  summary: ProfileSummary;
  samples: SlopeSample[];
}
