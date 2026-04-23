import { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";

const TITILER_URL = import.meta.env.VITE_TITILER_URL ?? "http://localhost:8001";
const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
const MINIO_BUCKET = "dem-cogs";
const REGION = "colorado";

const INITIAL_CENTER: [number, number] = [-107.75, 37.95];
const INITIAL_ZOOM = 10;
const COLORADO_MTN_BOUNDS: [number, number, number, number] = [-109.06, 37.0, -104.5, 41.0];

const MAP_STYLES = {
  light: "https://tiles.openfreemap.org/styles/positron",
  dark: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
};

function cogS3(path: string) {
  return `s3://${MINIO_BUCKET}/${path}`;
}

function cogTiles(cogPath: string, extra: Record<string, string> = {}): string[] {
  const params = new URLSearchParams({ url: cogS3(cogPath), ...extra });
  return [`${TITILER_URL}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?${params}`];
}


// ── types ──────────────────────────────────────────────────────────────────────

interface Legend {
  gradient: string;
  stops: string[];
}

interface ActiveLayer {
  id: string;
  label: string;
  tiles: string[];
  opacity: number;
  defaultVisible: boolean;
  legend?: Legend;
}

interface UpcomingLayer {
  id: string;
  label: string;
}

// ── terrain profile types ──────────────────────────────────────────────────────

interface SlopeSample {
  distance_m: number;
  elevation_m: number | null;
  slope_deg: number | null;
}

interface ProfileSummary {
  distance_m: number;
  avg_slope_deg: number | null;
  max_slope_deg: number | null;
  min_slope_deg: number | null;
  elevation_gain_m: number | null;
  elevation_loss_m: number | null;
  start_elevation_m: number | null;
  end_elevation_m: number | null;
}

interface ProfileResponse {
  summary: ProfileSummary;
  samples: SlopeSample[];
}

interface LayerGroup {
  id: string;
  label: string;
  color: string;
  active: ActiveLayer[];
  upcoming: UpcomingLayer[];
}

// ── layer definitions ──────────────────────────────────────────────────────────

const LAYER_GROUPS: LayerGroup[] = [
  {
    id: "terrain",
    label: "Terrain",
    color: "#a07850",
    active: [
      {
        id: "hillshade",
        label: "Hillshade",
        tiles: cogTiles(`${REGION}/hillshade.tif`),
        opacity: 0.7,
        defaultVisible: true,
      },
      {
        id: "slope",
        label: "Slope angle",
        // Served via API proxy which applies the CalTopo V1 colormap server-side
        tiles: [`${API_URL}/tiles/slope/{z}/{x}/{y}?region=${REGION}`],
        opacity: 0.75,
        defaultVisible: false,
        legend: {
          gradient: "linear-gradient(to right, transparent 0%, #1a9641 25%, #ffeb00 45%, #d7191c 67%, #2b7bb9 100%)",
          stops: ["0°", "15°", "27°", "40°", "60°"],
        },
      },
      {
        id: "aspect",
        label: "Aspect",
        tiles: cogTiles(`${REGION}/aspect.tif`, {
          colormap_name: "hsv",
          rescale: "0,360",
          nodata: "-9999",
        }),
        opacity: 0.7,
        defaultVisible: false,
        legend: {
          gradient:
            "linear-gradient(to right, #ff0000, #ffff00, #00ff00, #00ffff, #0000ff, #ff00ff, #ff0000)",
          stops: ["N", "E", "S", "W", "N"],
        },
      },
    ],
    upcoming: [],
  },
  {
    id: "snowpack",
    label: "Snowpack",
    color: "#4a90d9",
    active: [],
    upcoming: [
      { id: "snotel-swe", label: "SNOTEL SWE" },
      { id: "snotel-depth", label: "Snow Depth" },
    ],
  },
  {
    id: "avalanche",
    label: "Avalanche",
    color: "#e05a2b",
    active: [],
    upcoming: [
      { id: "caic-danger", label: "CAIC Danger Rose" },
      { id: "caic-obs", label: "Field Observations" },
    ],
  },
  {
    id: "weather",
    label: "Weather",
    color: "#2eaa6e",
    active: [],
    upcoming: [
      { id: "ndfd-precip", label: "Precipitation (NDFD)" },
      { id: "ndfd-temp", label: "Temperature (NDFD)" },
    ],
  },
];

const OVERLAY_LAYERS: ActiveLayer[] = LAYER_GROUPS.flatMap((g) => g.active);

// ── theme ──────────────────────────────────────────────────────────────────────

type Theme = typeof THEMES.light;
const THEMES = {
  light: {
    panel: "rgba(255,255,255,0.95)",
    text: "#1a1a1a",
    muted: "#777",
    divider: "rgba(0,0,0,0.08)",
    soonBg: "rgba(0,0,0,0.06)",
    soonText: "#aaa",
    accent: "#4a90d9",
  },
  dark: {
    panel: "rgba(18,18,28,0.96)",
    text: "#e8e8e8",
    muted: "#666",
    divider: "rgba(255,255,255,0.08)",
    soonBg: "rgba(255,255,255,0.05)",
    soonText: "#555",
    accent: "#5ba3f0",
  },
};

// ── coord search ──────────────────────────────────────────────────────────────

function parseCoords(raw: string): [number, number] | null {
  const parts = raw.trim().split(/[\s,]+/).filter(Boolean);
  if (parts.length !== 2) return null;
  const [a, b] = parts.map(Number);
  if (isNaN(a) || isNaN(b)) return null;
  if (a >= -90 && a <= 90 && b >= -180 && b <= 180) return [a, b];
  return null;
}

function SearchBar({
  theme,
  onSearch,
}: {
  theme: Theme;
  onSearch: (lat: number, lon: number) => void;
}) {
  const [value, setValue] = useState("");
  const [error, setError] = useState(false);

  function submit() {
    const coords = parseCoords(value);
    if (!coords) { setError(true); return; }
    setError(false);
    onSearch(coords[0], coords[1]);
  }

  return (
    <div
      style={{
        position: "fixed",
        top: 10,
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: 1000,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        gap: 4,
      }}
    >
      <form
        onSubmit={(e) => { e.preventDefault(); submit(); }}
        style={{ display: "flex", gap: 4 }}
      >
        <input
          value={value}
          onChange={(e) => { setValue(e.target.value); setError(false); }}
          placeholder="lat, lon — e.g. 37.95, −107.75"
          style={{
            width: 260,
            padding: "7px 12px",
            borderRadius: 6,
            border: `1.5px solid ${error ? "#e05a2b" : theme.divider}`,
            background: theme.panel,
            color: theme.text,
            fontFamily: "ui-sans-serif, system-ui, sans-serif",
            fontSize: 13,
            outline: "none",
            boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
          }}
        />
        <button
          type="submit"
          style={{
            padding: "7px 13px",
            borderRadius: 6,
            border: "none",
            background: theme.accent,
            color: "#fff",
            fontFamily: "ui-sans-serif, system-ui, sans-serif",
            fontSize: 13,
            cursor: "pointer",
            boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
          }}
        >
          Go
        </button>
      </form>
      {error && (
        <div
          style={{
            background: theme.panel,
            color: "#e05a2b",
            fontSize: 11,
            padding: "3px 10px",
            borderRadius: 4,
            boxShadow: "0 1px 4px rgba(0,0,0,0.15)",
          }}
        >
          Enter lat, lon — e.g. 37.95, −107.75
        </div>
      )}
    </div>
  );
}

// ── point readout ──────────────────────────────────────────────────────────────

interface PointData {
  lon: number;
  lat: number;
  loading: boolean;
  elevation?: number;
  slope?: number;
  aspect?: number;
}

function aspectCompass(deg: number): string {
  return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(deg / 45) % 8];
}

