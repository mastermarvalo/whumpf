import { useState } from "react";
import type { CSSProperties } from "react";
import type { Theme } from "./theme";
import type { TerrainFilterSettings } from "./layers/basemaps";
import { Z } from "./zIndex";
import { DragHandle, useDraggable } from "./useDraggable";

// ── dual-thumb range slider ────────────────────────────────────────────────────
// Two stacked <input type="range"> with a custom track drawn behind them.
// CSS pseudo-element styles can't go inline, so we inject a <style> block once.

const DUAL_RANGE_CSS = `
.wf-dual-range {
  position: absolute; top: 0; left: 0;
  width: 100%; height: 100%;
  background: transparent;
  pointer-events: none;
  -webkit-appearance: none;
  appearance: none;
  outline: none;
  padding: 0; margin: 0;
}
.wf-dual-range::-webkit-slider-thumb {
  pointer-events: all;
  -webkit-appearance: none;
  width: 16px; height: 16px;
  border-radius: 50%;
  background: #a07850;
  border: 2.5px solid #fff;
  cursor: grab;
  box-shadow: 0 1px 4px rgba(0,0,0,0.35);
}
.wf-dual-range:active::-webkit-slider-thumb { cursor: grabbing; }
.wf-dual-range::-moz-range-thumb {
  pointer-events: all;
  width: 16px; height: 16px;
  border-radius: 50%;
  background: #a07850;
  border: 2.5px solid #fff;
  cursor: grab;
  box-shadow: 0 1px 4px rgba(0,0,0,0.35);
}
.wf-dual-range::-webkit-slider-runnable-track { background: transparent; }
.wf-dual-range::-moz-range-track { background: transparent; }
`;

function DualRangeSlider({
  minVal, maxVal, onMinChange, onMaxChange, theme,
}: {
  minVal: number; maxVal: number;
  onMinChange: (v: number) => void;
  onMaxChange: (v: number) => void;
  theme: Theme;
}) {
  const minPct = (minVal / 90) * 100;
  const maxPct = (maxVal / 90) * 100;
  return (
    <div style={{ position: "relative", height: 20 }}>
      {/* Custom track */}
      <div style={{
        position: "absolute", top: "50%", transform: "translateY(-50%)",
        left: 0, right: 0, height: 4, borderRadius: 2,
        background: theme.soonBg, pointerEvents: "none",
      }}>
        <div style={{
          position: "absolute",
          left: `${minPct}%`,
          width: `${maxPct - minPct}%`,
          height: "100%", background: "#d7191c", borderRadius: 2,
        }} />
      </div>
      {/* Min thumb */}
      <input
        type="range" className="wf-dual-range"
        min={0} max={90} step={1} value={minVal}
        onChange={(e) => {
          const v = Number(e.target.value);
          if (v < maxVal) onMinChange(v);
        }}
      />
      {/* Max thumb — sits on top; z-index so max wins when thumbs overlap near high end */}
      <input
        type="range" className="wf-dual-range"
        min={0} max={90} step={1} value={maxVal}
        style={{ zIndex: minVal > 85 ? 1 : "auto" } as CSSProperties}
        onChange={(e) => {
          const v = Number(e.target.value);
          if (v > minVal) onMaxChange(v);
        }}
      />
    </div>
  );
}

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

function CompassRose({ selected, onChange, theme }: {
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
    <svg width={96} height={96} viewBox="0 0 96 96" style={{ display: "block", margin: "0 auto", cursor: "pointer", flexShrink: 0 }}>
      <circle cx={CX} cy={CY} r={R_OUTER} fill={theme.soonBg} />
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
              x={lx.toFixed(1)} y={(ly + 3.5).toFixed(1)}
              textAnchor="middle" fontSize={8}
              fontWeight={active ? 700 : 400}
              fill={active ? "#fff" : theme.muted}
              style={{ pointerEvents: "none", userSelect: "none", fontFamily: "ui-sans-serif,system-ui,sans-serif" }}
            >
              {name}
            </text>
          </g>
        );
      })}
      <circle cx={CX} cy={CY} r={4} fill={theme.panel} />
    </svg>
  );
}

// ── slope zone reference ───────────────────────────────────────────────────────

const SLOPE_ZONES = [
  { label: "< 30°",  color: "#1a9641", note: "Low hazard" },
  { label: "30–35°", color: "#f4820a", note: "Caution zone" },
  { label: "35–45°", color: "#d7191c", note: "Prime avy terrain" },
  { label: "> 45°",  color: "#2b7bb9", note: "Very steep" },
];

// ── aspect mode toggle (All ↔ From wind) ──────────────────────────────────────

