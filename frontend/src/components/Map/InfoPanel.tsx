import type { ForecastPeriod, PointData, Units } from "./types";
import type { Theme } from "./theme";
import { aspectCompass, mobilePanelStyle, panelShared, fmtTempF, fmtWindSpeed } from "./utils";
import { DragHandle, useDraggable } from "./useDraggable";
import { useEscapeKey } from "../../hooks/useEscapeKey";

type IconKind = "sun" | "partly-cloudy" | "cloud" | "fog" | "rain" | "snow" | "sleet" | "thunder";

function iconKind(shortForecast: string): IconKind {
  const f = shortForecast.toLowerCase();
  if (/thunder|t-storm/.test(f))                              return "thunder";
  if (/sleet|wintry|freezing rain/.test(f) || (/snow/.test(f) && /rain/.test(f))) return "sleet";
  if (/snow|flurr|blizzard/.test(f))                         return "snow";
  if (/rain|shower|drizzle/.test(f))                         return "rain";
  if (/fog|mist|haze/.test(f))                               return "fog";
  if (/partly|mostly sun|mostly clear/.test(f))              return "partly-cloudy";
  if (/cloud|overcast/.test(f))                              return "cloud";
  return "sun";
}

function WeatherIcon({ shortForecast, size = 15 }: { shortForecast: string; size?: number }) {
  const kind = iconKind(shortForecast);
  const s = size;
  const cloudPath = `M1.5 ${s * 0.64}a${s * 0.25} ${s * 0.25} 0 1 1 ${s * 0.43}-${s * 0.18} ${s * 0.17} ${s * 0.17} 0 0 1 0 ${s * 0.34}H1.5z`;

  if (kind === "thunder") return (
    <svg width={s} height={s} viewBox="0 0 15 15" fill="none" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 9.5a3.2 3.2 0 1 1 5.2-3.2 2.3 2.3 0 0 1 0 4.6H2z" fill="currentColor" fillOpacity=".18" stroke="currentColor" strokeWidth="1.3"/>
      <polyline points="7.5,7 6,10.5 8.2,10.5 6.5,14" strokeWidth="1.5" stroke="#f5c518" fill="none"/>
    </svg>
  );
  if (kind === "sleet") return (
    <svg width={s} height={s} viewBox="0 0 15 15" fill="none" strokeLinecap="round">
      <path d="M2 9a3 3 0 1 1 5-2.5 2 2 0 0 1 0 4H2z" fill="currentColor" fillOpacity=".18" stroke="currentColor" strokeWidth="1.3"/>
      <line x1="3.5" y1="11.5" x2="2.5" y2="13.5" stroke="#4a90d9" strokeWidth="1.3"/>
      <line x1="7" y1="11.5" x2="7" y2="13.5" stroke="currentColor" strokeWidth="1.3"/>
      <line x1="10.5" y1="11.5" x2="9.5" y2="13.5" stroke="#4a90d9" strokeWidth="1.3"/>
    </svg>
  );
  if (kind === "snow") return (
    <svg width={s} height={s} viewBox="0 0 15 15" fill="none" strokeLinecap="round">
      <path d="M2 9a3 3 0 1 1 5-2.5 2 2 0 0 1 0 4H2z" fill="currentColor" fillOpacity=".18" stroke="currentColor" strokeWidth="1.3"/>
      {[3.8, 7, 10.2].map((cx) => (
        <g key={cx} stroke="currentColor" strokeWidth="1.2">
          <line x1={cx} y1="11" x2={cx} y2="13.5"/>
          <line x1={cx - 1} y1="11.8" x2={cx + 1} y2="12.7"/>
          <line x1={cx + 1} y1="11.8" x2={cx - 1} y2="12.7"/>
        </g>
      ))}
    </svg>
  );
  if (kind === "rain") return (
    <svg width={s} height={s} viewBox="0 0 15 15" fill="none" strokeLinecap="round">
      <path d="M2 9a3 3 0 1 1 5-2.5 2 2 0 0 1 0 4H2z" fill="currentColor" fillOpacity=".18" stroke="currentColor" strokeWidth="1.3"/>
      <line x1="3.5" y1="11.5" x2="2.5" y2="13.5" stroke="#4a90d9" strokeWidth="1.3"/>
      <line x1="7" y1="11.5" x2="6" y2="13.5" stroke="#4a90d9" strokeWidth="1.3"/>
      <line x1="10.5" y1="11.5" x2="9.5" y2="13.5" stroke="#4a90d9" strokeWidth="1.3"/>
    </svg>
  );
  if (kind === "fog") return (
    <svg width={s} height={s} viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round">
      <line x1="2" y1="5" x2="13" y2="5"/>
      <line x1="1" y1="8" x2="14" y2="8"/>
      <line x1="3" y1="11" x2="12" y2="11"/>
    </svg>
  );
  if (kind === "partly-cloudy") return (
    <svg width={s} height={s} viewBox="0 0 15 15" fill="none" strokeLinecap="round">
      <circle cx="5.5" cy="5.5" r="2.5" fill="#f5c518" fillOpacity=".7" stroke="#f5c518" strokeWidth="1.2"/>
      <line x1="5.5" y1="1.5" x2="5.5" y2="0.5" stroke="#f5c518" strokeWidth="1.2"/>
      <line x1="1.5" y1="5.5" x2="0.5" y2="5.5" stroke="#f5c518" strokeWidth="1.2"/>
      <line x1="2.7" y1="2.7" x2="2" y2="2" stroke="#f5c518" strokeWidth="1.2"/>
      <path d="M5.5 9.5a2.8 2.8 0 0 1 5-1.5A2 2 0 0 1 10 12H5.5z" fill="currentColor" fillOpacity=".2" stroke="currentColor" strokeWidth="1.3"/>
    </svg>
  );
  if (kind === "cloud") return (
    <svg width={s} height={s} viewBox="0 0 15 15" fill="none" strokeLinecap="round">
      <path d={cloudPath} fill="currentColor" fillOpacity=".18" stroke="currentColor" strokeWidth="1.3"/>
    </svg>
  );
  // sun
  return (
    <svg width={s} height={s} viewBox="0 0 15 15" fill="none" stroke="#f5c518" strokeWidth="1.4" strokeLinecap="round">
      <circle cx="7.5" cy="7.5" r="2.6"/>
      {[0, 45, 90, 135, 180, 225, 270, 315].map((deg) => {
        const r0 = 4.3, r1 = 5.5;
        const a = (deg * Math.PI) / 180;
        return <line key={deg} x1={7.5 + r0 * Math.sin(a)} y1={7.5 - r0 * Math.cos(a)} x2={7.5 + r1 * Math.sin(a)} y2={7.5 - r1 * Math.cos(a)}/>;
      })}
    </svg>
  );
}

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
        top: 56,
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
            const pop = p.probabilityOfPrecipitation?.value;
            const pIn = p.precipIn;
            const precipStr = pIn != null
              ? imp ? `${pIn < 0.1 ? pIn.toFixed(2) : pIn.toFixed(1)}"` : `${Math.round(pIn * 25.4)}mm`
              : null;
            return (
              <div key={i} style={{
                display: "flex", alignItems: "center", gap: 5,
                fontSize: 11, paddingBottom: 5, marginBottom: 5,
                borderBottom: i < 3 ? `1px solid ${theme.divider}` : undefined,
                opacity: i === 0 ? 1 : 0.85,
              }}>
                <span style={{ flexShrink: 0, display: "flex", alignItems: "center", color: theme.text }}>
                  <WeatherIcon shortForecast={p.shortForecast} size={15} />
                </span>
                <span style={{ fontWeight: 600, color: theme.muted, flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p.name}</span>
                <span style={{ fontWeight: 600, flexShrink: 0 }}>{fmtTempF(p.temperature, units)}</span>
                <span style={{ color: theme.muted, flexShrink: 0 }}>{fmtWindSpeed(p.windSpeed, units)}</span>
                {(pop != null || precipStr) && (
                  <span style={{ color: "#4a90d9", flexShrink: 0, fontSize: 10 }}>
                    {pop != null ? `${pop}%` : ""}{pop != null && precipStr ? " " : ""}{precipStr ?? ""}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
