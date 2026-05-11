import type { CSSProperties } from "react";
import type { Theme } from "./theme";
import type { TerrainFilterSettings } from "./layers/basemaps";
import { Z } from "./zIndex";

// ── compass rose ───────────────────────────────────────────────────────────────

const CX = 48, CY = 48, R_OUTER = 42, R_LABEL = 29;

const ASPECTS: { name: string; svgDeg: number }[] = [
  { name: "N",  svgDeg: 270 },
  { name: "NE", svgDeg: 315 },
  { name: "E",  svgDeg: 0   },
  { name: "SE", svgDeg: 45  },
  { name: "S",  svgDeg: 90  },
  { name: "SW", svgDeg: 135 },
  { name: "W",  svgDeg: 180 },
  { name: "NW", svgDeg: 225 },
];

function toRad(d: number) { return (d * Math.PI) / 180; }

function wedgePath(centerDeg: number): string {
  const s = centerDeg - 22.5;
  const e = centerDeg + 22.5;
  const sx = CX + R_OUTER * Math.cos(toRad(s));
  const sy = CY + R_OUTER * Math.sin(toRad(s));
  const ex = CX + R_OUTER * Math.cos(toRad(e));
  const ey = CY + R_OUTER * Math.sin(toRad(e));
  return `M ${CX} ${CY} L ${sx.toFixed(1)} ${sy.toFixed(1)} A ${R_OUTER} ${R_OUTER} 0 0 1 ${ex.toFixed(1)} ${ey.toFixed(1)} Z`;
}

function CompassRose({
  selected,
  onChange,
  theme,
}: {
  selected: string[];
  onChange: (aspects: string[]) => void;
  theme: Theme;
}) {
  const toggle = (name: string) => {
    const next = selected.includes(name)
      ? selected.filter((x) => x !== name)
      : [...selected, name];
    onChange(next);
  };

  return (
    <svg width={96} height={96} viewBox="0 0 96 96" style={{ display: "block", margin: "0 auto", cursor: "pointer" }}>
      {/* Background circle */}
      <circle cx={CX} cy={CY} r={R_OUTER} fill={theme.soonBg} />
      {/* Wedge segments */}
      {ASPECTS.map(({ name, svgDeg }) => {
        const active = selected.includes(name);
        const lx = CX + R_LABEL * Math.cos(toRad(svgDeg));
        const ly = CY + R_LABEL * Math.sin(toRad(svgDeg));
        return (
          <g key={name} onClick={() => toggle(name)}>
            <path
              d={wedgePath(svgDeg)}
              fill={active ? "#a07850" : "transparent"}
              stroke={theme.divider}
              strokeWidth={1}
              style={{ transition: "fill 120ms" }}
            />
            <text
              x={lx.toFixed(1)}
              y={(ly + 3.5).toFixed(1)}
              textAnchor="middle"
              fontSize={8}
              fontWeight={active ? 700 : 400}
              fill={active ? "#fff" : theme.muted}
              style={{ pointerEvents: "none", userSelect: "none", fontFamily: "ui-sans-serif,system-ui,sans-serif" }}
            >
              {name}
            </text>
          </g>
        );
      })}
      {/* Center dot */}
      <circle cx={CX} cy={CY} r={4} fill={theme.panel} />
    </svg>
  );
}

// ── slope zone reference ───────────────────────────────────────────────────────

const SLOPE_ZONES = [
  { label: "< 30°", color: "#1a9641", note: "Low hazard" },
  { label: "30–35°", color: "#f4820a", note: "Caution zone" },
  { label: "35–45°", color: "#d7191c", note: "Prime avy terrain" },
  { label: "> 45°", color: "#2b7bb9", note: "Very steep / cliff" },
];

// ── input style helper ─────────────────────────────────────────────────────────

function numInput(theme: Theme): CSSProperties {
  return {
    width: 44,
    padding: "2px 4px",
    borderRadius: 4,
    border: `1px solid ${theme.divider}`,
    background: "transparent",
    color: theme.text,
    fontSize: 12,
    fontFamily: "inherit",
    textAlign: "center",
  };
}

// ── main component ─────────────────────────────────────────────────────────────

interface Props {
  filter: TerrainFilterSettings;
  onChange: (f: TerrainFilterSettings) => void;
  onApplyWindPreset: () => void;
  onClose: () => void;
  theme: Theme;
  mobile: boolean;
  mobileBottom: number;
}

const ALL_ASPECTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];

