import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

interface ReadyResponse {
  ready: boolean;
  env: string;
  version: string;
  checks: Record<string, { ok: boolean; error?: string }>;
}

async function fetchReady(): Promise<ReadyResponse> {
  const r = await fetch(`${API_URL}/readyz`);
  if (!r.ok) throw new Error(`readyz ${r.status}`);
  return r.json();
}

export function StatusBar() {
  const [hovered, setHovered] = useState(false);
  const { data, isError } = useQuery({
    queryKey: ["readyz"],
    queryFn: fetchReady,
    refetchInterval: 30_000,
    retry: false,
  });

  const ok = data?.ready === true;
  const dotColor = isError ? "#e05a2b" : ok ? "#2eaa6e" : "#f4c430";
  const tipText = isError
    ? "api unreachable"
    : ok
      ? `whumpf ${data?.version} · ${data?.env}`
      : "degraded";

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        position: "absolute",
        right: 12,
        bottom: 12,
        zIndex: 900,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        width: 20,
        height: 20,
        cursor: "default",
        userSelect: "none",
      }}
    >
      {hovered && (
        <div
          style={{
            position: "absolute",
            right: 0,
            bottom: "calc(100% + 5px)",
            background: "rgba(18,18,28,0.96)",
            color: "#ddd",
            padding: "5px 9px",
            borderRadius: 5,
            fontSize: 11,
            fontFamily: "ui-monospace, monospace",
            whiteSpace: "nowrap",
            border: "1px solid rgba(255,255,255,0.08)",
            boxShadow: "0 2px 8px rgba(0,0,0,0.35)",
            pointerEvents: "none",
          }}
        >
          <span style={{ color: dotColor }}>●</span>
          {" "}{tipText}
        </div>
      )}
      <span style={{ color: "#555", fontSize: 15, lineHeight: 1 }}>ⓘ</span>
    </div>
  );
}
