/**
 * Tiny status pill in the bottom-right that pings /readyz and reports
 * whether the backend + Postgres are alive. Cheap, reassuring, kills the
 * "is anything actually wired up?" question on day one.
 */
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
  const { data, isError } = useQuery({
    queryKey: ["readyz"],
    queryFn: fetchReady,
    refetchInterval: 30_000,
    retry: false,
  });

  const ok = data?.ready === true;
  const bg = isError ? "#8b0000" : ok ? "#1a5928" : "#8a6d00";
  const label = isError
    ? "api unreachable"
    : ok
      ? `whumpf ${data?.version} · ${data?.env}`
      : "degraded";

  return (
    <div
      style={{
        position: "absolute",
        right: 12,
        bottom: 12,
        padding: "6px 10px",
        background: bg,
        color: "#fff",
        fontFamily: "ui-monospace, monospace",
        fontSize: 12,
        borderRadius: 4,
        pointerEvents: "none",
        opacity: 0.9,
      }}
    >
      {label}
    </div>
  );
}
