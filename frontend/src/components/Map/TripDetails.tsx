import type { ProfileSummary, Units } from "./types";
import type { Theme } from "./theme";

// Cardinal-order so the aspect-distribution row reads N → NW (compass-clockwise).
const ASPECT_ORDER = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"] as const;

// Avalanche-relevant buckets — mirror the backend's _SLOPE_BUCKETS.
const SLOPE_ORDER = ["0-15", "15-27", "27-30", "30-35", "35-45", "45+"] as const;

// Color per slope bucket: green → yellow → red ramp aligned with the
// CalTopo V1 slope colormap; 27-30 is the "first concern" yellow band.
const SLOPE_BUCKET_COLOR: Record<string, string> = {
  "0-15":  "#1a9641",
  "15-27": "#a6d96a",
  "27-30": "#ffeb00",
  "30-35": "#f4820a",
  "35-45": "#d7191c",
  "45+":   "#2b7bb9",
};

/**
 * Slope + aspect distribution histograms and the elevation range row. Shared by
 * the A→B measure panel and the saved-route profile view so the two can't drift.
 */
export function TripDetails({
  summary,
  units,
  theme,
}: {
  summary: ProfileSummary;
  units: Units;
  theme: Theme;
}) {
  const imp = units === "imperial";
  const fmtElev = (e: number | null | undefined) =>
    e == null ? "—" : imp ? `${Math.round(e * 3.28084).toLocaleString()} ft` : `${Math.round(e).toLocaleString()} m`;

  const slopeDist = summary.slope_distribution ?? {};
  const aspectDist = summary.aspect_distribution ?? {};
  const minElev = summary.min_elevation_m ?? null;
  const maxElev = summary.max_elevation_m ?? null;

  // Total distribution may be < 1 when some samples have nodata; normalise the
  // bar widths to the data we actually have so the bar fills the row.
  const slopeTotal = Object.values(slopeDist).reduce((a, b) => a + b, 0) || 1;
  const aspectMax = Math.max(0.0001, ...Object.values(aspectDist));

  return (
    <div style={{
      marginTop: 8,
      paddingTop: 8,
      borderTop: `1px solid ${theme.divider}`,
      display: "flex",
      flexDirection: "column",
      gap: 6,
      fontSize: 11,
    }}>
      {/* Elevation range */}
      <div style={{ display: "flex", justifyContent: "space-between", color: theme.muted }}>
        <span>Elev range</span>
        <span style={{ color: theme.text, fontWeight: 600 }}>
          {fmtElev(minElev)} – {fmtElev(maxElev)}
        </span>
      </div>

      {/* Slope distribution — stacked bar */}
      {Object.keys(slopeDist).length > 0 && (
        <div>
          <div style={{ color: theme.muted, marginBottom: 3 }}>Slope distribution</div>
          <div style={{ display: "flex", height: 12, borderRadius: 3, overflow: "hidden", border: `1px solid ${theme.divider}` }}>
            {SLOPE_ORDER.map((bucket) => {
              const frac = (slopeDist[bucket] ?? 0) / slopeTotal;
              if (frac < 0.005) return null;
              return (
                <div
                  key={bucket}
                  title={`${bucket}° — ${(frac * 100).toFixed(0)}%`}
                  style={{
                    width: `${frac * 100}%`,
                    background: SLOPE_BUCKET_COLOR[bucket],
                  }}
                />
              );
            })}
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", marginTop: 2, fontSize: 9, color: theme.muted }}>
            {SLOPE_ORDER.map((bucket) => {
              const frac = slopeDist[bucket] ?? 0;
              if (frac < 0.05) return null;
              return (
                <span key={bucket} style={{ color: SLOPE_BUCKET_COLOR[bucket], fontWeight: 600 }}>
                  {bucket}° {(frac * 100).toFixed(0)}%
                </span>
              );
            })}
          </div>
        </div>
      )}

      {/* Aspect distribution — 8 bars, one per cardinal direction. Bar
          heights are pixel values, not percentages; % heights inside flex
          children with auto-height parents resolve to 0 and the bars vanish. */}
      {Object.keys(aspectDist).length > 0 && (
        <div>
          <div style={{ color: theme.muted, marginBottom: 3 }}>Aspect distribution</div>
          <div style={{ display: "flex", gap: 3, height: 34 }}>
            {ASPECT_ORDER.map((a) => {
              const frac = aspectDist[a] ?? 0;
              // The tallest bar is 22px; the rest scale proportionally. Bars
              // with any data get at least a 2px stub so a 1% sliver is still
              // visible.
              const heightPx = Math.max(Math.round((frac / aspectMax) * 22), frac > 0 ? 2 : 0);
              return (
                <div key={a} style={{ flex: 1, display: "flex", flexDirection: "column" }}>
                  <div style={{ height: 22, display: "flex", alignItems: "flex-end" }}>
                    <div
                      title={`${a} — ${(frac * 100).toFixed(0)}%`}
                      style={{
                        width: "100%",
                        height: heightPx,
                        background: theme.accent,
                        borderRadius: "2px 2px 0 0",
                        opacity: 0.85,
                      }}
                    />
                  </div>
                  <span style={{
                    fontSize: 9,
                    color: theme.muted,
                    textAlign: "center",
                    lineHeight: "12px",
                  }}>
                    {a}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

    </div>
  );
}
