import { type CSSProperties } from "react";
import type { RouteDetail, Units } from "./types";
import type { Theme } from "./theme";
import { mobilePanelStyle, panelShared } from "./utils";
import { ProfileChart } from "./ProfileChart";
import { TripDetails } from "./TripDetails";
import { DragHandle, useDraggable } from "./useDraggable";
import { useEscapeKey } from "../../hooks/useEscapeKey";
import { Z } from "./zIndex";

/**
 * Read-only view of a route opened via a share link. Renders the stored profile
 * (no re-fetch) and offers a single action: clone it into the viewer's account.
 */
export function SharedRoutePanel({
  detail,
  cloning,
  units,
  theme,
  mobile,
  mobileBottom,
  onClone,
  onClose,
}: {
  detail: RouteDetail;
  cloning: boolean;
  units: Units;
  theme: Theme;
  mobile?: boolean;
  mobileBottom?: number;
  onClone: () => void;
  onClose: () => void;
}) {
  const isMobile = mobile ?? false;
  const { panelRef, handleProps, panelEventProps, dragStyle } = useDraggable(isMobile);
  useEscapeKey(onClose);

  const panelStyle: CSSProperties = mobile
    ? mobilePanelStyle(mobileBottom, theme, { padding: "12px 16px" })
    : {
        ...panelShared(theme),
        // Right side, clear of the left-hand LayerPanel; above TOP_PANEL so the
        // drag handle is always reachable even if dragged over other controls.
        top: 56,
        right: 8,
        left: "auto",
        zIndex: Z.FLY_OUT,
        borderRadius: 8,
        padding: "10px 14px",
        fontSize: 13,
        boxShadow: "0 2px 10px rgba(0,0,0,0.22)",
        width: 320,
        maxHeight: "70vh",
        overflowY: "auto",
      };

  return (
    <div ref={panelRef} role="dialog" aria-label="Shared route" style={{ ...panelStyle, ...dragStyle }} {...panelEventProps}>
      <DragHandle mobile={isMobile} handleProps={handleProps} theme={theme} />
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <span style={{ color: "#1fb6ff", fontSize: 11, fontWeight: 700, letterSpacing: "0.04em" }}>
          SHARED ROUTE
        </span>
        <button
          onClick={onClose}
          aria-label="Close shared route"
          style={{
            background: "none", border: "none", cursor: "pointer",
            color: theme.muted, fontSize: 18, lineHeight: 1, padding: 4,
            minWidth: 28, minHeight: 28, display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >×</button>
      </div>

      <div style={{ fontWeight: 700, fontSize: 14, color: theme.text, marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {detail.name}
      </div>

      <ProfileChart samples={detail.samples} summary={detail.summary} units={units} theme={theme} />
      <TripDetails summary={detail.summary} units={units} theme={theme} />

      <button
        onClick={onClone}
        disabled={cloning}
        style={{
          width: "100%", marginTop: 10, padding: "8px 10px",
          fontSize: 12, fontWeight: 700, borderRadius: 6,
          cursor: cloning ? "default" : "pointer",
          border: "none", background: theme.accent, color: "#fff",
          opacity: cloning ? 0.7 : 1,
        }}
      >
        {cloning ? "Cloning…" : "Clone to my routes"}
      </button>
    </div>
  );
}
