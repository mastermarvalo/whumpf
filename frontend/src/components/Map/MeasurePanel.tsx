import type { CSSProperties, ReactNode } from "react";
import type { ProfileResponse, Units } from "./types";
import type { Theme } from "./theme";
import { slopeColor } from "./utils";
import { ProfileChart } from "./ProfileChart";

export function MeasurePanel({
  pts,
  loading,
  profile,
  units,
  theme,
  mobile,
  mobileBottom,
  onClose,
}: {
  pts: [number, number][];
  loading: boolean;
  profile: ProfileResponse | null;
  units: Units;
  theme: Theme;
  mobile?: boolean;
  mobileBottom?: number;
  onClose: () => void;
}) {
  const imp = units === "imperial";

  const summaryRow = profile ? (() => {
    const s = profile.summary;
    const distStr = imp
      ? `${(s.distance_m / 1609.344).toFixed(2)} mi`
      : `${(s.distance_m / 1000).toFixed(2)} km`;
    const gainStr = s.elevation_gain_m != null
      ? imp ? `+${Math.round(s.elevation_gain_m * 3.28084)} ft` : `+${Math.round(s.elevation_gain_m)} m`
      : null;
    const lossStr = s.elevation_loss_m != null
      ? imp ? `−${Math.round(s.elevation_loss_m * 3.28084)} ft` : `−${Math.round(s.elevation_loss_m)} m`
      : null;
    const avg = s.avg_slope_deg;
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap", paddingTop: 6 }}>
        <span><b>Dist</b> {distStr}</span>
        <span>
          <b>Avg</b>{" "}
          <span style={{ color: avg != null ? slopeColor(avg) : theme.muted, fontWeight: 700 }}>
            {avg != null ? `${avg.toFixed(1)}°` : "—"}
          </span>
        </span>
        <span><b>Max</b> {s.max_slope_deg != null ? `${s.max_slope_deg.toFixed(1)}°` : "—"}</span>
        {gainStr && <span style={{ color: "#1a9641" }}>{gainStr}</span>}
        {lossStr && <span style={{ color: theme.muted }}>{lossStr}</span>}
      </div>
    );
  })() : null;

  let statusLine: ReactNode = null;
  if (loading) {
    statusLine = <span style={{ color: theme.muted }}>Sampling terrain…</span>;
  } else if (!profile) {
    statusLine = (
      <span style={{ color: theme.muted }}>
        {pts.length === 1 ? "Click map to set end point (B)" : "Click map to set start point (A)"}
      </span>
    );
  }

  const hasChart = !loading && profile != null;

  const panelStyle: CSSProperties = mobile ? {
    position: "fixed",
    bottom: mobileBottom,
    left: 8,
    right: 8,
    background: theme.panel,
    borderRadius: 12,
    padding: "12px 16px",
    fontFamily: "ui-sans-serif, system-ui, sans-serif",
    fontSize: 13,
    color: theme.text,
    boxShadow: "0 2px 16px rgba(0,0,0,0.28)",
    zIndex: 999,
  } : {
    position: "fixed",
    bottom: 36,
    left: "50%",
    transform: "translateX(-50%)",
    background: theme.panel,
    borderRadius: 8,
    padding: hasChart ? "10px 14px 8px" : "9px 16px",
    fontFamily: "ui-sans-serif, system-ui, sans-serif",
    fontSize: 13,
    color: theme.text,
    boxShadow: "0 2px 10px rgba(0,0,0,0.22)",
    zIndex: 1000,
    width: hasChart ? 360 : undefined,
    whiteSpace: hasChart ? undefined : "nowrap",
  };

  return (
    <div style={panelStyle}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <span style={{ color: theme.muted, fontSize: 11 }}>A → B</span>
        <button
          onClick={onClose}
          style={{
            background: "none", border: "none", cursor: "pointer",
            color: theme.muted, fontSize: 18, lineHeight: 1,
            padding: 4, minWidth: 28, minHeight: 28,
            display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >×</button>
      </div>
      {hasChart && (
        <ProfileChart
          samples={profile!.samples}
          summary={profile!.summary}
          units={units}
          theme={theme}
        />
      )}
      {summaryRow}
      {statusLine}
    </div>
  );
}
