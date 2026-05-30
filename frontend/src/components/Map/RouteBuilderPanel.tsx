import { type CSSProperties, useState } from "react";
import type { Units } from "./types";
import type { Theme } from "./theme";
import { mobilePanelStyle, panelShared } from "./utils";
import { DragHandle, useDraggable } from "./useDraggable";
import { useEscapeKey } from "../../hooks/useEscapeKey";

// Haversine running length of the drawn polyline, in metres.
function polylineLengthM(pts: [number, number][]): number {
  const R = 6371000;
  const rad = (d: number) => (d * Math.PI) / 180;
  let total = 0;
  for (let i = 1; i < pts.length; i++) {
    const [lng0, lat0] = pts[i - 1];
    const [lng1, lat1] = pts[i];
    const dLat = rad(lat1 - lat0);
    const dLng = rad(lng1 - lng0);
    const a =
      Math.sin(dLat / 2) ** 2 +
      Math.cos(rad(lat0)) * Math.cos(rad(lat1)) * Math.sin(dLng / 2) ** 2;
    total += 2 * R * Math.asin(Math.sqrt(a));
  }
  return total;
}

export function RouteBuilderPanel({
  vertices,
  saving,
  units,
  theme,
  mobile,
  mobileBottom,
  siblingActive,
  onUndo,
  onClear,
  onSave,
  onClose,
}: {
  vertices: [number, number][];
  saving: boolean;
  units: Units;
  theme: Theme;
  mobile?: boolean;
  mobileBottom?: number;
  siblingActive?: boolean;
  onUndo: () => void;
  onClear: () => void;
  onSave: (name: string) => void;
  onClose: () => void;
}) {
  const isMobile = mobile ?? false;
  const { panelRef, handleProps, panelEventProps, dragStyle } = useDraggable(isMobile);
  useEscapeKey(onClose);

  const [name, setName] = useState("");
  const imp = units === "imperial";
  const dist = polylineLengthM(vertices);
  const distStr = imp
    ? `${(dist / 1609.344).toFixed(2)} mi`
    : `${(dist / 1000).toFixed(2)} km`;

  const canSave = vertices.length >= 2 && name.trim().length > 0 && !saving;

  const panelStyle: CSSProperties = mobile
    ? mobilePanelStyle(mobileBottom, theme, { padding: "12px 16px" })
    : {
        ...panelShared(theme),
        bottom: 36,
        ...(siblingActive ? { right: 8, left: "auto" } : { left: "50%", transform: "translateX(-50%)" }),
        borderRadius: 8,
        padding: "10px 14px",
        fontSize: 13,
        boxShadow: "0 2px 10px rgba(0,0,0,0.22)",
        width: 300,
      };

  const btn = (enabled: boolean, accent = false): CSSProperties => ({
    flex: 1,
    padding: "6px 10px",
    fontSize: 12,
    fontWeight: 600,
    borderRadius: 6,
    cursor: enabled ? "pointer" : "not-allowed",
    border: `1px solid ${theme.divider}`,
    background: accent ? (enabled ? theme.accent : theme.divider) : theme.panel,
    color: accent ? "#fff" : enabled ? theme.text : theme.muted,
    opacity: enabled ? 1 : 0.6,
  });

  return (
    <div ref={panelRef} role="dialog" aria-label="Route builder" style={{ ...panelStyle, ...dragStyle }} {...panelEventProps}>
      <DragHandle mobile={isMobile} handleProps={handleProps} theme={theme} />
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <span style={{ color: theme.muted, fontSize: 11 }}>Draw route</span>
        <button
          onClick={onClose}
          aria-label="Close route builder"
          style={{
            background: "none", border: "none", cursor: "pointer",
            color: theme.muted, fontSize: 18, lineHeight: 1, padding: 4,
            minWidth: 28, minHeight: 28, display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >×</button>
      </div>

      <div style={{ display: "flex", justifyContent: "space-between", paddingTop: 4, color: theme.muted, fontSize: 12 }}>
        <span><b style={{ color: theme.text }}>{vertices.length}</b> points</span>
        <span><b style={{ color: theme.text }}>{distStr}</b></span>
      </div>

      {vertices.length < 2 ? (
        <div style={{ color: theme.muted, fontSize: 11, paddingTop: 6 }}>
          Click the map to drop points along your route.
        </div>
      ) : (
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Route name"
          maxLength={255}
          style={{
            marginTop: 8, width: "100%", boxSizing: "border-box",
            padding: "6px 8px", fontSize: 13, borderRadius: 6,
            border: `1px solid ${theme.divider}`, background: theme.panel, color: theme.text,
          }}
        />
      )}

      <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
        <button onClick={onUndo} disabled={vertices.length === 0} style={btn(vertices.length > 0)}>Undo</button>
        <button onClick={onClear} disabled={vertices.length === 0} style={btn(vertices.length > 0)}>Clear</button>
        <button onClick={() => onSave(name.trim())} disabled={!canSave} style={btn(canSave, true)}>
          {saving ? "Saving…" : "Save route"}
        </button>
      </div>
    </div>
  );
}
