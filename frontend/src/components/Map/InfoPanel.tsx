import type { ForecastPeriod, PointData, Units } from "./types";
import type { Theme } from "./theme";
import { aspectCompass, mobilePanelStyle, panelShared, fmtTempF, fmtWindSpeed } from "./utils";
import { DragHandle, useDraggable } from "./useDraggable";
import { useEscapeKey } from "../../hooks/useEscapeKey";

export function InfoPanel({
  data,
  forecast,
  forecastLoading,
  units,
  theme,
  mobile,
  mobileBottom,
  onClose,
}: {
  data: PointData;
  forecast: ForecastPeriod[] | null;
  forecastLoading: boolean;
  units: Units;
  theme: Theme;
  mobile?: boolean;
  mobileBottom?: number;
  onClose: () => void;
}) {
  const isMobile = mobile ?? false;
  const { panelRef, handleProps, panelEventProps, dragStyle } = useDraggable(isMobile);

  useEscapeKey(onClose);

  const imp = units === "imperial";
  const fmt = (n: number | undefined, dec = 0) =>
    n == null || n === -9999 ? "—" : n.toFixed(dec);

  const elevM = data.elevation != null && data.elevation !== -9999 ? data.elevation : null;
  const elevStr = elevM != null
    ? imp ? `${(elevM * 3.28084).toFixed(0)} ft` : `${elevM.toFixed(0)} m`
    : "—";

  const baseStyle = isMobile
    ? mobilePanelStyle(mobileBottom, theme, { padding: "12px 16px", fontSize: 14 })
    : {
        ...panelShared(theme),
        bottom: 36,
        right: 10,
        borderRadius: 10,
        padding: "10px 14px",
        fontSize: 13,
        boxShadow: "0 2px 16px rgba(0,0,0,0.28)",
        width: 240,
      };

  const statLabel: React.CSSProperties = { color: theme.muted, fontSize: 11, marginBottom: 1 };
  const statVal: React.CSSProperties   = { fontWeight: 600, fontSize: 13 };

  return (
    <div
      ref={panelRef}
      role="dialog"
      aria-label="Point info"
      style={{ ...baseStyle, ...dragStyle }}
      {...panelEventProps}
    >
      <DragHandle mobile={isMobile} handleProps={handleProps} theme={theme} />

      {/* Header row: location name + close */}
      <div style={{ display: "flex", alignItems: "flex-start", gap: 6, marginBottom: 8 }}>
        <div style={{ flex: 1, fontSize: 11, color: theme.muted, lineHeight: 1.3 }}>
          {data.loading ? (
            <span style={{ fontStyle: "italic" }}>Locating…</span>
          ) : data.locationName ? (
            <span>{data.locationName}</span>
          ) : (
            <span>{data.lat.toFixed(4)}°, {data.lon.toFixed(4)}°</span>
          )}
        </div>
        <button
          onClick={onClose}
          aria-label="Close point info"
          style={{ background: "none", border: "none", cursor: "pointer", color: theme.muted, fontSize: 16, lineHeight: 1, padding: 0, flexShrink: 0 }}
        >
          ×
        </button>
      </div>

      {/* Terrain stats grid */}
      {!data.loading && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px 10px", marginBottom: 8 }}>
          <div>
            <div style={statLabel}>Elevation</div>
            <div style={statVal}>{elevStr}</div>
          </div>
          <div>
            <div style={statLabel}>Slope</div>
            <div style={statVal}>{fmt(data.slope, 1)}°</div>
          </div>
          <div>
            <div style={statLabel}>Aspect</div>
            <div style={statVal}>
              {data.aspect != null && data.aspect !== -9999
                ? `${fmt(data.aspect, 0)}° ${aspectCompass(data.aspect)}`
                : "—"}
            </div>
          </div>
          {data.tempF != null && (
            <div>
              <div style={statLabel}>Temp</div>
              <div style={statVal}>{fmtTempF(data.tempF, units)}</div>
            </div>
          )}
          {data.snowDepthIn != null && data.snowDepthIn > 0 && (
            <div>
              <div style={statLabel}>Snow depth</div>
              <div style={statVal}>
                {imp ? `${data.snowDepthIn.toFixed(0)}"` : `${Math.round(data.snowDepthIn * 2.54)} cm`}
              </div>
            </div>
          )}
          {!forecastLoading && forecast && forecast.length > 0 && (
            <div>
              <div style={statLabel}>Wind</div>
              <div style={statVal}>{fmtWindSpeed(forecast[0].windSpeed, units)} {forecast[0].windDirection}</div>
            </div>
          )}
        </div>
      )}

      {data.loading && (
        <div style={{ color: theme.muted, fontSize: 12, marginBottom: 8 }}>Loading…</div>
      )}

      {/* Compact coords when location name is shown */}
      {!data.loading && data.locationName && (
        <div style={{ fontSize: 10, color: theme.muted, marginBottom: 8 }}>
          {data.lat.toFixed(4)}°, {data.lon.toFixed(4)}°
        </div>
      )}

      {/* Forecast */}
      {forecastLoading && (
        <div style={{ borderTop: `1px solid ${theme.divider}`, paddingTop: 8, color: theme.muted, fontSize: 11 }}>
          Loading forecast…
        </div>
      )}
      {!forecastLoading && forecast && forecast.length > 0 && (
        <div style={{ borderTop: `1px solid ${theme.divider}`, paddingTop: 8 }}>
          {forecast.slice(0, 4).map((p, i) => {
            const precip = p.probabilityOfPrecipitation?.value;
            return (
              <div key={i} style={{
                display: "flex", justifyContent: "space-between", alignItems: "baseline",
                fontSize: 11, paddingBottom: 5, marginBottom: 5,
                borderBottom: i < 3 ? `1px solid ${theme.divider}` : undefined,
                opacity: i === 0 ? 1 : 0.85,
              }}>
                <span style={{ fontWeight: 600, color: theme.muted, minWidth: 70 }}>{p.name}</span>
                <span style={{ fontWeight: 600 }}>{fmtTempF(p.temperature, units)}</span>
                <span style={{ color: theme.muted }}>{fmtWindSpeed(p.windSpeed, units)}</span>
                {precip != null && <span style={{ color: "#4a90d9" }}>{precip}%</span>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
