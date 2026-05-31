import { showToast } from "../Toast";
import type { Theme } from "./theme";
import { Z } from "./zIndex";

export function ToolboxPanel({
  measureActive,
  slopeFilterActive,
  terrain3dActive,
  routeBuilderActive,
  savedRoutesActive,
  tripsActive,
  layerPanelCollapsed,
  theme,
  onMeasureToggle,
  onSlopeFilterToggle,
  onTerrain3dToggle,
  onRouteBuilderToggle,
  onSavedRoutesToggle,
  onTripsToggle,
}: {
  measureActive: boolean;
  slopeFilterActive: boolean;
  terrain3dActive: boolean;
  routeBuilderActive: boolean;
  savedRoutesActive: boolean;
  tripsActive: boolean;
  layerPanelCollapsed: boolean;
  theme: Theme;
  onMeasureToggle: () => void;
  onSlopeFilterToggle: () => void;
  onTerrain3dToggle: () => void;
  onRouteBuilderToggle: () => void;
  onSavedRoutesToggle: () => void;
  onTripsToggle: () => void;
}) {
  async function copyShareLink() {
    try {
      await navigator.clipboard.writeText(window.location.href);
      showToast("Map link copied to clipboard.", "success");
    } catch {
      showToast("Couldn't copy — select the address bar manually.", "error");
    }
  }

  const leftPos = layerPanelCollapsed ? 50 : 228;

  const tileStyle = (active: boolean, activeColor: string): React.CSSProperties => ({
    width: 36,
    height: 36,
    background: active ? `${activeColor}22` : theme.panel,
    color: active ? activeColor : theme.text,
    border: `1px solid ${active ? activeColor : "transparent"}`,
    borderRadius: 8,
    cursor: "pointer",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    boxShadow: "0 2px 12px rgba(0,0,0,0.18)",
    padding: 0,
    flexShrink: 0,
  });

  return (
    <div style={{
      position: "fixed",
      top: 10,
      left: leftPos,
      zIndex: Z.FLY_OUT,
      display: "flex",
      gap: 4,
      transition: "left 200ms ease",
    }}>
      {/* Measure Slope */}
      <button
        onClick={onMeasureToggle}
        title={measureActive ? "Exit slope measurement" : "Measure slope"}
        aria-label="Measure slope"
        aria-pressed={measureActive}
        style={tileStyle(measureActive, theme.accent)}
      >
        <svg width="16" height="16" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <rect x="1" y="4.5" width="12" height="5" rx="1"/>
          <line x1="4" y1="4.5" x2="4" y2="7"/>
          <line x1="7" y1="4.5" x2="7" y2="6.2"/>
          <line x1="10" y1="4.5" x2="10" y2="7"/>
        </svg>
      </button>

      {/* Slope Filter */}
      <button
        onClick={onSlopeFilterToggle}
        title={slopeFilterActive ? "Close slope filter" : "Slope filter"}
        aria-label="Slope filter"
        aria-pressed={slopeFilterActive}
        style={tileStyle(slopeFilterActive, "#a07850")}
      >
        <svg width="16" height="16" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M1 13 L7 2 L13 13 Z"/>
          <path d="M4.5 9.5 L7 7 L9.5 9.5"/>
        </svg>
      </button>

      {/* Draw route */}
      <button
        onClick={onRouteBuilderToggle}
        title={routeBuilderActive ? "Exit route drawing" : "Draw route"}
        aria-label="Draw route"
        aria-pressed={routeBuilderActive}
        style={tileStyle(routeBuilderActive, "#7b3fe4")}
      >
        <svg width="16" height="16" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M2 11 C4 11 4 4 7 4 C10 4 10 9 12 9"/>
          <circle cx="2" cy="11" r="1.3" fill="currentColor" stroke="none"/>
          <circle cx="12" cy="9" r="1.3" fill="currentColor" stroke="none"/>
        </svg>
      </button>

      {/* Saved routes */}
      <button
        onClick={onSavedRoutesToggle}
        title={savedRoutesActive ? "Hide saved routes" : "Saved routes"}
        aria-label="Saved routes"
        aria-pressed={savedRoutesActive}
        style={tileStyle(savedRoutesActive, "#7b3fe4")}
      >
        <svg width="16" height="16" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M2 3.5 L5 2.5 L9 4 L12 3 L12 10.5 L9 11.5 L5 10 L2 11 Z"/>
          <path d="M5 2.5 L5 10"/>
          <path d="M9 4 L9 11.5"/>
        </svg>
      </button>

      {/* Trips & party */}
      <button
        onClick={onTripsToggle}
        title={tripsActive ? "Hide trips" : "Trips & party"}
        aria-label="Trips and party"
        aria-pressed={tripsActive}
        style={tileStyle(tripsActive, "#1fb6ff")}
      >
        <svg width="16" height="16" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="4.5" cy="4" r="2"/>
          <circle cx="9.5" cy="4" r="2"/>
          <path d="M1.5 12 C1.5 9 3 8 4.5 8 C6 8 7.5 9 7.5 12"/>
          <path d="M6.5 12 C6.5 9 8 8 9.5 8 C11 8 12.5 9 12.5 12"/>
        </svg>
      </button>

      {/* 3D Terrain */}
      <button
        onClick={onTerrain3dToggle}
        title={terrain3dActive ? "Switch to 2D" : "3D terrain"}
        aria-label="Toggle 3D terrain"
        aria-pressed={terrain3dActive}
        style={tileStyle(terrain3dActive, "#3884dc")}
      >
        <svg width="16" height="16" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M1 4.5 L7 1.5 L13 4.5 L7 7.5 Z"/>
          <path d="M1 9 L7 12 L13 9"/>
          <path d="M1 6.5 L7 9.5 L13 6.5"/>
        </svg>
      </button>

      {/* Copy share link */}
      <button
        onClick={copyShareLink}
        title="Copy share link"
        aria-label="Copy share link"
        style={tileStyle(false, theme.accent)}
      >
        <svg width="16" height="16" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M6 8a3 3 0 0 0 4.24 0l1.41-1.41a3 3 0 1 0-4.24-4.24L6.7 3.07"/>
          <path d="M8 6a3 3 0 0 0-4.24 0L2.35 7.41a3 3 0 1 0 4.24 4.24L7.3 10.93"/>
        </svg>
      </button>
    </div>
  );
}
