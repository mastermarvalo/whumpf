import { useEffect, useCallback, useState } from "react";
import type { Theme } from "./theme";
import { Z } from "./zIndex";

// ── time constants ─────────────────────────────────────────────────────────────
// 2 hours back to "now" in 10-minute steps = 12 steps (0–12).
// Step 12 = now (rightmost); step 0 = −120 minutes.
const STEP_MIN  = 10;
const PAST_MIN  = 120;
export const TOTAL_STEPS = PAST_MIN / STEP_MIN; // 12
export const NOW_STEP    = TOTAL_STEPS;          // 12 (rightmost)

/** Convert a slider step index to an absolute Date (snapped to 10-min boundary). */
export function stepToDate(step: number): Date {
  const d = new Date();
  d.setSeconds(0, 0);
  d.setMinutes(Math.floor(d.getMinutes() / STEP_MIN) * STEP_MIN);
  d.setMinutes(d.getMinutes() - (NOW_STEP - step) * STEP_MIN);
  return d;
}

function formatStep(step: number): string {
  if (step === NOW_STEP) return "Now";
  const d = stepToDate(step);
  const minsBack = (NOW_STEP - step) * STEP_MIN;
  const relLabel = minsBack < 60 ? `−${minsBack}m` : `−${minsBack / 60}h`;
  const timePart = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true });
  return `${relLabel} · ${timePart}`;
}

// Tick marks at whole-hour boundaries within the 2-hour window.
function hourTicks(): number[] {
  const ticks: number[] = [];
  for (let s = 0; s < NOW_STEP; s++) {
    const minsBack = (NOW_STEP - s) * STEP_MIN;
    if (minsBack % 60 === 0) ticks.push(s);
  }
  return ticks;
}
const HOUR_TICKS = hourTicks();

interface Props {
  step: number;
  onChange: (step: number) => void;
  onDismiss: () => void;
  theme: Theme;
  mobile: boolean;
  mobileBottom: number;
  layerPanelCollapsed: boolean;
}

export function TimeSlider({
  step, onChange, onDismiss,
  theme, mobile, mobileBottom, layerPanelCollapsed,
}: Props) {
  const [localStep, setLocalStep] = useState(step);

  useEffect(() => { setLocalStep(step); }, [step]);

  const commit = useCallback((s: number) => {
    setLocalStep(s);
    onChange(s);
  }, [onChange]);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) =>
    setLocalStep(Number(e.target.value));

  const handleCommit = (e: React.SyntheticEvent<HTMLInputElement>) =>
    commit(Number((e.target as HTMLInputElement).value));

  const leftPx   = mobile ? 8 : layerPanelCollapsed ? 56 : 228;
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
          padding: "6px 10px 8px",
          display: "flex",
          flexDirection: "column",
          gap: 3,
          minWidth: 280,
          maxWidth: 520,
          width: "100%",
          boxShadow: "0 2px 8px rgba(0,0,0,0.35)",
          pointerEvents: "auto",
        }}
      >
        {/* Header row */}
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontSize: 10, color: theme.muted, letterSpacing: "0.06em", textTransform: "uppercase", flexShrink: 0 }}>
            Radar
          </span>
          <span style={{ flex: 1, fontSize: 12, color: localStep === NOW_STEP ? theme.accent : theme.text, fontWeight: 600, textAlign: "center" }}>
            {formatStep(localStep)}
          </span>
          {localStep !== NOW_STEP && (
            <button
              onClick={() => commit(NOW_STEP)}
              style={{ fontSize: 11, color: theme.accent, background: "none", border: `1px solid ${theme.accent}`, borderRadius: 6, padding: "1px 7px", cursor: "pointer", fontFamily: "inherit", flexShrink: 0 }}
            >
              Now
            </button>
          )}
          <button
            onClick={onDismiss}
            title="Hide time slider"
            style={{ fontSize: 14, color: theme.muted, background: "none", border: "none", cursor: "pointer", padding: "0 2px", lineHeight: 1, flexShrink: 0 }}
          >
            ×
          </button>
        </div>

        {/* Slider track */}
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
            style={{ width: "100%", accentColor: theme.accent, cursor: "pointer", margin: 0 }}
          />
          {/* Tick marks at -2h and -1h */}
          <div style={{ position: "relative", height: 16, marginTop: -2 }}>
            {HOUR_TICKS.map((s) => {
              const minsBack = (NOW_STEP - s) * STEP_MIN;
              const label = `−${minsBack / 60}h`;
              return (
                <div key={s} style={{
                  position: "absolute",
                  left: `${(s / TOTAL_STEPS) * 100}%`,
                  transform: "translateX(-50%)",
                  display: "flex", flexDirection: "column", alignItems: "center",
                  pointerEvents: "none",
                }}>
                  <div style={{ width: 1, height: 4, background: theme.muted, opacity: 0.4 }} />
                  <span style={{ fontSize: 8, color: theme.muted, whiteSpace: "nowrap", marginTop: 1 }}>{label}</span>
                </div>
              );
            })}
            {/* "Now" label at right edge */}
            <div style={{
              position: "absolute",
              right: 0,
              display: "flex", flexDirection: "column", alignItems: "center",
              pointerEvents: "none",
            }}>
              <div style={{ width: 1, height: 6, background: theme.accent, opacity: 0.8 }} />
              <span style={{ fontSize: 9, color: theme.accent, whiteSpace: "nowrap", marginTop: 1 }}>now</span>
            </div>
          </div>
        </div>

        {/* Range labels */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: -2 }}>
          <span style={{ fontSize: 9, color: theme.muted }}>−2 hrs</span>
          <span style={{ fontSize: 9, color: theme.muted }}>Now</span>
        </div>
      </div>
    </div>
  );
}
