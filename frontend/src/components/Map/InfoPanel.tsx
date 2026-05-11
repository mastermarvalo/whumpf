import { useEffect } from "react";
import type { ForecastPeriod, PointData, Units } from "./types";
import type { Theme } from "./theme";
import { aspectCompass } from "./utils";
import { Z } from "./zIndex";

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
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const imp = units === "imperial";
  const fmt = (n: number | undefined, dec = 0) =>
    n == null || n === -9999 ? "—" : n.toFixed(dec);

  const elevM = data.elevation != null && data.elevation !== -9999 ? data.elevation : null;
  const elevStr = elevM != null
    ? imp
      ? `${(elevM * 3.28084).toFixed(0)} ft`
      : `${elevM.toFixed(0)} m`
    : "—";

  return (
    <div
      role="dialog"
      aria-label="Point info"
      style={mobile ? {
        position: "fixed",
        bottom: mobileBottom,
        left: 8,
        right: 8,
        background: theme.panel,
        borderRadius: 12,
        padding: "12px 16px",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: 14,
        color: theme.text,
        boxShadow: "0 2px 16px rgba(0,0,0,0.28)",
        zIndex: Z.FLOATING_PANEL,
      } : {
        position: "fixed",
        bottom: 36,
        left: "50%",
        transform: "translateX(-50%)",
        background: theme.panel,
        borderRadius: 8,
        padding: "10px 16px",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: 13,
        color: theme.text,
        boxShadow: "0 2px 12px rgba(0,0,0,0.24)",
        zIndex: Z.FLOATING_PANEL,
        maxWidth: "calc(100vw - 40px)",
        minWidth: 320,
      }}
    >
      {/* Location name */}
      {(data.locationName || data.loading) && (
        <div style={{ fontSize: 12, color: theme.muted, marginBottom: 7, display: "flex", alignItems: "center", gap: 5 }}>
          <span style={{ fontSize: 11 }}>📍</span>
          <span style={{ fontStyle: data.loading ? "italic" : undefined }}>
            {data.loading ? "Locating…" : data.locationName}
          </span>
        </div>
      )}

      {/* Terrain row */}
      <div style={{ display: "flex", alignItems: "center", gap: 18, flexWrap: "wrap" }}>
        {data.loading ? (
          <span style={{ color: theme.muted }}>Loading…</span>
        ) : (
          <>
            <span><b>Elev</b> {elevStr}</span>
            <span><b>Slope</b> {fmt(data.slope, 1)}°</span>
            <span>
              <b>Aspect</b>{" "}
              {data.aspect != null && data.aspect !== -9999
                ? `${fmt(data.aspect, 0)}° ${aspectCompass(data.aspect)}`
                : "—"}
            </span>
            {data.tempF != null && (
              <span>
                <b>Temp</b>{" "}
                {imp
                  ? `${Math.round(data.tempF)}°F`
                  : `${Math.round((data.tempF - 32) * 5 / 9)}°C`}
              </span>
            )}
            {data.snowDepthIn != null && data.snowDepthIn > 0 && (
              <span>
                <b>Snow</b>{" "}
                {imp
                  ? `${data.snowDepthIn.toFixed(0)}"`
                  : `${Math.round(data.snowDepthIn * 2.54)} cm`}
              </span>
            )}
            {!forecastLoading && forecast && forecast.length > 0 && (
              <span>
                <b>Wind</b>{" "}
                {imp
                  ? forecast[0].windSpeed
                  : forecast[0].windSpeed.replace(/\d+/g, (n) => String(Math.round(Number(n) * 1.60934))).replace("mph", "km/h")
                }{" "}{forecast[0].windDirection}
              </span>
            )}
            <span style={{ color: theme.muted, fontSize: 11 }}>
              {data.lat.toFixed(4)}°, {data.lon.toFixed(4)}°
            </span>
          </>
        )}
        <button
          onClick={onClose}
          aria-label="Close point info"
          style={{
            marginLeft: "auto",
            background: "none",
            border: "none",
            cursor: "pointer",
            color: theme.muted,
            fontSize: 16,
            lineHeight: 1,
            padding: 0,
            flexShrink: 0,
          }}
        >
          ×
        </button>
      </div>

      {/* Forecast rows */}
      {forecastLoading && (
        <div style={{ marginTop: 8, color: theme.muted, fontSize: 12 }}>Loading forecast…</div>
      )}
      {!forecastLoading && forecast && forecast.length > 0 && (
        <div style={{ marginTop: 8, borderTop: `1px solid ${theme.divider}`, paddingTop: 8 }}>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: `repeat(${Math.min(forecast.length, 4)}, 1fr)`,
              gap: "6px 10px",
            }}
          >
            {forecast.slice(0, 8).map((p, i) => {
              const tempVal = p.temperature;
              const tempStr = imp
                ? `${tempVal}°F`
                : `${((tempVal - 32) * 5 / 9).toFixed(0)}°C`;
              // NWS wind is always mph strings like "15 mph" or "10 to 20 mph"
              const windStr = imp
                ? p.windSpeed
                : p.windSpeed.replace(/\d+/g, (n) => String(Math.round(Number(n) * 1.60934))).replace("mph", "km/h");
              const precip = p.probabilityOfPrecipitation?.value;
              return (
                <div key={i} style={{ fontSize: 11 }}>
                  <div style={{ fontWeight: 700, color: theme.muted, marginBottom: 2 }}>
                    {p.name}
                  </div>
                  <div style={{ fontWeight: 600 }}>{tempStr}</div>
                  <div style={{ color: theme.muted }}>{windStr} {p.windDirection}</div>
                  {precip != null && (
                    <div style={{ color: "#4a90d9" }}>{precip}% precip</div>
                  )}
                  <div style={{ color: theme.muted, marginTop: 1 }}>{p.shortForecast}</div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