export function SlopeFilterPanel({ filter, onChange, onApplyWindPreset, onClose, theme, mobile, mobileBottom }: Props) {
  const panelStyle: CSSProperties = mobile ? {
    position: "fixed",
    bottom: mobileBottom,
    left: 8,
    right: 8,
    zIndex: Z.FLOATING_PANEL,
    background: theme.panel,
    borderRadius: 12,
    padding: "12px 14px",
    fontFamily: "ui-sans-serif, system-ui, sans-serif",
    fontSize: 13,
    color: theme.text,
    boxShadow: "0 2px 16px rgba(0,0,0,0.28)",
  } : {
    position: "fixed",
    bottom: 36,
    left: "50%",
    transform: "translateX(-50%)",
    zIndex: Z.FLOATING_PANEL,
    background: theme.panel,
    borderRadius: 10,
    padding: "12px 14px",
    fontFamily: "ui-sans-serif, system-ui, sans-serif",
    fontSize: 13,
    color: theme.text,
    boxShadow: "0 2px 12px rgba(0,0,0,0.28)",
    width: 296,
  };

  const setAspects = (aspects: string[]) => onChange({ ...filter, aspects });

  const setSlopeMin = (v: number) => {
    const clamped = Math.max(0, Math.min(89, v));
    if (clamped < filter.slopeMax) onChange({ ...filter, slopeMin: clamped });
  };
  const setSlopeMax = (v: number) => {
    const clamped = Math.max(1, Math.min(90, v));
    if (clamped > filter.slopeMin) onChange({ ...filter, slopeMax: clamped });
  };

  return (
    <div role="dialog" aria-label="Slope filter" style={panelStyle}>

      {/* ── Header ── */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
          {/* Triangle / mountain icon */}
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke={theme.accent} strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
            <path d="M1 13 L7 2 L13 13 Z"/>
            <path d="M5 9 L7 7 L9 9"/>
          </svg>
          <span style={{ fontWeight: 700, fontSize: 13 }}>Slope Filter</span>
        </div>
        <button
          onClick={onClose}
          aria-label="Close slope filter"
          style={{ background: "none", border: "none", cursor: "pointer", color: theme.muted, fontSize: 18, lineHeight: 1, padding: 4 }}
        >×</button>
      </div>

      {/* ── Description ── */}
      <p style={{ margin: "0 0 10px", fontSize: 11, color: theme.muted, lineHeight: 1.5 }}>
        Highlights terrain matching your slope angle and aspect. Tap compass wedges or buttons to select aspects; adjust the degree range below.
      </p>

      {/* ── Compass rose + aspect buttons ── */}
      <div style={{ display: "flex", alignItems: "flex-start", gap: 10, marginBottom: 10 }}>
        <CompassRose selected={filter.aspects} onChange={setAspects} theme={theme} />
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 10, color: theme.muted, marginBottom: 4 }}>Aspects</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 3 }}>
            {ALL_ASPECTS.map((a) => {
              const active = filter.aspects.includes(a);
              return (
                <button
                  key={a}
                  onClick={() => {
                    const next = active
                      ? filter.aspects.filter((x) => x !== a)
                      : [...filter.aspects, a];
                    setAspects(next);
                  }}
                  style={{
                    width: 28,
                    padding: "3px 0",
                    borderRadius: 4,
                    border: `1px solid ${active ? "#a07850" : theme.divider}`,
                    background: active ? "#a07850" : "transparent",
                    color: active ? "#fff" : theme.muted,
                    fontSize: 10,
                    fontWeight: active ? 700 : 400,
                    cursor: "pointer",
                    fontFamily: "inherit",
                  }}
                >
                  {a}
                </button>
              );
            })}
          </div>
          {/* Presets */}
          <div style={{ display: "flex", gap: 8, marginTop: 6, fontSize: 10 }}>
            <button
              onClick={() => { onApplyWindPreset(); }}
              title="Auto-select leeward aspects from the NDFD wind forecast at the map center"
              style={{ background: "none", border: "none", padding: 0, color: theme.accent, cursor: "pointer", fontSize: 10, fontFamily: "inherit", textDecoration: "underline" }}
            >
              From wind ↗
            </button>
            <span style={{ color: theme.muted }}>·</span>
            <button
              onClick={() => setAspects([...ALL_ASPECTS])}
              style={{ background: "none", border: "none", padding: 0, color: theme.accent, cursor: "pointer", fontSize: 10, fontFamily: "inherit", textDecoration: "underline" }}
            >
              All
            </button>
          </div>
        </div>
      </div>

      {/* ── Slope range ── */}
      <div style={{ borderTop: `1px solid ${theme.divider}`, paddingTop: 10, marginBottom: 10 }}>
        <div style={{ fontSize: 10, color: theme.muted, marginBottom: 5 }}>Slope angle range</div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <input
            type="number" min={0} max={89}
            value={filter.slopeMin}
            onChange={(e) => setSlopeMin(parseInt(e.target.value, 10) || 0)}
            style={numInput(theme)}
          />
          <span style={{ color: theme.muted, fontSize: 12 }}>°  –</span>
          <input
            type="number" min={1} max={90}
            value={filter.slopeMax}
            onChange={(e) => setSlopeMax(parseInt(e.target.value, 10) || 0)}
            style={numInput(theme)}
          />
          <span style={{ color: theme.muted, fontSize: 12 }}>°</span>
          {/* Visual range bar */}
          <div style={{ flex: 1, height: 6, borderRadius: 3, background: theme.soonBg, position: "relative", overflow: "hidden" }}>
            <div style={{
              position: "absolute",
              left: `${(filter.slopeMin / 90) * 100}%`,
              width: `${((filter.slopeMax - filter.slopeMin) / 90) * 100}%`,
              height: "100%",
              background: "#d7191c",
              borderRadius: 3,
            }} />
          </div>
        </div>
      </div>

      {/* ── Slope zone reference guide ── */}
      <div style={{ borderTop: `1px solid ${theme.divider}`, paddingTop: 8 }}>
        <div style={{ fontSize: 10, color: theme.muted, marginBottom: 5 }}>Slope zone reference</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          {SLOPE_ZONES.map(({ label, color, note }) => (
            <div key={label} style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 11 }}>
              <div style={{ width: 10, height: 10, borderRadius: 2, background: color, flexShrink: 0 }} />
              <span style={{ color: theme.text, minWidth: 48 }}>{label}</span>
              <span style={{ color: theme.muted }}>{note}</span>
            </div>
          ))}
        </div>
        <p style={{ margin: "6px 0 0", fontSize: 10, color: theme.muted, lineHeight: 1.4 }}>
          <b style={{ color: theme.text }}>From wind</b> fetches the NDFD wind forecast at the map center and selects the leeward aspects — the faces where slab-building snow loads accumulate.
        </p>
      </div>

    </div>
  );
}
