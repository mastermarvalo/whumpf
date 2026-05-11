import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../../auth";
import { API_URL } from "./constants";
import type { ActivityCardProps, Units } from "./types";
import type { Theme } from "./theme";
import { Z } from "./zIndex";

export function StravaActivityCard({
  activities,
  index,
  onIndexChange,
  onClose,
  units,
  theme,
  mobile,
  mobileBottom,
}: {
  activities: ActivityCardProps[];
  index: number;
  onIndexChange: (i: number) => void;
  onClose: () => void;
  units: Units;
  theme: Theme;
  mobile?: boolean;
  mobileBottom?: number;
}) {
  const act = activities[index];
  const descCache = useRef<Record<number, string | null>>({});
  const [description, setDescription] = useState<string | null | "loading">("loading");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    if (act.id in descCache.current) {
      setDescription(descCache.current[act.id]);
      return;
    }
    setDescription("loading");
    apiFetch(`${API_URL}/strava/activities/${act.id}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        const desc = (data?.description as string | null) ?? null;
        descCache.current[act.id] = desc;
        setDescription(desc);
        if (data?.photo_url && !act.photo_url) {
          act.photo_url = data.photo_url as string;
        }
      })
      .catch(() => {
        descCache.current[act.id] = null;
        setDescription(null);
      });
  }, [act.id]);

  const dist = units === "imperial"
    ? `${(act.distance_m * 0.000621371).toFixed(1)} mi`
    : `${(act.distance_m / 1000).toFixed(1)} km`;

  const elapsed = (() => {
    const h = Math.floor(act.elapsed_time_s / 3600);
    const m = Math.floor((act.elapsed_time_s % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  })();

  const elev = units === "imperial"
    ? `+${Math.round(act.total_elevation_gain_m * 3.28084).toLocaleString()} ft`
    : `+${Math.round(act.total_elevation_gain_m).toLocaleString()} m`;

  const date = act.start_date
    ? new Date(act.start_date).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
    : "";

  const sportLabel = act.sport_type.replace(/([A-Z])/g, " $1").trim();

  return (
    <div
      role="dialog"
      aria-label={`Strava activity ${act.name}`}
      style={mobile ? {
        position: "fixed",
        bottom: mobileBottom,
        left: 8,
        right: 8,
        background: theme.panel,
        borderRadius: 12,
        boxShadow: "0 4px 24px rgba(0,0,0,0.28)",
        overflow: "hidden",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        zIndex: Z.FLOATING_PANEL,
        color: theme.text,
      } : {
        position: "fixed",
        bottom: 80,
        right: 10,
        width: 300,
        background: theme.panel,
        borderRadius: 10,
        boxShadow: "0 4px 24px rgba(0,0,0,0.28)",
        overflow: "hidden",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        zIndex: Z.FLOATING_PANEL,
        color: theme.text,
      }}
    >
      {act.photo_url && (
        <img
          src={act.photo_url}
          alt=""
          style={{ width: "100%", height: 160, objectFit: "cover", display: "block" }}
          onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
        />
      )}

      <div style={{ padding: "12px 14px" }}>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 6 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <span style={{
              display: "inline-block",
              background: act.color,
              color: "#fff",
              fontSize: 10,
              fontWeight: 700,
              padding: "2px 7px",
              borderRadius: 4,
              letterSpacing: "0.05em",
              marginBottom: 5,
              textTransform: "uppercase",
            }}>
              {sportLabel}
            </span>
            <div style={{ fontWeight: 700, fontSize: 14, lineHeight: 1.3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {act.name}
            </div>
            <div style={{ fontSize: 12, color: theme.muted, marginTop: 2 }}>{date}</div>
          </div>
          <button
            onClick={onClose}
            aria-label="Close activity card"
            style={{ background: "none", border: "none", cursor: "pointer", color: theme.muted, fontSize: 18, lineHeight: 1, padding: "0 0 0 8px", flexShrink: 0 }}
          >×</button>
        </div>

        <div style={{
          display: "flex",
          borderTop: `1px solid ${theme.divider}`,
          borderBottom: `1px solid ${theme.divider}`,
          padding: "8px 0",
          margin: "8px 0",
          textAlign: "center",
        }}>
          {([
            { label: "Distance", value: dist },
            { label: "Time", value: elapsed },
            { label: "Elevation", value: elev },
          ] as const).map(({ label, value }, i) => (
            <div key={label} style={{ flex: 1, borderLeft: i > 0 ? `1px solid ${theme.divider}` : "none", padding: "0 6px" }}>
              <div style={{ fontSize: 14, fontWeight: 700 }}>{value}</div>
              <div style={{ fontSize: 10, color: theme.muted, textTransform: "uppercase", letterSpacing: "0.05em", marginTop: 1 }}>{label}</div>
            </div>
          ))}
        </div>

        {description === "loading" && (
          <div style={{ fontSize: 12, color: theme.muted, marginBottom: 6 }}>Loading…</div>
        )}
        {description && description !== "loading" && (
          <div style={{ fontSize: 12, lineHeight: 1.5, marginBottom: 6, maxHeight: 80, overflowY: "auto", color: theme.text }}>
            {description}
          </div>
        )}

        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <a
            href={`https://www.strava.com/activities/${act.id}`}
            target="_blank"
            rel="noopener noreferrer"
            style={{ fontSize: 12, color: "#FC4C02", textDecoration: "none", fontWeight: 600 }}
          >
            View on Strava ↗
          </a>
          {activities.length > 1 && (
            <div style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: theme.muted }}>
              <button
                onClick={() => onIndexChange(index - 1)}
                disabled={index === 0}
                aria-label="Previous activity"
                style={{ background: "none", border: "none", cursor: index === 0 ? "default" : "pointer", color: index === 0 ? theme.muted : theme.text, fontSize: 16, padding: "0 2px", lineHeight: 1 }}
              >‹</button>
              <span>{index + 1} / {activities.length}</span>
              <button
                onClick={() => onIndexChange(index + 1)}
                disabled={index === activities.length - 1}
                aria-label="Next activity"
                style={{ background: "none", border: "none", cursor: index === activities.length - 1 ? "default" : "pointer", color: index === activities.length - 1 ? theme.muted : theme.text, fontSize: 16, padding: "0 2px", lineHeight: 1 }}
              >›</button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
