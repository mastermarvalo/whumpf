import { useEffect, useState } from "react";
import { AuthGate } from "./components/AuthGate";
import { Map as MapView } from "./components/Map";
import { StatusBar } from "./components/StatusBar";
import { clearToken, getToken } from "./auth";

export default function App() {
  const [authed, setAuthed] = useState(() => !!getToken());

  // Token expired mid-session → back to login.
  useEffect(() => {
    const handler = () => { clearToken(); setAuthed(false); };
    window.addEventListener("whumpf:unauthorized", handler);
    return () => window.removeEventListener("whumpf:unauthorized", handler);
  }, []);

  if (!authed) {
    return <AuthGate onAuth={() => setAuthed(true)} />;
  }

  return (
    <div style={{ position: "relative", width: "100vw", height: "100vh" }}>
      <MapView onLogout={() => { clearToken(); setAuthed(false); }} />
      <StatusBar />
    </div>
  );
}
