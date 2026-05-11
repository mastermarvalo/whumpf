import { useCallback, useEffect, useRef, useState } from "react";
import type { Theme } from "./theme";
import { Z } from "./zIndex";

// Time range: -24h to +168h (7 days forward) in 3-hour steps.
// Step 8 = "now" (24 hours in from the left end).
const STEP_H = 3;
const PAST_STEPS = 8;        // 24h back
const FUTURE_STEPS = 56;     // 168h (7 days) forward
export const TOTAL_STEPS = PAST_STEPS + FUTURE_STEPS; // 64, indices 0-63
export const NOW_STEP = PAST_STEPS;                    // 8

/** Convert a slider step index to an absolute Date. */
export function stepToDate(step: number): Date {
  const offsetH = (step - NOW_STEP) * STEP_H;
  const d = new Date();
  // Round to nearest 3h boundary so we align to NDFD/radar forecast hours.
  d.setMinutes(0, 0, 0);
  const h = d.getHours();
  d.setHours(h - (h % 3));
  d.setHours(d.getHours() + offsetH);
  return d;
}

function formatStep(step: number): string {
  if (step === NOW_STEP) return "Now";
  const d = stepToDate(step);
  const relH = (step - NOW_STEP) * STEP_H;
  const sign = relH > 0 ? "+" : "−";
  const absH = Math.abs(relH);
  const relStr = absH < 24 ? `${sign}${absH}h` : `${sign}${absH / 24}d`;
  const dateStr = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const timeStr = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true });
  return `${relStr} · ${dateStr} ${timeStr}`;
}

/** Tick positions (step index) for day boundaries. */
function dayTicks(): number[] {
  const ticks: number[] = [];
  for (let s = 0; s <= TOTAL_STEPS; s++) {
    if (s === NOW_STEP) continue;
    const d = stepToDate(s);
    if (d.getHours() === 0) ticks.push(s);
  }
  return ticks;
}

const DAY_TICKS = dayTicks();

interface Props {
  step: number;
  onChange: (step: number) => void;
  theme: Theme;
  mobile: boolean;
  mobileBottom: number;
  layerPanelCollapsed: boolean;
}

export function TimeSlider({ step, onChange, theme, mobile, mobileBottom, layerPanelCollapsed }: Props) {
  const [localStep, setLocalStep] = useState(step);
  const trackRef = useRef<HTMLDivElement>(null);

  // Keep localStep in sync when parent resets (e.g. "Now" button from outside).
  useEffect(() => { setLocalStep(step); }, [step]);

  const commit = useCallback((s: number) => {
    setLocalStep(s);
    onChange(s);
  }, [onChange]);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setLocalStep(Number(e.target.value));
  };

  const handleCommit = (e: React.SyntheticEvent<HTMLInputElement>) => {
    commit(Number((e.target as HTMLInputElement).value));
  };

  const resetNow = () => commit(NOW_STEP);

  // Left offset on desktop: clear the layer panel (280px) when expanded.
  const leftPx = mobile ? 8 : layerPanelCollapsed ? 56 : 288;
  const bottomPx = mobile ? mobileBottom + 8 : 36;

  return (
    <div
      style={{
        position: "fixed",
        bottom: bottomPx,
        left: leftPx,
        right: mobile ? 8 : 56,
        zIndex: Z.MAP_OVERLAY,
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        pointerEvents: "none",
        display: "flex",
        justifyContent: "center",
      }}
    >
      <div
        style={{
          background: theme.panel,
          border: `1px solid ${theme.divider}`,
          borderRadius: 12,
          padding: "6px 12px 8px",
          display: "flex",
          flexDirection: "column",
          gap: 4,
          minWidth: 260,
          maxWidth: 480,
          width: "100%",
          boxShadow: "0 2px 8px rgba(0,0,0,0.35)",
          pointerEvents: "auto",
        }}
      >
        {/* Header row */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <span style={{ fontSize: 10, color: theme.muted, letterSpacing: "0.06em", textTransform: "uppercase" }}>
            Time
          </span>
          <span style={{ fontSize: 12, color: localStep === NOW_STEP ? theme.accent : theme.text, fontWeight: 600 }}>
            {formatStep(localStep)}
          </span>
          {localStep !== NOW_STEP && (
            <button
              onClick={resetNow}
              style={{
                fontSize: 11,
                color: theme.accent,
                background: "none",
                border: `1px solid ${theme.accent}`,
                borderRadius: 6,
                padding: "1px 7px",
                cursor: "pointer",
                fontFamily: "inherit",
              }}
            >
              Now
            </button>
          )}
          {localStep === NOW_STEP && (
            <span style={{ fontSize: 11, color: theme.muted, width: 44 }} />
          )}
        </div>

        {/* Slider + tick marks */}
        <div style={{ position: "relative" }}>
          <input
            type="range"
            min={0}
            max={TOTAL_STEPS}
            step={1}
            value={localStep}
            onChange={handleChange}
            onMouseUp={handleCommit}
            onTouchEnd={handleCommit}
            onKeyUp={handleCommit}
            style={{
              width: "100%",
              accentColor: theme.accent,
              cursor: "pointer",
              margin: 0,
            }}
          />
          {/* Day boundary ticks */}
          <div ref={trackRef} style={{ position: "relative", height: 14, marginTop: -2 }}>
            {/* "Now" marker */}
            <div
              style={{
                position: "absolute",
                left: `${(NOW_STEP / TOTAL_STEPS) * 100}%`,
                transform: "translateX(-50%)",
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                pointerEvents: "none",
              }}
            >
              <div style={{ width: 1, height: 6, background: theme.accent, opacity: 0.7 }} />
              <span style={{ fontSize: 9, color: theme.accent, whiteSpace: "nowrap", marginTop: 1 }}>now</span>
            </div>
            {/* Day ticks */}
            {DAY_TICKS.map((s) => {
              const d = stepToDate(s);
              const label = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
              return (
                <div
                  key={s}
                  style={{
                    position: "absolute",
                    left: `${(s / TOTAL_STEPS) * 100}%`,
                    transform: "translateX(-50%)",
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                    pointerEvents: "none",
                  }}
                >
                  <div style={{ width: 1, height: 4, background: theme.muted, opacity: 0.5 }} />
                  <span style={{ fontSize: 9, color: theme.muted, whiteSpace: "nowrap", marginTop: 1 }}>{label}</span>
                </div>
              );
            })}
          </div>
        </div>

        {/* Range labels */}
        <div style={{ display: "flex", justifyContent: "space-between", marginTop: -2 }}>
          <span style={{ fontSize: 9, color: theme.muted }}>−24h</span>
          <span style={{ fontSize: 9, color: theme.muted }}>+7 days</span>
        </div>

        {/* Forecast note when scrubbing into the future */}
        {localStep > NOW_STEP && (
          <div style={{ fontSize: 10, color: theme.muted, textAlign: "center", marginTop: 2 }}>
            NDFD forecast · radar not available for future times
          </div>
        )}
      </div>
    </div>
  );
}
