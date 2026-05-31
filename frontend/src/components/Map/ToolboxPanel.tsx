import { useState } from "react";
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
  tripInviteCount,
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
  tripInviteCount: number;
  layerPanelCollapsed: boolean;
  theme: Theme;
  onMeasureToggle: () => void;
  onSlopeFilterToggle: () => void;
  onTerrain3dToggle: () => void;
  onRouteBuilderToggle: () => void;
  onSavedRoutesToggle: () => void;
  onTripsToggle: () => void;
}) {
  const [planOpen, setPlanOpen] = useState(false);

  async function copyShareLink() {
    try {
      await navigator.clipboard.writeText(window.location.href);
      showToast("Map link copied to clipboard.", "success");
    } catch {
      showToast("Couldn't copy — select the address bar manually.", "error");
    }
  }

  const leftPos = layerPanelCollapsed ? 50 : 228;
  const planActive = routeBuilderActive || savedRoutesActive || tripsActive;

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

  const dropdownRow = (
    active: boolean,
    activeColor: string,
    label: string,
    sub: string,
    icon: React.ReactNode,
    onClick: () => void,
    badge?: number,
  ): React.ReactNode => (
    <button
      onClick={onClick}
      aria-pressed={active}
      style={{
        display: "flex", alignItems: "center", gap: 10, width: "100%",
        background: active ? `${activeColor}18` : "transparent",
        color: active ? activeColor : theme.text,
        border: `1px solid ${active ? activeColor : "transparent"}`,
        borderRadius: 8, padding: "9px 10px", cursor: "pointer",
        fontSize: 13, fontWeight: 500, fontFamily: "ui-sans-serif, system-ui, sans-serif",
        textAlign: "left", marginBottom: 4,
      }}
    >
      {icon}
      <div style={{ flex: 1 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          {label}
          {badge ? (
            <span style={{ background: activeColor, color: "#fff", borderRadius: 8, fontSize: 10, fontWeight: 700, padding: "1px 5px" }}>
              {badge}
            </span>
          ) : null}
        </div>
        <div style={{ fontSize: 11, color: theme.muted, fontWeight: 400, marginTop: 1 }}>{sub}</div>
      </div>
    </button>
  );

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

      {/* Plan dropdown */}
      <div style={{ position: "relative" }}>
        <button
          onClick={() => setPlanOpen((o) => !o)}
          title="Plan"
          aria-label="Plan"
          aria-pressed={planActive || planOpen}
          style={{
            ...tileStyle(planActive || planOpen, "#7b3fe4"),
            position: "relative",
          }}
        >
          <svg width="16" height="16" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M2 11 L5 5 L8 9 L10 6 L12 11 Z" fill="currentColor" fillOpacity="0.2"/>
            <path d="M2 11 L5 5 L8 9 L10 6 L12 11"/>
            <circle cx="12" cy="3" r="1.5" fill="currentColor" stroke="none"/>
          </svg>
          {/* Badge when plan items active but dropdown closed */}
          {(planActive || tripInviteCount > 0) && !planOpen && (
            <span style={{
              position: "absolute", top: -4, right: -4,
              width: 8, height: 8, borderRadius: "50%",
              background: "#7b3fe4", border: `2px solid ${theme.panel}`,
            }} />
          )}
        </button>

        {planOpen && (
          <>
            {/* click-outside dismiss */}
            <div
              onClick={() => setPlanOpen(false)}
              style={{ position: "fixed", inset: 0, zIndex: Z.FLY_OUT - 1 }}
            />
            <div style={{
              position: "absolute",
              top: "calc(100% + 6px)",
              left: 0,
              zIndex: Z.FLY_OUT,
              background: theme.panel,
              border: `1px solid ${theme.divider}`,
              borderRadius: 10,
              padding: 8,
              width: 230,
              boxShadow: "0 4px 20px rgba(0,0,0,0.28)",
            }}>
              <div style={{ fontSize: 10, color: theme.muted, textTransform: "uppercase", letterSpacing: "0.07em", padding: "2px 4px 8px" }}>
                Plan
              </div>
              {dropdownRow(
                routeBuilderActive, "#7b3fe4", "Draw Route",
                routeBuilderActive ? "Click map to add points" : "Trace a route on the map",
                <svg width="16" height="16" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M2 12 Q5 4 8 8 Q11 12 12 3"/>
                  <circle cx="2" cy="12" r="1.2" fill="currentColor" stroke="none"/>
                  <circle cx="12" cy="3" r="1.2" fill="currentColor" stroke="none"/>
                </svg>,
                () => { onRouteBuilderToggle(); setPlanOpen(false); },
              )}
              {dropdownRow(
                savedRoutesActive, "#7b3fe4", "Saved Routes",
                "View and manage your routes",
                <svg width="16" height="16" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M2 11 L5 5 L8 9 L10 6 L12 11 Z" fill="currentColor" fillOpacity="0.15"/>
                  <path d="M2 11 L5 5 L8 9 L10 6 L12 11"/>
                </svg>,
                () => { onSavedRoutesToggle(); setPlanOpen(false); },
              )}
              {dropdownRow(
                tripsActive, "#1fb6ff", "Trips & Party",
                "Plan trips and invite your crew",
                <svg width="16" height="16" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="4.5" cy="4" r="2"/>
                  <circle cx="9.5" cy="4" r="2"/>
                  <path d="M1.5 12 C1.5 9 3 8 4.5 8 C6 8 7.5 9 7.5 12"/>
                  <path d="M6.5 12 C6.5 9 8 8 9.5 8 C11 8 12.5 9 12.5 12"/>
                </svg>,
                () => { onTripsToggle(); setPlanOpen(false); },
                tripInviteCount || undefined,
              )}
            </div>
          </>
        )}
      </div>

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
