import { useEffect, useRef, useState, type CSSProperties } from "react";
import type { Theme } from "./theme";
import {
  parseCoords,
  photonLabel,
  photonSub,
  type PhotonFeature,
} from "./utils";

// Colorado bbox for Photon: lon_min,lat_min,lon_max,lat_max
const CO_BBOX = "-109.06,37.0,-102.05,41.0";

export function SearchBar({
  theme,
  mobile,
  onSearch,
}: {
  theme: Theme;
  mobile: boolean;
  onSearch: (lat: number, lon: number) => void;
}) {
  const [value, setValue] = useState("");
  const [suggestions, setSuggestions] = useState<PhotonFeature[]>([]);
  const [activeIdx, setActiveIdx] = useState(-1);
  const [open, setOpen] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  function commit(lat: number, lon: number) {
    setOpen(false);
    setSuggestions([]);
    onSearch(lat, lon);
  }

  function submitValue(raw: string) {
    const coords = parseCoords(raw);
    if (coords) { commit(coords[0], coords[1]); return; }
    // If there's an active suggestion use it; otherwise use first suggestion
    const pick = activeIdx >= 0 ? suggestions[activeIdx] : suggestions[0];
    if (pick) {
      const [lon, lat] = pick.geometry.coordinates;
      setValue(photonLabel(pick));
      commit(lat, lon);
    }
  }

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (value.trim().length < 2 || parseCoords(value)) {
      setSuggestions([]);
      setOpen(false);
      return;
    }
    debounceRef.current = setTimeout(async () => {
      try {
        const url =
          `https://photon.komoot.io/api/?q=${encodeURIComponent(value)}&bbox=${CO_BBOX}&limit=7&lang=en`;
        const resp = await fetch(url);
        if (!resp.ok) return;
        const data = await resp.json();
        const feats: PhotonFeature[] = data.features ?? [];
        setSuggestions(feats);
        setActiveIdx(-1);
        setOpen(feats.length > 0);
      } catch {
        // network failure — silently ignore
      }
    }, 280);
  }, [value]);

  // Close dropdown on outside click
  useEffect(() => {
    function onDown(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, []);

  const inputStyle: CSSProperties = {
    flex: 1,
    minWidth: 0,
    padding: "9px 12px",
    borderRadius: 6,
    border: `1.5px solid ${theme.divider}`,
    background: theme.panel,
    color: theme.text,
    fontFamily: "ui-sans-serif, system-ui, sans-serif",
    fontSize: 14,
    outline: "none",
    boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
  };

  return (
    <div
      ref={containerRef}
      style={{
        position: "fixed",
        top: 10,
        ...(mobile
          ? { left: 10, right: 10 }
          : { left: "55%", transform: "translateX(-50%)", width: 380 }),
        zIndex: 1000,
        display: "flex",
        flexDirection: "column",
        alignItems: "stretch",
        gap: 4,
      }}
    >
      <form
        onSubmit={(e) => { e.preventDefault(); submitValue(value); }}
        style={{ display: "flex", gap: 4 }}
      >
        <input
          value={value}
          onChange={(e) => { setValue(e.target.value); }}
          onFocus={() => { if (suggestions.length > 0) setOpen(true); }}
          onKeyDown={(e) => {
            if (!open) return;
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setActiveIdx((i) => Math.min(i + 1, suggestions.length - 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActiveIdx((i) => Math.max(i - 1, -1));
            } else if (e.key === "Escape") {
              setOpen(false);
            }
          }}
          placeholder="Search Colorado trails, peaks, places…"
          autoComplete="off"
          style={inputStyle}
        />
        <button
          type="submit"
          style={{
            padding: "9px 16px",
            borderRadius: 6,
            border: "none",
            background: theme.accent,
            color: "#fff",
            fontFamily: "ui-sans-serif, system-ui, sans-serif",
            fontSize: 14,
            cursor: "pointer",
            boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
            flexShrink: 0,
          }}
        >
          Go
        </button>
      </form>

      {open && suggestions.length > 0 && (
        <div
          style={{
            background: theme.panel,
            border: `1px solid ${theme.divider}`,
            borderRadius: 6,
            boxShadow: "0 4px 16px rgba(0,0,0,0.35)",
            overflow: "hidden",
          }}
        >
          {suggestions.map((f, i) => (
            <div
              key={i}
              onMouseDown={(e) => {
                e.preventDefault();
                const [lon, lat] = f.geometry.coordinates;
                setValue(photonLabel(f));
                commit(lat, lon);
              }}
              onMouseEnter={() => setActiveIdx(i)}
              style={{
                padding: "8px 12px",
                cursor: "pointer",
                background: i === activeIdx ? "rgba(255,255,255,0.07)" : "transparent",
                borderTop: i > 0 ? `1px solid ${theme.divider}` : undefined,
              }}
            >
              <div style={{ fontSize: 13, color: theme.text, fontWeight: 500 }}>
                {photonLabel(f)}
              </div>
              {photonSub(f) && (
                <div style={{ fontSize: 11, color: theme.muted, marginTop: 1 }}>
                  {photonSub(f)}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
