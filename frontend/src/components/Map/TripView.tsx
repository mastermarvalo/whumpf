import { type CSSProperties, useState } from "react";
import type { Friend, RouteListItem, TripDetail, Units, WaypointKind } from "./types";
import type { Theme } from "./theme";
import { mobilePanelStyle, panelShared } from "./utils";
import { ProfileChart } from "./ProfileChart";
import { TripDetails } from "./TripDetails";
import { buildCaicDetailHtml, type CaicZoneDetail } from "./layers/caic";
import { DragHandle, useDraggable } from "./useDraggable";
import { useEscapeKey } from "../../hooks/useEscapeKey";
import { Z } from "./zIndex";

const KIND_EMOJI: Record<string, string> = {
  parking: "🅿️", trailhead: "🥾", transition: "🔄", decision: "⚠️",
  summit: "⛰️", hazard: "❗", other: "📍",
};

export function TripView({
  detail,
  friends,
  savedRoutes,
  currentUserId,
  units,
  theme,
  mobile,
  mobileBottom,
  waypointMode,
  waypointKind,
  onToggleWaypointMode,
  onWaypointKindChange,
  onInvite,
  onDeleteWaypoint,
  selectedTripRouteId,
  onSelectTripRoute,
  onAddRoute,
  onRemoveRoute,
  onDeleteTrip,
  onClose,
}: {
  detail: TripDetail;
  friends: Friend[];
  savedRoutes: RouteListItem[];
  currentUserId: number;
  units: Units;
  theme: Theme;
  mobile?: boolean;
  mobileBottom?: number;
  waypointMode: boolean;
  waypointKind: WaypointKind;
  onToggleWaypointMode: () => void;
  onWaypointKindChange: (k: WaypointKind) => void;
  onInvite: (email: string) => void;
  onDeleteWaypoint: (wid: number) => void;
  selectedTripRouteId: number | null;
  onSelectTripRoute: (routeId: number) => void;
  onAddRoute: (routeId: number, day: number) => void;
  onRemoveRoute: (tripRouteId: number) => void;
  onDeleteTrip: () => void;
  onClose: () => void;
}) {
  const isMobile = mobile ?? false;
  const { panelRef, handleProps, panelEventProps, dragStyle } = useDraggable(isMobile);
  useEscapeKey(onClose);
  const [inviteEmail, setInviteEmail] = useState("");
  const [addRouteDay, setAddRouteDay] = useState<number | null>(null);
  const [selectedRouteId, setSelectedRouteId] = useState<number | "">("");

  const isOwner = detail.owner_id === currentUserId;
  const snapshot = detail.forecast_snapshot as CaicZoneDetail | null;

  const panelStyle: CSSProperties = mobile
    ? mobilePanelStyle(mobileBottom, theme, { padding: "12px 16px" })
    : {
        ...panelShared(theme),
        top: 56,
        right: 8,
        left: "auto",
        zIndex: Z.FLY_OUT,
        borderRadius: 8,
        padding: "10px 14px",
        fontSize: 13,
        boxShadow: "0 2px 10px rgba(0,0,0,0.22)",
        width: 440,
        maxHeight: "82vh",
        overflowX: "hidden",
        overflowY: "auto",
      };

  const sectionTitle: CSSProperties = {
    color: theme.muted, fontSize: 11, textTransform: "uppercase",
    letterSpacing: "0.05em", marginTop: 12, marginBottom: 4,
  };
  const smallBtn = (accent = false): CSSProperties => ({
    background: accent ? theme.accent : "none",
    border: `1px solid ${accent ? theme.accent : theme.divider}`,
    color: accent ? "#fff" : theme.text,
    cursor: "pointer", fontSize: 11, padding: "4px 10px", borderRadius: 6,
  });

  const dateStr = new Date(detail.date + "T00:00:00").toLocaleDateString("en-US", {
    weekday: "short", month: "short", day: "numeric", year: "numeric",
  });

  return (
    <div ref={panelRef} role="dialog" aria-label="Trip plan" style={{ ...panelStyle, ...dragStyle }} {...panelEventProps}>
      <DragHandle mobile={isMobile} handleProps={handleProps} theme={theme} />
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <span style={{ color: theme.accent, fontSize: 11, fontWeight: 700, letterSpacing: "0.04em" }}>TRIP PLAN</span>
        <button onClick={onClose} aria-label="Close trip" style={{ background: "none", border: "none", cursor: "pointer", color: theme.muted, fontSize: 18, lineHeight: 1, padding: 4 }}>×</button>
      </div>

      <div style={{ fontWeight: 700, fontSize: 15, color: theme.text, marginTop: 2 }}>{detail.name}</div>
      <div style={{ color: theme.muted, fontSize: 12 }}>
        {dateStr}{detail.caic_zone ? ` · ${detail.caic_zone}` : ""}
      </div>

      {/* Frozen CAIC forecast */}
      {snapshot ? (
        <>
          <div style={sectionTitle}>Forecast (frozen at planning)</div>
          <div style={{ background: theme.panel, borderRadius: 6, overflow: "hidden" }}
               dangerouslySetInnerHTML={{ __html: buildCaicDetailHtml(snapshot) }} />
        </>
      ) : (
        <div style={{ ...sectionTitle }}>No forecast snapshot</div>
      )}

      {/* Routes, grouped by day */}
      {detail.days.map((day) => {
        const assignedIds = new Set(day.routes.map((r) => r.id));
        const available = savedRoutes.filter((r) => !assignedIds.has(r.id));
        const isAddingThisDay = addRouteDay === day.day;
        return (
          <div key={day.day}>
            {detail.num_days > 1 && (
              <div style={{ ...sectionTitle, color: theme.accent }}>
                Day {day.day} · {new Date(day.date + "T00:00:00").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" })}
              </div>
            )}
            {detail.num_days === 1 && <div style={sectionTitle}>Routes</div>}
            {day.routes.length === 0 && (
              <div style={{ color: theme.muted, fontSize: 11, marginTop: 4 }}>No routes assigned.</div>
            )}
            {day.routes.map((r) => {
              const isSelected = selectedTripRouteId === r.id;
              return (
                <div
                  key={r.trip_route_id}
                  onClick={() => onSelectTripRoute(r.id)}
                  style={{
                    marginTop: 8, borderRadius: 6, cursor: "pointer",
                    padding: "4px 6px", marginLeft: -6, marginRight: -6,
                    background: isSelected ? `${theme.accent}22` : "transparent",
                    border: `1px solid ${isSelected ? theme.accent : "transparent"}`,
                    transition: "background 0.15s, border-color 0.15s",
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <div style={{ fontWeight: 600, fontSize: 12, color: isSelected ? theme.accent : theme.text }}>{r.name}</div>
                    <button
                      onClick={(e) => { e.stopPropagation(); onRemoveRoute(r.trip_route_id); }}
                      style={{ background: "none", border: "none", color: theme.muted, cursor: "pointer", fontSize: 14, padding: "0 2px" }}
                      aria-label="Remove route"
                    >×</button>
                  </div>
                  <ProfileChart samples={r.samples} summary={r.summary} units={units} theme={theme} />
                  <TripDetails summary={r.summary} units={units} theme={theme} />
                </div>
              );
            })}
            {isAddingThisDay ? (
              <div style={{ display: "flex", gap: 4, marginTop: 6 }}>
                <select
                  value={selectedRouteId}
                  onChange={(e) => setSelectedRouteId(e.target.value === "" ? "" : parseInt(e.target.value, 10))}
                  style={{ flex: 1, fontSize: 12, padding: "4px 6px", borderRadius: 6, border: `1px solid ${theme.divider}`, background: theme.panel, color: theme.text }}
                >
                  <option value="">— pick a route —</option>
                  {available.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
                </select>
                <button
                  disabled={selectedRouteId === ""}
                  onClick={() => { if (selectedRouteId !== "") { onAddRoute(selectedRouteId as number, day.day); setAddRouteDay(null); setSelectedRouteId(""); } }}
                  style={{ ...smallBtn(true), opacity: selectedRouteId === "" ? 0.5 : 1 }}
                >Add</button>
                <button onClick={() => { setAddRouteDay(null); setSelectedRouteId(""); }} style={smallBtn()}>Cancel</button>
              </div>
            ) : (
              <button
                onClick={() => { setAddRouteDay(day.day); setSelectedRouteId(""); }}
                style={{ ...smallBtn(), marginTop: 6, fontSize: 11 }}
              >+ Add route</button>
            )}
          </div>
        );
      })}

      {/* Waypoints */}
      <div style={sectionTitle}>Waypoints</div>
      <div style={{ display: "flex", gap: 6, marginBottom: 6, alignItems: "center" }}>
        <button onClick={onToggleWaypointMode} style={smallBtn(waypointMode)}>
          {waypointMode ? "Click map to drop — done" : "+ Add waypoint"}
        </button>
        {waypointMode && (
          <select
            value={waypointKind}
            onChange={(e) => onWaypointKindChange(e.target.value as WaypointKind)}
            style={{ fontSize: 11, padding: "3px 6px", borderRadius: 6, border: `1px solid ${theme.divider}`, background: theme.panel, color: theme.text }}
          >
            {(["parking", "trailhead", "transition", "decision", "summit", "hazard", "other"] as WaypointKind[]).map((k) => (
              <option key={k} value={k}>{k}</option>
            ))}
          </select>
        )}
      </div>
      {detail.waypoints.length === 0 ? (
        <div style={{ color: theme.muted, fontSize: 11 }}>None yet.</div>
      ) : (
        detail.waypoints.map((w) => (
          <div key={w.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 12, padding: "2px 0" }}>
            <span>{KIND_EMOJI[w.kind] ?? "📍"} {w.label || w.kind}</span>
            <button onClick={() => onDeleteWaypoint(w.id)} style={{ background: "none", border: "none", color: theme.muted, cursor: "pointer", fontSize: 14 }} aria-label="Delete waypoint">×</button>
          </div>
        ))
      )}

      {/* Party roster */}
      <div style={sectionTitle}>Party</div>
      {detail.members.map((m) => (
        <div key={m.id} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, padding: "1px 0" }}>
          <span>{m.email}</span>
          <span style={{ color: theme.muted }}>{m.role === "owner" ? "owner" : m.status}</span>
        </div>
      ))}

      {/* Owner: invite + delete */}
      {isOwner && (
        <>
          <div style={sectionTitle}>Invite to party</div>
          <div style={{ display: "flex", gap: 4 }}>
            <input
              list="trip-friend-emails"
              value={inviteEmail}
              onChange={(e) => setInviteEmail(e.target.value)}
              placeholder="friend or email"
              style={{ flex: 1, padding: "5px 8px", fontSize: 12, borderRadius: 6, border: `1px solid ${theme.divider}`, background: theme.panel, color: theme.text }}
            />
            <datalist id="trip-friend-emails">
              {friends.map((f) => <option key={f.user_id} value={f.email} />)}
            </datalist>
            <button
              onClick={() => { const e = inviteEmail.trim(); if (e) { onInvite(e); setInviteEmail(""); } }}
              disabled={!inviteEmail.trim()}
              style={smallBtn(true)}
            >Invite</button>
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
            <button onClick={onDeleteTrip} style={{ background: "none", border: `1px solid ${theme.divider}`, color: "#d7191c", cursor: "pointer", fontSize: 11, padding: "4px 10px", borderRadius: 6 }}>Delete trip</button>
          </div>
        </>
      )}
    </div>
  );
}
