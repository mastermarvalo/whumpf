import { useEffect, useCallback, useState } from "react";
import type { Theme } from "./theme";
import { Z } from "./zIndex";

// ── time constants ─────────────────────────────────────────────────────────────
// -7 days to +7 days in 3-hour steps = 336h / 3 = 112 steps (0-112).
// NOW_STEP is at the midpoint (step 56).
const STEP_H = 3;
const PAST_H  = 168;  // 7 days back
const FUTURE_H = 168; // 7 days forward
export const TOTAL_STEPS = (PAST_H + FUTURE_H) / STEP_H; // 112
export const NOW_STEP    = PAST_H / STEP_H;               // 56

/** Convert a slider step index to an absolute Date (snapped to 3-hour boundary). */
export function stepToDate(step: number): Date {
  const d = new Date();
  d.setMinutes(0, 0, 0);
  d.setHours(d.getHours() - (d.getHours() % STEP_H)); // snap to 3h grid
  d.setHours(d.getHours() + (step - NOW_STEP) * STEP_H);
  return d;
}

function formatStep(step: number): string {
  if (step === NOW_STEP) return "Now";
  const d = stepToDate(step);
  const relH = (step - NOW_STEP) * STEP_H;
  const sign = relH > 0 ? "+" : "−";
  const absH = Math.abs(relH);
  const relLabel = absH < 24
    ? `${sign}${absH}h`
    : `${sign}${Math.round(absH / 24)}d`;
  const datePart = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const timePart = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true });
  return `${relLabel} · ${datePart} ${timePart}`;
}

function dayTicks(): number[] {
  const seen = new Set<string>();
  const ticks: number[] = [];
  for (let s = 0; s <= TOTAL_STEPS; s++) {
    if (s === NOW_STEP) continue;
    const d = stepToDate(s);
    if (d.getHours() === 0) {
      const key = d.toDateString();
      if (!seen.has(key)) { seen.add(key); ticks.push(s); }
    }
  }
  return ticks;
}
const DAY_TICKS = dayTicks();

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

  const leftPx  = mobile ? 8 : layerPanelCollapsed ? 56 : 228;
  const bottomPx = mobile ? mobileBottom + 8 : 36;
  const isFuture = localStep > NOW_STEP;
  const isPast   = localStep < NOW_STEP;

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
            {isPast ? "Past" : isFuture ? "Forecast" : "Time"}
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
          {/* Tick marks + labels */}
          <div style={{ position: "relative", height: 16, marginTop: -2 }}>
            {/* "Now" marker */}
            <div style={{
              position: "absolute",
              left: `${(NOW_STEP / TOTAL_STEPS) * 100}%`,
              transform: "translateX(-50%)",
              display: "flex", flexDirection: "column", alignItems: "center",
              pointerEvents: "none",
            }}>
              <div style={{ width: 1, height: 6, background: theme.accent, opacity: 0.8 }} />
              <span style={{ fontSize: 9, color: theme.accent, whiteSpace: "nowrap", marginTop: 1 }}>now</span>
            </div>
            {/* Day boundary ticks — only show every other one to avoid crowding on narrow screens */}
            {DAY_TICKS.filter((_, i) => i % 2 === 0).map((s) => {
              const d = stepToDate(s);
              const label = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
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
          </div>
        </div>

        {/* Range labels + note */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: -2 }}>
          <span style={{ fontSize: 9, color: theme.muted }}>−7 days</span>
          {isFuture && (
            <span style={{ fontSize: 9, color: theme.muted, textAlign: "center" }}>
              radar unavailable for future times
            </span>
          )}
          <span style={{ fontSize: 9, color: theme.muted }}>+7 days</span>
        </div>
      </div>
    </div>
  );
}
