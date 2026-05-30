import { type CSSProperties, useEffect, useState } from "react";
import type { RouteDetail, RouteListItem, Units } from "./types";
import type { Theme } from "./theme";
import { mobilePanelStyle, panelShared } from "./utils";
import { ProfileChart } from "./ProfileChart";
import { TripDetails } from "./TripDetails";
import { DragHandle, useDraggable } from "./useDraggable";
import { useEscapeKey } from "../../hooks/useEscapeKey";
import { fetchRoute } from "./layers/routes";

export function SavedRoutesPanel({
  routes,
  loading,
  selectedId,
  units,
  theme,
  mobile,
  mobileBottom,
  siblingActive,
  onSelect,
  onDelete,
  onClose,
}: {
  routes: RouteListItem[];
  loading: boolean;
  selectedId: number | null;
  units: Units;
  theme: Theme;
  mobile?: boolean;
  mobileBottom?: number;
  siblingActive?: boolean;
  onSelect: (id: number | null) => void;
  onDelete: (id: number) => void;
  onClose: () => void;
}) {
  const isMobile = mobile ?? false;
  const { panelRef, handleProps, panelEventProps, dragStyle } = useDraggable(isMobile);
  useEscapeKey(onClose);

  const [detail, setDetail] = useState<RouteDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // Load the full route (with cached samples) when a row is selected. Renders
  // the stored profile offline — no terrain re-fetch.
  useEffect(() => {
    if (selectedId == null) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    setDetail(null);
    fetchRoute(selectedId)
      .then((d) => { if (!cancelled) { setDetail(d); setDetailLoading(false); } })
      .catch(() => { if (!cancelled) setDetailLoading(false); });
    return () => { cancelled = true; };
  }, [selectedId]);

  const imp = units === "imperial";
  const fmtDist = (m: number) =>
    imp ? `${(m / 1609.344).toFixed(1)} mi` : `${(m / 1000).toFixed(1)} km`;

  const panelStyle: CSSProperties = mobile
    ? mobilePanelStyle(mobileBottom, theme, { padding: "12px 16px" })
    : {
        ...panelShared(theme),
        top: 56,
        right: 8,
        left: "auto",
        ...(siblingActive ? {} : {}),
        borderRadius: 8,
        padding: "10px 14px",
        fontSize: 13,
        boxShadow: "0 2px 10px rgba(0,0,0,0.22)",
        width: 320,
        maxHeight: "70vh",
        overflowY: "auto",
      };

  return (
    <div ref={panelRef} role="dialog" aria-label="Saved routes" style={{ ...panelStyle, ...dragStyle }} {...panelEventProps}>
      <DragHandle mobile={isMobile} handleProps={handleProps} theme={theme} />
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <span style={{ color: theme.muted, fontSize: 11 }}>Saved routes</span>
        <button
          onClick={onClose}
          aria-label="Close saved routes"
          style={{
            background: "none", border: "none", cursor: "pointer",
            color: theme.muted, fontSize: 18, lineHeight: 1, padding: 4,
            minWidth: 28, minHeight: 28, display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >×</button>
      </div>

      {loading && <div style={{ color: theme.muted, fontSize: 12, paddingTop: 6 }}>Loading…</div>}
      {!loading && routes.length === 0 && (
        <div style={{ color: theme.muted, fontSize: 12, paddingTop: 6 }}>
          No saved routes yet. Use the draw tool to create one.
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 6 }}>
        {routes.map((r) => {
          const active = r.id === selectedId;
          return (
            <div key={r.id}>
              <button
                onClick={() => onSelect(active ? null : r.id)}
                style={{
                  width: "100%", textAlign: "left", cursor: "pointer",
                  padding: "6px 8px", borderRadius: 6,
                  border: `1px solid ${active ? theme.accent : theme.divider}`,
                  background: active ? `${theme.accent}18` : theme.panel,
                  color: theme.text, fontSize: 13,
                  display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8,
                }}
              >
                <span style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {r.name}
                </span>
                <span style={{ color: theme.muted, fontSize: 11, flexShrink: 0 }}>
                  {fmtDist(r.summary.distance_m)}
                </span>
              </button>

              {active && (
                <div style={{ padding: "6px 2px 4px" }}>
                  {detailLoading && <div style={{ color: theme.muted, fontSize: 11 }}>Loading profile…</div>}
                  {detail && detail.id === r.id && (
                    <>
                      <ProfileChart
                        samples={detail.samples}
                        summary={detail.summary}
                        units={units}
                        theme={theme}
                      />
                      <TripDetails summary={detail.summary} units={units} theme={theme} />
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 8 }}>
                        <span style={{ color: theme.muted, fontSize: 11 }}>{detail.visibility}</span>
                        <button
                          onClick={() => onDelete(r.id)}
                          style={{
                            background: "none", border: `1px solid ${theme.divider}`,
                            color: "#d7191c", cursor: "pointer", fontSize: 11,
                            padding: "4px 10px", borderRadius: 6,
                          }}
                        >Delete</button>
                      </div>
                    </>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