// ── measure helpers ────────────────────────────────────────────────────────────

const MEASURE_MARKER_STYLE =
  "background:#e05a2b;color:#fff;border-radius:50%;width:22px;height:22px;" +
  "display:flex;align-items:center;justify-content:center;font-size:11px;" +
  "font-weight:700;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,0.3);cursor:default;";

function slopeColor(deg: number): string {
  if (deg < 15) return "#1a9641";
  if (deg < 27) return "#c8a800";
  if (deg < 40) return "#d7191c";
  return "#2b7bb9";
}

async function fetchProfile(
  a: [number, number],
  b: [number, number],
): Promise<ProfileResponse> {
  const p = new URLSearchParams({
    start_lng: String(a[0]),
    start_lat: String(a[1]),
    end_lng: String(b[0]),
    end_lat: String(b[1]),
    region: REGION,
    n: "64",
  });
  const r = await fetch(`${API_URL}/terrain/profile?${p}`);
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json() as Promise<ProfileResponse>;
}

function addMeasureLayers(map: maplibregl.Map) {
  if (map.getSource("measure-line")) return;
  map.addSource("measure-line", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer({
    id: "measure-line",
    type: "line",
    source: "measure-line",
    paint: { "line-color": "#e05a2b", "line-width": 2.5, "line-dasharray": [4, 2] },
  });
}

function updateMeasureSource(map: maplibregl.Map | null, pts: [number, number][]) {
  if (!map) return;
  const src = map.getSource("measure-line") as maplibregl.GeoJSONSource | undefined;
  if (!src) return;
  src.setData(
    pts.length === 2
      ? { type: "Feature", geometry: { type: "LineString", coordinates: pts }, properties: {} }
      : { type: "FeatureCollection", features: [] },
  );
}

// ── map setup helpers ──────────────────────────────────────────────────────────

function addOverlayLayers(
  map: maplibregl.Map,
  visible: Record<string, boolean>,
  opacity: Record<string, number>,
) {
  const firstLabelId = map.getStyle().layers.find((l) => l.type === "symbol")?.id;
  for (const layer of OVERLAY_LAYERS) {
    map.addSource(layer.id, {
      type: "raster",
      tiles: layer.tiles,
      tileSize: 256,
      bounds: COLORADO_MTN_BOUNDS,
      minzoom: 6,
      maxzoom: 16,
      attribution: "USGS 3DEP",
    });
    map.addLayer(
      {
        id: layer.id,
        type: "raster",
        source: layer.id,
        paint: { "raster-opacity": opacity[layer.id] ?? layer.opacity },
        layout: { visibility: visible[layer.id] ? "visible" : "none" },
      },
      firstLabelId,
    );
  }
}

// ── Map component ──────────────────────────────────────────────────────────────

export function Map() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const searchMarkerRef = useRef<maplibregl.Marker | null>(null);

  const [dark, setDark] = useState(false);
  const [visible, setVisible] = useState<Record<string, boolean>>(
    () => Object.fromEntries(OVERLAY_LAYERS.map((l) => [l.id, l.defaultVisible])),
  );
  const [opacity, setOpacity] = useState<Record<string, number>>(
    () => Object.fromEntries(OVERLAY_LAYERS.map((l) => [l.id, l.opacity])),
  );
  const [point, setPoint] = useState<PointData | null>(null);
  const [measureMode, setMeasureMode] = useState(false);
  const [measurePts, setMeasurePts] = useState<[number, number][]>([]);
  const [profile, setProfile] = useState<ProfileResponse | null>(null);
  const [profileLoading, setProfileLoading] = useState(false);

  // Refs so style-load callbacks can read current state without stale closures.
  const visibleRef = useRef(visible);
  const opacityRef = useRef(opacity);
  useEffect(() => { visibleRef.current = visible; }, [visible]);
  useEffect(() => { opacityRef.current = opacity; }, [opacity]);

  const measureModeRef = useRef(false);
  const measurePtsRef = useRef<[number, number][]>([]);
  const measureMarkersRef = useRef<maplibregl.Marker[]>([]);

  // Initialise map once.
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: MAP_STYLES.light,
      center: INITIAL_CENTER,
      zoom: INITIAL_ZOOM,
      renderWorldCopies: false,
    });

    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "imperial" }), "bottom-left");

    map.on("load", () => {
      addOverlayLayers(map, visibleRef.current, opacityRef.current);
      addMeasureLayers(map);
    });

    map.on("click", async (e) => {
      const { lng, lat } = e.lngLat;

      if (measureModeRef.current) {
        const pts = measurePtsRef.current;
        const newPts: [number, number][] =
          pts.length < 2 ? [...pts, [lng, lat]] : [[lng, lat]];
        if (newPts.length === 1) setProfile(null);
        measurePtsRef.current = newPts;
        setMeasurePts(newPts);
        return;
      }

      setPoint({ lon: lng, lat, loading: true });
      const pick = (name: string) =>
        fetch(
          `${TITILER_URL}/cog/point/${lng},${lat}?url=${encodeURIComponent(cogS3(`${REGION}/${name}.tif`))}`,
        )
          .then((r) => (r.ok ? r.json() : null))
          .then((d) => d?.values?.[0] as number | undefined)
          .catch(() => undefined);
      const [elevation, slope, aspect] = await Promise.all([
        pick("dem"),
        pick("slope"),
        pick("aspect"),
      ]);
      setPoint({ lon: lng, lat, loading: false, elevation, slope, aspect });
    });

    mapRef.current = map;
    return () => { map.remove(); mapRef.current = null; };
  }, []);

  // Switch basemap style when dark mode toggles; re-add overlay layers after.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    map.setStyle(dark ? MAP_STYLES.dark : MAP_STYLES.light);
    map.once("style.load", () => {
      addOverlayLayers(map, visibleRef.current, opacityRef.current);
      addMeasureLayers(map);
      updateMeasureSource(map, measurePtsRef.current);
    });
  }, [dark]);

  // Sync visibility state → MapLibre.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const apply = () => {
      for (const [id, isVis] of Object.entries(visible)) {
        if (map.getLayer(id))
          map.setLayoutProperty(id, "visibility", isVis ? "visible" : "none");
      }
    };
    if (map.isStyleLoaded()) apply();
    else map.once("styledata", apply);
  }, [visible]);

  // Sync opacity state → MapLibre.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const apply = () => {
      for (const [id, op] of Object.entries(opacity)) {
        if (map.getLayer(id)) map.setPaintProperty(id, "raster-opacity", op);
      }
    };
    if (map.isStyleLoaded()) apply();
    else map.once("styledata", apply);
  }, [opacity]);

  // Cursor + cleanup when measure mode toggles.
  useEffect(() => {
    measureModeRef.current = measureMode;
    const canvas = mapRef.current?.getCanvas();
    if (canvas) canvas.style.cursor = measureMode ? "crosshair" : "";
    if (!measureMode) {
      measureMarkersRef.current.forEach((m) => m.remove());
      measureMarkersRef.current = [];
      setMeasurePts([]);
      measurePtsRef.current = [];
      setProfile(null);
      updateMeasureSource(mapRef.current, []);
    }
  }, [measureMode]);

  // Markers, line, and profile fetch when measure points change.
  useEffect(() => {
    measurePtsRef.current = measurePts;
    const map = mapRef.current;
    if (!map) return;

    measureMarkersRef.current.forEach((m) => m.remove());
    measureMarkersRef.current = measurePts.map((pt, i) => {
      const el = document.createElement("div");
      el.textContent = i === 0 ? "A" : "B";
      el.style.cssText = MEASURE_MARKER_STYLE;
      return new maplibregl.Marker({ element: el }).setLngLat(pt).addTo(map);
    });

    updateMeasureSource(map, measurePts);

    if (measurePts.length === 2) {
      setProfileLoading(true);
      setProfile(null);
      fetchProfile(measurePts[0], measurePts[1])
        .then((r) => { setProfile(r); setProfileLoading(false); })
        .catch(() => setProfileLoading(false));
    }
  }, [measurePts]);

  const theme = dark ? THEMES.dark : THEMES.light;

  function flyToCoords(lat: number, lon: number) {
    const map = mapRef.current;
    if (!map) return;
    map.flyTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), 13) });
    searchMarkerRef.current?.remove();
    searchMarkerRef.current = new maplibregl.Marker({ color: theme.accent })
      .setLngLat([lon, lat])
      .addTo(map);
  }

  return (
    <>
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />
      <SearchBar theme={theme} onSearch={flyToCoords} />
      <LayerPanel
        groups={LAYER_GROUPS}
        visible={visible}
        opacity={opacity}
        dark={dark}
        theme={theme}
        onToggle={(id) => setVisible((v) => ({ ...v, [id]: !v[id] }))}
        onOpacity={(id, val) => setOpacity((o) => ({ ...o, [id]: val }))}
        onDarkToggle={() => setDark((d) => !d)}
      />
      <MeasureButton
        active={measureMode}
        theme={theme}
        onToggle={() => setMeasureMode((m) => !m)}
      />
      {measureMode && (
        <MeasurePanel
          pts={measurePts}
          loading={profileLoading}
          profile={profile}
          theme={theme}
          onClose={() => setMeasureMode(false)}
        />
      )}
      {!measureMode && point && (
        <InfoPanel data={point} theme={theme} onClose={() => setPoint(null)} />
      )}
    </>
  );
}

