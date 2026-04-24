import { useEffect, useState } from "react";
import { AuthGate } from "./components/AuthGate";
import { Map as MapView } from "./components/Map";
import { StatusBar } from "./components/StatusBar";
import { apiFetch, clearToken, getToken } from "./auth";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export interface StravaStatus {
  connected: boolean;
  athlete_name: string | null;
  athlete_icon_url: string | null;
}

export default function App() {
  const [authed, setAuthed] = useState(() => !!getToken());
  const [stravaStatus, setStravaStatus] = useState<StravaStatus>({
    connected: false,
    athlete_name: null,
    athlete_icon_url: null,
  });

  async function refreshStravaStatus() {
    const r = await apiFetch(`${API_URL}/strava/status`);
    if (r.ok) setStravaStatus(await r.json());
  }

  // Token expired mid-session → back to login.
  useEffect(() => {
    const handler = () => { clearToken(); setAuthed(false); };
    window.addEventListener("whumpf:unauthorized", handler);
    return () => window.removeEventListener("whumpf:unauthorized", handler);
  }, []);

  // On auth or page load: check for OAuth callback result, then load Strava status.
  useEffect(() => {
    if (!authed) return;
    const params = new URLSearchParams(window.location.search);
    const stravaParam = params.get("strava");
    if (stravaParam) {
      window.history.replaceState({}, "", window.location.pathname);
    }
    refreshStravaStatus();
  }, [authed]);

  if (!authed) {
    return <AuthGate onAuth={() => setAuthed(true)} />;
  }

  return (
    <div style={{ position: "relative", width: "100vw", height: "100vh" }}>
      <MapView
        onLogout={() => { clearToken(); setAuthed(false); }}
        stravaStatus={stravaStatus}
        onStravaStatusChange={refreshStravaStatus}
      />
      <StatusBar />
    </div>
  );
}