function AspectToggle({ windMode, onSelectAll, onSelectWind, theme }: {
  windMode: boolean;
  onSelectAll: () => void;
  onSelectWind: () => void;
  theme: Theme;
}) {
  return (
    <div style={{
      position: "relative",
      display: "flex",
      borderRadius: 8,
      border: `1.5px solid ${theme.divider}`,
      overflow: "hidden",
      marginTop: 8,
      flexShrink: 0,
    }}>
      {/* Sliding pill */}
      <div style={{
        position: "absolute",
        top: 2, bottom: 2,
        left: windMode ? "calc(50% + 1px)" : "2px",
        right: windMode ? "2px" : "calc(50% + 1px)",
        background: "#a07850",
        borderRadius: 6,
        transition: "left 180ms ease, right 180ms ease",
        pointerEvents: "none",
      }} />
      <button
        onClick={onSelectAll}
        style={{
          flex: 1, position: "relative", zIndex: 1,
          padding: "6px 8px", background: "transparent", border: "none",
          cursor: "pointer", fontSize: 11, fontWeight: 600, fontFamily: "inherit",
          color: !windMode ? "#fff" : theme.text,
          transition: "color 120ms",
        }}
      >
        All aspects
      </button>
      <div style={{ width: 1, background: theme.divider, alignSelf: "stretch", flexShrink: 0 }} />
      <button
        onClick={onSelectWind}
        style={{
          flex: 1, position: "relative", zIndex: 1,
          padding: "6px 8px", background: "transparent", border: "none",
          cursor: "pointer", fontSize: 11, fontWeight: 600, fontFamily: "inherit",
          color: windMode ? "#fff" : theme.text,
          transition: "color 120ms",
          display: "flex", alignItems: "center", justifyContent: "center", gap: 4,
        }}
      >
        <svg width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round">
          <path d="M2 10 L10 2"/><path d="M5 2 L10 2 L10 7"/>
        </svg>
        From wind
      </button>
    </div>
  );
}

// ── number input style ─────────────────────────────────────────────────────────

function numInput(theme: Theme): CSSProperties {
  return {
    width: 40,
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
  const [windMode, setWindMode] = useState(false);
  const { panelRef, handleProps, panelEventProps, dragStyle } = useDraggable(mobile);
  const panelStyle: CSSProperties = mobile ? {
    position: "fixed",
    bottom: mobileBottom,
    left: 8, right: 8,
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
    width: 300,
  };

  const setAspects = (aspects: string[]) => { setWindMode(false); onChange({ ...filter, aspects }); };

  return (
    <>
      <style>{DUAL_RANGE_CSS}</style>
      <div ref={panelRef} role="dialog" aria-label="Slope filter" style={{ ...panelStyle, ...dragStyle }} {...panelEventProps}>

        <DragHandle mobile={mobile} handleProps={handleProps} theme={theme} />

        {/* ── Header ── */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke={theme.accent} strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
              <path d="M1 13 L7 2 L13 13 Z"/><path d="M5 9 L7 7 L9 9"/>
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
          Highlights terrain by slope angle and aspect. Tap compass wedges to select aspects; drag or type to set the degree range.
        </p>

        {/* ── Compass + aspect toggle ── */}
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
          <CompassRose selected={filter.aspects} onChange={setAspects} theme={theme} />
          <div style={{ flex: 1 }}>
            <AspectToggle
              windMode={windMode}
              onSelectAll={() => setAspects([...ALL_ASPECTS])}
              onSelectWind={() => {
                setWindMode(true);
                onApplyWindPreset();
              }}
              theme={theme}
            />
          </div>
        </div>

        {/* ── Slope range ── */}
        <div style={{ borderTop: `1px solid ${theme.divider}`, paddingTop: 10, marginBottom: 10 }}>
          <div style={{ fontSize: 10, color: theme.muted, marginBottom: 6 }}>Slope angle range</div>

          {/* Number inputs + degree labels */}
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
            <input
              type="number" min={0} max={89} value={filter.slopeMin}
              onChange={(e) => {
                const v = Math.max(0, Math.min(89, parseInt(e.target.value, 10) || 0));
                if (v < filter.slopeMax) onChange({ ...filter, slopeMin: v });
              }}
              style={numInput(theme)}
            />
            <span style={{ color: theme.muted, fontSize: 11 }}>° –</span>
            <input
              type="number" min={1} max={90} value={filter.slopeMax}
              onChange={(e) => {
                const v = Math.max(1, Math.min(90, parseInt(e.target.value, 10) || 0));
                if (v > filter.slopeMin) onChange({ ...filter, slopeMax: v });
              }}
              style={numInput(theme)}
            />
            <span style={{ color: theme.muted, fontSize: 11 }}>°</span>
          </div>

          {/* Dual-thumb drag slider */}
          <DualRangeSlider
            minVal={filter.slopeMin}
            maxVal={filter.slopeMax}
            onMinChange={(v) => onChange({ ...filter, slopeMin: v })}
            onMaxChange={(v) => onChange({ ...filter, slopeMax: v })}
            theme={theme}
          />

          {/* Axis labels */}
          <div style={{ display: "flex", justifyContent: "space-between", marginTop: 3 }}>
            <span style={{ fontSize: 9, color: theme.muted }}>0°</span>
            <span style={{ fontSize: 9, color: theme.muted }}>45°</span>
            <span style={{ fontSize: 9, color: theme.muted }}>90°</span>
          </div>
        </div>

        {/* ── Slope zone reference ── */}
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
            <b style={{ color: theme.text }}>From wind</b> fetches the NDFD forecast at the map center and selects leeward aspects — where slab-building snow loads accumulate.
          </p>
        </div>

      </div>
    </>
  );
}
