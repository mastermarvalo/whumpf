import { type CSSProperties, useState } from "react";
import type { FriendsData, RouteListItem, TripListItem } from "./types";
import type { Theme } from "./theme";
import { mobilePanelStyle, panelShared } from "./utils";
import { DragHandle, useDraggable } from "./useDraggable";
import { useEscapeKey } from "../../hooks/useEscapeKey";

type Tab = "trips" | "friends";

export function TripsPanel({
  trips,
  invites,
  friends,
  savedRoutes,
  loading,
  theme,
  mobile,
  mobileBottom,
  onSelectTrip,
  onCreateTrip,
  onRespondInvite,
  onSendFriendRequest,
  onRespondFriend,
  onRemoveFriend,
  onClose,
}: {
  trips: TripListItem[];
  invites: TripListItem[];
  friends: FriendsData;
  savedRoutes: RouteListItem[];
  loading: boolean;
  theme: Theme;
  mobile?: boolean;
  mobileBottom?: number;
  onSelectTrip: (id: number) => void;
  onCreateTrip: (payload: { name: string; date: string; days: { route_ids: number[] }[] }) => void;
  onRespondInvite: (tripId: number, action: "accept" | "decline") => void;
  onSendFriendRequest: (email: string) => void;
  onRespondFriend: (friendshipId: number, action: "accept" | "decline") => void;
  onRemoveFriend: (friendshipId: number) => void;
  onClose: () => void;
}) {
  const isMobile = mobile ?? false;
  const { panelRef, handleProps, panelEventProps, dragStyle } = useDraggable(isMobile);
  useEscapeKey(onClose);

  const [tab, setTab] = useState<Tab>("trips");
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [date, setDate] = useState("");
  const [multiDay, setMultiDay] = useState(false);
  // Route ids assigned per day; index 0 = day 1. Always length >= 1.
  const [dayRoutes, setDayRoutes] = useState<number[][]>([[]]);
  const [friendEmail, setFriendEmail] = useState("");

  const panelStyle: CSSProperties = mobile
    ? mobilePanelStyle(mobileBottom, theme, { padding: "12px 16px" })
    : {
        ...panelShared(theme),
        top: 56, right: 8, left: "auto",
        borderRadius: 8, padding: "10px 14px", fontSize: 13,
        boxShadow: "0 2px 10px rgba(0,0,0,0.22)",
        width: 320, maxHeight: "76vh", overflowY: "auto",
      };

  const tabBtn = (t: Tab): CSSProperties => ({
    flex: 1, padding: "6px 0", border: "none", borderRadius: 5, cursor: "pointer",
    background: tab === t ? `${theme.accent}22` : "transparent",
    color: tab === t ? theme.accent : theme.muted,
    fontSize: 12, fontWeight: tab === t ? 700 : 400,
  });
  const input: CSSProperties = {
    width: "100%", boxSizing: "border-box", padding: "6px 8px", fontSize: 13,
    borderRadius: 6, border: `1px solid ${theme.divider}`, background: theme.panel, color: theme.text,
  };
  const primaryBtn: CSSProperties = {
    background: theme.accent, border: "none", color: "#fff", cursor: "pointer",
    fontSize: 12, fontWeight: 600, padding: "6px 10px", borderRadius: 6,
  };
  const ghostBtn: CSSProperties = {
    background: "none", border: `1px solid ${theme.divider}`, color: theme.text,
    cursor: "pointer", fontSize: 11, padding: "4px 8px", borderRadius: 6,
  };

  const canCreate = name.trim().length > 0 && date.length > 0;

  function toggleRoute(dayIdx: number, id: number) {
    setDayRoutes((prev) => prev.map((ids, i) =>
      i !== dayIdx ? ids : (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id])
    ));
  }
  function setDayCount(n: number) {
    const count = Math.max(1, Math.min(14, n));
    setDayRoutes((prev) => {
      const next = prev.slice(0, count);
      while (next.length < count) next.push([]);
      return next;
    });
  }
  function resetForm() {
    setCreating(false); setName(""); setDate(""); setMultiDay(false); setDayRoutes([[]]);
  }
  function submitCreate() {
    if (!canCreate) return;
    onCreateTrip({ name: name.trim(), date, days: dayRoutes.map((ids) => ({ route_ids: ids })) });
    resetForm();
  }

  return (
    <div ref={panelRef} role="dialog" aria-label="Trips and party" style={{ ...panelStyle, ...dragStyle }} {...panelEventProps}>
      <DragHandle mobile={isMobile} handleProps={handleProps} theme={theme} />
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <span style={{ color: theme.muted, fontSize: 11 }}>Trips &amp; party</span>
        <button onClick={onClose} aria-label="Close" style={{ background: "none", border: "none", cursor: "pointer", color: theme.muted, fontSize: 18, lineHeight: 1, padding: 4 }}>×</button>
      </div>

      <div style={{ display: "flex", gap: 4, background: `${theme.divider}55`, borderRadius: 6, padding: 3, margin: "8px 0" }}>
        <button style={tabBtn("trips")} onClick={() => setTab("trips")}>Trips</button>
        <button style={tabBtn("friends")} onClick={() => setTab("friends")}>
          Friends{friends.incoming.length ? ` (${friends.incoming.length})` : ""}
        </button>
      </div>

      {tab === "trips" && (
        <>
          {/* Pending trip invites */}
          {invites.length > 0 && (
            <div style={{ marginBottom: 8 }}>
              <div style={{ color: theme.muted, fontSize: 11, marginBottom: 4 }}>Pending invites</div>
              {invites.map((t) => (
                <div key={t.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 6, padding: "3px 0" }}>
                  <span style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.name}</span>
                  <span style={{ display: "flex", gap: 4, flexShrink: 0 }}>
                    <button style={primaryBtn} onClick={() => onRespondInvite(t.id, "accept")}>Accept</button>
                    <button style={ghostBtn} onClick={() => onRespondInvite(t.id, "decline")}>Decline</button>
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* New trip */}
          {creating ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 8, paddingBottom: 8, borderBottom: `1px solid ${theme.divider}` }}>
              <input style={input} placeholder="Trip name" value={name} onChange={(e) => setName(e.target.value)} maxLength={255} />
              <input style={input} type="date" value={date} onChange={(e) => setDate(e.target.value)} />
              <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={multiDay}
                  onChange={(e) => { setMultiDay(e.target.checked); setDayCount(e.target.checked ? Math.max(2, dayRoutes.length) : 1); }}
                />
                Multi-day trip
              </label>
              {multiDay && (
                <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
                  Days
                  <input
                    type="number" min={2} max={14} value={dayRoutes.length}
                    onChange={(e) => setDayCount(parseInt(e.target.value, 10) || 1)}
                    style={{ ...input, width: 60 }}
                  />
                </label>
              )}
              {savedRoutes.length === 0 && <div style={{ color: theme.muted, fontSize: 11 }}>No saved routes yet.</div>}
              {dayRoutes.map((ids, dayIdx) => (
                <div key={dayIdx}>
                  <div style={{ color: theme.muted, fontSize: 11, marginTop: 2 }}>
                    {multiDay ? `Day ${dayIdx + 1} routes` : "Routes"}
                  </div>
                  <div style={{ maxHeight: 110, overflowY: "auto" }}>
                    {savedRoutes.map((r) => (
                      <label key={r.id} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, padding: "2px 0", cursor: "pointer" }}>
                        <input type="checkbox" checked={ids.includes(r.id)} onChange={() => toggleRoute(dayIdx, r.id)} />
                        {r.name}
                      </label>
                    ))}
                  </div>
                </div>
              ))}
              <div style={{ display: "flex", gap: 6 }}>
                <button style={{ ...primaryBtn, flex: 1, opacity: canCreate ? 1 : 0.6, cursor: canCreate ? "pointer" : "not-allowed" }} disabled={!canCreate} onClick={submitCreate}>Create</button>
                <button style={ghostBtn} onClick={resetForm}>Cancel</button>
              </div>
            </div>
          ) : (
            <button style={{ ...primaryBtn, width: "100%", marginBottom: 8 }} onClick={() => setCreating(true)}>+ New trip</button>
          )}

          {/* Trip list */}
          {loading && <div style={{ color: theme.muted, fontSize: 12 }}>Loading…</div>}
          {!loading && trips.length === 0 && <div style={{ color: theme.muted, fontSize: 12 }}>No trips yet.</div>}
          {trips.map((t) => (
            <button key={t.id} onClick={() => onSelectTrip(t.id)} style={{ width: "100%", textAlign: "left", cursor: "pointer", padding: "6px 8px", borderRadius: 6, border: `1px solid ${theme.divider}`, background: theme.panel, color: theme.text, marginBottom: 4, display: "flex", justifyContent: "space-between", gap: 8 }}>
              <span style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.name}</span>
              <span style={{ color: theme.muted, fontSize: 11, flexShrink: 0 }}>{t.date}</span>
            </button>
          ))}
        </>
      )}

      {tab === "friends" && (
        <>
          <div style={{ display: "flex", gap: 4, marginBottom: 8 }}>
            <input style={input} placeholder="Add friend by email" value={friendEmail} onChange={(e) => setFriendEmail(e.target.value)} />
            <button style={primaryBtn} disabled={!friendEmail.trim()} onClick={() => { const e = friendEmail.trim(); if (e) { onSendFriendRequest(e); setFriendEmail(""); } }}>Add</button>
          </div>

          {friends.incoming.length > 0 && (
            <>
              <div style={{ color: theme.muted, fontSize: 11, marginBottom: 4 }}>Requests</div>
              {friends.incoming.map((f) => (
                <div key={f.friendship_id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 6, padding: "3px 0" }}>
                  <span style={{ fontSize: 12 }}>{f.email}</span>
                  <span style={{ display: "flex", gap: 4 }}>
                    <button style={primaryBtn} onClick={() => onRespondFriend(f.friendship_id, "accept")}>Accept</button>
                    <button style={ghostBtn} onClick={() => onRespondFriend(f.friendship_id, "decline")}>Decline</button>
                  </span>
                </div>
              ))}
            </>
          )}

          <div style={{ color: theme.muted, fontSize: 11, margin: "8px 0 4px" }}>Friends</div>
          {friends.friends.length === 0 && <div style={{ color: theme.muted, fontSize: 12 }}>No friends yet.</div>}
          {friends.friends.map((f) => (
            <div key={f.friendship_id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 12, padding: "2px 0" }}>
              <span>{f.email}</span>
              <button style={{ background: "none", border: "none", color: theme.muted, cursor: "pointer", fontSize: 14 }} onClick={() => onRemoveFriend(f.friendship_id)} aria-label="Remove friend">×</button>
            </div>
          ))}

          {friends.outgoing.length > 0 && (
            <>
              <div style={{ color: theme.muted, fontSize: 11, margin: "8px 0 4px" }}>Sent</div>
              {friends.outgoing.map((f) => (
                <div key={f.friendship_id} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, padding: "2px 0", color: theme.muted }}>
                  <span>{f.email}</span><span>pending</span>
                </div>
              ))}
            </>
          )}
        </>
      )}
    </div>
  );
}
