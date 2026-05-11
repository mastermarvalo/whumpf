import { useState, type ReactNode } from "react";
import type { ProfileSummary, SlopeSample, Units } from "./types";
import type { Theme } from "./theme";
import { slopeColor } from "./utils";

export function ProfileChart({
  samples,
  summary,
  units,
  theme,
}: {
  samples: SlopeSample[];
  summary: ProfileSummary;
  units: Units;
  theme: Theme;
}) {
  const [hover, setHover] = useState<number | null>(null);

  const valid = samples.filter((s) => s.elevation_m != null);
  if (valid.length < 2) return null;

  const W = 400;
  const H = 80;
  const PL = 2, PR = 2, PT = 6, PB = 16;

  const distMax = summary.distance_m;
  const elevMin = Math.min(...valid.map((s) => s.elevation_m!));
  const elevMax = Math.max(...valid.map((s) => s.elevation_m!));
  const elevSpan = Math.max(elevMax - elevMin, 1);

  const sx = (d: number) => PL + (d / distMax) * (W - PL - PR);
  const sy = (e: number) => PT + (1 - (e - elevMin) / elevSpan) * (H - PT - PB);
  const base = H - PB;

  const segs: ReactNode[] = [];
  for (let i = 0; i < samples.length - 1; i++) {
    const a = samples[i], b = samples[i + 1];
    if (a.elevation_m == null || b.elevation_m == null) continue;
    segs.push(
      <polygon
        key={i}
        points={`${sx(a.distance_m)},${sy(a.elevation_m)} ${sx(b.distance_m)},${sy(b.elevation_m)} ${sx(b.distance_m)},${base} ${sx(a.distance_m)},${base}`}
        fill={slopeColor(a.slope_deg ?? 0)}
        fillOpacity={0.55}
      />,
    );
  }

  const linePts = valid.map((s) => `${sx(s.distance_m)},${sy(s.elevation_m!)}`).join(" ");

  const imp = units === "imperial";
  const fmtElev = (e: number | null) =>
    e == null ? "—" : imp ? `${Math.round(e * 3.28084).toLocaleString()} ft` : `${Math.round(e).toLocaleString()} m`;
  const fmtDist = (d: number) =>
    imp ? `${(d / 1609.344).toFixed(2)} mi` : `${(d / 1000).toFixed(2)} km`;

  const hs = hover != null ? samples[hover] : null;

  const onMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const xFrac = (e.clientX - rect.left) / rect.width;
    const d = Math.max(0, Math.min(1, xFrac)) * distMax;
    let best = 0, bestDist = Infinity;
    samples.forEach((s, i) => {
      const dd = Math.abs(s.distance_m - d);
      if (dd < bestDist) { bestDist = dd; best = i; }
    });
    setHover(best);
  };

  return (
    <div style={{ width: "100%" }}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        style={{ width: "100%", height: H, display: "block", cursor: "crosshair" }}
        onMouseMove={onMouseMove}
        onMouseLeave={() => setHover(null)}
      >
        {segs}
        <polyline points={linePts} fill="none" stroke="rgba(255,255,255,0.8)" strokeWidth={1.5} />
        {hs && hs.elevation_m != null && (
          <>
            <line x1={sx(hs.distance_m)} y1={PT} x2={sx(hs.distance_m)} y2={base}
              stroke="white" strokeWidth={1} opacity={0.55} />
            <circle cx={sx(hs.distance_m)} cy={sy(hs.elevation_m)} r={3} fill="white" />
          </>
        )}
        <text x={PL + 2} y={PT + 8} fontSize={8} fill="rgba(255,255,255,0.4)">{fmtElev(elevMax)}</text>
        <text x={PL + 2} y={base - 2} fontSize={8} fill="rgba(255,255,255,0.4)">{fmtElev(elevMin)}</text>
        <text x={PL + 2} y={H - 2} fontSize={9} fill="rgba(255,255,255,0.45)">A</text>
        <text x={W - PR - 2} y={H - 2} fontSize={9} fill="rgba(255,255,255,0.45)" textAnchor="end">B</text>
      </svg>
      <div style={{ fontSize: 10, color: theme.muted, minHeight: 14, paddingTop: 1 }}>
        {hs ? (
          <span style={{ display: "flex", gap: 10 }}>
            <span>{fmtDist(hs.distance_m)}</span>
            <span style={{ color: theme.text }}>{fmtElev(hs.elevation_m)}</span>
            {hs.slope_deg != null && (
              <span style={{ color: slopeColor(hs.slope_deg), fontWeight: 700 }}>
                {hs.slope_deg.toFixed(1)}°
              </span>
            )}
          </span>
        ) : (
          <span>hover for elevation · slope</span>
        )}
      </div>
    </div>
  );
}
