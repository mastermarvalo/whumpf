import { useState } from "react";
import { showToast } from "../Toast";
import type { Theme } from "./theme";
import { Z } from "./zIndex";

// Sits at top-left below the hamburger menu. Wrench icon opens a fly-out panel
// listing available tools. Add new tools here as the feature set grows.
export function ToolboxPanel({
  measureActive,
  slopeFilterActive,
  terrain3dActive,
  layerPanelCollapsed,
  theme,
  onMeasureToggle,
  onSlopeFilterToggle,
  onTerrain3dToggle,
}: {
  measureActive: boolean;
  slopeFilterActive: boolean;
  terrain3dActive: boolean;
  layerPanelCollapsed: boolean;
  theme: Theme;
  onMeasureToggle: () => void;
  onSlopeFilterToggle: () => void;
  onTerrain3dToggle: () => void;
}) {
  const [open, setOpen] = useState(false);

  function activateTool(toggle: () => void) {
    toggle();
    setOpen(false);
  }

  async function copyShareLink() {
    const url = window.location.href;
    try {
      await navigator.clipboard.writeText(url);
      showToast("Map link copied to clipboard.", "success");
    } catch {
      showToast("Couldn't copy — select the address bar manually.", "error");
    }
    setOpen(false);
  }

  const hasActive = measureActive || slopeFilterActive || terrain3dActive;
  // Hugs the top row. Collapsed: right of the 36px hamburger (10+36+4=50).
  // Expanded: right of the panel (box-sizing:border-box, so right edge = 10+210=220, +8 gap = 228).
  const leftPos = layerPanelCollapsed ? 50 : 228;

  return (
    <div style={{ position: "fixed", top: 10, left: leftPos, zIndex: Z.FLY_OUT, transition: "left 200ms ease" }}>
      {/* Toolbox trigger button */}
      <button
        onClick={() => setOpen((o) => !o)}
        title="Tools"
        aria-label="Tools"
        aria-expanded={open}
        style={{
          width: 36,
          height: 36,
          background: open ? theme.accent : hasActive ? "rgba(224,90,43,0.15)" : theme.panel,
          color: open ? "#fff" : hasActive ? theme.accent : theme.text,
          border: `1px solid ${open || hasActive ? theme.accent : "transparent"}`,
          borderRadius: 8,
          cursor: "pointer",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          boxShadow: "0 2px 12px rgba(0,0,0,0.18)",
          padding: 0,
        }}
      >
        {/* Wrench icon */}
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
          <path d="M10.5 1.5a4 4 0 0 0-3.78 5.27L2 11.5 4.5 14l4.73-4.72A4 4 0 1 0 10.5 1.5z"/>
          <line x1="10.5" y1="1.5" x2="12.5" y2="3.5"/>
          <line x1="8.5" y1="3.5" x2="10.5" y2="5.5"/>
        </svg>
      </button>

      {/* Fly-out tool list */}
      {open && (
        <div
          style={{
            position: "absolute",
            top: 0,
            left: 44,
            background: theme.panel,
            borderRadius: 8,
            padding: "6px",
            boxShadow: "0 2px 12px rgba(0,0,0,0.25)",
            display: "flex",
            flexDirection: "column",
            gap: 2,
            minWidth: 170,
            fontFamily: "ui-sans-serif, system-ui, sans-serif",
          }}
        >
          <div style={{ fontSize: 10, color: theme.muted, textTransform: "uppercase", letterSpacing: "0.07em", padding: "2px 6px 5px" }}>
            Tools
          </div>

          {/* Measure Slope */}
          <button
            onClick={() => activateTool(onMeasureToggle)}
            title={measureActive ? "Exit slope measurement" : "Measure slope between two points"}
            aria-label="Measure slope between two points"
            aria-pressed={measureActive}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 9,
              background: measureActive ? "rgba(224,90,43,0.15)" : "transparent",
              color: measureActive ? theme.accent : theme.text,
              border: `1px solid ${measureActive ? theme.accent : "transparent"}`,
              borderRadius: 6,
              padding: "7px 10px",
              cursor: "pointer",
              fontSize: 12,
              fontWeight: 500,
              textAlign: "left",
              width: "100%",
            }}
          >
            {/* Ruler icon */}
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
              <rect x="1" y="4.5" width="12" height="5" rx="1"/>
              <line x1="4" y1="4.5" x2="4" y2="7"/>
              <line x1="7" y1="4.5" x2="7" y2="6.2"/>
              <line x1="10" y1="4.5" x2="10" y2="7"/>
            </svg>
            Measure Slope
          </button>

          {/* Slope Filter */}
          <button
            onClick={() => activateTool(onSlopeFilterToggle)}
            title={slopeFilterActive ? "Close slope filter" : "Highlight terrain by slope angle and aspect"}
            aria-label="Slope filter"
            aria-pressed={slopeFilterActive}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 9,
              background: slopeFilterActive ? "rgba(160,120,80,0.15)" : "transparent",
              color: slopeFilterActive ? "#a07850" : theme.text,
              border: `1px solid ${slopeFilterActive ? "#a07850" : "transparent"}`,
              borderRadius: 6,
              padding: "7px 10px",
              cursor: "pointer",
              fontSize: 12,
              fontWeight: 500,
              textAlign: "left",
              width: "100%",
            }}
          >
            {/* Mountain icon */}
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
              <path d="M1 13 L7 2 L13 13 Z"/>
              <path d="M4.5 9.5 L7 7 L9.5 9.5"/>
            </svg>
            Slope Filter
          </button>

          {/* 3D Terrain */}
          <button
            onClick={() => activateTool(onTerrain3dToggle)}
            title={terrain3dActive ? "Switch to 2D" : "Enable 3D terrain"}
            aria-label="Toggle 3D terrain"
            aria-pressed={terrain3dActive}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 9,
              background: terrain3dActive ? "rgba(56,132,220,0.15)" : "transparent",
              color: terrain3dActive ? "#3884dc" : theme.text,
              border: `1px solid ${terrain3dActive ? "#3884dc" : "transparent"}`,
              borderRadius: 6,
              padding: "7px 10px",
              cursor: "pointer",
              fontSize: 12,
              fontWeight: 500,
              textAlign: "left",
              width: "100%",
            }}
          >
            {/* Layers icon */}
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
              <path d="M1 4.5 L7 1.5 L13 4.5 L7 7.5 Z"/>
              <path d="M1 9 L7 12 L13 9"/>
              <path d="M1 6.5 L7 9.5 L13 6.5"/>
            </svg>
            {terrain3dActive ? "2D" : "3D"} Terrain
          </button>

          {/* Copy share link */}
          <button
            onClick={copyShareLink}
            title="Copy a shareable link to the current map view"
            aria-label="Copy a shareable link to the current map view"
            style={{
              display: "flex",
              alignItems: "center",
              gap: 9,
              background: "transparent",
              color: theme.text,
              border: "1px solid transparent",
              borderRadius: 6,
              padding: "7px 10px",
              cursor: "pointer",
              fontSize: 12,
              fontWeight: 500,
              textAlign: "left",
              width: "100%",
            }}
          >
            {/* Link icon */}
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
              <path d="M6 8a3 3 0 0 0 4.24 0l1.41-1.41a3 3 0 1 0-4.24-4.24L6.7 3.07"/>
              <path d="M8 6a3 3 0 0 0-4.24 0L2.35 7.41a3 3 0 1 0 4.24 4.24L7.3 10.93"/>
            </svg>
            Copy share link
          </button>
        </div>
      )}
    </div>
  );
}