// ── LayerPanel ─────────────────────────────────────────────────────────────────

function LayerPanel({
  groups,
  visible,
  opacity,
  dark,
  theme,
  onToggle,
  onOpacity,
  onDarkToggle,
}: {
  groups: LayerGroup[];
  visible: Record<string, boolean>;
  opacity: Record<string, number>;
  dark: boolean;
  theme: Theme;
  onToggle: (id: string) => void;
  onOpacity: (id: string, val: number) => void;
  onDarkToggle: () => void;
}) {
  return (
    <div
      style={{
        position: "fixed",
        top: 10,
        left: 10,
        background: theme.panel,
        borderRadius: 8,
        padding: "12px 14px",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: 13,
        color: theme.text,
        boxShadow: "0 2px 12px rgba(0,0,0,0.18)",
        display: "flex",
        flexDirection: "column",
        gap: 0,
        zIndex: 1000,
        width: 210,
        maxHeight: "calc(100vh - 20px)",
        overflowY: "auto",
        userSelect: "none",
      }}
    >
      {/* Header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 10,
        }}
      >
        <span style={{ fontWeight: 700, fontSize: 12, letterSpacing: "0.08em", color: theme.muted }}>
          LAYERS
        </span>
        <button
          onClick={onDarkToggle}
          title={dark ? "Switch to light mode" : "Switch to dark mode"}
          style={{
            background: "none",
            border: "none",
            cursor: "pointer",
            fontSize: 15,
            padding: "2px 4px",
            borderRadius: 4,
            color: theme.text,
            lineHeight: 1,
          }}
        >
          {dark ? "☀️" : "🌙"}
        </button>
      </div>

      {/* Groups */}
      {groups.map((group, gi) => (
        <div key={group.id} style={{ marginBottom: gi < groups.length - 1 ? 12 : 0 }}>
          {/* Group header */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              marginBottom: 6,
              paddingBottom: 5,
              borderBottom: `1px solid ${theme.divider}`,
            }}
          >
            <span
              style={{
                width: 7,
                height: 7,
                borderRadius: "50%",
                background: group.color,
                flexShrink: 0,
              }}
            />
            <span
              style={{
                fontSize: 11,
                fontWeight: 600,
                letterSpacing: "0.06em",
                color: theme.muted,
                textTransform: "uppercase",
              }}
            >
              {group.label}
            </span>
          </div>

          {/* Active layers */}
          {group.active.map((layer) => (
            <div key={layer.id} style={{ marginBottom: 8 }}>
              <label
                style={{ display: "flex", alignItems: "center", gap: 7, cursor: "pointer", marginBottom: 3 }}
              >
                <input
                  type="checkbox"
                  checked={visible[layer.id] ?? false}
                  onChange={() => onToggle(layer.id)}
                  style={{ accentColor: group.color, cursor: "pointer" }}
                />
                <span>{layer.label}</span>
              </label>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={opacity[layer.id] ?? layer.opacity}
                onChange={(e) => onOpacity(layer.id, parseFloat(e.target.value))}
                style={{ width: "100%", accentColor: group.color, margin: 0, display: "block" }}
              />
              {layer.legend && visible[layer.id] && (
                <div style={{ marginTop: 4 }}>
                  <div style={{ height: 7, borderRadius: 3, background: layer.legend.gradient }} />
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      fontSize: 10,
                      color: theme.muted,
                      marginTop: 2,
                    }}
                  >
                    {layer.legend.stops.map((s, i) => <span key={i}>{s}</span>)}
                  </div>
                </div>
              )}
            </div>
          ))}

          {/* Upcoming layers */}
          {group.upcoming.map((layer) => (
            <div
              key={layer.id}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                marginBottom: 5,
                opacity: 0.5,
              }}
            >
              <span style={{ display: "flex", alignItems: "center", gap: 7 }}>
                <span
                  style={{
                    width: 13,
                    height: 13,
                    borderRadius: 2,
                    border: `1.5px solid ${theme.muted}`,
                    flexShrink: 0,
                  }}
                />
                <span style={{ color: theme.text }}>{layer.label}</span>
              </span>
              <span
                style={{
                  fontSize: 9,
                  fontWeight: 600,
                  letterSpacing: "0.05em",
                  color: theme.muted,
                  background: theme.soonBg,
                  borderRadius: 3,
                  padding: "2px 5px",
                }}
              >
                SOON
              </span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

// ── InfoPanel ──────────────────────────────────────────────────────────────────

function InfoPanel({
  data,
  theme,
  onClose,
}: {
  data: PointData;
  theme: Theme;
  onClose: () => void;
}) {
  const fmt = (n: number | undefined, dec = 0) =>
    n == null || n === -9999 ? "—" : n.toFixed(dec);

  const elevM = data.elevation != null && data.elevation !== -9999 ? data.elevation : null;
  const elevFt = elevM != null ? (elevM * 3.28084).toFixed(0) : null;

  return (
    <div
      style={{
        position: "fixed",
        bottom: 36,
        left: "50%",
        transform: "translateX(-50%)",
        background: theme.panel,
        borderRadius: 8,
        padding: "9px 16px",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: 13,
        color: theme.text,
        boxShadow: "0 2px 10px rgba(0,0,0,0.22)",
        display: "flex",
        alignItems: "center",
        gap: 20,
        zIndex: 1000,
        whiteSpace: "nowrap",
      }}
    >
      {data.loading ? (
        <span style={{ color: theme.muted }}>Loading…</span>
      ) : (
        <>
          <span>
            <b>Elev</b>{" "}
            {elevM != null ? `${fmt(elevM)} m / ${elevFt} ft` : "—"}
          </span>
          <span><b>Slope</b> {fmt(data.slope, 1)}°</span>
          <span>
            <b>Aspect</b>{" "}
            {data.aspect != null && data.aspect !== -9999
              ? `${fmt(data.aspect, 0)}° ${aspectCompass(data.aspect)}`
              : "—"}
          </span>
          <span style={{ color: theme.muted, fontSize: 11 }}>
            {data.lat.toFixed(4)}°, {data.lon.toFixed(4)}°
          </span>
        </>
      )}
      <button
        onClick={onClose}
        style={{
          background: "none",
          border: "none",
          cursor: "pointer",
          color: theme.muted,
          fontSize: 16,
          lineHeight: 1,
          padding: "0 0 0 4px",
        }}
      >
        ×
      </button>
    </div>
  );
}

// ── MeasureButton ──────────────────────────────────────────────────────────────

function MeasureButton({
  active,
  theme,
  onToggle,
}: {
  active: boolean;
  theme: Theme;
  onToggle: () => void;
}) {
  return (
    <button
      onClick={onToggle}
      title={active ? "Exit slope measurement" : "Measure slope between two points"}
      style={{
        position: "fixed",
        bottom: 36,
        right: 10,
        zIndex: 1000,
        background: active ? theme.accent : theme.panel,
        color: active ? "#fff" : theme.text,
        border: `1.5px solid ${active ? theme.accent : theme.divider}`,
        borderRadius: 6,
        padding: "7px 12px",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: 12,
        fontWeight: 600,
        cursor: "pointer",
        boxShadow: "0 2px 8px rgba(0,0,0,0.18)",
        letterSpacing: "0.03em",
      }}
    >
      Measure Slope
    </button>
  );
}

// ── MeasurePanel ───────────────────────────────────────────────────────────────

function MeasurePanel({
  pts,
  loading,
  profile,
  theme,
  onClose,
}: {
  pts: [number, number][];
  loading: boolean;
  profile: ProfileResponse | null;
  theme: Theme;
  onClose: () => void;
}) {
  let content: React.ReactNode;

  if (loading) {
    content = <span style={{ color: theme.muted }}>Sampling terrain…</span>;
  } else if (profile) {
    const s = profile.summary;
    const distMi = (s.distance_m / 1609.344).toFixed(2);
    const gainFt = s.elevation_gain_m != null ? Math.round(s.elevation_gain_m * 3.28084) : null;
    const lossFt = s.elevation_loss_m != null ? Math.round(s.elevation_loss_m * 3.28084) : null;
    const avg = s.avg_slope_deg;
    content = (
      <>
        <span style={{ color: theme.muted, fontSize: 11 }}>A→B</span>
        <span><b>Dist</b> {distMi} mi</span>
        <span>
          <b>Avg slope</b>{" "}
          <span style={{ color: avg != null ? slopeColor(avg) : theme.muted, fontWeight: 700 }}>
            {avg != null ? `${avg.toFixed(1)}°` : "—"}
          </span>
        </span>
        <span><b>Max</b> {s.max_slope_deg != null ? `${s.max_slope_deg.toFixed(1)}°` : "—"}</span>
        {gainFt != null && <span style={{ color: "#1a9641" }}>+{gainFt} ft</span>}
        {lossFt != null && <span style={{ color: theme.muted }}>−{lossFt} ft</span>}
      </>
    );
  } else if (pts.length === 1) {
    content = <span style={{ color: theme.muted }}>Click map to set end point (B)</span>;
  } else {
    content = <span style={{ color: theme.muted }}>Click map to set start point (A)</span>;
  }

  return (
    <div
      style={{
        position: "fixed",
        bottom: 36,
        left: "50%",
        transform: "translateX(-50%)",
        background: theme.panel,
        borderRadius: 8,
        padding: "9px 16px",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: 13,
        color: theme.text,
        boxShadow: "0 2px 10px rgba(0,0,0,0.22)",
        display: "flex",
        alignItems: "center",
        gap: 16,
        zIndex: 1000,
        whiteSpace: "nowrap",
      }}
    >
      {content}
      <button
        onClick={onClose}
        style={{
          background: "none",
          border: "none",
          cursor: "pointer",
          color: theme.muted,
          fontSize: 16,
          lineHeight: 1,
          padding: "0 0 0 4px",
        }}
      >
        ×
      </button>
    </div>
  );
}
